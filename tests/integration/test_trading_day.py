"""End-to-end: one whole UTC trading day through the runner.

This is the test that proves the pieces compose. It found two real defects that
no unit test could have: a sequential executor that could not finish 16 symbols
inside the 55-minute window, and a taker price rounded to the nearest tick
instead of through the touch, so every IOC escalation expired instead of filling.
"""

from __future__ import annotations

import pytest

from aegis.core.clock import to_ms
from aegis.core.types import EngineState
from aegis.strategy_trend.runner import TrendRunner

BARS_AT = "2026-09-08T00:02:00Z"
REBALANCE_AT = "2026-09-08T00:05:00Z"


@pytest.fixture
def runner(world):
    r = TrendRunner(world)
    r.start(world.clock.now_ms())
    return r


def run_day(world, runner, *, until: str = REBALANCE_AT):
    reports = []
    for when in (BARS_AT, until):
        world.clock.set(to_ms(when))
        reports.append(runner.tick(world.clock.now_ms()))
    return reports


def test_a_full_day_selects_a_universe_computes_signals_and_trades(world, runner):
    bars_report, rebalance_report = run_day(world, runner)

    assert "universe_refresh" in bars_report.actions
    assert "bars_ready" in bars_report.actions

    month = world.repos.universe.latest_month()
    assert len(world.repos.universe.symbols(month)) == world.cfg.universe.size

    day = world.repos.signals.latest_day()
    assert len(world.repos.signals.day(day)) == world.cfg.universe.size

    targets = world.repos.targets.latest()
    assert len(targets) == world.cfg.universe.size
    assert any(abs(t["target_notional"]) > 0 for t in targets)

    assert any(a.startswith("rebalance:") for a in rebalance_report.actions)
    assert rebalance_report.state == str(EngineState.IDLE)


def test_the_rebalance_completes_inside_the_window(world, runner):
    """US-T10 / P1: the completion rate must be able to reach 95 %.

    Sequentially this is impossible — sixteen symbols at a five-minute escalation
    each need eighty minutes against a fifty-five minute window — so this test is
    what pins the executor's concurrency.
    """
    run_day(world, runner)
    rebalance = world.repos.rebalances.recent(1)[0]
    assert rebalance["status"] == "complete"
    assert rebalance["completion_pct"] >= 95.0
    window_ms = to_ms("2026-09-08T01:00:00Z") - to_ms(REBALANCE_AT)
    assert rebalance["ended_ts"] - rebalance["started_ts"] < window_ms


def test_every_planned_symbol_actually_reaches_its_target(world, runner):
    run_day(world, runner)
    rebalance_id = world.repos.rebalances.recent(1)[0]["rebalance_id"]
    targets = {t["symbol"]: t for t in world.repos.targets.for_rebalance(rebalance_id)}
    positions = world.gateway.positions()
    for symbol, target in targets.items():
        if abs(target["target_qty"]) < 1e-9:
            continue
        assert symbol in positions, f"{symbol} was planned but never traded"
        assert positions[symbol].qty == pytest.approx(target["target_qty"], rel=0.05)


def test_the_plan_is_persisted_before_any_order_exists(world, runner):
    """US-T09 AC 3 — proved by the order plan being complete while orders are not."""
    run_day(world, runner)
    row = world.repos.rebalances.recent(1)[0]
    plan = row["order_plan_json"]
    assert plan and plan != "[]"
    assert row["started_ts"] <= min(
        o.created_ts_ms for o in world.repos.orders.for_rebalance(row["rebalance_id"])
    )


def test_risk_reducing_orders_are_planned_before_risk_increasing(world, runner):
    import json

    run_day(world, runner)
    plan = json.loads(world.repos.rebalances.recent(1)[0]["order_plan_json"])
    flags = [p["risk_reducing"] for p in sorted(plan, key=lambda p: p["sequence"])]
    assert flags == sorted(flags, reverse=True), "reductions must come first (5.8)"


def test_fills_carry_slippage_against_the_decision_mid(world, runner):
    run_day(world, runner)
    fills = world.repos.fills.for_rebalance(world.repos.rebalances.recent(1)[0]["rebalance_id"])
    assert fills
    for fill in fills:
        assert fill.decision_mid is not None and fill.decision_mid > 0
        assert abs(fill.slippage_bps) < 1000, "a sane slippage measurement"


def test_a_second_tick_the_same_day_does_not_rebalance_again(world, runner):
    run_day(world, runner)
    before = len(world.repos.rebalances.recent(10))
    world.clock.set(to_ms("2026-09-08T00:30:00Z"))
    runner.tick(world.clock.now_ms())
    assert len(world.repos.rebalances.recent(10)) == before


def test_the_second_days_rebalance_respects_hysteresis(world, runner):
    """Nothing moved overnight, so almost nothing should trade (5.8)."""
    run_day(world, runner)
    first = world.repos.rebalances.recent(1)[0]

    world.clock.set(to_ms("2026-09-09T00:05:00Z"))
    runner.tick(world.clock.now_ms())
    rebalances = world.repos.rebalances.recent(5)
    if len(rebalances) > 1:
        second = next(r for r in rebalances if r["rebalance_id"] != first["rebalance_id"])
        assert second["traded_notional"] < first["traded_notional"] * 0.5


def test_no_alert_above_info_on_a_healthy_day(world, runner):
    run_day(world, runner)
    loud = [a for a in world.repos.alerts.recent(50) if a["severity"] != "INFO"]
    assert loud == [], f"a healthy day should be quiet, got {loud}"
