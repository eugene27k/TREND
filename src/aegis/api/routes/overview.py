"""Overview page (US-T18 AC 1) and the combined all-sleeves view (AC 7).

The chart carries three lines: the equity curve as recorded, the backtest
reference run aligned by day, and a cash alternative. The first two are read
straight out of ``equity_curve`` and ``backtest_runs``; the cash line is
compounded from the configured risk-free rate over the same days, because it is
a chart overlay rather than a stored metric — the metric engine stores
``cash_alternative`` as a single number, which is the KPI tile here.

Every other number on this page is either a stored metric or a SQL roll-up of
``symbol_pnl_daily`` (US-T18 AC 8: no metric is recomputed on the fly).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from aegis.api.deps import (
    DAY_MAX,
    DAY_MIN,
    MetricIndex,
    Registry,
    StrategyDeps,
    engine_state,
    first_equity_day,
    get_registry,
    get_sleeve,
    latest_equity,
    risk_status,
    window_for,
)
from aegis.storage.db import json_loads

router = APIRouter(prefix="/api/{strategy}", tags=["overview"])
combined_router = APIRouter(prefix="/api", tags=["overview"])


class CurvePoint(BaseModel):
    day: str
    equity: float
    twr_index: float
    drawdown: float
    cash_alternative: float | None = None
    backtest_reference: float | None = None


class PnlComponents(BaseModel):
    price_pnl: float
    funding: float
    fees: float
    slippage: float
    net_pnl: float
    traded_notional: float


class SidePnl(BaseModel):
    side: str
    net_pnl: float


class Kpis(BaseModel):
    equity: float
    since_inception_net: float
    net_30d: float
    realised_vol: float | None
    vol_target: float
    vol_ratio: float | None
    max_drawdown: float | None
    current_drawdown: float
    governor_g: float
    sharpe: float | None
    sharpe_std_error: float | None
    sharpe_n_obs: int
    cash_alternative: float | None
    gross_notional: float
    net_notional: float
    gross_x: float
    net_x: float
    phase: str
    state: str
    risk_status: str
    paused: bool
    blocks: list[str]
    below_min_active_days: bool


class OverviewResponse(BaseModel):
    strategy: str
    as_of_ts: int
    kpis: Kpis
    curve: list[CurvePoint]
    cumulative_components: PnlComponents
    by_side: list[SidePnl]
    reference_run_id: str | None


class SleeveSummary(BaseModel):
    strategy: str
    equity: float
    current_drawdown: float
    max_drawdown: float | None
    governor_g: float
    state: str
    phase: str
    risk_status: str
    paused: bool
    blocks: list[str]


class CombinedOverviewResponse(BaseModel):
    as_of_ts: int
    sleeves: list[SleeveSummary]
    total_equity: float


def _reference_by_day(sleeve: StrategyDeps) -> tuple[str | None, dict[str, float]]:
    """The latest default backtest run's equity path, keyed by day."""
    run = sleeve.repos.backtest.latest_run()
    if not run:
        return None, {}
    points = json_loads(run["equity_json"], []) or []
    by_day: dict[str, float] = {}
    for p in points:
        if isinstance(p, dict) and p.get("day") is not None:
            by_day[str(p["day"])] = float(p.get("equity", 0.0))
    return str(run["run_id"]), by_day


def _cash_curve(rows: list[dict[str, Any]], rf_annual: float) -> dict[str, float]:
    """Equity had the same capital sat in cash at ``bench.rf_annual``."""
    if not rows:
        return {}
    start_equity = float(rows[0]["equity"])
    start_day = date.fromisoformat(rows[0]["day"])
    out: dict[str, float] = {}
    for r in rows:
        days = (date.fromisoformat(r["day"]) - start_day).days
        out[r["day"]] = start_equity * (1.0 + rf_annual) ** (days / 365.0)
    return out


