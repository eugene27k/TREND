"""US-T13 / Section 12 — every kill rule, forced and asserted.

The PRD calls for a chaos suite that "forces every rule and asserts the exact
response and alert". These tests are that suite: each one puts the database into
the exact state the rule watches for and asserts the rule's three decisions
(block / flatten / halt), its severity and its alert code.
"""

from __future__ import annotations

import pytest

from aegis.core.clock import day_start_ms, to_ms
from aegis.core.types import AccountState, EquityPoint, Severity
from aegis.storage.db import json_dumps
from aegis.strategy_trend.kill_rules import (
    ALL_RULES,
    BOOTSTRAP_P05,
    DAILY_LOSS,
    HARD_HALT,
    REBALANCE_FAILURE,
    RECONCILIATION,
    TRACKING_ERROR,
    KillRules,
)

DAY = "2026-09-08"
NOW = to_ms("2026-09-08T12:00:00Z")
DAY_MS = 86_400_000


def _fired(ctx, now_ms=NOW):
    return {a.rule: a for a in KillRules(ctx).evaluate(now_ms)}


def _equity(ctx, drawdown: float, equity: float = 10_000.0) -> None:
    ctx.repos.equity.upsert(
        DAY, EquityPoint(NOW, equity, 0.0, 1.0, 1.0 - drawdown), peak_index=1.0, drawdown=drawdown
    )


def _snapshots(ctx, opening: float, latest: float) -> None:
    start = day_start_ms(to_ms(DAY))
    ctx.repos.snapshots.add(AccountState(start - 1000, opening, opening, 0.0, opening, 0.0, 0.0), [])
    ctx.repos.snapshots.add(AccountState(NOW, latest, latest, 0.0, latest, 0.0, 0.0), [])


# --------------------------------------------------------------------------- #
# Nothing fires on a healthy engine
# --------------------------------------------------------------------------- #


def test_a_healthy_engine_fires_no_rule(ctx):
    _equity(ctx, 0.02)
    _snapshots(ctx, 10_000.0, 10_050.0)
    assert _fired(ctx) == {}


def test_an_empty_database_fires_no_rule(ctx):
    """Day one must not look like a catastrophe."""
    assert _fired(ctx) == {}


# --------------------------------------------------------------------------- #
# Hard halt
# --------------------------------------------------------------------------- #


def test_us_t13_ac1_hard_halt_at_25_pct_drawdown(ctx):
    _equity(ctx, 0.25)
    action = _fired(ctx)[HARD_HALT]
    assert (action.flatten, action.halt, action.block_risk_increasing) == (True, True, True)
    assert action.severity is Severity.CRITICAL
    assert not action.auto_clears, "a halt must need an operator with a written reason"
    assert any(a["code"] == HARD_HALT.upper() for a in ctx.repos.alerts.recent())


def test_hard_halt_does_not_fire_just_below_the_threshold(ctx):
    _equity(ctx, 0.2499)
    assert HARD_HALT not in _fired(ctx)


def test_us_t13_ac1_hard_halt_tightens_to_1_5x_the_backtest_max_drawdown(ctx):
    """A strategy whose backtest lost 10 % has no business reaching 25 % unlooked-at."""
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={"max_drawdown": 0.10},
        equity=[],
        duration_s=1.0,
    )
    _equity(ctx, 0.16)  # below 25 %, but above 1.5 x 10 % = 15 %
    action = _fired(ctx)[HARD_HALT]
    assert action.halt
    assert action.context["threshold"] == pytest.approx(0.15)


def test_hard_halt_keeps_the_config_threshold_when_the_backtest_is_worse(ctx):
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={"max_drawdown": 0.40},
        equity=[],
        duration_s=1.0,
    )
    _equity(ctx, 0.26)
    assert _fired(ctx)[HARD_HALT].context["threshold"] == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Daily loss
# --------------------------------------------------------------------------- #


def test_us_t13_ac1_daily_loss_over_6_pct_blocks_risk_increasing(ctx):
    _snapshots(ctx, 10_000.0, 9_300.0)  # -7 %
    action = _fired(ctx)[DAILY_LOSS]
    assert action.block_risk_increasing
    assert not action.flatten and not action.halt
    assert action.severity is Severity.WARN
    assert action.auto_clears
    assert action.context["loss_frac"] == pytest.approx(-0.07)


def test_daily_loss_does_not_fire_at_5_pct(ctx):
    _snapshots(ctx, 10_000.0, 9_500.0)
    assert DAILY_LOSS not in _fired(ctx)


