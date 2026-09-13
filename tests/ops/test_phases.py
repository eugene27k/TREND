"""Phase gates — PRD Section 7.

The point of these tests is the difference between a gate and a decoration: an
absent input must report ``actual=None`` and ``passed=False``, never pass by
default.
"""

from __future__ import annotations

from datetime import date

import pytest

from aegis.core.clock import DAY_MS, day_of, to_ms
from aegis.core.context import Context
from aegis.core.types import EquityPoint, MetricValue, Phase, Strategy
from aegis.ops.phases import GateResult, PhaseGates
from aegis.storage.db import json_loads

NOW = to_ms("2026-09-08T00:05:00Z")
WEEK_MS = 7 * DAY_MS


def _criteria(result: GateResult) -> dict[str, dict]:
    return {c["name"]: c for c in result.criteria}


# --------------------------------------------------------------------------- #
# The missing-input rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("phase", list(Phase))
def test_section7_every_gate_fails_on_an_empty_database(ctx: Context, phase: Phase) -> None:
    result = PhaseGates(ctx).evaluate(phase, NOW)

    assert result.passed is False
    assert result.criteria, "a gate with no criteria would pass vacuously"


def test_section7_p0_reports_actual_none_when_there_is_no_backtest(ctx: Context) -> None:
    result = PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW)

    for criterion in result.criteria:
        assert criterion["actual"] is None
        assert criterion["passed"] is False


def test_section7_p1_reports_actual_none_for_every_missing_input(ctx: Context) -> None:
    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    for name in (
        "paper_weeks",
        "rebalances_completed",
        "rebalance_completion_pct",
        "maker_ratio",
        "realised_slippage_within_model",
        "tracking_in_bounds",
        "heartbeat_uptime_pct",
    ):
        assert named[name]["actual"] is None, name
        assert named[name]["passed"] is False, name


def test_a_heartbeat_table_with_no_rows_is_missing_evidence_not_zero_uptime(ctx: Context) -> None:
    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["heartbeat_uptime_pct"]["actual"] is None


# --------------------------------------------------------------------------- #
# P0 — the backtest gate
# --------------------------------------------------------------------------- #


def _save_backtest(ctx: Context, evidence: dict, robustness: list[dict] | None = None) -> None:
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day="2026-09-01",
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={"p0_evidence": evidence},
        equity=[],
        duration_s=1.0,
    )
    if robustness is not None:
        ctx.repos.backtest.save_robustness("run-1", robustness)


PASSING_P0 = {
    "annualised_return": 0.12,
    "sharpe": 0.9,
    "max_drawdown": 0.21,
    "positive_year_fraction": 0.8,
    "max_symbol_contribution": 0.31,
    "turnover_annualised": 18.0,
}
PASSING_ROBUSTNESS = [
    {"variant": "default", "net_pnl": 100.0, "sharpe": 0.9, "max_dd": 0.2, "sign_ok": True},
    {"variant": "fees_x2", "net_pnl": 40.0, "sharpe": 0.5, "max_dd": 0.3, "sign_ok": True},
]


def test_section7_p0_passes_when_every_threshold_is_met(ctx: Context) -> None:
    _save_backtest(ctx, PASSING_P0, PASSING_ROBUSTNESS)

    result = PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW)

    assert result.passed is True
    assert result.failing() == ()
    assert _criteria(result)["net_annualised_return"]["required"] == ">= 0.08"


@pytest.mark.parametrize(
    ("key", "value", "criterion"),
    [
        ("annualised_return", 0.07, "net_annualised_return"),
        ("sharpe", 0.69, "net_sharpe"),
        ("max_drawdown", 0.36, "max_drawdown"),
        ("positive_year_fraction", 0.59, "positive_year_fraction"),
        ("max_symbol_contribution", 0.51, "max_symbol_contribution"),
        ("turnover_annualised", 30.1, "annualised_turnover"),
    ],
)
def test_section7_p0_fails_on_each_threshold_independently(
    ctx: Context, key: str, value: float, criterion: str
) -> None:
    _save_backtest(ctx, {**PASSING_P0, key: value}, PASSING_ROBUSTNESS)

    result = PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW)

    assert result.passed is False
    assert result.failing() == (criterion,)


def test_section7_p0_fails_when_a_robustness_variant_flips_the_sign(ctx: Context) -> None:
    flipped = [
        *PASSING_ROBUSTNESS,
        {"variant": "no_funding", "net_pnl": -10.0, "sharpe": -0.2, "max_dd": 0.4, "sign_ok": False},
    ]
    _save_backtest(ctx, PASSING_P0, flipped)

    result = PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW)

    assert result.failing() == ("robustness_sign_unchanged",)


