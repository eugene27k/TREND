"""US-T09 AC 4 / Section 14.1 — killing the process mid-rebalance.

A real kill leaves the ``rebalances`` row in ``running`` with a partial cursor.
Simulating that faithfully matters: stopping the venue from *filling* is not an
interruption — the rebalance still finishes, at 0 % — so these tests make the
gateway fail partway through the plan, which is what a dying process looks like
from the database's point of view.
"""

from __future__ import annotations

import pytest

from aegis.core.clock import to_ms
from aegis.core.errors import ExchangeUnreachable
from aegis.core.types import EngineState
from aegis.strategy_trend.runner import TrendRunner


def _warm(world) -> TrendRunner:
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:02:00Z"))
    runner.tick(world.clock.now_ms())
    return runner


def _die_after(world, n_orders: int) -> None:
    """Let ``n_orders`` reach the venue, then make it unreachable."""
    counter = {"n": 0}

    def hook(gateway, order) -> None:
        counter["n"] += 1
        if counter["n"] >= n_orders:
            gateway.inject_error("place_order", ExchangeUnreachable("process died"))

    world.gateway.set_fill_policy("callable", hook)


def _interrupted_rebalance(world, *, after: int = 5) -> str:
    runner = _warm(world)
    _die_after(world, after)
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    row = world.repos.rebalances.unfinished()
    assert row is not None, "the kill must leave the rebalance unfinished"
    return str(row["rebalance_id"])


def test_a_kill_leaves_the_rebalance_resumable(world):
    rebalance_id = _interrupted_rebalance(world)
    row = world.repos.rebalances.get(rebalance_id)
    assert row["status"] == "running"
    assert row["order_plan_json"] not in ("", "[]")


def test_us_t09_ac4_a_new_process_resumes_the_persisted_plan(world):
    rebalance_id = _interrupted_rebalance(world)
    plan_json = world.repos.rebalances.get(rebalance_id)["order_plan_json"]
    cursor_before = world.repos.rebalances.get(rebalance_id)["cursor"]

    # A brand-new process against the same database, still inside the window.
    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-08T00:30:00Z"))
    revived = TrendRunner(world)
    report = revived.start(world.clock.now_ms())

    assert f"resumed:{rebalance_id}" in report.actions
    after = world.repos.rebalances.get(rebalance_id)
    assert after["order_plan_json"] == plan_json, "the plan must not be recomputed"
    assert after["cursor"] >= cursor_before
    assert report.state == str(EngineState.IDLE)


def test_the_resumed_rebalance_never_overshoots_a_target(world):
    """The decisive property: replaying must not push a position past its target."""
    rebalance_id = _interrupted_rebalance(world)
    targets = {t["symbol"]: t["target_qty"] for t in world.repos.targets.for_rebalance(rebalance_id)}

    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-08T00:20:00Z"))
    TrendRunner(world).start(world.clock.now_ms())

    for symbol, position in world.gateway.positions().items():
        target = targets.get(symbol)
        if target is None or abs(target) < 1e-9:
            continue
        assert abs(position.qty) <= abs(target) * 1.05 + 1e-6, (
            f"{symbol} overshot: {position.qty} vs target {target}"
        )
        assert position.qty * target > 0, f"{symbol} flipped sign"


def test_resuming_makes_progress_the_interrupted_run_could_not(world):
    rebalance_id = _interrupted_rebalance(world, after=3)
    traded_before = world.repos.rebalances.get(rebalance_id)["traded_notional"]

    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-08T00:20:00Z"))
    TrendRunner(world).start(world.clock.now_ms())

    after = world.repos.rebalances.get(rebalance_id)
    assert after["traded_notional"] > traded_before
    assert after["status"] in ("complete", "window_end")


def test_a_restart_after_the_window_abandons_rather_than_trading_stale_targets(world):
    rebalance_id = _interrupted_rebalance(world)

    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-08T02:00:00Z"))  # the window closed at 01:00
    report = TrendRunner(world).start(world.clock.now_ms())

    assert f"abandoned:{rebalance_id}" in report.actions
    assert world.repos.rebalances.get(rebalance_id)["status"] == "window_end"


def test_the_kill_puts_the_engine_in_safe_mode_not_into_a_crash(world):
    runner = _warm(world)
    _die_after(world, 5)
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    report = runner.tick(world.clock.now_ms())
    assert "safe_mode" in report.actions
    assert runner.machine.state.safe_mode
    assert not runner.machine.state.may_increase_risk()
    assert runner.machine.state.may_reduce_risk()


def test_a_clean_restart_resumes_nothing(world):
    runner = _warm(world)
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    world.clock.set(to_ms("2026-09-08T06:00:00Z"))
    report = TrendRunner(world).start(world.clock.now_ms())
    assert report.actions == []
    assert report.state == str(EngineState.IDLE)


@pytest.mark.parametrize("after", [2, 5, 9])
def test_resuming_from_several_kill_points_always_converges(world, after):
    rebalance_id = _interrupted_rebalance(world, after=after)
    targets = {t["symbol"]: t["target_qty"] for t in world.repos.targets.for_rebalance(rebalance_id)}

    world.gateway.clear_errors()
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-08T00:25:00Z"))
    TrendRunner(world).start(world.clock.now_ms())

    positions = world.gateway.positions()
    for symbol, target in targets.items():
        if abs(target) < 1e-9:
            continue
        got = positions.get(symbol)
        assert got is not None and got.qty == pytest.approx(target, rel=0.05), symbol
