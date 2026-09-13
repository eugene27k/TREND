"""US-T15 AC 1-2 — the PRD Section 10 TREND additions, incl. the Appendix C.4 vectors."""

from __future__ import annotations

import math

import numpy as np
import pytest

from aegis.analytics.metrics import MetricInputError
from aegis.analytics.trend_metrics import (
    beta_to_btc,
    cost_per_unit_bps,
    execution_alpha,
    exposure_stats,
    funding_pnl_share,
    governor_time_in_state,
    maker_ratio,
    per_symbol_contribution,
    realised_vol_vs_target,
    rebalance_completion,
    regime_table,
    side_attribution,
    signal_statistics,
    trade_statistics,
    turnover,
    vol_target_adherence,
)
from tests.fixtures.appendix_c import (
    C4_AVG_EQUITY,
    C4_BETA,
    C4_BETA_BTC_SD,
    C4_BETA_DAYS,
    C4_BETA_EPS_SD,
    C4_BETA_SEED,
    C4_BETA_TOL,
    C4_CORR,
    C4_CORR_TOL,
    C4_DAYS,
    C4_TRADED_NOTIONAL,
    C4_TURNOVER_ANNUALISED,
    C4_TURNOVER_PERIOD,
)

DAY_MS = 86_400_000
NAN = float("nan")


def _c4_beta_series() -> tuple[list[float], list[float]]:
    """Appendix C.4 exactly: BTC drawn first, then the residual."""
    rng = np.random.default_rng(C4_BETA_SEED)
    btc = rng.normal(0.0, C4_BETA_BTC_SD, C4_BETA_DAYS)
    eps = rng.normal(0.0, C4_BETA_EPS_SD, C4_BETA_DAYS)
    strategy = 0.3 * btc + eps
    return list(strategy), list(btc)


# --------------------------------------------------------------------------- #
# AC 1 — Appendix C.4
# --------------------------------------------------------------------------- #


def test_us_t15_ac1_appendix_c4_turnover_vector() -> None:
    period, annualised = turnover(C4_TRADED_NOTIONAL, C4_AVG_EQUITY, C4_DAYS)
    assert period == pytest.approx(C4_TURNOVER_PERIOD, abs=1e-12)
    assert annualised == pytest.approx(C4_TURNOVER_ANNUALISED, abs=1e-9)


def test_us_t15_ac1_appendix_c4_beta_and_correlation_vector() -> None:
    strategy, btc = _c4_beta_series()
    value, corr = beta_to_btc(strategy, btc, window=C4_BETA_DAYS)
    assert value == pytest.approx(C4_BETA, abs=C4_BETA_TOL)
    assert corr == pytest.approx(C4_CORR, abs=C4_CORR_TOL)


def test_us_t15_ac1_appendix_c4_beta_matches_ordinary_least_squares() -> None:
    strategy, btc = _c4_beta_series()
    value, corr = beta_to_btc(strategy, btc, window=None)
    assert value == pytest.approx(float(np.polyfit(btc, strategy, 1)[0]), rel=1e-9)
    assert corr == pytest.approx(float(np.corrcoef(btc, strategy)[0, 1]), rel=1e-9)


def test_us_t15_ac1_beta_window_trims_to_the_most_recent_observations() -> None:
    strategy, btc = _c4_beta_series()
    tail = beta_to_btc(strategy, btc, window=30)
    explicit = beta_to_btc(strategy[-30:], btc[-30:], window=None)
    assert tail == explicit


def test_us_t15_ac1_beta_is_none_without_enough_paired_days() -> None:
    assert beta_to_btc([0.01], [0.02]) == (None, None)
    assert beta_to_btc([], []) == (None, None)


def test_us_t15_ac1_beta_requires_equal_length_series() -> None:
    with pytest.raises(ValueError):
        beta_to_btc([0.01, 0.02], [0.01])


# --------------------------------------------------------------------------- #
# Turnover and vol targeting
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_turnover_is_none_without_equity_or_time() -> None:
    assert turnover(12_000.0, 0.0, 30) == (None, None)
    assert turnover(12_000.0, 10_000.0, 0) == (None, None)


def test_us_t15_ac2_realised_vol_uses_active_days_only() -> None:
    active = [0.01, -0.02, 0.015, -0.005, 0.02, -0.01]
    padded = [*active, 0.0, 0.0, 0.0, 0.0]
    assert realised_vol_vs_target(padded, 0.20) == realised_vol_vs_target(active, 0.20)


def test_us_t15_ac2_realised_vol_ratio_is_realised_over_target() -> None:
    realised, ratio = realised_vol_vs_target([0.01, -0.02, 0.015, -0.005], 0.20)
    assert ratio == pytest.approx(realised / 0.20, rel=1e-12)


