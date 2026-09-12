"""Report bodies — PRD Appendix D, line by line (US-T17 AC 1, 2, 4)."""

from __future__ import annotations

import pytest

from aegis.core.clock import DAY_MS, FakeClock, day_start_ms, to_ms
from aegis.core.context import Context
from aegis.core.types import Side, Strategy
from aegis.ops.alerts import AlertBus
from aegis.ops.reports import DAILY, MONTHLY, NA, REBALANCE, WEEKLY, Reporter
from aegis.storage.repositories import Repositories
from tests.ops.conftest import DAY, REBALANCE_ID, seed_daily

EXPECTED_DAILY = (
    "TREND · daily · 2026-09-08\n"
    "Equity 9 874.10 USDT (−0.62 % day, −1.26 % since start) · DD from peak 2.1 % · g = 1.0\n"
    "Net P&L day −61.20 = price −55.90 · funding +3.10 · fees −6.30 · slippage −2.10\n"
    "Exposure gross 1.32× · net −0.41× · 6 long / 9 short / 1 flat · largest ETH short 0.21×\n"
    "Realised vol 30d 17.8 % (target 20 %) · Sharpe since start n/a (12 obs)\n"
    "Rebalance 100 % in 23 min · maker 71 % · slippage 1.4 bps · traded 3 120 USDT\n"
    "Risk GREEN · margin 9 % · ADL n/a · caps ok\n"
    "Heartbeat 100 % · reconciliation OK · backup lag 8 s · BNB fees 41 d\n"
    "Next universe refresh 2026-10-01 · infra cost month-to-date €0.00"
)


def _ctx(cfg, report_clock: FakeClock, gateway, repos: Repositories) -> Context:
    return Context(
        cfg=cfg,
        clock=report_clock,
        gateway=gateway,
        repos=repos,
        alerts=AlertBus(repos.alerts, report_clock, Strategy.TREND),
    )


@pytest.fixture
def rctx(cfg, report_clock, gateway, repos) -> Context:
    seed_daily(repos)
    return _ctx(cfg, report_clock, gateway, repos)


# --------------------------------------------------------------------------- #
# Appendix D — daily
# --------------------------------------------------------------------------- #


def test_us_t17_ac4_daily_body_matches_appendix_d_line_by_line(rctx: Context) -> None:
    body = Reporter(rctx).daily(DAY, rctx.now_ms())

    assert body.split("\n") == EXPECTED_DAILY.split("\n")


def test_us_t17_ac4_daily_body_is_stored_in_reports(rctx: Context) -> None:
    body = Reporter(rctx).daily(DAY, rctx.now_ms())

    row = rctx.repos.reports.get(DAILY, DAY.isoformat())
    assert row is not None
    assert row["body"] == body
    assert row["delivered"] == 0


def test_a_daily_report_on_an_empty_database_renders_n_a_and_never_crashes(
    cfg, report_clock, gateway, repos
) -> None:
    ctx = _ctx(cfg, report_clock, gateway, repos)

    body = Reporter(ctx).daily(DAY, ctx.now_ms())

    lines = body.split("\n")
    assert lines[0] == "TREND · daily · 2026-09-08"
    assert lines[1] == f"Equity {NA} {NA} · DD from peak {NA} · g = 1.0"
    assert lines[2] == f"Net P&L day {NA}"
    assert lines[3] == f"Exposure gross {NA} · net {NA} · {NA} · largest {NA}"
    assert lines[5] == f"Rebalance {NA}"
    assert lines[6] == f"Risk {NA} · margin {NA} · ADL {NA} · caps {NA}"
    assert "0" not in lines[2]


def test_a_cap_breach_downgrades_the_risk_line_to_amber(cfg, report_clock, gateway, repos) -> None:
    seed_daily(repos)
    # Re-snapshot with a gross far above the 2.5x cap.
    from aegis.core.types import AccountState

    end_ms = day_start_ms(DAY) + DAY_MS
    repos.snapshots.add(
        AccountState(
            ts_ms=end_ms - 1,
            wallet_balance=9874.10,
            margin_balance=9874.10,
            unrealized_pnl=0.0,
            available_balance=1000.0,
            maint_margin=9874.10 * 0.09,
            initial_margin=2000.0,
        ),
        [],
        gross=3.0 * 9874.10,
        net=0.0,
    )
    ctx = _ctx(cfg, report_clock, gateway, repos)

    line = Reporter(ctx).daily(DAY, ctx.now_ms()).split("\n")[6]

    assert line.startswith("Risk AMBER")
    assert "caps breached gross" in line


