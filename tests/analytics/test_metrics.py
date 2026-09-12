"""US-T15 AC 3 — the shared metric set, proved against independent formulas.

Every value here is recomputed from ``statistics``/``numpy``/``scipy`` inside the
test rather than copied out of the implementation, so a test failure means the
implementation disagrees with the textbook, not with itself.
"""

from __future__ import annotations

import math
import statistics

import numpy as np
import pytest
from scipy import stats as sp

from aegis.analytics.metrics import (
    YEAR_DAYS,
    MetricInputError,
    beta,
    cagr,
    calmar,
    cash_alternative,
    correlation,
    cvar,
    downside_deviation,
    finite,
    hit_rate,
    information_ratio,
    kurtosis,
    max_drawdown,
    net_of_infra,
    paired,
    quantile,
    sharpe,
    skew,
    sortino,
    var,
    volatility,
)

RETURNS = [
    0.012,
    -0.004,
    0.021,
    0.0,
    -0.015,
    0.008,
    0.003,
    -0.009,
    0.017,
    -0.002,
    0.006,
    0.011,
    -0.021,
    0.004,
    0.009,
    -0.006,
    0.013,
    0.002,
    -0.011,
    0.007,
]
BENCH = [
    0.010,
    -0.002,
    0.018,
    0.001,
    -0.012,
    0.005,
    0.004,
    -0.007,
    0.014,
    -0.001,
    0.003,
    0.009,
    -0.018,
    0.002,
    0.007,
    -0.004,
    0.010,
    0.001,
    -0.009,
    0.005,
]

NAN = float("nan")

#: Every single-series function, for the shared "too few observations" sweep.
SINGLE_SERIES = [
    lambda r: sharpe(r)[0],
    sortino,
    max_drawdown,
    lambda r: calmar(r, 0.2),
    var,
    cvar,
    skew,
    kurtosis,
    volatility,
    downside_deviation,
    lambda r: cagr(r, 30.0),
]

#: Every two-series function, for the shared mismatched-length sweep.
TWO_SERIES = [information_ratio, beta, correlation]


# --------------------------------------------------------------------------- #
# Sharpe and its standard error
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_sharpe_matches_the_textbook_definition() -> None:
    expected = (
        (statistics.fmean(RETURNS) - 0.04 / YEAR_DAYS) / statistics.stdev(RETURNS) * math.sqrt(YEAR_DAYS)
    )
    value, _ = sharpe(RETURNS, 0.04)
    assert value == pytest.approx(expected, rel=1e-12)


def test_us_t15_ac3_sharpe_standard_error_is_the_lo_approximation() -> None:
    value, se = sharpe(RETURNS, 0.04)
    assert value is not None and se is not None
    assert se == pytest.approx(math.sqrt((1 + 0.5 * value**2) / len(RETURNS)), rel=1e-12)


def test_us_t15_ac3_sharpe_annualises_on_365_not_252() -> None:
    daily, _ = sharpe(RETURNS, 0.0, periods=1)
    annual, _ = sharpe(RETURNS, 0.0, periods=YEAR_DAYS)
    assert annual == pytest.approx(daily * math.sqrt(365), rel=1e-12)


def test_us_t15_ac3_sharpe_is_none_when_variance_is_zero() -> None:
    assert sharpe([0.01] * 10) == (None, None)


# --------------------------------------------------------------------------- #
# Sortino / downside deviation
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_sortino_uses_only_downside_dispersion() -> None:
    dd = math.sqrt(sum(min(0.0, r) ** 2 for r in RETURNS) / (len(RETURNS) - 1)) * math.sqrt(YEAR_DAYS)
    expected = statistics.fmean(RETURNS) * YEAR_DAYS / dd
    assert sortino(RETURNS) == pytest.approx(expected, rel=1e-12)


def test_us_t15_ac3_sortino_exceeds_sharpe_when_losses_are_small() -> None:
    value, _ = sharpe(RETURNS)
    assert sortino(RETURNS) > value


def test_us_t15_ac3_sortino_is_none_when_nothing_ever_fell_below_target() -> None:
    assert downside_deviation([0.01] * 5) == 0.0
    assert sortino([0.01] * 5) is None


# --------------------------------------------------------------------------- #
# Drawdown, Calmar, CAGR
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_max_drawdown_is_peak_to_trough_of_the_index() -> None:
    assert max_drawdown([100.0, 120.0, 60.0, 90.0]) == pytest.approx(0.5)


def test_us_t15_ac3_max_drawdown_ignores_recovery_after_the_trough() -> None:
    assert max_drawdown([100.0, 50.0, 400.0]) == pytest.approx(0.5)


