"""Capital phase gates (PRD Section 7).

A gate is a question asked of *stored evidence*, not a checklist someone ticks.
Every condition in the Section 7 table becomes one row —
``{"name", "required", "actual", "passed"}`` — so the operator can see which
single number is holding the phase back, and so the evidence can be written
verbatim into ``approvals`` when a phase is entered.

The rule that makes this a gate rather than a decoration: **a criterion whose
input is missing reports ``actual=None`` and ``passed=False``.** An absent
backtest, an empty tracking table or a heartbeat table with no rows is not
evidence of success; defaulting any of those to "fine" would let capital move on
the strength of a query returning nothing.

Phase gates never trade and never call the exchange. They read the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aegis.core.clock import DAY_MS
from aegis.core.context import Context
from aegis.core.types import Phase
from aegis.storage.db import json_loads
from aegis.strategy_trend.kill_rules import ALL_RULES

WEEK_MS = 7 * DAY_MS

#: Section 7 thresholds. Named constants because the report prints them.
P0_MIN_ANNUAL_RETURN = 0.08
P0_MIN_SHARPE = 0.7
P0_MAX_DRAWDOWN = 0.35
P0_MIN_POSITIVE_YEARS = 0.60
P0_MAX_SYMBOL_SHARE = 0.50
P0_MAX_TURNOVER = 30.0

P1_MIN_WEEKS = 8
P1_MIN_REBALANCES = 40
P1_MIN_COMPLETION_PCT = 95.0
P1_MIN_MAKER_RATIO = 0.50
P1_MIN_UPTIME_PCT = 99.5

P3_MIN_WEEKS_IN_P2 = 12
P3_VOL_RATIO_MIN = 0.5
P3_VOL_RATIO_MAX = 1.5

#: ``evidence_json["kind"]`` values in ``approvals``.
GATE_EVIDENCE = "gate"
OPERATOR_EVIDENCE = "operator_approval"

#: Alert codes emitted by the kill rules (``KillAction.rule.upper()``).
KILL_CODES: tuple[str, ...] = tuple(rule.upper() for rule in ALL_RULES)


@dataclass(frozen=True, slots=True)
class GateResult:
    phase: Phase
    passed: bool
    criteria: tuple[dict[str, Any], ...]

    def failing(self) -> tuple[str, ...]:
        return tuple(c["name"] for c in self.criteria if not c["passed"])

    def as_evidence(self) -> dict[str, Any]:
        return {
            "kind": GATE_EVIDENCE,
            "phase": str(self.phase),
            "passed": self.passed,
            "criteria": [dict(c) for c in self.criteria],
        }


# --------------------------------------------------------------------------- #
# Criterion builders — the only place a comparison is written
# --------------------------------------------------------------------------- #


def _row(name: str, required: Any, actual: Any, passed: bool) -> dict[str, Any]:
    return {"name": name, "required": required, "actual": actual, "passed": bool(passed)}


def _at_least(name: str, required: float, actual: float | None) -> dict[str, Any]:
    return _row(name, f">= {required}", actual, actual is not None and actual >= required)


def _at_most(name: str, required: float, actual: float | None) -> dict[str, Any]:
    return _row(name, f"<= {required}", actual, actual is not None and actual <= required)


def _exactly(name: str, required: Any, actual: Any) -> dict[str, Any]:
    return _row(name, f"== {required}", actual, actual is not None and actual == required)


def _is_true(name: str, actual: bool | None) -> dict[str, Any]:
    return _row(name, "true", actual, actual is True)


def _within(name: str, low: float, high: float, actual: float | None) -> dict[str, Any]:
    return _row(name, f"{low}-{high}", actual, actual is not None and low <= actual <= high)


class PhaseGates:
    """Evaluates a phase's entry conditions and records the evidence."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- public ------------------------------------------------------------- #

    def evaluate(self, phase: Phase, now_ms: int) -> GateResult:
        builders = {
            Phase.P0_BACKTEST: self._p0,
            Phase.P1_PAPER: self._p1,
            Phase.P2_MICRO_LIVE: self._p2,
            Phase.P3_SCALED: self._p3,
        }
        criteria = tuple(builders[Phase(phase)](now_ms))
        return GateResult(
            phase=Phase(phase),
            passed=bool(criteria) and all(c["passed"] for c in criteria),
            criteria=criteria,
        )

    def record(self, result: GateResult, operator: str, reason: str, capital: float, now_ms: int) -> None:
        """Write the evaluated gate to ``approvals`` — pass or fail, both are evidence."""
        self.ctx.repos.approvals.add(
            str(result.phase),
            result.passed,
            operator,
            reason,
            result.as_evidence(),
            capital,
            now_ms,
        )

    def approve(
        self,
        phase: Phase,
        operator: str,
        reason: str,
        capital: float,
        now_ms: int,
        *,
        demo_checklist: bool = False,
    ) -> None:
        """Record the operator's own approval for a phase (Section 7, P2/P3).

        Separate from ``record``: the gate's arithmetic and a human's decision
        are different pieces of evidence, and P2 asks for both.
        """
        evidence = {
            "kind": OPERATOR_EVIDENCE,
            "phase": str(Phase(phase)),
            "demo_checklist": bool(demo_checklist),
        }
        self.ctx.repos.approvals.add(str(Phase(phase)), True, operator, reason, evidence, capital, now_ms)

    # -- P0: the backtest ----------------------------------------------------- #

    def _p0(self, _now_ms: int) -> list[dict[str, Any]]:
        run = self.ctx.repos.backtest.latest_run("default")
        metrics: dict[str, Any] = json_loads(run["metrics_json"], {}) if run else {}
        evidence: dict[str, Any] = dict(metrics.get("p0_evidence") or {})

        def num(key: str) -> float | None:
            value = evidence.get(key, metrics.get(key))
            return None if value is None else float(value)

        return [
            _at_least("net_annualised_return", P0_MIN_ANNUAL_RETURN, num("annualised_return")),
            _at_least("net_sharpe", P0_MIN_SHARPE, num("sharpe")),
            _at_most("max_drawdown", P0_MAX_DRAWDOWN, num("max_drawdown")),
            _at_least("positive_year_fraction", P0_MIN_POSITIVE_YEARS, num("positive_year_fraction")),
            _at_most("max_symbol_contribution", P0_MAX_SYMBOL_SHARE, num("max_symbol_contribution")),
            _is_true("robustness_sign_unchanged", self._robustness_sign_ok(run)),
            _at_most("annualised_turnover", P0_MAX_TURNOVER, num("turnover_annualised")),
        ]

    def _robustness_sign_ok(self, run: dict[str, Any] | None) -> bool | None:
        """None when no variant was ever run — an unasked question is not a pass."""
        if run is None:
            return None
        rows = self.ctx.repos.backtest.robustness(run["run_id"])
        if not rows:
            return None
        return all(bool(r["sign_ok"]) for r in rows)

    # -- P1: paper ------------------------------------------------------------ #

    def _p1(self, now_ms: int) -> list[dict[str, Any]]:
        repos = self.ctx.repos
        start_ms = self._live_start_ms()
        weeks = None if start_ms is None else (now_ms - start_ms) / WEEK_MS
        completed = repos.rebalances.count(kind="scheduled", status="complete")
        realised_bps, model_bps = self._cost_bps()
        tracking = repos.tracking.latest()
        beats = 0 if start_ms is None else repos.heartbeats.count(start_ms, now_ms)
        uptime = repos.heartbeats.uptime_pct(start_ms, now_ms) if beats else None
        maker, _ = self._metric("maker_ratio", "since_inception")

        return [
            _is_true("p0_passed", self.evaluate(Phase.P0_BACKTEST, now_ms).passed),
            _at_least("paper_weeks", P1_MIN_WEEKS, weeks),
            _at_least("rebalances_completed", P1_MIN_REBALANCES, float(completed) if completed else None),
            _at_least("rebalance_completion_pct", P1_MIN_COMPLETION_PCT, repos.rebalances.avg_completion()),
            _at_least("maker_ratio", P1_MIN_MAKER_RATIO, maker),
            _row(
                "realised_slippage_within_model",
                "<= model" if model_bps is None else f"<= {model_bps}",
                realised_bps,
                realised_bps is not None and model_bps is not None and realised_bps <= model_bps,
            ),
            _is_true("tracking_in_bounds", None if tracking is None else bool(tracking["in_bounds"])),
            _exactly("reconciliation_halts", 0, repos.reconciliations.count_breaks()),
            _at_least("heartbeat_uptime_pct", P1_MIN_UPTIME_PCT, uptime),
        ]

    # -- P2: micro live -------------------------------------------------------- #

    def _p2(self, now_ms: int) -> list[dict[str, Any]]:
        approval = self._operator_approval(Phase.P2_MICRO_LIVE)
        checklist = None if approval is None else bool(approval["evidence"].get("demo_checklist"))
        return [
            _is_true("p1_passed", self.evaluate(Phase.P1_PAPER, now_ms).passed),
            _is_true("operator_approval", None if approval is None else True),
            _is_true("demo_checklist_passed", checklist),
        ]

    # -- P3: scaled ------------------------------------------------------------ #

    def _p3(self, now_ms: int) -> list[dict[str, Any]]:
        entered = self._entered_p2_ms()
        weeks = None if entered is None else (now_ms - entered) / WEEK_MS
        tracking = self.ctx.repos.tracking.latest()
        cost_ratio = None if tracking is None else _float_or_none(tracking["cost_ratio"])
        vol_ratio, _ = self._metric("vol_ratio", "30d")
        since = entered if entered is not None else 0
        kills = self.ctx.repos.alerts.count_codes_since(KILL_CODES, since)
        approval = self._operator_approval(Phase.P3_SCALED)

        return [
            _at_least("weeks_in_p2", P3_MIN_WEEKS_IN_P2, weeks),
            _is_true("tracking_in_bounds", None if tracking is None else bool(tracking["in_bounds"])),
            _at_most("fee_ratio", self.ctx.cfg.tracking.max_cost_ratio, cost_ratio),
            _within("realised_vol_ratio_30d", P3_VOL_RATIO_MIN, P3_VOL_RATIO_MAX, vol_ratio),
            _exactly("kill_rules_triggered", 0, kills),
            _is_true("operator_approval", None if approval is None else True),
        ]

    # -- evidence loaders -------------------------------------------------------- #

    def _metric(self, name: str, period: str) -> tuple[float | None, dict[str, Any]]:
        row = self.ctx.repos.metrics.latest(period).get(f"{name}:{period}")
        if row is None:
            return None, {}
        value = row["value"]
        return (None if value is None else float(value)), json_loads(row["extra_json"], {})

    def _cost_bps(self) -> tuple[float | None, float | None]:
        """Realised vs modelled cost in bps — both None when the metric is absent."""
        _, extra = self._metric("execution_alpha", "since_inception")
        return _float_or_none(extra.get("realised_bps")), _float_or_none(extra.get("model_bps"))

    def _live_start_ms(self) -> int | None:
        """First day on the equity curve, else the first account snapshot."""
        rows = self.ctx.repos.equity.all()
        if rows:
            return int(rows[0]["ts"])
        first = self.ctx.repos.snapshots.first()
        return int(first["ts"]) if first else None

    def _operator_approval(self, phase: Phase) -> dict[str, Any] | None:
        for row in self._operator_approvals(phase):
            return row
        return None

    def _operator_approvals(self, phase: Phase) -> Sequence[dict[str, Any]]:
        return [
            r
            for r in self.ctx.repos.approvals.for_phase(str(Phase(phase)))
            if r["granted"] and (r.get("evidence") or {}).get("kind") == OPERATOR_EVIDENCE
        ]

    def _entered_p2_ms(self) -> int | None:
        """When micro-live started — the *earliest* operator approval for P2."""
        rows = self._operator_approvals(Phase.P2_MICRO_LIVE)
        return min(int(r["ts"]) for r in rows) if rows else None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "GATE_EVIDENCE",
    "KILL_CODES",
    "OPERATOR_EVIDENCE",
    "GateResult",
    "PhaseGates",
]