def test_a_high_margin_ratio_reports_red(cfg, report_clock, gateway, repos) -> None:
    from aegis.core.types import AccountState

    end_ms = day_start_ms(DAY) + DAY_MS
    repos.snapshots.add(
        AccountState(
            ts_ms=end_ms - 1,
            wallet_balance=1000.0,
            margin_balance=1000.0,
            unrealized_pnl=0.0,
            available_balance=10.0,
            maint_margin=400.0,
            initial_margin=500.0,
        ),
        [],
        gross=0.0,
        net=0.0,
    )
    ctx = _ctx(cfg, report_clock, gateway, repos)

    assert Reporter(ctx).daily(DAY, ctx.now_ms()).split("\n")[6].startswith("Risk RED")


# --------------------------------------------------------------------------- #
# Appendix D — rebalance summary
# --------------------------------------------------------------------------- #


def test_us_t17_ac1_rebalance_summary_matches_appendix_d(rctx: Context) -> None:
    repos = rctx.repos
    _seed_plan(repos)

    body = Reporter(rctx).rebalance_summary(REBALANCE_ID, rctx.now_ms())

    assert body.split("\n") == [
        "TREND · rebalance 2026-09-08 · 100 % · 3 orders · 2 escalated · residual 0",
        "Largest deltas: SOL +410 (short → flat), LINK −380 (new short), BTC +120 (add long)",
        "Fees 4.90 · slippage 1.2 bps · maker 76 %",
    ]
    assert repos.reports.get(REBALANCE, REBALANCE_ID)["body"] == body


def test_a_rebalance_summary_for_an_unknown_id_renders_n_a(rctx: Context) -> None:
    body = Reporter(rctx).rebalance_summary("nope", rctx.now_ms())

    assert body == f"TREND · rebalance nope · {NA}"
    assert rctx.repos.reports.get(REBALANCE, "nope") is not None


# --------------------------------------------------------------------------- #
# Appendix D — governor alert
# --------------------------------------------------------------------------- #


def test_us_t17_ac3_governor_alert_matches_appendix_d(rctx: Context) -> None:
    body = Reporter(rctx).governor_alert(
        dd=0.123,
        peak_equity=10240.00,
        g_before=1.0,
        g_after=0.5,
        gross_before=1.30,
        gross_after=0.65,
        n_orders=14,
    )

    assert body.split("\n") == [
        "TREND · WARN · GOVERNOR 1.0 → 0.5",
        "Drawdown 12.3 % from peak 10 240.00. Immediate cut: gross 1.30× → 0.65×"
        " (14 reduce-only orders, taker allowed).",
        "Restore to 1.0 when DD < 8 %.",
    ]


def test_the_restore_rung_comes_from_the_configured_governor_ladder(rctx: Context) -> None:
    reporter = Reporter(rctx)

    assert reporter.restore_step(0.5) == (1.0, 0.08)
    assert reporter.restore_step(0.25) == (0.5, 0.15)
    assert reporter.restore_step(1.0) == (None, None)


# --------------------------------------------------------------------------- #
# Weekly / monthly (US-T17 AC 2)
# --------------------------------------------------------------------------- #


def test_weekly_report_carries_the_metric_contribution_and_tracking_tables(rctx: Context) -> None:
    rctx.repos.tracking.upsert(
        DAY,
        corr_30d=0.82,
        cum_diff_frac=0.004,
        cost_ratio=1.2,
        turnover_ratio=1.1,
        in_bounds=True,
        breach_days=0,
    )

    body = Reporter(rctx).weekly("2026-W37", rctx.now_ms())
    lines = body.split("\n")

    assert lines[0] == "TREND · weekly · 2026-W37 (2026-09-07 → 2026-09-13)"
    assert lines[2] == "P&L price −55.90 · funding +3.10 · fees −6.30 · slippage −2.10"
    assert lines[5].startswith("Contribution: BTC −33.30 (54 %) · ETH −27.90 (46 %)")
    assert lines[7] == (
        "Tracking: in bounds · corr 0.82 · cum diff 0.4 % · cost ratio 1.20 · turnover ratio 1.10"
    )
    assert rctx.repos.reports.get(WEEKLY, "2026-W37")["body"] == body


def test_monthly_report_covers_the_calendar_month(rctx: Context) -> None:
    body = Reporter(rctx).monthly("2026-09", rctx.now_ms())

    assert body.split("\n")[0] == "TREND · monthly · 2026-09 (2026-09-01 → 2026-09-30)"
    assert rctx.repos.reports.get(MONTHLY, "2026-09") is not None


