"""Rebalance planning, execution and drift monitoring (PRD 5.8-5.9, US-T09-T11).

``planner`` is pure and decides *what* to trade; ``executor`` is the only place
in the engine that sends orders and decides *how*; ``drift`` watches the book
between rebalances and only ever reports.
"""

from aegis.rebalance.drift import (
    ALERT_DRIFT,
    ALERT_UNEXPLAINED,
    KIND_DRIFT,
    KIND_UNEXPLAINED,
    DriftMonitor,
)
from aegis.rebalance.executor import (
    ALERT_ILLIQUID,
    ALERT_ORDER_REJECTED,
    ALERT_REDUCE_ONLY_REJECTED,
    ALERT_WINDOW_END,
    KIND_FLATTEN,
    KIND_RISK_CUT,
    KIND_SCHEDULED,
    REASON_UNFILLED,
    REASON_WINDOW_END,
    RebalanceExecutor,
    RebalanceOutcome,
)
from aegis.rebalance.planner import (
    MINUTES_PER_DAY,
    build_plan,
    clip_notional,
    minute_volume,
    planned_notional,
    slice_count,
)

__all__ = [
    "ALERT_DRIFT",
    "ALERT_ILLIQUID",
    "ALERT_ORDER_REJECTED",
    "ALERT_REDUCE_ONLY_REJECTED",
    "ALERT_UNEXPLAINED",
    "ALERT_WINDOW_END",
    "KIND_DRIFT",
    "KIND_FLATTEN",
    "KIND_RISK_CUT",
    "KIND_SCHEDULED",
    "KIND_UNEXPLAINED",
    "MINUTES_PER_DAY",
    "REASON_UNFILLED",
    "REASON_WINDOW_END",
    "DriftMonitor",
    "RebalanceExecutor",
    "RebalanceOutcome",
    "build_plan",
    "clip_notional",
    "minute_volume",
    "planned_notional",
    "slice_count",
]
