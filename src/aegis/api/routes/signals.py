"""Signals page (US-T18 AC 2).

One row per symbol of the current universe: the signal and the three ``u_k``
that made it, the vol estimate, what the last rebalance wanted versus what is
actually held, and the funding overlay with its haircut flag. The per-symbol
history endpoint feeds the 90-day chart.

Symbols are taken from the latest stored signal day and unioned with whatever
the book still holds, so a position in a symbol that has left the universe stays
visible instead of quietly disappearing from the page that explains it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aegis.api.deps import StrategyDeps, get_registry, get_sleeve

router = APIRouter(prefix="/api/{strategy}", tags=["signals"])


class SignalRow(BaseModel):
    symbol: str
    day: str | None
    signal: float
    u1: float | None
    u2: float | None
    u3: float | None
    warm: bool
    vol: float | None
    target_notional: float
    current_notional: float
    delta_notional: float
    funding_ann: float
    funding_haircut: float
    haircut_applied: bool
    in_universe: bool
    illiquid: bool


class SignalsResponse(BaseModel):
    strategy: str
    as_of_ts: int
    day: str | None
    rebalance_id: str | None
    rows: list[SignalRow]


class SignalHistoryPoint(BaseModel):
    day: str
    signal: float
    u1: float | None
    u2: float | None
    u3: float | None
    warm: bool


class SignalHistoryResponse(BaseModel):
    strategy: str
    symbol: str
    days: int
    points: list[SignalHistoryPoint]


def _f(row: dict[str, Any] | None, key: str, default: float = 0.0) -> float:
    if not row or row.get(key) is None:
        return default
    return float(row[key])


@router.get("/signals", response_model=SignalsResponse)
def signals(request: Request, sleeve: StrategyDeps = Depends(get_sleeve)) -> SignalsResponse:
    repos = sleeve.repos
    now_ms = get_registry(request).now_ms()

    day = repos.signals.latest_day()
    snapshots = {r["symbol"]: r for r in (repos.signals.day(day) if day else [])}
    targets = {r["symbol"]: r for r in repos.targets.latest()}
    positions = repos.positions.all()
    month = repos.universe.latest_month()
    universe = set(repos.universe.symbols(month)) if month else set()
    illiquid = repos.illiquid.active_symbols(now_ms)
    rebalance_id = next(iter(targets.values()))["rebalance_id"] if targets else None

    rows: list[SignalRow] = []
    for symbol in sorted(set(snapshots) | set(targets) | set(positions)):
        snap = snapshots.get(symbol)
        tgt = targets.get(symbol)
        pos = positions.get(symbol)
        current = pos.notional if pos else 0.0
        target_notional = _f(tgt, "target_notional")
        haircut = _f(tgt, "funding_haircut", 1.0)
        rows.append(
            SignalRow(
                symbol=symbol,
                day=day,
                signal=_f(snap, "signal") if snap else _f(tgt, "signal"),
                u1=snap.get("u1") if snap else None,
                u2=snap.get("u2") if snap else None,
                u3=snap.get("u3") if snap else None,
                warm=bool(snap["warm"]) if snap else False,
                vol=_f(tgt, "vol") if tgt else None,
                target_notional=target_notional,
                current_notional=current,
                delta_notional=target_notional - current,
                funding_ann=_f(tgt, "funding_ann"),
                funding_haircut=haircut,
                haircut_applied=haircut < 1.0,
                in_universe=symbol in universe,
                illiquid=symbol in illiquid,
            )
        )
    return SignalsResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        day=day,
        rebalance_id=rebalance_id,
        rows=rows,
    )


@router.get("/signals/{symbol}/history", response_model=SignalHistoryResponse)
def signal_history(
    symbol: str,
    days: int = Query(90, ge=1, le=3650),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> SignalHistoryResponse:
    rows = sleeve.repos.signals.history(symbol, days)
    return SignalHistoryResponse(
        strategy=str(sleeve.strategy),
        symbol=symbol,
        days=days,
        points=[
            SignalHistoryPoint(
                day=r["day"],
                signal=float(r["signal"]),
                u1=r["u1"],
                u2=r["u2"],
                u3=r["u3"],
                warm=bool(r["warm"]),
            )
            for r in rows
        ],
    )


__all__ = ["router"]
