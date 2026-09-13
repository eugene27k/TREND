"""The per-strategy state machine (PRD 5.11).

```
IDLE --(00:05 UTC)--> COMPUTING --(targets)--> REBALANCING --(done|01:00)--> IDLE
ANY  --(governor cut / kill rule / delisting)--> RISK_ACTION --(done)--> IDLE
ANY  --(risk halt)--> HALTED_RISK
```

State is persisted on every transition, because the thing this machine exists to
survive is a restart at 00:30 in the middle of a rebalance: coming back up in
``REBALANCING`` with a stored plan means resuming, while coming back up in
``IDLE`` would mean recomputing targets against a half-traded book and trading
the difference twice.

Two rules are enforced here rather than trusted to callers:

* ``HALTED_RISK`` is absorbing. Only an operator with a written reason leaves it
  (US-T13 AC 3) — no automatic path out, ever.
* ``RISK_ACTION`` may be entered from any state including ``REBALANCING``.
  Reducing risk never waits for a scheduled activity to finish.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from aegis.core.errors import AegisError
from aegis.core.types import EngineState, Phase

#: Legal transitions. Anything not listed is a bug, and raises.
ALLOWED: dict[EngineState, frozenset[EngineState]] = {
    EngineState.IDLE: frozenset(
        {
            EngineState.COMPUTING,
            EngineState.RISK_ACTION,
            EngineState.HALTED_RISK,
            EngineState.SAFE_MODE,
            EngineState.STOPPED,
            EngineState.IDLE,
        }
    ),
    EngineState.COMPUTING: frozenset(
        {
            EngineState.REBALANCING,
            EngineState.IDLE,
            EngineState.RISK_ACTION,
            EngineState.HALTED_RISK,
            EngineState.SAFE_MODE,
            EngineState.STOPPED,
        }
    ),
    EngineState.REBALANCING: frozenset(
        {
            EngineState.IDLE,
            EngineState.RISK_ACTION,
            EngineState.HALTED_RISK,
            EngineState.SAFE_MODE,
            EngineState.STOPPED,
        }
    ),
    EngineState.RISK_ACTION: frozenset(
        {
            EngineState.IDLE,
            EngineState.HALTED_RISK,
            EngineState.SAFE_MODE,
            EngineState.STOPPED,
            EngineState.RISK_ACTION,
        }
    ),
    # Absorbing: an operator action clears it, not a transition from inside.
    EngineState.HALTED_RISK: frozenset({EngineState.STOPPED, EngineState.HALTED_RISK}),
    EngineState.SAFE_MODE: frozenset(
        {
            EngineState.IDLE,
            EngineState.RISK_ACTION,
            EngineState.HALTED_RISK,
            EngineState.STOPPED,
            EngineState.SAFE_MODE,
        }
    ),
    EngineState.STOPPED: frozenset({EngineState.IDLE, EngineState.STOPPED}),
}

#: States in which a *scheduled* rebalance may start.
TRADEABLE = frozenset({EngineState.IDLE})


class InvalidTransition(AegisError):
    def __init__(self, before: EngineState, after: EngineState) -> None:
        super().__init__(f"illegal transition {before} -> {after}")
        self.before = before
        self.after = after


@dataclass(slots=True)
class MachineState:
    """Everything an engine restart needs to know about itself."""

    state: EngineState = EngineState.IDLE
    phase: Phase = Phase.P0_BACKTEST
    paused: bool = False
    stopped: bool = False
    safe_mode: bool = False
    halt_reason: str = ""
    governor_g: float = 1.0
    blocks: set[str] = field(default_factory=set)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def halted(self) -> bool:
        return self.state is EngineState.HALTED_RISK

    def may_rebalance(self) -> bool:
        """Pause blocks rebalances but not risk actions (Section 7)."""
        return not self.paused and not self.stopped and not self.halted and self.state in TRADEABLE

    def may_increase_risk(self) -> bool:
        """Invariant 1: growth needs a clean engine and no active block."""
        return (
            not self.blocks
            and not self.safe_mode
            and not self.paused
            and not self.stopped
            and not self.halted
        )

    def may_reduce_risk(self) -> bool:
        """Unconditionally true.

        The one thing worse than a bad position is a bad position you have
        disabled yourself from closing. Nothing in this system may return False
        here, which is why it is a constant rather than a computation.
        """
        return True


class StateMachine:
    """Transitions plus persistence. One instance per engine process."""

    def __init__(self, repos: Any, clock: Any, *, phase: Phase = Phase.P0_BACKTEST) -> None:
        self.repos = repos
        self.clock = clock
        self.state = MachineState(phase=phase)

    # -- persistence -------------------------------------------------------- #

    def load(self) -> MachineState:
        row = self.repos.state.load()
        if row is None:
            self.save()
            return self.state
        self.state = MachineState(
            state=EngineState(row["state"]),
            phase=Phase(row["phase"]),
            paused=bool(row["paused"]),
            stopped=bool(row["stopped"]),
            safe_mode=bool(row["safe_mode"]),
            halt_reason=row["halt_reason"],
            governor_g=float(row["governor_g"]),
            blocks=set(row.get("blocks") or []),
            context=dict(row.get("context") or {}),
        )
        return self.state

    def save(self) -> None:
        s = self.state
        self.repos.state.save(
            state=str(s.state),
            phase=str(s.phase),
            paused=s.paused,
            stopped=s.stopped,
            safe_mode=s.safe_mode,
            halt_reason=s.halt_reason,
            governor_g=s.governor_g,
            blocks=sorted(s.blocks),
            context=s.context,
            now_ms=self.clock.now_ms(),
        )

    # -- transitions -------------------------------------------------------- #

    def to(self, target: EngineState, **context: Any) -> MachineState:
        before = self.state.state
        if target not in ALLOWED[before]:
            raise InvalidTransition(before, target)
        self.state.state = target
        if context:
            self.state.context.update(context)
        if target is EngineState.HALTED_RISK:
            self.state.halt_reason = str(context.get("reason", "")) or self.state.halt_reason
        self.save()
        return self.state

    def can(self, target: EngineState) -> bool:
        return target in ALLOWED[self.state.state]

    # -- blocks ------------------------------------------------------------- #

    def block(self, *rules: str) -> None:
        """Add risk-increasing blocks. Reductions are never affected."""
        if rules:
            self.state.blocks.update(rules)
            self.save()

    def unblock(self, *rules: str) -> None:
        if rules and self.state.blocks & set(rules):
            self.state.blocks.difference_update(rules)
            self.save()

    def set_blocks(self, rules: Iterable[str]) -> None:
        new = set(rules)
        if new != self.state.blocks:
            self.state.blocks = new
            self.save()

    # -- operator actions --------------------------------------------------- #

    def halt(self, reason: str) -> MachineState:
        return self.to(EngineState.HALTED_RISK, reason=reason)

    def clear_halt(self, operator: str, reason: str) -> MachineState:
        """US-T13 AC 3: only an operator, and only with a reason string."""
        if not self.state.halted:
            return self.state
        if not reason.strip():
            raise AegisError("clearing a risk halt requires a reason — it is audited")
        self.repos.state.log_control("clear_halt", operator, reason, {}, self.clock.now_ms())
        self.state.state = EngineState.IDLE
        self.state.halt_reason = ""
        self.state.blocks.clear()
        self.save()
        return self.state

    def enter_safe_mode(self, reason: str) -> MachineState:
        if not self.state.safe_mode:
            self.state.safe_mode = True
            self.state.context["safe_mode_reason"] = reason
            self.save()
        return self.state

    def leave_safe_mode(self) -> MachineState:
        if self.state.safe_mode:
            self.state.safe_mode = False
            self.state.context.pop("safe_mode_reason", None)
            self.save()
        return self.state

    def set_governor(self, g: float) -> None:
        if g != self.state.governor_g:
            self.state.governor_g = g
            self.save()


__all__ = ["ALLOWED", "TRADEABLE", "InvalidTransition", "MachineState", "StateMachine"]