def _summary(sleeve: StrategyDeps) -> SleeveSummary:
    repos = sleeve.repos
    state = engine_state(repos, sleeve.cfg)
    metrics = MetricIndex(repos)
    curve_row = repos.equity.latest()
    snap = repos.snapshots.latest()
    margin_ratio = float(snap["margin_ratio"]) if snap else 0.0
    return SleeveSummary(
        strategy=str(sleeve.strategy),
        equity=latest_equity(repos),
        current_drawdown=float(curve_row["drawdown"]) if curve_row else 0.0,
        max_drawdown=metrics.value("max_drawdown", "since_inception"),
        governor_g=state["governor_g"],
        state=state["state"],
        phase=state["phase"],
        risk_status=str(risk_status(sleeve.cfg, margin_ratio, state)),
        paused=state["paused"],
        blocks=state["blocks"],
    )


@router.get("/overview", response_model=OverviewResponse)
def overview(request: Request, sleeve: StrategyDeps = Depends(get_sleeve)) -> OverviewResponse:
    repos = sleeve.repos
    cfg = sleeve.cfg
    now_ms = get_registry(request).now_ms()
    metrics = MetricIndex(repos)
    state = engine_state(repos, cfg)

    rows = repos.equity.all()
    cash = _cash_curve(rows, cfg.bench.rf_annual)
    run_id, reference = _reference_by_day(sleeve)
    curve = [
        CurvePoint(
            day=r["day"],
            equity=float(r["equity"]),
            twr_index=float(r["twr_index"]),
            drawdown=float(r["drawdown"]),
            cash_alternative=cash.get(r["day"]),
            backtest_reference=reference.get(r["day"]),
        )
        for r in rows
    ]

    components = repos.symbol_pnl.components(DAY_MIN, DAY_MAX)
    w30 = window_for("30d", now_ms, first_equity_day(repos))
    net_30d = repos.symbol_pnl.components(w30.start_day, w30.end_day)["net_pnl"]
    by_side = [SidePnl(side=k, net_pnl=v) for k, v in sorted(repos.symbol_pnl.by_side(DAY_MIN, DAY_MAX).items())]

    snap = repos.snapshots.latest()
    equity = latest_equity(repos)
    gross = float(snap["gross_notional"]) if snap else 0.0
    net = float(snap["net_notional"]) if snap else 0.0
    margin_ratio = float(snap["margin_ratio"]) if snap else 0.0
    latest_curve = repos.equity.latest()

    kpis = Kpis(
        equity=equity,
        since_inception_net=components["net_pnl"],
        net_30d=net_30d,
        realised_vol=metrics.value("realised_vol", "30d"),
        vol_target=cfg.sizing.sigma_target_portfolio,
        vol_ratio=metrics.value("vol_ratio", "30d"),
        max_drawdown=metrics.value("max_drawdown", "since_inception"),
        current_drawdown=float(latest_curve["drawdown"]) if latest_curve else 0.0,
        governor_g=state["governor_g"],
        sharpe=metrics.value("sharpe", "since_inception"),
        sharpe_std_error=metrics.std_error("sharpe", "since_inception"),
        sharpe_n_obs=metrics.n_obs("sharpe", "since_inception"),
        cash_alternative=metrics.value("cash_alternative", "since_inception"),
        gross_notional=gross,
        net_notional=net,
        gross_x=gross / equity if equity > 0 else 0.0,
        net_x=net / equity if equity > 0 else 0.0,
        phase=state["phase"],
        state=state["state"],
        risk_status=str(risk_status(cfg, margin_ratio, state)),
        paused=state["paused"],
        blocks=state["blocks"],
        below_min_active_days=bool(
            metrics.extra("sharpe", "since_inception").get("below_min_active", True)
        ),
    )
    return OverviewResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=now_ms,
        kpis=kpis,
        curve=curve,
        cumulative_components=PnlComponents(**components),
        by_side=by_side,
        reference_run_id=run_id,
    )


@combined_router.get("/overview", response_model=CombinedOverviewResponse)
def combined_overview(registry: Registry = Depends(get_registry)) -> CombinedOverviewResponse:
    """Every deployed sleeve side by side (US-T18 AC 7)."""
    sleeves = [_summary(registry.get(str(s))) for s in registry.strategies]
    return CombinedOverviewResponse(
        as_of_ts=registry.now_ms(),
        sleeves=sleeves,
        total_equity=sum(s.equity for s in sleeves),
    )


__all__ = ["combined_router", "router"]