def test_prd_11_5_6_the_governor_off_variant_does_not_gate_p0(ctx: Context) -> None:
    """11.5.6: governor off exists "to show the governor's contribution, not a gate condition"."""
    with_governor_off = [
        *PASSING_ROBUSTNESS,
        {"variant": "governor_off", "net_pnl": -50.0, "sharpe": -0.3, "max_dd": 0.5, "sign_ok": False},
    ]
    _save_backtest(ctx, PASSING_P0, with_governor_off)

    result = PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW)

    assert result.failing() == ()
    assert _criteria(result)["robustness_sign_unchanged"]["actual"] is True


def test_p0_robustness_is_unanswered_when_only_the_non_gating_variant_ran(ctx: Context) -> None:
    _save_backtest(
        ctx,
        PASSING_P0,
        [{"variant": "governor_off", "net_pnl": 10.0, "sharpe": 0.4, "max_dd": 0.3, "sign_ok": True}],
    )

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW))

    assert named["robustness_sign_unchanged"]["actual"] is None
    assert named["robustness_sign_unchanged"]["passed"] is False


def test_section7_p0_robustness_is_unanswered_when_no_variant_ran(ctx: Context) -> None:
    _save_backtest(ctx, PASSING_P0, robustness=None)

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P0_BACKTEST, NOW))

    assert named["robustness_sign_unchanged"]["actual"] is None
    assert named["robustness_sign_unchanged"]["passed"] is False


# --------------------------------------------------------------------------- #
# P1 — paper
# --------------------------------------------------------------------------- #


def _seed_p1(
    ctx: Context,
    *,
    weeks: float = 9.0,
    rebalances: int = 40,
    completion: float = 98.0,
    uptime_gap: bool = False,
) -> None:
    _save_backtest(ctx, PASSING_P0, PASSING_ROBUSTNESS)
    start = NOW - int(weeks * WEEK_MS)
    ctx.repos.equity.upsert(
        _day(start), EquityPoint(ts_ms=start, equity=10_000.0), peak_index=1.0, drawdown=0.0
    )
    for i in range(rebalances):
        rid = f"rb-{i}"
        day = _day(start + i * DAY_MS)
        ctx.repos.rebalances.create(rid, day, start + i * DAY_MS)
        ctx.repos.rebalances.finish(
            rid,
            ended_ts=start + i * DAY_MS + 600_000,
            status="complete",
            completion_pct=completion,
            traded_notional=1000.0,
            fees=1.0,
            avg_slippage_bps=1.0,
            maker_ratio=0.7,
            residuals=[],
        )
    ctx.repos.metrics.save_many(
        [
            MetricValue(Strategy.TREND, "maker_ratio", "since_inception", 0.71, NOW, n_obs=100),
            MetricValue(
                Strategy.TREND,
                "execution_alpha",
                "since_inception",
                10.0,
                NOW,
                n_obs=100,
                extra={"model_bps": 6.0, "realised_bps": 3.2},
            ),
        ]
    )
    ctx.repos.tracking.upsert(
        _day(NOW),
        corr_30d=0.8,
        cum_diff_frac=0.01,
        cost_ratio=1.2,
        turnover_ratio=1.1,
        in_bounds=True,
        breach_days=0,
    )
    # Beats every 300 s across the whole window — uptime is healthy beats over
    # the beats that interval expected, so a gap is downtime, not invisible.
    beats = (NOW - start) // 300_000
    unhealthy = int(beats * 0.02) if uptime_gap else 0
    for i in range(beats):
        ctx.repos.heartbeats.add(start + i * 300_000, i >= unhealthy, "")


def _day(ms: int) -> date:
    return day_of(ms)


def test_section7_p1_passes_with_eight_weeks_of_clean_paper(ctx: Context) -> None:
    _seed_p1(ctx)

    result = PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW)

    assert result.failing() == ()
    assert result.passed is True


def test_section7_p1_requires_p0_to_have_passed(ctx: Context) -> None:
    _seed_p1(ctx)
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day="2026-09-01",
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={"p0_evidence": {**PASSING_P0, "sharpe": 0.1}},
        equity=[],
        duration_s=1.0,
    )

    assert "p0_passed" in PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW).failing()


def test_section7_p1_needs_eight_weeks(ctx: Context) -> None:
    _seed_p1(ctx, weeks=6.0)

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["paper_weeks"]["passed"] is False
    assert named["paper_weeks"]["actual"] == pytest.approx(6.0, abs=0.01)


def test_section7_p1_needs_forty_rebalances(ctx: Context) -> None:
    _seed_p1(ctx, rebalances=39)

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["rebalances_completed"]["actual"] == 39.0
    assert named["rebalances_completed"]["passed"] is False