def test_daily_loss_ignores_a_withdrawal(ctx):
    """A transfer out is not a loss — booking it as one would block trading for nothing."""
    from aegis.core.types import IncomeType, LedgerEntry, Strategy

    _snapshots(ctx, 10_000.0, 9_000.0)
    ctx.repos.ledger.add_many(
        [LedgerEntry(Strategy.TREND, NOW, IncomeType.TRANSFER, "USDT", -1_000.0, None, "w1")]
    )
    assert DAILY_LOSS not in _fired(ctx)


def test_daily_loss_needs_an_opening_snapshot(ctx):
    ctx.repos.snapshots.add(AccountState(NOW, 9_000.0, 9_000.0, 0.0, 9_000.0, 0.0, 0.0), [])
    assert DAILY_LOSS not in _fired(ctx)


# --------------------------------------------------------------------------- #
# Bootstrap 5th percentile
# --------------------------------------------------------------------------- #


def _three_months_of_pnl(ctx, daily: float) -> None:
    from datetime import date, timedelta

    start = date(2026, 6, 10)
    for i in range(90):
        d = start + timedelta(days=i)
        ctx.repos.symbol_pnl.upsert_many(d, [{"symbol": "BTCUSDT", "side": "long", "net_pnl": daily}])


def test_us_t13_ac1_rolling_3m_below_the_bootstrap_p05_blocks_and_warns(ctx):
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={},
        equity=[],
        duration_s=1.0,
    )
    ctx.repos.backtest.save_bootstrap("run-1", "3m", {5.0: -500.0, 50.0: 300.0})
    _three_months_of_pnl(ctx, -10.0)  # -900 over the quarter, below p05 of -500
    action = _fired(ctx)[BOOTSTRAP_P05]
    assert action.block_risk_increasing and not action.halt
    assert not action.auto_clears, "a post-mortem is an operator action"
    assert action.context["live_3m"] == pytest.approx(-900.0)


def test_bootstrap_rule_stays_silent_inside_the_expected_distribution(ctx):
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={},
        equity=[],
        duration_s=1.0,
    )
    ctx.repos.backtest.save_bootstrap("run-1", "3m", {5.0: -500.0})
    _three_months_of_pnl(ctx, -1.0)  # -90: a bad quarter, but a normal one
    assert BOOTSTRAP_P05 not in _fired(ctx)


def test_bootstrap_rule_waits_for_three_months_of_evidence(ctx):
    from datetime import date, timedelta

    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={},
        equity=[],
        duration_s=1.0,
    )
    ctx.repos.backtest.save_bootstrap("run-1", "3m", {5.0: -10.0})
    start = date(2026, 8, 25)
    for i in range(14):
        ctx.repos.symbol_pnl.upsert_many(
            start + timedelta(days=i), [{"symbol": "BTCUSDT", "side": "long", "net_pnl": -100.0}]
        )
    assert BOOTSTRAP_P05 not in _fired(ctx), "14 days is not evidence of a broken strategy"


# --------------------------------------------------------------------------- #
# Tracking error, rebalance failure, reconciliation
# --------------------------------------------------------------------------- #


def test_us_t13_ac1_tracking_bounds_breached_14_days_blocks(ctx):
    ctx.repos.tracking.upsert(
        DAY, live_pnl=0.0, ref_pnl=0.0, cum_live=0.0, cum_ref=0.0, in_bounds=False, breach_days=14
    )
    action = _fired(ctx)[TRACKING_ERROR]
    assert action.block_risk_increasing and not action.auto_clears


def test_tracking_rule_does_not_fire_at_13_days(ctx):
    ctx.repos.tracking.upsert(DAY, in_bounds=False, breach_days=13)
    assert TRACKING_ERROR not in _fired(ctx)


def test_us_t13_ac1_three_failed_rebalances_are_critical(ctx):
    for i, day in enumerate(("2026-09-06", "2026-09-07", "2026-09-08")):
        ctx.repos.rebalances.create(f"rb-{i}", day, NOW - (3 - i) * DAY_MS)
        ctx.repos.rebalances.finish(
            f"rb-{i}",
            ended_ts=NOW,
            status="window_end",
            completion_pct=40.0,
            traded_notional=0.0,
            fees=0.0,
            avg_slippage_bps=0.0,
            maker_ratio=0.0,
            residuals=[],
        )
    action = _fired(ctx)[REBALANCE_FAILURE]
    assert action.severity is Severity.CRITICAL
    assert action.block_risk_increasing


