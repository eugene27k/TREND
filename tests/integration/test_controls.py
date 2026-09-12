"""Operator controls reaching a running engine (Section 7, Aegis US-18).

The dashboard process never writes ``engine_state``: it queues an action in
``control_log`` and the engine applies it on its next tick. These tests drive
that path end to end, because a Pause that the engine does not actually honour
is worse than no Pause at all.
"""

from __future__ import annotations

import pytest

from aegis.core.clock import to_ms
from aegis.ops.controls import CONFIRM_TOKEN
from aegis.strategy_trend.runner import TrendRunner

BARS_AT = "2026-09-08T00:02:00Z"
REBALANCE_AT = "2026-09-08T00:05:00Z"


def queue(world, action: str, *, operator: str = "eugene", reason: str = "test",
          confirm: str = "") -> None:
    """What the API's POST /controls endpoint does — and nothing more."""
    world.repos.state.log_control(
        action, operator, reason, {"source": "api", "confirm": confirm}, world.clock.now_ms()
    )


@pytest.fixture
def runner(world):
    r = TrendRunner(world)
    r.start(world.clock.now_ms())
    world.clock.set(to_ms(BARS_AT))
    r.tick(world.clock.now_ms())
    return r


def test_pause_blocks_the_rebalance(world, runner):
    queue(world, "pause", reason="watching a news event")
    world.clock.set(to_ms(REBALANCE_AT))
    report = runner.tick(world.clock.now_ms())

    assert "control:pause" in report.actions
    assert "rebalance_skipped:not_tradeable" in report.actions
    assert world.repos.rebalances.recent(5) == []


def test_pause_does_not_block_a_risk_reduction(world, runner):
    """Section 7: 'Pause blocks rebalances but not risk actions'."""
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())            # take a book first
    assert world.gateway.positions()

    queue(world, "pause")
    world.clock.set(to_ms("2026-09-08T02:00:00Z"))
    runner.tick(world.clock.now_ms())
    assert runner.machine.state.paused
    assert runner.controls.may_reduce_risk()
    assert not runner.controls.may_increase_risk()

    # A governor-style cut still executes while paused: half the book goes.
    before = {s: p.qty for s, p in world.gateway.positions().items()}
    runner.executor.reduce_by(dict.fromkeys(before, 0.5), "governor", world.clock.now_ms())
    after = {s: p.qty for s, p in world.gateway.positions().items()}
    assert after, "the cut must not close everything"
    for symbol, qty in before.items():
        assert abs(after.get(symbol, 0.0)) < abs(qty), f"{symbol} was not reduced while paused"


def test_resume_lets_the_next_rebalance_run(world, runner):
    queue(world, "pause")
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())
    assert world.repos.rebalances.recent(5) == []

    queue(world, "resume")
    world.clock.set(to_ms("2026-09-09T00:02:00Z"))
    resumed = runner.tick(world.clock.now_ms())
    assert "control:resume" in resumed.actions
    assert not runner.machine.state.paused

    world.clock.set(to_ms("2026-09-09T00:05:00Z"))
    report = runner.tick(world.clock.now_ms())
    assert any(a.startswith("rebalance:") for a in report.actions)
    assert world.repos.rebalances.recent(5)


def test_stop_without_the_confirmation_token_changes_nothing(world, runner):
    queue(world, "stop", reason="wrong token", confirm="please")
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    report = runner.tick(world.clock.now_ms())

    assert "control_refused:stop" in report.actions
    assert not runner.machine.state.stopped
    assert any(a["code"] == "CONTROL_REFUSED" for a in world.repos.alerts.recent())


def test_stop_with_the_token_stops_the_engine(world, runner):
    queue(world, "stop", reason="maintenance", confirm=CONFIRM_TOKEN)
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    runner.tick(world.clock.now_ms())
    assert runner.machine.state.stopped

    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())
    assert world.repos.rebalances.recent(5) == []


def test_flatten_all_closes_the_book(world, runner):
    world.clock.set(to_ms(REBALANCE_AT))
    runner.tick(world.clock.now_ms())
    assert world.gateway.positions()

    queue(world, "flatten_all", reason="going on holiday", confirm=CONFIRM_TOKEN)
    world.clock.set(to_ms("2026-09-08T03:00:00Z"))
    report = runner.tick(world.clock.now_ms())

    assert "control:flatten_all" in report.actions
    assert all(p.qty == 0 for p in world.gateway.positions().values())


def test_an_unknown_action_is_refused_not_executed(world, runner):
    queue(world, "sell_everything_now")
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    report = runner.tick(world.clock.now_ms())
    assert "control_refused:sell_everything_now" in report.actions


def test_each_queued_action_is_applied_exactly_once(world, runner):
    queue(world, "pause")
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    first = runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:07:00Z"))
    second = runner.tick(world.clock.now_ms())

    assert "control:pause" in first.actions
    assert "control:pause" not in second.actions


def test_the_engines_own_audit_rows_are_never_replayed(world, runner):
    """Controls writes its own log row; re-applying it would loop forever."""
    runner.controls.pause("eugene", "directly, not via the API")
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    report = runner.tick(world.clock.now_ms())
    assert not any(a.startswith("control:") for a in report.actions)
    assert runner.machine.state.paused


def test_clear_halt_requires_a_reason(world, runner):
    runner.machine.halt("drawdown 26 %")
    queue(world, "clear_halt", reason="")
    world.clock.set(to_ms("2026-09-08T00:06:00Z"))
    report = runner.tick(world.clock.now_ms())
    assert "control_refused:clear_halt" in report.actions
    assert runner.machine.load().halted

    queue(world, "clear_halt", reason="post-mortem reviewed, regime change confirmed")
    world.clock.set(to_ms("2026-09-08T00:07:00Z"))
    runner.tick(world.clock.now_ms())
    assert not runner.machine.load().halted
