"""Operator controls — the only way a human changes what the engine may do.

Two ideas shape this module.

**Pause is not a kill switch.** PRD Section 7 says pause blocks *rebalances*,
not risk actions, and Invariant 1 says the engine may never autonomously
increase risk. Both are asked as questions here — ``may_rebalance()``,
``may_increase_risk()``, ``may_reduce_risk()`` — rather than being re-derived by
each caller from a bag of booleans, because a caller that forgets one of those
booleans fails in the dangerous direction. ``may_reduce_risk()`` is a constant
``True``: the one state worse than a bad position is a bad position you have
locked yourself out of closing.

**Every action is audited before it takes effect.** Each method appends to
``control_log`` with the operator and the reason and then writes
``engine_state``. A control that changed the engine but left no row would make
the post-mortem of a bad day unanswerable.

The destructive actions (``stop``, ``flatten_all``) need the typed-out
confirmation string of Aegis US-18, and ``clear_halt`` needs a non-empty reason
(US-T13 AC 3) — it is the only exit from ``HALTED_RISK``, so the audit trail for
that exit may not be blank.
"""

from __future__ import annotations

from typing import Any

from aegis.core.context import Context
from aegis.core.errors import ConfigError
from aegis.core.types import EngineState, Phase

#: The Aegis US-18 confirmation. Kept here rather than imported from
#: ``engine.startup`` so that ``aegis`` never depends on the process package;
#: the two literals are the same string by design.
CONFIRM_TOKEN = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"

#: Actions written to ``control_log`` (stable — the dashboard filters on them).
START = "start"
PAUSE = "pause"
RESUME = "resume"
STOP = "stop"
FLATTEN_ALL = "flatten_all"
CLEAR_HALT = "clear_halt"

#: Context key the runner reads to discover an operator flatten request. The
#: control itself never sends an order: flattening is an execution action and
#: belongs to the executor, under the same window and slicing rules as any other.
FLATTEN_REQUEST = "flatten_requested"


