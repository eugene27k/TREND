"""Universe page (US-T18 AC 6, PRD 5.1 and 5.10).

The monthly selection history with, for each month, who entered and who left
against the month before and the stored reason for every exclusion — plus the
symbols currently flagged illiquid, which are in the universe but not traded
(5.9 step 6). Entrants and leavers are set differences over rows already
stored; nothing re-runs the selection.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aegis.api.deps import StrategyDeps, get_registry, get_sleeve

router = APIRouter(prefix="/api/{strategy}", tags=["universe"])


class UniverseEntryRow(BaseModel):
    symbol: str
    rank: int
    median_quote_volume_30d: float
    history_days: int
    included: bool
    reason: str


class MonthRow(BaseModel):
    month: str
    symbols: list[str]
    entrants: list[str]
    leavers: list[str]
    excluded: list[UniverseEntryRow]
    entries: list[UniverseEntryRow]


class IlliquidRow(BaseModel):
    symbol: str
    flagged_ts: int
    until_ts: int
    reason: str


class UniverseResponse(BaseModel):
    strategy: str
    as_of_ts: int
    size: int
    current_month: str | None
    current_symbols: list[str]
    months: list[MonthRow]
    illiquid: list[IlliquidRow]


@router.get("/universe", response_model=UniverseResponse)
def universe(
    request: Request,
    months: int = Query(24, ge=1, le=240),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> UniverseResponse:
    repos = sleeve.repos
    now_ms = get_registry(request).now_ms()

    all_months = repos.universe.months()
    shown = all_months[-months:]
    # The month before the window, so the first row shown still has entrants.
    first = all_months.index(shown[0]) if shown else 0
    previous = all_months[first - 1] if first > 0 else None

    rows: list[MonthRow] = []
    prior: set[str] = set(repos.universe.symbols(previous)) if previous else set()
    known_prior = previous is not None
    for month in shown:
        result = repos.universe.month(month)
        entries = [
            UniverseEntryRow(
                symbol=e.symbol,
                rank=e.rank,
                median_quote_volume_30d=e.median_quote_volume_30d,
                history_days=e.history_days,
                included=e.included,
                reason=e.reason,
            )
            for e in (result.entries if result else ())
        ]
        included = [e.symbol for e in entries if e.included]
        rows.append(
            MonthRow(
                month=month,
                symbols=included,
                entrants=sorted(set(included) - prior) if known_prior else [],
                leavers=sorted(prior - set(included)) if known_prior else [],
                excluded=[e for e in entries if not e.included],
                entries=entries,
            )
        )
        prior = set(included)
        known_prior = True

    current_month = repos.universe.latest_month()
    return UniverseResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        size=sleeve.cfg.universe.size,
        current_month=current_month,
        current_symbols=repos.universe.symbols(current_month) if current_month else [],
        months=rows,
        illiquid=[
            IlliquidRow(
                symbol=r["symbol"],
                flagged_ts=int(r["flagged_ts"]),
                until_ts=int(r["until_ts"]),
                reason=r["reason"],
            )
            for r in repos.illiquid.active(now_ms)
        ],
    )


__all__ = ["router"]
