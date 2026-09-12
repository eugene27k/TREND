"""Metrics page (US-T18 AC 6).

The stored metric set, grouped by period, exactly as the metric engine last
wrote it: value, ``n_obs`` and ``std_error`` travel together so the UI can grey
a period with fewer than ``metrics.min_active_days`` active days and show ±SE
where the engine computed one (PRD Section 10.5). Nothing is recomputed here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aegis.api.deps import MetricIndex, StrategyDeps, get_registry, get_sleeve

router = APIRouter(prefix="/api/{strategy}", tags=["metrics"])


class MetricRow(BaseModel):
    name: str
    period: str
    value: float | None
    n_obs: int
    std_error: float | None
    as_of_ts: int
    extra: dict[str, Any]


class PeriodBlock(BaseModel):
    period: str
    as_of_ts: int | None
    active_days: int | None
    below_min_active_days: bool
    metrics: list[MetricRow]


class MetricsResponse(BaseModel):
    strategy: str
    as_of_ts: int
    min_active_days: int
    periods: list[PeriodBlock]


@router.get("/metrics", response_model=MetricsResponse)
def metrics(
    request: Request,
    period: str | None = Query(None, description="restrict to one period, e.g. 30d"),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> MetricsResponse:
    index = MetricIndex(sleeve.repos, period)
    rows = [
        MetricRow(
            name=r["name"],
            period=r["period"],
            value=r["value"],
            n_obs=int(r["n_obs"]),
            std_error=r["std_error"],
            as_of_ts=int(r["as_of_ts"]),
            extra=index.extra(r["name"], r["period"]),
        )
        for r in index.rows()
    ]

    wanted = [period] if period else list(sleeve.cfg.metrics.periods)
    blocks: list[PeriodBlock] = []
    for p in wanted:
        in_period = sorted([r for r in rows if r.period == p], key=lambda r: r.name)
        extra = in_period[0].extra if in_period else {}
        blocks.append(
            PeriodBlock(
                period=p,
                as_of_ts=max((r.as_of_ts for r in in_period), default=None),
                active_days=extra.get("active_days"),
                below_min_active_days=bool(extra.get("below_min_active", True)),
                metrics=in_period,
            )
        )

    return MetricsResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=get_registry(request).now_ms(),
        min_active_days=sleeve.cfg.metrics.min_active_days,
        periods=blocks,
    )


__all__ = ["router"]
