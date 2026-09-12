"""Positions and risk page (US-T18 AC 3).

Answers "what is it holding and how close is it to a limit": the book with
funding accrued per symbol, caps utilisation against the configured multiples of
equity, the margin picture with the survivable adverse move, the governor's
history and the kill-rule board.

Two reuse notes. ``survivable_move`` is called on ``RiskSupervisor`` unbound —
the rule takes nothing from ``self``, and borrowing it keeps one definition of
the formula while the API stays free of a ``Context`` (which would carry a
gateway this process must never have). The kill-rule board is rebuilt from
stored state rather than by calling ``KillRules.evaluate``, which emits alerts
and would therefore make a page load a write.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aegis.api.deps import (
    StrategyDeps,
    engine_state,
    get_registry,
    get_sleeve,
    latest_equity,
    risk_status,
)
from aegis.core.types import IncomeType
from aegis.strategy_trend.kill_rules import ALL_RULES
from aegis.strategy_trend.risk_supervisor import RiskSupervisor

router = APIRouter(prefix="/api/{strategy}", tags=["positions"])


class PositionRow(BaseModel):
    symbol: str
    qty: float
    side: str
    notional: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    funding_accrued: float
    adl_quantile: int
    leverage: float
    liquidation_price: float
    target_notional: float
    ts: int


class CapRow(BaseModel):
    cap: str
    limit_x: float
    limit_usdt: float
    used_usdt: float
    used_x: float
    utilisation: float
    breached: bool


class MarginPanel(BaseModel):
    equity: float
    wallet_balance: float
    available_balance: float
    maint_margin: float
    margin_ratio: float
    margin_amber: float
    margin_red: float
    survivable_move: float
    shock_price: float
    survives_downtime: bool


class GovernorRow(BaseModel):
    ts: int
    dd: float
    g_before: float
    g_after: float
    trigger: str
    applied: bool


class KillRuleRow(BaseModel):
    rule: str
    active: bool
    detail: str
    last_fired_ts: int | None


class PositionsResponse(BaseModel):
    strategy: str
    as_of_ts: int
    risk_status: str
    state: str
    blocks: list[str]
    governor_g: float
    positions: list[PositionRow]
    caps: list[CapRow]
    margin: MarginPanel
    governor_history: list[GovernorRow]
    kill_rules: list[KillRuleRow]


def _cap(name: str, limit_x: float, used: float, equity: float) -> CapRow:
    limit_usdt = limit_x * equity
    return CapRow(
        cap=name,
        limit_x=limit_x,
        limit_usdt=limit_usdt,
        used_usdt=used,
        used_x=used / equity if equity > 0 else 0.0,
        utilisation=used / limit_usdt if limit_usdt > 0 else 0.0,
        breached=used > limit_usdt > 0,
    )


def _kill_board(repos: Any, blocks: list[str]) -> list[KillRuleRow]:
    out: list[KillRuleRow] = []
    for rule in ALL_RULES:
        alert = repos.alerts.last_of_code(rule.upper())
        out.append(
            KillRuleRow(
                rule=rule,
                active=rule in blocks,
                detail=str(alert["message"]) if alert else "",
                last_fired_ts=int(alert["ts"]) if alert else None,
            )
        )
    return out


@router.get("/positions", response_model=PositionsResponse)
def positions(
    request: Request,
    governor_limit: int = Query(200, ge=1, le=2000),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> PositionsResponse:
    repos = sleeve.repos
    cfg = sleeve.cfg
    now_ms = get_registry(request).now_ms()

    book = repos.positions.all()
    equity = latest_equity(repos)
    state = engine_state(repos, cfg)
    snap = repos.snapshots.latest()
    funding = repos.ledger.sum_by_symbol(str(IncomeType.FUNDING_FEE), 0, now_ms + 1)
    targets = repos.targets.latest_by_symbol()

    rows = [
        PositionRow(
            symbol=p.symbol,
            qty=p.qty,
            side=str(p.side),
            notional=p.notional,
            entry_price=p.entry_price,
            mark_price=p.mark_price,
            unrealized_pnl=p.unrealized_pnl,
            funding_accrued=funding.get(p.symbol, 0.0),
            adl_quantile=p.adl_quantile,
            leverage=p.leverage,
            liquidation_price=p.liquidation_price,
            target_notional=targets.get(p.symbol, 0.0),
            ts=p.ts_ms,
        )
        for p in sorted(book.values(), key=lambda p: -abs(p.notional))
    ]

    gross = sum(abs(p.notional) for p in book.values())
    net = sum(p.notional for p in book.values())
    largest = max((abs(p.notional) for p in book.values()), default=0.0)
    caps = [
        _cap("gross", cfg.caps.gross, gross, equity),
        _cap("net", cfg.caps.net, abs(net), equity),
        _cap("single", cfg.caps.single, largest, equity),
    ]

    # The supervisor's rule, borrowed rather than restated — see the docstring.
    survivable = RiskSupervisor.survivable_move(None, book, equity)  # type: ignore[arg-type]
    margin = MarginPanel(
        equity=equity,
        wallet_balance=float(snap["wallet_balance"]) if snap else 0.0,
        available_balance=float(snap["available_balance"]) if snap else 0.0,
        maint_margin=float(snap["maint_margin"]) if snap else 0.0,
        margin_ratio=float(snap["margin_ratio"]) if snap else 0.0,
        margin_amber=cfg.risk.margin_amber,
        margin_red=cfg.risk.margin_red,
        survivable_move=survivable,
        shock_price=cfg.risk.shock_price,
        survives_downtime=survivable > cfg.risk.shock_price,
    )

    return PositionsResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        risk_status=str(risk_status(cfg, margin.margin_ratio, state)),
        state=state["state"],
        blocks=state["blocks"],
        governor_g=state["governor_g"],
        positions=rows,
        caps=caps,
        margin=margin,
        governor_history=[
            GovernorRow(
                ts=int(r["ts"]),
                dd=float(r["dd"]),
                g_before=float(r["g_before"]),
                g_after=float(r["g_after"]),
                trigger=str(r["trigger"]),
                applied=bool(r["applied"]),
            )
            for r in repos.governor.history(governor_limit)
        ],
        kill_rules=_kill_board(repos, state["blocks"]),
    )


__all__ = ["router"]