def test_a_period_with_no_data_renders_n_a_rather_than_zero(cfg, report_clock, gateway, repos) -> None:
    ctx = _ctx(cfg, report_clock, gateway, repos)

    lines = Reporter(ctx).weekly("2026-W37", ctx.now_ms()).split("\n")

    assert lines[2] == f"P&L {NA}"
    assert lines[5] == f"Contribution: {NA}"
    assert lines[7] == f"Tracking: {NA}"


# --------------------------------------------------------------------------- #
# due_reports
# --------------------------------------------------------------------------- #


def test_us_t17_ac1_daily_is_due_at_00_10_and_the_summary_at_01_05(rctx: Context) -> None:
    reporter = Reporter(rctx)

    before = FakeClock(to_ms("2026-09-09T00:09:00Z")).now_ms()
    assert (DAILY, "2026-09-08") not in reporter.due_reports(before)

    at_0010 = to_ms("2026-09-09T00:10:00Z")
    assert (DAILY, "2026-09-08") in reporter.due_reports(at_0010)

    before_0105 = to_ms("2026-09-08T01:04:00Z")
    assert (REBALANCE, REBALANCE_ID) not in reporter.due_reports(before_0105)

    at_0105 = to_ms("2026-09-08T01:05:00Z")
    assert (REBALANCE, REBALANCE_ID) in reporter.due_reports(at_0105)


def test_a_sent_report_is_no_longer_due(rctx: Context) -> None:
    reporter = Reporter(rctx)
    now = to_ms("2026-09-09T00:10:00Z")
    assert (DAILY, "2026-09-08") in reporter.due_reports(now)

    reporter.daily(DAY, now)

    assert (DAILY, "2026-09-08") not in reporter.due_reports(now)


def test_a_report_missed_during_downtime_is_still_due_later(rctx: Context) -> None:
    reporter = Reporter(rctx)

    due = reporter.due_reports(to_ms("2026-09-11T00:10:00Z"))

    assert (DAILY, "2026-09-08") in due
    assert (DAILY, "2026-09-10") in due


def test_weekly_and_monthly_keys_become_due_after_their_period_closes(rctx: Context) -> None:
    reporter = Reporter(rctx)

    due = dict.fromkeys(reporter.due_reports(to_ms("2026-10-01T00:10:00Z")))

    assert (WEEKLY, "2026-W39") in due
    assert (MONTHLY, "2026-09") in due
    assert (MONTHLY, "2026-10") not in due


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _seed_plan(repos: Repositories) -> None:
    """Three symbols whose deltas exercise every transition phrase."""
    repos.rebalances.finish(
        REBALANCE_ID,
        ended_ts=day_start_ms(DAY) + 28 * 60_000,
        status="complete",
        completion_pct=100.0,
        traded_notional=3120.0,
        fees=4.90,
        avg_slippage_bps=1.2,
        maker_ratio=0.76,
        residuals=[],
    )
    rows = [
        ("SOLUSDT", 410.0, -3.0, 0.0),
        ("LINKUSDT", -380.0, 0.0, -30.0),
        ("BTCUSDT", 120.0, 1.0, 1.5),
    ]
    for symbol, delta, current, target in rows:
        repos.db.execute(
            "INSERT OR REPLACE INTO targets (strategy, rebalance_id, symbol, signal, vol, raw,"
            " sigma_p, conv, sigma_eff, s, g, target_notional, target_qty, current_qty,"
            " delta_notional) VALUES ('TREND',?,?,0,0.5,0,0.2,0.5,0.2,1,1,0,?,?,?)",
            (REBALANCE_ID, symbol, target, current, delta),
        )
    for i in range(3):
        repos.db.execute(
            "INSERT OR REPLACE INTO orders (strategy, order_id, client_order_id, symbol, side,"
            " order_type, qty, price, time_in_force, status, created_ts, updated_ts, rebalance_id)"
            " VALUES ('TREND',?,?,'BTCUSDT','BUY','LIMIT',1,1,'GTX','FILLED',0,0,?)",
            (f"o{i}", f"c{i}", REBALANCE_ID),
        )
    for i in range(3):
        repos.slices.create(f"s{i}", REBALANCE_ID, "BTCUSDT", i, Side.BUY, 1.0, False, 0)
        repos.slices.finish(f"s{i}", "escalated" if i < 2 else "filled", 1.0, 1.0, i < 2, 0)


