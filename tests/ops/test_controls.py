"""Operator controls: PRD Section 7, Aegis US-18 confirmation, US-T13 AC 3."""

from __future__ import annotations

import pytest

from aegis.core.context import Context
from aegis.core.errors import ConfigError
from aegis.core.types import EngineState
from aegis.ops.controls import CONFIRM_TOKEN, Controls


def _save(ctx: Context, **over) -> None:
    base = dict(
        state=str(EngineState.IDLE),
        phase="P0_BACKTEST",
        paused=False,
        stopped=False,
        safe_mode=False,
        halt_reason="",
        governor_g=1.0,
        blocks=[],
        context={},
        now_ms=ctx.now_ms(),
    )
    base.update(over)
    ctx.repos.state.save(**base)


def test_section7_pause_blocks_rebalances_but_not_risk_reduction(ctx: Context) -> None:
    controls = Controls(ctx)
    assert controls.may_rebalance() is True

    controls.pause("alice", "watching a news event")

    assert controls.may_rebalance() is False
    assert controls.may_increase_risk() is False
    assert controls.may_reduce_risk() is True


def test_section7_resume_restores_rebalancing(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.pause("alice", "hold")
    controls.resume("alice", "clear")

    assert controls.may_rebalance() is True
    assert controls.may_increase_risk() is True


def test_invariant1_a_block_stops_risk_increase_but_not_reduction(ctx: Context) -> None:
    _save(ctx, blocks=["daily_loss"])
    controls = Controls(ctx)

    assert controls.may_increase_risk() is False
    assert controls.may_reduce_risk() is True


def test_invariant1_safe_mode_stops_risk_increase(ctx: Context) -> None:
    _save(ctx, safe_mode=True)
    controls = Controls(ctx)

    assert controls.may_increase_risk() is False
    assert controls.may_reduce_risk() is True


def test_every_action_appends_an_audited_control_row(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.start("alice", "morning start")
    controls.pause("bob", "coffee")
    controls.resume("bob", "back")

    rows = ctx.repos.state.controls()
    assert [r["action"] for r in rows] == ["resume", "pause", "start"]
    assert [r["operator"] for r in rows] == ["bob", "bob", "alice"]
    assert rows[-1]["reason"] == "morning start"


def test_us18_stop_requires_the_confirmation_and_changes_nothing_when_wrong(ctx: Context) -> None:
    controls = Controls(ctx)

    with pytest.raises(ConfigError):
        controls.stop("alice", "shutting down", "yes")

    assert ctx.repos.state.controls() == []
    assert ctx.repos.state.load() is None
    assert controls.may_rebalance() is True


def test_us18_stop_with_the_confirmation_stops_the_engine(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.stop("alice", "shutting down", CONFIRM_TOKEN)

    assert controls.state()["stopped"] is True
    assert controls.state()["state"] == str(EngineState.STOPPED)
    assert controls.may_rebalance() is False
    assert controls.may_increase_risk() is False
    assert controls.may_reduce_risk() is True


def test_us18_flatten_all_requires_the_confirmation_and_changes_nothing_when_wrong(ctx: Context) -> None:
    controls = Controls(ctx)

    with pytest.raises(ConfigError):
        controls.flatten_all("alice", "get out", "")

    assert ctx.repos.state.controls() == []
    assert ctx.repos.state.load() is None


def test_us18_flatten_all_records_a_request_for_the_executor(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.flatten_all("alice", "get out", CONFIRM_TOKEN)

    request = controls.state()["context"]["flatten_requested"]
    assert request["operator"] == "alice"
    assert request["reason"] == "get out"
    assert ctx.repos.state.controls()[0]["action"] == "flatten_all"


def test_start_after_stop_clears_the_operator_holds(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.stop("alice", "done", CONFIRM_TOKEN)
    controls.start("alice", "back on")

    assert controls.state()["stopped"] is False
    assert controls.state()["state"] == str(EngineState.IDLE)
    assert controls.may_rebalance() is True


def test_us_t13_ac3_clear_halt_refuses_an_empty_reason(ctx: Context) -> None:
    _save(ctx, state=str(EngineState.HALTED_RISK), halt_reason="hard_halt_drawdown")
    controls = Controls(ctx)

    with pytest.raises(ConfigError):
        controls.clear_halt("alice", "   ")

    assert controls.state()["state"] == str(EngineState.HALTED_RISK)
    assert ctx.repos.state.controls() == []


def test_us_t13_ac3_clear_halt_is_the_only_exit_from_halted_risk(ctx: Context) -> None:
    _save(
        ctx,
        state=str(EngineState.HALTED_RISK),
        halt_reason="hard_halt_drawdown",
        blocks=["hard_halt_drawdown"],
    )
    controls = Controls(ctx)

    assert controls.may_rebalance() is False
    assert controls.may_increase_risk() is False
    assert controls.may_reduce_risk() is True

    controls.start("alice", "try to restart")
    assert controls.state()["state"] == str(EngineState.HALTED_RISK)

    controls.clear_halt("alice", "reconciled the book by hand, cause understood")

    state = controls.state()
    assert state["state"] == str(EngineState.IDLE)
    assert state["halt_reason"] == ""
    assert state["blocks"] == []
    assert controls.may_increase_risk() is True
    logged = ctx.repos.state.controls()[0]
    assert logged["action"] == "clear_halt"
    assert logged["reason"].startswith("reconciled")


def test_clear_halt_on_a_healthy_engine_is_recorded_and_harmless(ctx: Context) -> None:
    controls = Controls(ctx)
    controls.clear_halt("alice", "belt and braces")

    assert controls.state()["state"] == str(EngineState.IDLE)
    assert ctx.repos.state.controls()[0]["payload_json"].find('"was_halted":false') >= 0


def test_a_configured_token_overrides_the_default_confirmation(cfg, clock, gateway, repos) -> None:
    from aegis.core.config import load_config
    from aegis.core.types import Strategy
    from aegis.ops.alerts import AlertBus

    tuned = load_config("config/trend.yaml", {"phase": {"live_confirm": "GO"}}, use_env=False)
    ctx = Context(
        cfg=tuned,
        clock=clock,
        gateway=gateway,
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )
    controls = Controls(ctx)

    with pytest.raises(ConfigError):
        controls.stop("alice", "r", CONFIRM_TOKEN)
    controls.stop("alice", "r", "GO")
    assert controls.state()["stopped"] is True


def test_an_unknown_configured_phase_falls_back_to_p0(clock, gateway, repos) -> None:
    from aegis.core.config import load_config
    from aegis.core.types import Strategy
    from aegis.ops.alerts import AlertBus

    tuned = load_config("config/trend.yaml", {"phase": {"current": "NOT_A_PHASE"}}, use_env=False)
    ctx = Context(cfg=tuned, clock=clock, gateway=gateway, repos=repos,
                  alerts=AlertBus(repos.alerts, clock, Strategy.TREND))

    assert Controls(ctx).state()["phase"] == "P0_BACKTEST"
