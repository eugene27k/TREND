"""Attribution page (US-T18 AC 5).

Per-symbol and per-side tables for each of the configured metric periods, with
contribution shares, plus the regime table. The tables are SQL roll-ups of
``symbol_pnl_daily`` — the rows the attribution job already wrote — and the
regime table is read out of the stored ``regime_table`` metric rather than
re-bucketed here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from aegis.api.deps import (
    MetricIndex,
    StrategyDeps,
    first_equity_day,
    get_registry,
    get_sleeve,
    share,
    window_for,
)

router = APIRouter(prefix="/api/{strategy}", tags=["attribution"])


class SymbolRow(BaseModel):
    symbol: str
    net_pnl: float
    contribution_share: float


class SideRow(BaseModel):
    side: str
    net_pnl: float
    contribution_share: float


class Components(BaseModel):
    price_pnl: float
    funding: float
    fees: float
    slippage: float
    net_pnl: float
    traded_notional: float


class PeriodAttribution(BaseModel):
    period: str
    start_day: str
    end_day: str
    days: int
    total_net_pnl: float
    components: Components
    by_symbol: list[SymbolRow]
    by_side: list[SideRow]
    concentration: float | None
    n_obs: int
    below_min_active_days: bool


class RegimeRow(BaseModel):
    bucket: str
    label: str
    months: int
    pnl: float
    hit_rate: float | None
    avg_exposure: float | None


class AttributionResponse(BaseModel):
    strategy: str
    as_of_ts: int
    periods: list[PeriodAttribution]
    regime: list[RegimeRow]
    monthly_net_pnl: dict[str, float]


@router.get("/attribution", response_model=AttributionResponse)
def attribution(request: Request, sleeve: StrategyDeps = Depends(get_sleeve)) -> AttributionResponse:
    repos = sleeve.repos
    now_ms = get_registry(request).now_ms()
    metrics = MetricIndex(repos)
    first_day = first_equity_day(repos)

    periods: list[PeriodAttribution] = []
    for period in sleeve.cfg.metrics.periods:
        w = window_for(period, now_ms, first_day)
        components = repos.symbol_pnl.components(w.start_day, w.end_day)
        by_symbol = repos.symbol_pnl.by_symbol(w.start_day, w.end_day)
        by_side = repos.symbol_pnl.by_side(w.start_day, w.end_day)
        total = components["net_pnl"]
        periods.append(
            PeriodAttribution(
                period=period,
                start_day=w.start_day.isoformat(),
                end_day=w.end_day.isoformat(),
                days=w.days,
                total_net_pnl=total,
                components=Components(**components),
                by_symbol=[
                    SymbolRow(symbol=s, net_pnl=v, contribution_share=share(v, total))
                    for s, v in sorted(by_symbol.items(), key=lambda kv: -abs(kv[1]))
                ],
                by_side=[
                    SideRow(side=s, net_pnl=v, contribution_share=share(v, total))
                    for s, v in sorted(by_side.items())
                ],
                concentration=metrics.value("concentration", period),
                n_obs=metrics.n_obs("concentration", period),
                below_min_active_days=bool(
                    metrics.extra("concentration", period).get("below_min_active", True)
                ),
            )
        )

    buckets = metrics.extra("regime_table", "since_inception").get("buckets", {}) or {}
    regime = [
        RegimeRow(
            bucket=key,
            label=str(row.get("label", key)),
            months=int(row.get("months", 0)),
            pnl=float(row.get("pnl", 0.0)),
            hit_rate=row.get("hit_rate"),
            avg_exposure=row.get("avg_exposure"),
        )
        for key, row in buckets.items()
        if isinstance(row, dict)
    ]

    since = window_for("since_inception", now_ms, first_day)
    return AttributionResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        periods=periods,
        regime=regime,
        monthly_net_pnl=repos.symbol_pnl.by_month(since.start_day, since.end_day),
    )


__all__ = ["router"]