# --------------------------------------------------------------------------- #
# Formatting primitives — Appendix D uses U+2212, not a hyphen
# --------------------------------------------------------------------------- #


def test_appendix_d_number_formatting() -> None:
    from aegis.ops.reports import MINUS, g_text, num, pct, signed, signed_times, times, unit

    assert num(9874.1) == "9 874.10"
    assert num(-9874.1) == f"{MINUS}9 874.10"
    assert num(None) == NA
    assert signed(3.1) == "+3.10"
    assert signed(-2.1) == f"{MINUS}2.10"
    assert signed(None) == NA
    assert pct(0.021) == "2.1 %"
    assert pct(None) == NA
    assert times(1.32) == "1.32×"
    assert signed_times(-0.41) == f"{MINUS}0.41×"
    assert g_text(1.0) == "1.0"
    assert g_text(0.5) == "0.5"
    assert g_text(0.25) == "0.25"
    assert g_text(None) == NA
    assert unit("1.4", "bps") == "1.4 bps"
    assert unit(NA, "bps") == NA


def test_delta_transition_phrases_cover_every_book_change() -> None:
    from aegis.ops.reports import _transition

    assert _transition(0.0, 2.0) == "new long"
    assert _transition(0.0, -2.0) == "new short"
    assert _transition(-3.0, 0.0) == "short → flat"
    assert _transition(3.0, 0.0) == "long → flat"
    assert _transition(-3.0, 2.0) == "short → long"
    assert _transition(1.0, 1.5) == "add long"
    assert _transition(-1.0, -1.5) == "add short"
    assert _transition(2.0, 1.0) == "trim long"
    assert _transition(1.0, 1.0) == "unchanged"


def test_a_rebalance_with_no_stored_targets_renders_n_a_deltas(rctx: Context) -> None:
    body = Reporter(rctx).rebalance_summary(REBALANCE_ID, rctx.now_ms())

    assert body.split("\n")[1] == f"Largest deltas: {NA}"


def test_net_and_single_cap_breaches_are_named(cfg, report_clock, gateway, repos) -> None:
    from aegis.core.types import AccountState, Position

    end_ms = day_start_ms(DAY) + DAY_MS
    equity = 1000.0
    repos.snapshots.add(
        AccountState(
            ts_ms=end_ms - 1,
            wallet_balance=equity,
            margin_balance=equity,
            unrealized_pnl=0.0,
            available_balance=100.0,
            maint_margin=10.0,
            initial_margin=100.0,
        ),
        [Position(symbol="BTCUSDT", qty=1.0, entry_price=1.0, mark_price=400.0)],
        gross=2.0 * equity,
        net=1.9 * equity,
    )
    ctx = _ctx(cfg, report_clock, gateway, repos)

    line = Reporter(ctx).daily(DAY, ctx.now_ms()).split("\n")[6]

    assert "caps breached net/single" in line


def test_the_regime_table_is_rendered_when_the_metric_exists(rctx: Context) -> None:
    from aegis.core.types import MetricValue

    rctx.repos.metrics.save_many(
        [
            MetricValue(
                Strategy.TREND,
                "regime_table",
                "30d",
                None,
                rctx.now_ms(),
                n_obs=3,
                extra={
                    "buckets": {
                        "btc_up": {"months": 2, "pnl": 120.0},
                        "btc_down": {"months": 1, "pnl": -30.0},
                    }
                },
            ),
        ]
    )

    line = Reporter(rctx).weekly("2026-W37", rctx.now_ms()).split("\n")[6]

    assert line == "Regime: btc_down 1 m −30.00 · btc_up 2 m +120.00"


def test_a_report_uses_the_last_bnb_low_alert_when_the_state_has_no_reading(
    cfg, report_clock, gateway, repos
) -> None:
    from tests.ops.conftest import seed_daily as _seed

    _seed(repos)
    repos.state.save(
        state="IDLE",
        phase="P1_PAPER",
        paused=False,
        stopped=False,
        safe_mode=False,
        halt_reason="",
        governor_g=1.0,
        blocks=[],
        context={},
        now_ms=day_start_ms(DAY),
    )
    ctx = _ctx(cfg, report_clock, gateway, repos)
    ctx.alerts.warn("BNB_LOW", "cover is thin", {"balance": 1.0, "days": 5.0})

    line = Reporter(ctx).daily(DAY, ctx.now_ms()).split("\n")[7]

    assert line.endswith(f"backup lag {NA} · BNB fees 5 d")
