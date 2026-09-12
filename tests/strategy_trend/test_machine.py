"""PRD 5.11 — the state machine, and what it refuses to do."""

from __future__ import annotations

import pytest

from aegis.core.errors import AegisError
from aegis.core.types import EngineState, Phase
from aegis.strategy_trend.machine import InvalidTransition, MachineState, StateMachine


@pytest.fixture
def machine(repos, clock) -> StateMachine:
    return StateMachine(repos, clock, phase=Phase.P1_PAPER)


def test_the_documented_happy_path(machine):
    """IDLE -> COMPUTING -> REBALANCING -> IDLE."""
    assert machine.state.state is EngineState.IDLE
    machine.to(EngineState.COMPUTING)
    machine.to(EngineState.REBALANCING)
    machine.to(EngineState.IDLE)
    assert machine.state.state is EngineState.IDLE


def test_risk_action_can_be_entered_from_any_state(machine):
    """Reducing risk never waits for a scheduled activity to finish."""
    for start in (EngineState.IDLE, EngineState.COMPUTING, EngineState.REBALANCING):
        machine.state.state = start
        machine.to(EngineState.RISK_ACTION)
        assert machine.state.state is EngineState.RISK_ACTION
        machine.to(EngineState.IDLE)


def test_illegal_transitions_raise_rather_than_silently_pass(machine):
    machine.to(EngineState.COMPUTING)
    with pytest.raises(InvalidTransition):
        machine.to(EngineState.COMPUTING)  # COMPUTING -> COMPUTING is not a thing
    machine.state.state = EngineState.IDLE
    with pytest.raises(InvalidTransition):
        machine.to(EngineState.REBALANCING)  # never without computing targets first


def test_us_t13_ac3_halted_risk_is_absorbing(machine):
    machine.halt("drawdown 26 %")
    assert machine.state.halted
    for target in (
        EngineState.IDLE,
        EngineState.COMPUTING,
        EngineState.REBALANCING,
        EngineState.RISK_ACTION,
        EngineState.SAFE_MODE,
    ):
        with pytest.raises(InvalidTransition):
            machine.to(target)


def test_us_t13_ac3_clearing_a_halt_needs_an_operator_and_a_reason(machine, repos):
    machine.halt("drawdown 26 %")
    with pytest.raises(AegisError):
        machine.clear_halt("eugene", "   ")
    assert machine.state.halted, "a blank reason must change nothing"

    machine.clear_halt("eugene", "reviewed the post-mortem; regime change confirmed")
    assert machine.state.state is EngineState.IDLE
    assert machine.state.halt_reason == ""
    assert machine.state.blocks == set()
    logged = repos.state.controls()
    assert logged[0]["action"] == "clear_halt"
    assert logged[0]["operator"] == "eugene"
    assert "post-mortem" in logged[0]["reason"]


def test_clearing_a_halt_that_is_not_set_is_a_no_op(machine):
    before = machine.state.state
    machine.clear_halt("eugene", "nothing to clear")
    assert machine.state.state is before


def test_pause_blocks_rebalances_but_never_reductions():
    state = MachineState(paused=True)
    assert not state.may_rebalance()
    assert not state.may_increase_risk()
    assert state.may_reduce_risk()


def test_may_reduce_risk_is_true_in_every_possible_state():
    """The one invariant that must never be conditional."""
    for engine_state in EngineState:
        state = MachineState(
            state=engine_state,
            paused=True,
            stopped=True,
            safe_mode=True,
            blocks={"hard_halt_drawdown", "reconciliation_break"},
        )
        assert state.may_reduce_risk() is True, engine_state


def test_any_block_stops_risk_increasing(machine):
    assert machine.state.may_increase_risk()
    machine.block("daily_loss")
    assert not machine.state.may_increase_risk()
    assert machine.state.may_reduce_risk()
    machine.unblock("daily_loss")
    assert machine.state.may_increase_risk()


def test_safe_mode_stops_risk_increasing_but_not_reductions(machine):
    machine.enter_safe_mode("exchange unreachable")
    assert not machine.state.may_increase_risk()
    assert machine.state.may_reduce_risk()
    assert machine.state.context["safe_mode_reason"] == "exchange unreachable"
    machine.leave_safe_mode()
    assert machine.state.may_increase_risk()


def test_a_rebalance_may_not_start_while_one_is_running(machine):
    machine.to(EngineState.COMPUTING)
    machine.to(EngineState.REBALANCING)
    assert not machine.state.may_rebalance()


def test_state_survives_a_restart(repos, clock):
    first = StateMachine(repos, clock, phase=Phase.P2_MICRO_LIVE)
    first.to(EngineState.COMPUTING)
    first.to(EngineState.REBALANCING, rebalance_id="rb-2026-09-08")
    first.block("reconciliation_break")
    first.set_governor(0.5)

    revived = StateMachine(repos, clock)
    state = revived.load()
    assert state.state is EngineState.REBALANCING
    assert state.phase is Phase.P2_MICRO_LIVE
    assert state.blocks == {"reconciliation_break"}
    assert state.governor_g == 0.5
    assert state.context["rebalance_id"] == "rb-2026-09-08"


def test_loading_with_no_stored_row_persists_the_default(repos, clock):
    machine = StateMachine(repos, clock)
    assert machine.load().state is EngineState.IDLE
    assert repos.state.load() is not None


def test_set_blocks_replaces_the_whole_set(machine):
    machine.block("a", "b")
    machine.set_blocks(["c"])
    assert machine.state.blocks == {"c"}
    machine.set_blocks([])
    assert machine.state.blocks == set()
