"""Backtest page (US-T18 AC 6, Section 11).

Runs with their manifests and stored metrics, the robustness variants of
Section 11.5, the walk-forward ranking of 11.6, the block-bootstrap percentiles
the ``backtest_p05`` kill rule reads, and the live-vs-reference tracking error.
The page defaults to the latest ``default`` run; ``?run_id=`` pins one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from aegis.api.deps import StrategyDeps, get_registry, get_sleeve
from aegis.storage.db import json_loads

router = APIRouter(prefix="/api/{strategy}", tags=["backtest"])


class RunRow(BaseModel):
    run_id: str
    created_ts: int
    start_day: str
    end_day: str
    variant: str
    git_commit: str
    duration_s: float


class RunDetail(BaseModel):
    run_id: str
    created_ts: int
    start_day: str
    end_day: str
    variant: str
    git_commit: str
    duration_s: float
    metrics: dict[str, Any]
    manifest: dict[str, Any]
    equity: list[dict]


class RobustnessRow(BaseModel):
    variant: str
    net_pnl: float
    sharpe: float
    max_dd: float
    sign_ok: bool
    detail: dict


class WalkforwardRow(BaseModel):
    window: str
    param_set: str
    test_sharpe: float
    rank: int
    n_params: int
    default_in_top_half: bool
    is_default: bool


class TrackingRow(BaseModel):
    day: str
    live_pnl: float
    ref_pnl: float
    cum_live: float
    cum_ref: float
    corr_30d: float | None
    cum_diff_frac: float
    cost_ratio: float | None
    turnover_ratio: float | None
    in_bounds: bool
    breach_days: int


class TrackingPanel(BaseModel):
    latest: TrackingRow | None
    series: list[TrackingRow]
    min_corr: float
    max_cum_diff: float
    max_cost_ratio: float
    max_turnover_ratio: float
    breach_days_block: int


class BacktestResponse(BaseModel):
    strategy: str
    as_of_ts: int
    runs: list[RunRow]
    run: RunDetail | None
    robustness: list[RobustnessRow]
    walkforward: list[WalkforwardRow]
    bootstrap: dict[str, float]
    bootstrap_horizon: str
    tracking: TrackingPanel


def _tracking_row(r: dict[str, Any]) -> TrackingRow:
    return TrackingRow(
        day=r["day"],
        live_pnl=float(r["live_pnl"]),
        ref_pnl=float(r["ref_pnl"]),
        cum_live=float(r["cum_live"]),
        cum_ref=float(r["cum_ref"]),
        corr_30d=r["corr_30d"],
        cum_diff_frac=float(r["cum_diff_frac"]),
        cost_ratio=r["cost_ratio"],
        turnover_ratio=r["turnover_ratio"],
        in_bounds=bool(r["in_bounds"]),
        breach_days=int(r["breach_days"]),
    )


@router.get("/backtest", response_model=BacktestResponse)
def backtest(
    request: Request,
    run_id: str | None = Query(None),
    horizon: str = Query("3m"),
    limit: int = Query(50, ge=1, le=200),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> BacktestResponse:
    repos = sleeve.repos
    runs = repos.backtest.runs(limit)

    row = repos.backtest.get_run(run_id) if run_id else repos.backtest.latest_run()
    if run_id and row is None:
        raise HTTPException(404, f"no backtest run {run_id!r}")

    detail = None
    if row is not None:
        detail = RunDetail(
            run_id=row["run_id"],
            created_ts=int(row["created_ts"]),
            start_day=row["start_day"],
            end_day=row["end_day"],
            variant=row["variant"],
            git_commit=row["git_commit"],
            duration_s=float(row["duration_s"]),
            metrics=json_loads(row["metrics_json"], {}) or {},
            manifest=json_loads(row["manifest_json"], {}) or {},
            equity=json_loads(row["equity_json"], []) or [],
        )

    chosen = detail.run_id if detail else None
    tracking_rows = [_tracking_row(r) for r in repos.tracking.series()]
    latest_tracking = repos.tracking.latest()

    return BacktestResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=get_registry(request).now_ms(),
        runs=[
            RunRow(
                run_id=r["run_id"],
                created_ts=int(r["created_ts"]),
                start_day=r["start_day"],
                end_day=r["end_day"],
                variant=r["variant"],
                git_commit=r["git_commit"],
                duration_s=float(r["duration_s"]),
            )
            for r in runs
        ],
        run=detail,
        robustness=[
            RobustnessRow(
                variant=r["variant"],
                net_pnl=float(r["net_pnl"]),
                sharpe=float(r["sharpe"]),
                max_dd=float(r["max_dd"]),
                sign_ok=bool(r["sign_ok"]),
                detail=json_loads(r["detail_json"], {}) or {},
            )
            for r in (repos.backtest.robustness(chosen) if chosen else [])
        ],
        walkforward=[
            WalkforwardRow(
                window=r["window"],
                param_set=r["param_set"],
                test_sharpe=float(r["test_sharpe"]),
                rank=int(r["rank"]),
                n_params=int(r["n_params"]),
                default_in_top_half=bool(r["default_in_top_half"]),
                is_default=bool(r["is_default"]),
            )
            for r in (repos.backtest.walkforward(chosen) if chosen else [])
        ],
        bootstrap={
            str(p): v for p, v in (repos.backtest.bootstrap(chosen, horizon) if chosen else {}).items()
        },
        bootstrap_horizon=horizon,
        tracking=TrackingPanel(
            latest=_tracking_row(latest_tracking) if latest_tracking else None,
            series=tracking_rows,
            min_corr=sleeve.cfg.tracking.min_corr,
            max_cum_diff=sleeve.cfg.tracking.max_cum_diff,
            max_cost_ratio=sleeve.cfg.tracking.max_cost_ratio,
            max_turnover_ratio=sleeve.cfg.tracking.max_turnover_ratio,
            breach_days_block=sleeve.cfg.tracking.breach_days_block,
        ),
    )


__all__ = ["router"]