def test_section7_p1_needs_a_95_percent_completion_rate(ctx: Context) -> None:
    _seed_p1(ctx, completion=94.0)

    assert "rebalance_completion_pct" in PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW).failing()


def test_section7_p1_needs_uptime_of_99_5_percent(ctx: Context) -> None:
    _seed_p1(ctx, uptime_gap=True)

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["heartbeat_uptime_pct"]["actual"] == pytest.approx(98.0, abs=0.05)
    assert named["heartbeat_uptime_pct"]["passed"] is False


def test_section7_p1_uptime_counts_the_beats_a_dead_process_never_wrote(ctx: Context) -> None:
    """A day of downtime in an eight-week window is 98.2 % uptime, not 100 %.

    The beats the process failed to write are the downtime; dividing healthy
    beats by the rows that happen to exist would make the criterion unfailable.
    """
    _seed_p1(ctx)
    start = NOW - int(9.0 * WEEK_MS)
    ctx.repos.heartbeats.prune(NOW + 1)  # start from a clean table
    beats = (NOW - start) // 300_000
    missing = DAY_MS // 300_000
    for i in range(beats):
        if i < missing:  # the process was off for the first day
            continue
        ctx.repos.heartbeats.add(start + i * 300_000, True, "")

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["heartbeat_uptime_pct"]["actual"] == pytest.approx(
        100.0 * (beats - missing) / beats, abs=0.05
    )
    assert named["heartbeat_uptime_pct"]["passed"] is False


def test_section7_p1_fails_on_any_reconciliation_break(ctx: Context) -> None:
    _seed_p1(ctx)
    ctx.repos.reconciliations.add(NOW - DAY_MS, "positions", False, "qty mismatch", [{"s": "BTC"}])

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["reconciliation_halts"]["actual"] == 1
    assert named["reconciliation_halts"]["passed"] is False


def test_section7_p1_fails_when_realised_slippage_exceeds_the_model(ctx: Context) -> None:
    _seed_p1(ctx)
    ctx.repos.metrics.save_many(
        [
            MetricValue(
                Strategy.TREND,
                "execution_alpha",
                "since_inception",
                -5.0,
                NOW + 1,
                n_obs=100,
                extra={"model_bps": 6.0, "realised_bps": 9.0},
            ),
        ]
    )

    named = _criteria(PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW))

    assert named["realised_slippage_within_model"]["actual"] == 9.0
    assert named["realised_slippage_within_model"]["passed"] is False


def test_section7_p1_fails_when_tracking_is_out_of_bounds(ctx: Context) -> None:
    _seed_p1(ctx)
    ctx.repos.tracking.upsert(
        _day(NOW),
        corr_30d=0.3,
        cum_diff_frac=0.09,
        cost_ratio=3.0,
        turnover_ratio=2.0,
        in_bounds=False,
        breach_days=15,
    )

    assert "tracking_in_bounds" in PhaseGates(ctx).evaluate(Phase.P1_PAPER, NOW).failing()


# --------------------------------------------------------------------------- #
# P2 / P3 — operator approval
# --------------------------------------------------------------------------- #


def test_section7_p2_needs_the_operator_approval_and_the_demo_checklist(ctx: Context) -> None:
    _seed_p1(ctx)
    gates = PhaseGates(ctx)

    named = _criteria(gates.evaluate(Phase.P2_MICRO_LIVE, NOW))
    assert named["p1_passed"]["passed"] is True
    assert named["operator_approval"]["actual"] is None
    assert named["demo_checklist_passed"]["actual"] is None

    gates.approve(Phase.P2_MICRO_LIVE, "alice", "demo run clean", 1500.0, NOW, demo_checklist=False)
    named = _criteria(gates.evaluate(Phase.P2_MICRO_LIVE, NOW))
    assert named["operator_approval"]["passed"] is True
    assert named["demo_checklist_passed"]["passed"] is False

    gates.approve(Phase.P2_MICRO_LIVE, "alice", "checklist re-run", 1500.0, NOW + 1, demo_checklist=True)
    assert gates.evaluate(Phase.P2_MICRO_LIVE, NOW + 1).passed is True


def test_section7_p3_counts_twelve_weeks_from_the_p2_approval(ctx: Context) -> None:
    gates = PhaseGates(ctx)
    entered = NOW - 11 * WEEK_MS
    gates.approve(Phase.P2_MICRO_LIVE, "alice", "go", 1500.0, entered, demo_checklist=True)
    ctx.repos.tracking.upsert(
        _day(NOW),
        corr_30d=0.8,
        cum_diff_frac=0.01,
        cost_ratio=1.2,
        turnover_ratio=1.1,
        in_bounds=True,
        breach_days=0,
    )
    ctx.repos.metrics.save_many(
        [
            MetricValue(Strategy.TREND, "vol_ratio", "30d", 0.9, NOW, n_obs=30),
        ]
    )
    gates.approve(Phase.P3_SCALED, "alice", "scale to 5k", 5000.0, NOW)

    named = _criteria(gates.evaluate(Phase.P3_SCALED, NOW))
    assert named["weeks_in_p2"]["passed"] is False

    later = NOW + 2 * WEEK_MS
    assert gates.evaluate(Phase.P3_SCALED, later).passed is True


