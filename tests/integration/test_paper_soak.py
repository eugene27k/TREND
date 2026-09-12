"""PRD Section 14.1 — the paper soak, in fake time.

"Paper soak of 7 days (>= 7 rebalances) with a forced process kill during one
rebalance window and a forced status change to SETTLING for one symbol."

Seven calendar days cannot be waited out in a build, but every behaviour the
soak is meant to exercise is a function of the clock, and the engine takes its
clock as an argument. So this runs the real runner over seven simulated days,
ticking it the way the process would, and performs both drills inside it. What
it cannot prove is wall-clock endurance — a leak, a file handle, a slow
memory creep — which is what the real P1 soak on the host is for.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.core.clock import at_utc
from aegis.core.errors import ExchangeUnreachable
from aegis.core.types import EngineState
from aegis.strategy_trend.runner import TrendRunner

pytestmark = pytest.mark.soak

DAY_ONE = date(2026, 9, 8)
DAYS = 7

#: The moments a real process would find itself awake on any given day.
TICKS = ("00:02", "00:05", "00:20", "01:10", "06:00", "12:00", "18:00")


def tick_day(runner: TrendRunner, ctx, day: date, *, skip: set[str] = frozenset()) -> list:
    reports = []
    for hhmm in TICKS:
        if hhmm in skip:
            continue
        ctx.clock.set(at_utc(day, hhmm))
        reports.append(runner.tick(ctx.clock.now_ms()))
    return reports


@pytest.fixture
def soak(world):
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    return world, runner


def test_seven_days_of_operation_complete_at_least_seven_rebalances(soak):
    world, runner = soak
    for i in range(DAYS):
        tick_day(runner, world, DAY_ONE + timedelta(days=i))

    rebalances = world.repos.rebalances.recent(50)
    scheduled = [r for r in rebalances if r["kind"] == "scheduled"]
    assert len(scheduled) >= DAYS, f"{len(scheduled)} rebalances over {DAYS} days"
    assert all(r["status"] in ("complete", "window_end") for r in scheduled)
    assert runner.machine.state.state is EngineState.IDLE
    assert not runner.machine.state.halted


def test_the_soak_stays_within_the_caps_every_single_day(soak):
    """Invariant 8 across a week, not just at one instant."""
    world, runner = soak
    cfg = world.cfg
    for i in range(DAYS):
        tick_day(runner, world, DAY_ONE + timedelta(days=i))
        account = world.gateway.account()
        positions = world.gateway.positions()
        equity = account.equity
        gross = sum(abs(p.notional) for p in positions.values())
        net = sum(p.notional for p in positions.values())
        largest = max((abs(p.notional) for p in positions.values()), default=0.0)
        assert gross <= cfg.caps.gross * equity * 1.01, f"day {i}: gross {gross / equity:.2f}x"
        assert abs(net) <= cfg.caps.net * equity * 1.01, f"day {i}: net {net / equity:.2f}x"
        assert largest <= cfg.caps.single * equity * 1.01, f"day {i}: single {largest / equity:.2f}x"


def test_the_soak_survives_a_process_kill_during_a_rebalance_window(soak):
    """Drill 1: die mid-rebalance on day 3, come back, finish the week."""
    world, runner = soak
    killed_on = DAY_ONE + timedelta(days=2)

    for i in range(2):
        tick_day(runner, world, DAY_ONE + timedelta(days=i))

    # Day 3: the venue stops accepting orders partway through the plan.
    counter = {"n": 0}

    def die_after_five(gateway, order) -> None:
        counter["n"] += 1
        if counter["n"] >= 5:
            gateway.inject_error("place_order", ExchangeUnreachable("process died"))

    world.gateway.set_fill_policy("callable", die_after_five)
    world.clock.set(at_utc(killed_on, "00:02"))
    runner.tick(world.clock.now_ms())
    world.clock.set(at_utc(killed_on, "00:05"))
    runner.tick(world.clock.now_ms())

    interrupted = world.repos.rebalances.unfinished()
    assert interrupted is not None, "the kill must leave a resumable rebalance"

    # A brand-new process against the same database, still inside the window.
    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(at_utc(killed_on, "00:20"))
    revived = TrendRunner(world)
    report = revived.start(world.clock.now_ms())
    assert any(a.startswith("resumed:") for a in report.actions)

    for i in range(3, DAYS):
        tick_day(revived, world, DAY_ONE + timedelta(days=i))

    assert world.repos.rebalances.unfinished() is None, "the week must end with nothing in flight"
    scheduled = [r for r in world.repos.rebalances.recent(50) if r["kind"] == "scheduled"]
    assert len(scheduled) >= DAYS - 1


def test_the_soak_closes_a_symbol_that_leaves_trading_status(soak):
    """Drill 2: force SETTLING on a held symbol; the position must go."""
    world, runner = soak
    tick_day(runner, world, DAY_ONE)

    held = [s for s, p in world.gateway.positions().items() if p.qty]
    assert held, "the first day must leave a book to close"
    victim = held[0]

    day_two = DAY_ONE + timedelta(days=1)
    world.gateway.set_symbol_info(victim, status="SETTLING")

    # The status watch runs hourly; one tick after the change is enough.
    world.clock.set(at_utc(day_two, "06:00"))
    report = runner.tick(world.clock.now_ms())

    assert any("risk_cut:delisting" in a for a in report.actions), report.actions
    position = world.gateway.positions().get(victim)
    assert position is None or position.qty == 0.0, f"{victim} was not closed"
    codes = [a["code"] for a in world.repos.alerts.recent(50)]
    assert "SYMBOL_STATUS" in codes


def test_a_settling_symbol_is_dropped_from_the_next_universe(soak):
    world, runner = soak
    tick_day(runner, world, DAY_ONE)
    month = world.repos.universe.latest_month()
    victim = world.repos.universe.symbols(month)[0]
    world.gateway.set_symbol_info(victim, status="SETTLING")

    # Force the monthly refresh: the selector must refuse a non-TRADING symbol.
    world.clock.set(at_utc(DAY_ONE + timedelta(days=1), "06:00"))
    runner.tick(world.clock.now_ms())
    refreshed = runner.universe.refresh_if_due(world.clock.now_ms(), force=True)
    assert refreshed is not None
    assert victim not in refreshed.symbols
    entry = refreshed.entry(victim)
    assert entry is not None and not entry.included
    assert "SETTLING" in entry.reason or "status" in entry.reason.lower()


def test_the_week_leaves_a_complete_audit_trail(soak):
    """Every day must be reconstructible from the database alone."""
    world, runner = soak
    for i in range(DAYS):
        tick_day(runner, world, DAY_ONE + timedelta(days=i))

    days = {r["day"] for r in world.repos.rebalances.recent(50)}
    assert len(days) >= DAYS - 1

    for day_str in sorted(days):
        rebalance = next(r for r in world.repos.rebalances.recent(50) if r["day"] == day_str)
        rid = rebalance["rebalance_id"]
        assert rid == f"rb-{day_str}", "the id must name the trading day the report looks up"
        assert world.repos.targets.for_rebalance(rid), f"{day_str}: no targets stored"
        # Signals are stored under the BAR day: the 00:05 rebalance of day D works
        # the bar that closed at 00:00 that morning, i.e. D-1's bar.
        bar_day = (date.fromisoformat(day_str) - timedelta(days=1)).isoformat()
        assert world.repos.signals.day(bar_day), f"{day_str}: no signals for bar day {bar_day}"
        assert rebalance["order_plan_json"] not in ("", "[]"), f"{day_str}: no plan stored"

    assert world.repos.snapshots.latest() is not None
    assert world.repos.equity.all(), "the equity path must exist"


def test_the_soak_is_quiet_when_nothing_goes_wrong(soak):
    """A week of CRITICALs on a healthy run would train the operator to ignore them."""
    world, runner = soak
    for i in range(DAYS):
        tick_day(runner, world, DAY_ONE + timedelta(days=i))
    critical = [a for a in world.repos.alerts.recent(200) if a["severity"] == "CRITICAL"]
    assert critical == [], f"unexpected CRITICALs: {[(a['code'], a['message']) for a in critical]}"