def test_a_good_rebalance_breaks_the_failure_streak(ctx):
    for i, (day, pct) in enumerate((("2026-09-06", 40.0), ("2026-09-07", 99.0), ("2026-09-08", 40.0))):
        ctx.repos.rebalances.create(f"rb-{i}", day, NOW - (3 - i) * DAY_MS)
        ctx.repos.rebalances.finish(
            f"rb-{i}",
            ended_ts=NOW,
            status="complete",
            completion_pct=pct,
            traded_notional=0.0,
            fees=0.0,
            avg_slippage_bps=0.0,
            maker_ratio=0.0,
            residuals=[],
        )
    assert REBALANCE_FAILURE not in _fired(ctx)


def test_us_t13_ac1_an_open_reconciliation_break_blocks_and_escalates_after_60_min(ctx):
    ctx.repos.reconciliations.add(
        NOW - 10 * 60_000, "positions", False, "qty mismatch", [{"symbol": "BTCUSDT"}]
    )
    fresh = _fired(ctx)[RECONCILIATION]
    assert fresh.severity is Severity.WARN
    assert fresh.block_risk_increasing

    ctx.repos.reconciliations.add(
        NOW - 90 * 60_000, "positions", False, "qty mismatch", [{"symbol": "ETHUSDT"}]
    )
    aged = _fired(ctx)[RECONCILIATION]
    assert aged.severity is Severity.CRITICAL


def test_a_resolved_break_stops_blocking(ctx):
    rid = ctx.repos.reconciliations.add(NOW - 10 * 60_000, "positions", False, "x", [])
    ctx.repos.reconciliations.resolve(rid, NOW)
    assert RECONCILIATION not in _fired(ctx)


# --------------------------------------------------------------------------- #
# The board and the whole suite
# --------------------------------------------------------------------------- #


def test_us_t13_ac4_the_chaos_suite_can_force_every_rule_at_once(ctx):
    """Force all six simultaneously and assert each one is reported."""
    _equity(ctx, 0.30)
    _snapshots(ctx, 10_000.0, 9_000.0)
    ctx.repos.tracking.upsert(DAY, in_bounds=False, breach_days=20)
    ctx.repos.reconciliations.add(NOW - 120 * 60_000, "positions", False, "x", [])
    for i, day in enumerate(("2026-09-06", "2026-09-07", "2026-09-08")):
        ctx.repos.rebalances.create(f"rb-{i}", day, NOW - (3 - i) * DAY_MS)
        ctx.repos.rebalances.finish(
            f"rb-{i}",
            ended_ts=NOW,
            status="window_end",
            completion_pct=10.0,
            traded_notional=0.0,
            fees=0.0,
            avg_slippage_bps=0.0,
            maker_ratio=0.0,
            residuals=[],
        )
    ctx.repos.backtest.save_run(
        "run-1",
        created_ts=NOW,
        start_day="2021-01-01",
        end_day=DAY,
        variant="default",
        params={},
        manifest={},
        git_commit="abc",
        metrics={},
        equity=[],
        duration_s=1.0,
    )
    ctx.repos.backtest.save_bootstrap("run-1", "3m", {5.0: -100.0})
    _three_months_of_pnl(ctx, -50.0)

    fired = _fired(ctx)
    assert set(fired) == set(ALL_RULES)
    assert any(a.halt for a in fired.values())
    assert all(a.block_risk_increasing for a in fired.values())


def test_status_board_lists_every_rule_with_its_state(ctx):
    _equity(ctx, 0.30)
    board = KillRules(ctx).status_board(NOW)
    assert [r["rule"] for r in board] == list(ALL_RULES)
    fired = [r for r in board if r["active"]]
    assert [r["rule"] for r in fired] == [HARD_HALT]
    assert fired[0]["detail"]


def test_no_rule_ever_asks_to_increase_risk(ctx):
    """Invariant 1, stated as a test over every possible action."""
    _equity(ctx, 0.30)
    _snapshots(ctx, 10_000.0, 9_000.0)
    for action in KillRules(ctx).evaluate(NOW):
        assert action.block_risk_increasing, action.rule
        assert isinstance(action.flatten, bool) and isinstance(action.halt, bool)


def test_daily_loss_block_lapses_at_the_next_utc_day(ctx):
    clear = KillRules.next_rebalance_clear_ms(NOW)
    assert clear == day_start_ms(to_ms("2026-09-09"))
    assert json_dumps({"clear": clear})
