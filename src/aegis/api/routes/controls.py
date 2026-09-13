"""Controls (US-T18 AC 6) — the one endpoint in this service that writes.

It writes exactly one row, to ``control_log``, and never touches
``engine_state``. That split is deliberate: the engine owns its own state
machine, reads the log on its next tick and applies the action under the same
rules as any other control (``aegis.ops.controls``). A dashboard that flipped
``paused`` itself would be a second writer of engine state and could, on a bad
day, disagree with the process that is actually holding the positions.

The validation here is a pre-flight, not a substitute for the engine's: the
destructive actions need the typed confirmation of Aegis US-18 and
``clear_halt`` needs a written reason (US-T13 AC 3), so an unusable row never
reaches the log in the first place. The token is forwarded in the payload rather
than consumed here, because the engine re-checks it — one rule, one place.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aegis.api.deps import Registry, StrategyDeps, engine_state, get_registry, get_sleeve
from aegis.ops.controls import (
    CLEAR_HALT,
    CONFIRM_TOKEN,
    FLATTEN_ALL,
    PAUSE,
    RESUME,
    START,
    STOP,
)

router = APIRouter(prefix="/api/{strategy}", tags=["controls"])

#: Actions that change the book or end the run — they need the typed confirmation.
_NEEDS_CONFIRM = (STOP, FLATTEN_ALL)


class ControlRequest(BaseModel):
    action: Literal["start", "pause", "resume", "stop", "flatten_all", "clear_halt"]
    operator: str = Field(min_length=1, description="who is asking — stored in the audit row")
    reason: str = ""
    confirm: str = ""


class ControlAccepted(BaseModel):
    accepted: bool
    strategy: str
    action: str
    operator: str
    reason: str
    ts: int
    detail: str


class ControlLogRow(BaseModel):
    id: int
    ts: int
    action: str
    operator: str
    reason: str


class ControlsResponse(BaseModel):
    strategy: str
    as_of_ts: int
    state: dict
    actions: list[str]
    confirm_required: list[str]
    log: list[ControlLogRow]


@router.get("/controls", response_model=ControlsResponse)
def controls(request: Request, sleeve: StrategyDeps = Depends(get_sleeve)) -> ControlsResponse:
    repos = sleeve.repos
    return ControlsResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=get_registry(request).now_ms(),
        state=engine_state(repos, sleeve.cfg),
        actions=[START, PAUSE, RESUME, STOP, FLATTEN_ALL, CLEAR_HALT],
        confirm_required=list(_NEEDS_CONFIRM),
        log=[
            ControlLogRow(
                id=int(c["id"]),
                ts=int(c["ts"]),
                action=c["action"],
                operator=c["operator"],
                reason=c["reason"],
            )
            for c in repos.state.controls(100)
        ],
    )


@router.post("/controls", response_model=ControlAccepted, status_code=202)
def submit_control(
    body: ControlRequest,
    request: Request,
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> ControlAccepted:
    registry: Registry = get_registry(request)
    _validate(body, sleeve)
    now_ms = registry.now_ms()
    registry.append_control(
        sleeve,
        body.action,
        body.operator,
        body.reason,
        # The token is forwarded, not consumed: the engine re-checks it, so the
        # rule lives in exactly one place (aegis.ops.controls) and a request that
        # somehow bypassed this pre-flight still cannot stop or flatten anything.
        {"source": "api", "confirm": body.confirm},
        now_ms,
    )
    return ControlAccepted(
        accepted=True,
        strategy=str(sleeve.strategy),
        action=body.action,
        operator=body.operator,
        reason=body.reason,
        ts=now_ms,
        detail="queued in control_log; the engine applies it on its next tick",
    )


def _expected_confirm(sleeve: StrategyDeps) -> str:
    """The token this sleeve's engine will accept — ``aegis.ops.controls`` decides it.

    A deployment may set ``phase.live_confirm`` to its own string, and
    ``Controls._check_confirm`` then requires *that* one. The pre-flight has to
    ask the same question, or a sleeve with a configured token would refuse the
    operator's correct string with a 400 and accept the wrong one with a 202.
    """
    return sleeve.cfg.phase.live_confirm or CONFIRM_TOKEN


def _validate(body: ControlRequest, sleeve: StrategyDeps) -> None:
    expected = _expected_confirm(sleeve)
    if body.action in _NEEDS_CONFIRM and body.confirm != expected:
        raise HTTPException(400, f"action {body.action!r} requires confirm == {expected!r}")
    if body.action in (STOP, FLATTEN_ALL, CLEAR_HALT) and not body.reason.strip():
        raise HTTPException(400, f"action {body.action!r} requires a written reason")


__all__ = ["router"]