def test_us_t15_ac2_realised_vol_is_none_when_the_book_never_traded() -> None:
    assert realised_vol_vs_target([0.0] * 30, 0.20) == (None, None)
    assert realised_vol_vs_target([0.01, -0.01], 0.0) == (None, None)


def test_us_t15_ac2_vol_target_adherence_counts_days_inside_half_to_one_and_a_half() -> None:
    series = [0.09, 0.10, 0.20, 0.30, 0.31]  # 0.10, 0.20 and 0.30 are inside 0.5x-1.5x of 0.20
    assert vol_target_adherence(series, 0.20) == pytest.approx(3 / 5)


def test_us_t15_ac2_vol_target_adherence_is_none_without_a_target_or_data() -> None:
    assert vol_target_adherence([], 0.20) is None
    assert vol_target_adherence([0.2], 0.0) is None


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_exposure_stats_reports_average_max_and_current() -> None:
    stats = exposure_stats([1000.0, 2000.0, 1500.0], [500.0, -1800.0, 300.0], [1000.0, 1000.0, 1000.0])
    assert stats["avg_gross"] == pytest.approx(1.5)
    assert stats["max_gross"] == pytest.approx(2.0)
    assert stats["current_gross"] == pytest.approx(1.5)
    assert stats["avg_net"] == pytest.approx((0.5 - 1.8 + 0.3) / 3)
    assert stats["max_net"] == pytest.approx(-1.8)  # most extreme, sign kept
    assert stats["current_net"] == pytest.approx(0.3)


def test_us_t15_ac2_exposure_stats_skips_rows_without_equity() -> None:
    stats = exposure_stats([1000.0, 2000.0], [0.0, 0.0], [1000.0, 0.0])
    assert stats["n_obs"] == 1
    assert stats["avg_gross"] == pytest.approx(1.0)


def test_us_t15_ac2_exposure_stats_is_all_none_when_empty() -> None:
    stats = exposure_stats([], [], [])
    assert stats["n_obs"] == 0
    assert stats["avg_gross"] is None and stats["max_net"] is None


def test_us_t15_ac2_exposure_stats_requires_equal_length_series() -> None:
    with pytest.raises(MetricInputError):
        exposure_stats([1.0, 2.0], [1.0], [1.0, 2.0])


# --------------------------------------------------------------------------- #
# Execution quality
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_cost_per_unit_is_in_basis_points_of_traded_notional() -> None:
    assert cost_per_unit_bps(4.0, 2.0, 100_000.0) == pytest.approx(0.6)


def test_us_t15_ac2_cost_per_unit_treats_signed_inputs_as_costs() -> None:
    assert cost_per_unit_bps(-4.0, -2.0, 100_000.0) == cost_per_unit_bps(4.0, 2.0, 100_000.0)


def test_us_t15_ac2_cost_per_unit_is_none_without_trading() -> None:
    assert cost_per_unit_bps(0.0, 0.0, 0.0) is None


def test_us_t15_ac2_execution_alpha_is_positive_when_we_beat_the_model() -> None:
    assert execution_alpha(11.0, 6.0, 100_000.0) == pytest.approx(50.0)
    assert execution_alpha(6.0, 11.0, 100_000.0) == pytest.approx(-50.0)


def test_us_t15_ac2_execution_alpha_is_none_without_trading() -> None:
    assert execution_alpha(11.0, 6.0, 0.0) is None


def test_us_t15_ac2_maker_ratio_is_maker_over_total_notional() -> None:
    assert maker_ratio(7_000.0, 10_000.0) == pytest.approx(0.7)
    assert maker_ratio(0.0, 0.0) is None


def test_us_t15_ac2_rebalance_completion_is_the_mean_of_the_window() -> None:
    assert rebalance_completion([100.0, 80.0, 90.0]) == pytest.approx(90.0)
    assert rebalance_completion([100.0, NAN]) == pytest.approx(100.0)
    assert rebalance_completion([]) is None


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_per_symbol_contribution_shares_sum_to_one() -> None:
    shares, concentration = per_symbol_contribution({"AAA": 60.0, "BBB": 30.0, "CCC": 10.0})
    assert sum(shares.values()) == pytest.approx(1.0)
    assert concentration == pytest.approx(0.6)


def test_us_t15_ac2_concentration_is_the_p0_gate_input() -> None:
    _, concentration = per_symbol_contribution({"AAA": 51.0, "BBB": 49.0})
    assert concentration > 0.5


def test_us_t15_ac2_per_symbol_contribution_is_empty_when_pnl_nets_to_zero() -> None:
    assert per_symbol_contribution({"AAA": 10.0, "BBB": -10.0}) == ({}, None)
    assert per_symbol_contribution({}) == ({}, None)