class Controls:
    """Start / pause / resume / stop / flatten-all / clear-halt, with an audit row each."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- queries ------------------------------------------------------------ #

    def state(self) -> dict[str, Any]:
        """The persisted engine state, with defaults for a database that has none yet."""
        row = self.ctx.repos.state.load()
        if row is None:
            return {
                "state": str(EngineState.IDLE),
                "phase": str(self._default_phase()),
                "paused": False,
                "stopped": False,
                "safe_mode": False,
                "halt_reason": "",
                "governor_g": 1.0,
                "blocks": [],
                "context": {},
            }
        return {
            "state": str(row["state"]),
            "phase": str(row["phase"]),
            "paused": bool(row["paused"]),
            "stopped": bool(row["stopped"]),
            "safe_mode": bool(row["safe_mode"]),
            "halt_reason": str(row["halt_reason"] or ""),
            "governor_g": float(row["governor_g"]),
            "blocks": list(row.get("blocks") or []),
            "context": dict(row.get("context") or {}),
        }

    def may_rebalance(self) -> bool:
        """The scheduled rebalance — the only scheduled trading event (Invariant 9).

        Safe mode is a refusal here, not just for growth: US-T13 AC 2 permits
        "risk-reducing orders only" while it holds, and a rebalance sends both
        directions. The engine must also be idle — asking again while it is
        already ``COMPUTING`` or ``REBALANCING`` would start a second pass over a
        half-traded book and trade the difference twice (5.11).
        """
        s = self.state()
        return (
            not s["paused"]
            and not s["stopped"]
            and not s["safe_mode"]
            and s["state"] == str(EngineState.IDLE)
        )

    def may_increase_risk(self) -> bool:
        """Invariant 1: growth needs a clean engine, no block and no operator hold."""
        s = self.state()
        return (
            not s["paused"]
            and not s["stopped"]
            and not s["safe_mode"]
            and not s["blocks"]
            and s["state"] not in _NO_TRADE
        )

    def may_reduce_risk(self) -> bool:
        """Always true.

        Nothing in this system may return False here. It is a constant rather
        than a computation so that no future condition can accidentally disable
        the engine's ability to get smaller.
        """
        return True

    # -- actions ------------------------------------------------------------ #

    def start(self, operator: str, reason: str = "") -> None:
        """Clear the operator holds. A halted engine stays halted — see ``clear_halt``."""
        s = self.state()
        self._apply(
            START,
            operator,
            reason,
            {},
            s,
            paused=False,
            stopped=False,
            state=EngineState.IDLE if s["state"] == str(EngineState.STOPPED) else None,
        )

    def pause(self, operator: str, reason: str = "") -> None:
        """Block rebalances. Risk actions continue (Section 7)."""
        self._apply(PAUSE, operator, reason, {}, self.state(), paused=True)

    def resume(self, operator: str, reason: str = "") -> None:
        self._apply(RESUME, operator, reason, {}, self.state(), paused=False)

    def stop(self, operator: str, reason: str, confirm: str) -> None:
        """Stop trading entirely. Requires the Aegis US-18 confirmation string."""
        self._check_confirm(STOP, confirm)
        self._apply(
            STOP,
            operator,
            reason,
            {},
            self.state(),
            paused=True,
            stopped=True,
            state=EngineState.STOPPED,
        )

    def flatten_all(self, operator: str, reason: str, confirm: str) -> None:
        """Request that the whole book be closed. Requires the US-18 confirmation.

        The request is recorded, not executed: the runner picks it up and hands
        it to the risk-cut executor, so an operator flatten goes through exactly
        the same slicing, reduce-only and escalation path as an automatic one.
        """
        self._check_confirm(FLATTEN_ALL, confirm)
        s = self.state()
        context = dict(s["context"])
        context[FLATTEN_REQUEST] = {
            "operator": operator,
            "reason": reason,
            "ts": self.ctx.now_ms(),
        }
        self._apply(FLATTEN_ALL, operator, reason, {}, s, context=context)

    def clear_halt(self, operator: str, reason: str) -> None:
        """The only exit from ``HALTED_RISK`` (US-T13 AC 3). A reason is mandatory."""
        if not reason.strip():
            raise ConfigError("clearing a risk halt requires a reason — the exit is audited")
        s = self.state()
        halted = s["state"] == str(EngineState.HALTED_RISK)
        self._apply(
            CLEAR_HALT,
            operator,
            reason,
            {"was_halted": halted},
            s,
            state=EngineState.IDLE if halted else None,
            halt_reason="" if halted else None,
            blocks=[] if halted else None,
        )

    # -- internals ---------------------------------------------------------- #

    def _check_confirm(self, action: str, confirm: str) -> None:
        expected = self.ctx.cfg.phase.live_confirm or CONFIRM_TOKEN
        if confirm != expected:
            raise ConfigError(f"{action} requires the confirmation string {expected!r}")

    def _default_phase(self) -> Phase:
        try:
            return Phase(self.ctx.cfg.phase.current)
        except ValueError:
            return Phase.P0_BACKTEST

    def _apply(
        self,
        action: str,
        operator: str,
        reason: str,
        payload: dict[str, Any],
        before: dict[str, Any],
        *,
        paused: bool | None = None,
        stopped: bool | None = None,
        state: EngineState | None = None,
        halt_reason: str | None = None,
        blocks: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Audit first, then persist. Both happen or the caller sees the exception."""
        now = self.ctx.now_ms()
        after = {
            "state": str(state) if state is not None else before["state"],
            "phase": before["phase"],
            "paused": before["paused"] if paused is None else paused,
            "stopped": before["stopped"] if stopped is None else stopped,
            "safe_mode": before["safe_mode"],
            "halt_reason": before["halt_reason"] if halt_reason is None else halt_reason,
            "governor_g": before["governor_g"],
            "blocks": before["blocks"] if blocks is None else blocks,
            "context": before["context"] if context is None else context,
        }
        self.ctx.repos.state.log_control(
            action,
            operator,
            reason,
            {**payload, "before": before["state"], "after": after["state"]},
            now,
        )
        self.ctx.repos.state.save(
            state=after["state"],
            phase=after["phase"],
            paused=after["paused"],
            stopped=after["stopped"],
            safe_mode=after["safe_mode"],
            halt_reason=after["halt_reason"],
            governor_g=after["governor_g"],
            blocks=after["blocks"],
            context=after["context"],
            now_ms=now,
        )


#: States in which no order may be *initiated* by a scheduled or growth path.
_NO_TRADE = frozenset({str(EngineState.HALTED_RISK), str(EngineState.STOPPED)})


__all__ = [
    "CLEAR_HALT",
    "CONFIRM_TOKEN",
    "FLATTEN_ALL",
    "FLATTEN_REQUEST",
    "PAUSE",
    "RESUME",
    "START",
    "STOP",
    "Controls",
]