def test_us_t15_ac3_calmar_is_annualised_growth_over_drawdown() -> None:
    growth = math.prod(1 + r for r in RETURNS)
    expected = (growth ** (YEAR_DAYS / len(RETURNS)) - 1) / 0.25
    assert calmar(RETURNS, 0.25) == pytest.approx(expected, rel=1e-12)


def test_us_t15_ac3_calmar_is_none_without_a_drawdown() -> None:
    assert calmar(RETURNS, 0.0) is None
    assert calmar(RETURNS, None) is None


def test_us_t15_ac3_cagr_doubles_over_a_year() -> None:
    assert cagr([100.0, 200.0], 365) == pytest.approx(1.0, rel=1e-12)
    assert cagr([100.0, 200.0], 730) == pytest.approx(math.sqrt(2) - 1, rel=1e-12)


def test_us_t15_ac3_cagr_is_none_for_impossible_inputs() -> None:
    assert cagr([100.0, 200.0], 0) is None
    assert cagr([0.0, 200.0], 365) is None


# --------------------------------------------------------------------------- #
# Tail and shape
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_var_is_the_sign_flipped_historical_quantile() -> None:
    assert var(RETURNS, 0.95) == pytest.approx(-float(np.quantile(RETURNS, 0.05)), rel=1e-12)


def test_us_t15_ac3_var_reports_a_loss_as_positive() -> None:
    assert var(RETURNS) > 0


def test_us_t15_ac3_cvar_is_at_least_as_bad_as_var() -> None:
    assert cvar(RETURNS) >= var(RETURNS)


def test_us_t15_ac3_var_level_outside_the_unit_interval_raises() -> None:
    with pytest.raises(ValueError):
        var(RETURNS, 1.0)
    with pytest.raises(ValueError):
        cvar(RETURNS, 0.0)


def test_us_t15_ac3_skew_matches_scipy_unbiased() -> None:
    assert skew(RETURNS) == pytest.approx(float(sp.skew(RETURNS, bias=False)), rel=1e-10)


def test_us_t15_ac3_kurtosis_is_excess_fisher_and_matches_scipy() -> None:
    assert kurtosis(RETURNS) == pytest.approx(float(sp.kurtosis(RETURNS, fisher=True, bias=False)), rel=1e-10)


def test_us_t15_ac3_skew_and_kurtosis_need_three_and_four_points() -> None:
    assert skew([0.01, 0.02]) is None
    assert skew([0.01, 0.02, 0.03]) is not None
    assert kurtosis([0.01, 0.02, 0.03]) is None
    assert kurtosis([0.01, 0.02, 0.03, 0.05]) is not None


# --------------------------------------------------------------------------- #
# Dispersion and hit rate
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_volatility_is_the_annualised_sample_stdev() -> None:
    assert volatility(RETURNS) == pytest.approx(statistics.stdev(RETURNS) * math.sqrt(365), rel=1e-12)


def test_us_t15_ac3_hit_rate_counts_flat_days_as_misses() -> None:
    assert hit_rate([0.01, -0.01, 0.0, 0.02]) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Benchmark-relative
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_beta_recovers_a_known_slope_exactly() -> None:
    bench = [0.01, -0.02, 0.03, 0.005, -0.015]
    strat = [2.5 * b + 0.001 for b in bench]
    assert beta(strat, bench) == pytest.approx(2.5, rel=1e-12)
    assert correlation(strat, bench) == pytest.approx(1.0, rel=1e-12)


def test_us_t15_ac3_beta_matches_numpy_least_squares() -> None:
    expected = float(np.polyfit(BENCH, RETURNS, 1)[0])
    assert beta(RETURNS, BENCH) == pytest.approx(expected, rel=1e-9)


def test_us_t15_ac3_correlation_matches_numpy() -> None:
    assert correlation(RETURNS, BENCH) == pytest.approx(float(np.corrcoef(RETURNS, BENCH)[0, 1]), rel=1e-10)


def test_us_t15_ac3_information_ratio_is_active_return_over_tracking_error() -> None:
    active = [a - b for a, b in zip(RETURNS, BENCH, strict=True)]
    expected = statistics.fmean(active) / statistics.stdev(active) * math.sqrt(YEAR_DAYS)
    assert information_ratio(RETURNS, BENCH) == pytest.approx(expected, rel=1e-12)


def test_us_t15_ac3_information_ratio_is_none_when_tracking_is_perfect() -> None:
    assert information_ratio(RETURNS, RETURNS) is None


def test_us_t15_ac3_beta_is_none_when_the_benchmark_never_moves() -> None:
    assert beta([0.01, 0.02, 0.03], [0.0, 0.0, 0.0]) is None
    assert correlation([0.01, 0.02, 0.03], [0.0, 0.0, 0.0]) is None