def test_us_t15_ac2_side_attribution_splits_pnl_and_hit_rate_by_side() -> None:
    rows = [
        {"side": "long", "net_pnl": 10.0},
        {"side": "long", "net_pnl": -4.0},
        {"side": "short", "net_pnl": 6.0},
        {"side": "short", "net_pnl": 3.0},
        {"side": "flat", "net_pnl": 100.0},
    ]
    out = side_attribution(rows)
    assert out["long_pnl"] == pytest.approx(6.0)
    assert out["short_pnl"] == pytest.approx(9.0)
    assert out["hit_rate_long"] == pytest.approx(0.5)
    assert out["hit_rate_short"] == pytest.approx(1.0)


def test_us_t15_ac2_side_attribution_hit_rate_is_none_for_a_side_never_held() -> None:
    out = side_attribution([{"side": "long", "net_pnl": 1.0}])
    assert out["hit_rate_short"] is None
    assert out["short_pnl"] == 0.0


def test_us_t15_ac2_trade_statistics_cover_count_holding_win_rate_and_mae() -> None:
    trades = [
        {"days": 10.0, "pnl": 100.0, "mae": -20.0},
        {"days": 4.0, "pnl": -50.0, "mae": -60.0},
        {"days": 7.0, "pnl": 30.0, "mae": -5.0},
    ]
    out = trade_statistics(trades)
    assert out["count"] == 3
    assert out["avg_holding_days"] == pytest.approx(7.0)
    assert out["win_rate"] == pytest.approx(2 / 3)
    assert out["avg_win"] == pytest.approx(65.0)
    assert out["avg_loss"] == pytest.approx(-50.0)
    assert out["mae_max"] == pytest.approx(60.0)
    assert out["mae_median"] == pytest.approx(20.0)


def test_us_t15_ac2_trade_statistics_are_none_without_trades() -> None:
    out = trade_statistics([])
    assert out["count"] == 0
    assert out["win_rate"] is None and out["avg_holding_days"] is None


def test_us_t15_ac2_signal_statistics_measure_level_turnover_and_strength() -> None:
    signals = {
        "2026-09-01": {"AAA": 0.6, "BBB": -0.2},
        "2026-09-02": {"AAA": 0.4, "BBB": -0.4},
    }
    out = signal_statistics(signals)
    assert out["mean_abs_signal"] == pytest.approx((0.6 + 0.2 + 0.4 + 0.4) / 4)
    assert out["signal_turnover"] == pytest.approx(0.2)
    assert out["strong_frac"] == pytest.approx(0.25)


def test_us_t15_ac2_signal_turnover_ignores_symbols_that_joined_or_left() -> None:
    signals = {
        "2026-09-01": {"AAA": 0.5},
        "2026-09-02": {"AAA": 0.5, "NEW": 0.9},
    }
    assert signal_statistics(signals)["signal_turnover"] == pytest.approx(0.0)


def test_us_t15_ac2_signal_statistics_turnover_is_none_on_a_single_day() -> None:
    out = signal_statistics({"2026-09-01": {"AAA": 0.5}})
    assert out["signal_turnover"] is None
    assert out["mean_abs_signal"] == pytest.approx(0.5)


def test_us_t15_ac2_signal_statistics_are_none_without_signals() -> None:
    assert signal_statistics({})["mean_abs_signal"] is None


# --------------------------------------------------------------------------- #
# Governor time-in-state
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_governor_time_in_state_is_time_weighted_not_row_counted() -> None:
    start = 0
    end = 10 * DAY_MS
    rows = [{"ts": 9 * DAY_MS, "g_after": 0.25}]  # one row, but only the last day at 0.25
    out = governor_time_in_state(rows, start, end)
    assert out["g1"] == pytest.approx(0.9)
    assert out["g025"] == pytest.approx(0.1)
    assert out["g05"] == pytest.approx(0.0)


def test_us_t15_ac2_governor_state_before_the_window_is_carried_in() -> None:
    out = governor_time_in_state([{"ts": -5 * DAY_MS, "g_after": 0.5}], 0, 10 * DAY_MS)
    assert out["g05"] == pytest.approx(1.0)
    assert out["g1"] == pytest.approx(0.0)


def test_us_t15_ac2_governor_defaults_to_full_risk_without_any_history() -> None:
    out = governor_time_in_state([], 0, DAY_MS)
    assert out["g1"] == pytest.approx(1.0)