def test_section7_p3_fails_when_a_kill_rule_fired_since_p2(ctx: Context) -> None:
    gates = PhaseGates(ctx)
    entered = NOW - 13 * WEEK_MS
    gates.approve(Phase.P2_MICRO_LIVE, "alice", "go", 1500.0, entered, demo_checklist=True)
    gates.approve(Phase.P3_SCALED, "alice", "scale", 5000.0, NOW)
    ctx.repos.tracking.upsert(
        _day(NOW),
        corr_30d=0.8,
        cum_diff_frac=0.01,
        cost_ratio=1.2,
        turnover_ratio=1.1,
        in_bounds=True,
        breach_days=0,
    )
    ctx.repos.metrics.save_many([MetricValue(Strategy.TREND, "vol_ratio", "30d", 1.0, NOW)])
    assert gates.evaluate(Phase.P3_SCALED, NOW).passed is True

    ctx.alerts.critical("HARD_HALT_DRAWDOWN", "flattening")

    named = _criteria(gates.evaluate(Phase.P3_SCALED, NOW))
    assert named["kill_rules_triggered"]["actual"] == 1
    assert named["kill_rules_triggered"]["passed"] is False


def test_section7_p3_fails_when_realised_vol_leaves_the_half_to_one_and_a_half_band(
    ctx: Context,
) -> None:
    gates = PhaseGates(ctx)
    gates.approve(Phase.P2_MICRO_LIVE, "a", "go", 1500.0, NOW - 13 * WEEK_MS, demo_checklist=True)
    ctx.repos.metrics.save_many([MetricValue(Strategy.TREND, "vol_ratio", "30d", 1.8, NOW)])

    named = _criteria(gates.evaluate(Phase.P3_SCALED, NOW))

    assert named["realised_vol_ratio_30d"]["required"] == "0.5-1.5"
    assert named["realised_vol_ratio_30d"]["passed"] is False


def test_section7_p3_fails_when_the_fee_ratio_doubles_the_backtest(ctx: Context) -> None:
    gates = PhaseGates(ctx)
    gates.approve(Phase.P2_MICRO_LIVE, "a", "go", 1500.0, NOW - 13 * WEEK_MS, demo_checklist=True)
    ctx.repos.tracking.upsert(
        _day(NOW),
        corr_30d=0.8,
        cum_diff_frac=0.01,
        cost_ratio=2.5,
        turnover_ratio=1.1,
        in_bounds=True,
        breach_days=0,
    )

    assert "fee_ratio" in gates.evaluate(Phase.P3_SCALED, NOW).failing()


# --------------------------------------------------------------------------- #
# Recording the evidence
# --------------------------------------------------------------------------- #


def test_record_writes_the_criteria_to_approvals_as_evidence(ctx: Context) -> None:
    _save_backtest(ctx, PASSING_P0, PASSING_ROBUSTNESS)
    gates = PhaseGates(ctx)
    result = gates.evaluate(Phase.P0_BACKTEST, NOW)

    gates.record(result, "alice", "P0 cleared", 0.0, NOW)

    row = ctx.repos.approvals.latest(str(Phase.P0_BACKTEST))
    assert row["granted"] == 1
    assert row["operator"] == "alice"
    evidence = json_loads(row["evidence_json"], {})
    assert evidence["passed"] is True
    assert [c["name"] for c in evidence["criteria"]] == [c["name"] for c in result.criteria]


def test_record_stores_a_failed_gate_too_because_a_refusal_is_also_evidence(ctx: Context) -> None:
    gates = PhaseGates(ctx)
    result = gates.evaluate(Phase.P0_BACKTEST, NOW)

    gates.record(result, "alice", "checked before the run", 0.0, NOW)

    row = ctx.repos.approvals.latest(str(Phase.P0_BACKTEST))
    assert row["granted"] == 0
    assert json_loads(row["evidence_json"], {})["passed"] is False


def test_a_recorded_gate_is_not_mistaken_for_an_operator_approval(ctx: Context) -> None:
    _seed_p1(ctx)
    gates = PhaseGates(ctx)
    gates.record(gates.evaluate(Phase.P2_MICRO_LIVE, NOW), "alice", "checked", 0.0, NOW)

    named = _criteria(gates.evaluate(Phase.P2_MICRO_LIVE, NOW))

    assert named["operator_approval"]["actual"] is None