# --------------------------------------------------------------------------- #
# Opportunity cost
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_cash_alternative_compounds_on_the_crypto_year() -> None:
    assert cash_alternative(10_000.0, 0.04, 365) == pytest.approx(400.0, rel=1e-12)
    assert cash_alternative(10_000.0, 0.04, 730) == pytest.approx(10_000 * (1.04**2 - 1), rel=1e-12)


def test_us_t15_ac3_cash_alternative_is_none_without_capital_or_time() -> None:
    assert cash_alternative(10_000.0, 0.04, 0) is None
    assert cash_alternative(0.0, 0.04, 30) is None


def test_us_t15_ac3_net_of_infra_prorates_the_monthly_bill() -> None:
    assert net_of_infra(1000.0, 10.0, 365) == pytest.approx(1000.0 - 120.0, rel=1e-12)
    assert net_of_infra(1000.0, 10.0, 30) == pytest.approx(1000.0 - 10.0 * 30 * 12 / 365, rel=1e-12)


def test_us_t15_ac3_net_of_infra_converts_the_currency() -> None:
    assert net_of_infra(0.0, 10.0, 365, eur_usdt=1.1) == pytest.approx(-132.0, rel=1e-12)


def test_us_t15_ac3_net_of_infra_is_none_over_an_empty_window() -> None:
    assert net_of_infra(1000.0, 10.0, 0) is None


# --------------------------------------------------------------------------- #
# Edge cases every metric must survive (empty, single, flat, NaN, mismatched)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fn", SINGLE_SERIES)
def test_us_t15_ac3_every_metric_returns_none_on_empty_input(fn) -> None:
    assert fn([]) is None


@pytest.mark.parametrize("fn", SINGLE_SERIES)
def test_us_t15_ac3_every_metric_returns_none_on_one_observation(fn) -> None:
    assert fn([0.01]) is None


@pytest.mark.parametrize("fn", SINGLE_SERIES)
def test_us_t15_ac3_no_metric_ever_returns_nan(fn) -> None:
    value = fn([0.0] * 8)
    assert value is None or math.isfinite(value)


@pytest.mark.parametrize("fn", SINGLE_SERIES)
def test_us_t15_ac3_nan_observations_are_skipped(fn) -> None:
    with_nan = [*RETURNS[:5], NAN, *RETURNS[5:]]
    assert fn(with_nan) == fn(RETURNS)


@pytest.mark.parametrize("fn", TWO_SERIES)
def test_us_t15_ac3_mismatched_lengths_raise_value_error(fn) -> None:
    with pytest.raises(ValueError):
        fn(RETURNS, BENCH[:-1])


@pytest.mark.parametrize("fn", TWO_SERIES)
def test_us_t15_ac3_nan_is_skipped_pairwise(fn) -> None:
    a = [*RETURNS[:5], NAN, *RETURNS[5:]]
    b = [*BENCH[:5], 0.5, *BENCH[5:]]
    assert fn(a, b) == pytest.approx(fn(RETURNS, BENCH), rel=1e-12)


@pytest.mark.parametrize("fn", TWO_SERIES)
def test_us_t15_ac3_two_series_metrics_are_none_on_empty_input(fn) -> None:
    assert fn([], []) is None


def test_us_t15_ac3_the_metric_error_is_both_an_aegis_error_and_a_value_error() -> None:
    from aegis.core.errors import AegisError

    assert issubclass(MetricInputError, AegisError)
    assert issubclass(MetricInputError, ValueError)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def test_finite_drops_none_nan_and_infinity() -> None:
    assert finite([1.0, None, NAN, math.inf, -math.inf, 2.0]) == [1.0, 2.0]


def test_paired_keeps_only_rows_finite_on_both_sides() -> None:
    a, b = paired([1.0, NAN, 3.0], [4.0, 5.0, NAN])
    assert (a, b) == ([1.0], [4.0])


def test_quantile_matches_numpy_linear_interpolation() -> None:
    for p in (0.0, 0.05, 0.5, 0.9, 1.0):
        assert quantile(RETURNS, p) == pytest.approx(float(np.quantile(RETURNS, p)), rel=1e-12)


def test_quantile_outside_the_unit_interval_raises() -> None:
    with pytest.raises(ValueError):
        quantile(RETURNS, 1.5)


def test_hit_rate_is_none_without_observations() -> None:
    assert hit_rate([]) is None


def test_calmar_is_none_when_the_book_was_wiped_out() -> None:
    assert calmar([-1.0, 0.5, 0.2], 0.5) is None


def test_paired_drops_explicit_none_entries() -> None:
    a, b = paired([1.0, None, 3.0], [4.0, 5.0, 6.0])
    assert (a, b) == ([1.0, 3.0], [4.0, 6.0])


def test_quantile_of_an_empty_series_is_none() -> None:
    assert quantile([], 0.5) is None


def test_quantile_of_a_single_observation_is_that_observation() -> None:
    assert quantile([0.02], 0.9) == pytest.approx(0.02)