def test_us_t15_ac2_governor_fractions_sum_to_one() -> None:
    rows = [
        {"ts": 2 * DAY_MS, "g_after": 0.5},
        {"ts": 5 * DAY_MS, "g_after": 0.25},
        {"ts": 8 * DAY_MS, "g_after": 1.0},
        {"ts": 20 * DAY_MS, "g_after": 0.5},
    ]
    out = governor_time_in_state(rows, 0, 10 * DAY_MS)
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["g05"] == pytest.approx(0.3)
    assert out["g025"] == pytest.approx(0.3)
    assert out["g1"] == pytest.approx(0.4)


def test_us_t15_ac2_governor_time_is_none_for_an_empty_window() -> None:
    out = governor_time_in_state([], 10, 10)
    assert all(v is None for v in out.values())


def test_us_t15_ac2_governor_records_an_unrecognised_g_as_other() -> None:
    out = governor_time_in_state([{"ts": -1, "g_after": 0.75}], 0, DAY_MS)
    assert out["other"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Regime table and funding share
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_regime_table_buckets_months_by_btc_return() -> None:
    pnl = {"2026-01": 100.0, "2026-02": -50.0, "2026-03": 20.0, "2026-04": 40.0}
    btc = {"2026-01": 0.25, "2026-02": -0.15, "2026-03": 0.05, "2026-04": -0.30}
    exposure = {"2026-01": 2.0, "2026-02": 1.0, "2026-03": 1.5, "2026-04": 1.2}
    table = regime_table(pnl, btc, exposure)
    assert table["up"]["months"] == 1 and table["up"]["pnl"] == pytest.approx(100.0)
    assert table["flat"]["months"] == 1 and table["flat"]["avg_exposure"] == pytest.approx(1.5)
    assert table["down"]["months"] == 2
    assert table["down"]["pnl"] == pytest.approx(-10.0)
    assert table["down"]["hit_rate"] == pytest.approx(0.5)
    assert table["down"]["avg_exposure"] == pytest.approx(1.1)


def test_us_t15_ac2_regime_boundaries_are_strict_at_plus_and_minus_ten_percent() -> None:
    table = regime_table({"m1": 1.0, "m2": 1.0}, {"m1": -0.10, "m2": 0.10}, {})
    assert table["flat"]["months"] == 2
    assert table["down"]["months"] == 0 and table["up"]["months"] == 0


def test_us_t15_ac2_regime_table_always_returns_all_three_buckets() -> None:
    table = regime_table({}, {}, {})
    assert set(table) == {"down", "flat", "up"}
    assert all(b["months"] == 0 and b["hit_rate"] is None for b in table.values())


def test_us_t15_ac2_funding_share_is_funding_over_gross_pnl() -> None:
    assert funding_pnl_share(25.0, 100.0) == pytest.approx(0.25)
    assert funding_pnl_share(25.0, 0.0) is None
    assert funding_pnl_share(NAN, 100.0) is None


def test_us_t15_ac2_no_trend_metric_returns_nan() -> None:
    values = [
        realised_vol_vs_target([NAN, NAN], 0.2)[0],
        turnover(0.0, 1.0, 1)[0],
        cost_per_unit_bps(0.0, 0.0, 1.0),
        maker_ratio(0.0, 1.0),
        funding_pnl_share(0.0, 1.0),
    ]
    assert all(v is None or math.isfinite(v) for v in values)


def test_us_t15_ac2_exposure_stats_skips_missing_readings() -> None:
    stats = exposure_stats([1000.0, None, NAN], [0.0, 0.0, 0.0], [1000.0, 1000.0, 1000.0])
    assert stats["n_obs"] == 1


def test_us_t15_ac2_side_attribution_skips_unusable_rows() -> None:
    out = side_attribution([{"side": "long", "net_pnl": NAN}, {"side": "long", "net_pnl": 5.0}])
    assert out["n_long"] == 1 and out["long_pnl"] == pytest.approx(5.0)


def test_us_t15_ac2_trade_statistics_skip_unusable_rows() -> None:
    out = trade_statistics([{"days": NAN, "pnl": NAN, "mae": NAN}, {"days": 3.0, "pnl": 10.0, "mae": -2.0}])
    assert out["count"] == 1 and out["avg_holding_days"] == pytest.approx(3.0)


def test_us_t15_ac2_signal_turnover_is_none_when_no_symbol_survives_a_day() -> None:
    signals = {"2026-09-01": {"AAA": 0.5}, "2026-09-02": {"BBB": 0.5}}
    assert signal_statistics(signals)["signal_turnover"] is None


def test_us_t15_ac2_regime_table_skips_months_with_unusable_numbers() -> None:
    table = regime_table({"m1": NAN, "m2": 5.0}, {"m1": 0.2, "m2": NAN}, {})
    assert all(b["months"] == 0 for b in table.values())
