"""Rebalances page (US-T18 AC 4).

The list answers "did yesterday's rebalance finish" at a glance; the drill-down
answers "why not" by showing the stored plan, the per-symbol targets, every
slice and every fill of one rebalance. All four come from the rows the executor
persisted, so an aborted run is as readable afterwards as a clean one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from aegis.api.deps import StrategyDeps, get_registry, get_sleeve
from aegis.storage.db import json_loads

router = APIRouter(prefix="/api/{strategy}", tags=["rebalances"])


class RebalanceRow(BaseModel):
    rebalance_id: str
    day: str
    kind: str
    status: str
    started_ts: int
    ended_ts: int | None
    duration_s: float | None
    completion_pct: float
    traded_notional: float
    planned_notional: float
    fees: float
    avg_slippage_bps: float
    maker_ratio: float
    equity: float
    governor_g: float
    n_residuals: int


class RebalancesResponse(BaseModel):
    strategy: str
    as_of_ts: int
    rows: list[RebalanceRow]
    avg_completion_pct: float | None


class TargetRow(BaseModel):
    symbol: str
    signal: float
    vol: float
    raw: float
    target_notional: float
    target_qty: float
    current_qty: float
    delta_notional: float
    funding_ann: float
    funding_haircut: float
    caps_applied: list[str]
    traded: bool


class SliceRow(BaseModel):
    slice_id: str
    symbol: str
    seq: int
    side: str
    qty: float
    reduce_only: bool
    placed_ts: int
    ended_ts: int | None
    repegs: int
    outcome: str
    fill_qty: float
    avg_price: float
    taker: bool


class FillRow(BaseModel):
    trade_id: str
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    is_maker: bool
    realized_pnl: float
    slippage_bps: float
    ts: int


class RebalanceDetail(BaseModel):
    strategy: str
    as_of_ts: int
    rebalance: RebalanceRow
    sizing: dict[str, float]
    order_plan: list[dict]
    residuals: list[dict]
    decision_mids: dict[str, float]
    cursor: int
    targets: list[TargetRow]
    slices: list[SliceRow]
    fills: list[FillRow]


def _row(r: dict) -> RebalanceRow:
    ended = r["ended_ts"]
    residuals = json_loads(r["residuals_json"], []) or []
    return RebalanceRow(
        rebalance_id=r["rebalance_id"],
        day=r["day"],
        kind=r["kind"],
        status=r["status"],
        started_ts=int(r["started_ts"]),
        ended_ts=int(ended) if ended is not None else None,
        duration_s=(int(ended) - int(r["started_ts"])) / 1000.0 if ended is not None else None,
        completion_pct=float(r["completion_pct"]),
        traded_notional=float(r["traded_notional"]),
        planned_notional=float(r["planned_notional"]),
        fees=float(r["fees"]),
        avg_slippage_bps=float(r["avg_slippage_bps"]),
        maker_ratio=float(r["maker_ratio"]),
        equity=float(r["equity"]),
        governor_g=float(r["governor_g"]),
        n_residuals=len(residuals),
    )


@router.get("/rebalances", response_model=RebalancesResponse)
def rebalances(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> RebalancesResponse:
    repos = sleeve.repos
    return RebalancesResponse(
        strategy=str(sleeve.strategy),
        as_of_ts=get_registry(request).now_ms(),
        rows=[_row(r) for r in repos.rebalances.recent(limit)],
        avg_completion_pct=repos.rebalances.avg_completion(),
    )


@router.get("/rebalances/{rebalance_id}", response_model=RebalanceDetail)
def rebalance_detail(
    rebalance_id: str,
    request: Request,
    sleeve: StrategyDeps = Depends(get_sleeve),
) -> RebalanceDetail:
    repos = sleeve.repos
    row = repos.rebalances.get(rebalance_id)
    if row is None:
        raise HTTPException(404, f"no rebalance {rebalance_id!r} for {sleeve.strategy}")

    targets = repos.targets.for_rebalance(rebalance_id)
    sizing: dict[str, float] = {}
    if targets:
        first = targets[0]
        sizing = {k: float(first[k]) for k in ("sigma_p", "conv", "sigma_eff", "s", "g")}

    return RebalanceDetail(
        strategy=str(sleeve.strategy),
        as_of_ts=get_registry(request).now_ms(),
        rebalance=_row(row),
        sizing=sizing,
        order_plan=json_loads(row["order_plan_json"], []) or [],
        residuals=json_loads(row["residuals_json"], []) or [],
        decision_mids=json_loads(row["decision_mids_json"], {}) or {},
        cursor=int(row["cursor"]),
        targets=[
            TargetRow(
                symbol=t["symbol"], signal=float(t["signal"]), vol=float(t["vol"]),
                raw=float(t["raw"]), target_notional=float(t["target_notional"]),
                target_qty=float(t["target_qty"]), current_qty=float(t["current_qty"]),
                delta_notional=float(t["delta_notional"]), funding_ann=float(t["funding_ann"]),
                funding_haircut=float(t["funding_haircut"]),
                caps_applied=list(json_loads(t["caps_json"], []) or []),
                traded=bool(t["traded"]),
            )
            for t in targets
        ],
        slices=[
            SliceRow(
                slice_id=s["slice_id"], symbol=s["symbol"], seq=int(s["seq"]), side=s["side"],
                qty=float(s["qty"]), reduce_only=bool(s["reduce_only"]),
                placed_ts=int(s["placed_ts"]),
                ended_ts=int(s["ended_ts"]) if s["ended_ts"] is not None else None,
                repegs=int(s["repegs"]), outcome=s["outcome"], fill_qty=float(s["fill_qty"]),
                avg_price=float(s["avg_price"]), taker=bool(s["taker"]),
            )
            for s in repos.slices.for_rebalance(rebalance_id)
        ],
        fills=[
            FillRow(
                trade_id=f.trade_id, order_id=f.order_id, symbol=f.symbol, side=str(f.side),
                qty=f.qty, price=f.price, fee=f.fee, is_maker=f.is_maker,
                realized_pnl=f.realized_pnl, slippage_bps=f.slippage_bps, ts=f.ts_ms,
            )
            for f in repos.fills.for_rebalance(rebalance_id)
        ],
    )


__all__ = ["router"]
