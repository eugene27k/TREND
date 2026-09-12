"""PRD 11.3 — the block bootstrap that sets the kill rule's 3-month threshold."""

from __future__ import annotations

import numpy as np
import pytest

from aegis.backtest_trend.bootstrap import block_bootstrap, percentile_of


def test_a_constant_series_gives_the_exact_compound_value_with_no_spread():
    d = block_bootstrap([0.001] * 900, resamples=500, horizon_days=91)
    expected = 10_000 * ((1.001 ** 91) - 1)
    assert d[50.0] == pytest.approx(expected, rel=1e-9)
    assert max(d.values()) - min(d.values()) == pytest.approx(0.0, abs=1e-6)


def test_the_distribution_matches_the_realised_windows_it_resamples():
    rng = np.random.default_rng(1)
    series = rng.normal(0.0005, 0.02, 900)
    realised = [10_000 * (np.prod(1 + series[i:i + 91]) - 1) for i in range(900 - 91)]
    d = block_bootstrap(series.tolist(), resamples=20_000, horizon_days=91)
    assert d[50.0] == pytest.approx(float(np.median(realised)), rel=0.25)
    assert d[5.0] == pytest.approx(float(np.percentile(realised, 5)), rel=0.25)


def test_it_is_deterministic_for_a_fixed_seed():
    rng = np.random.default_rng(3)
    series = rng.normal(0.0, 0.02, 400).tolist()
    assert block_bootstrap(series, resamples=1_000) == block_bootstrap(series, resamples=1_000)
    other = block_bootstrap(series, resamples=1_000, seed=99)
    assert other != block_bootstrap(series, resamples=1_000)


def test_shorter_blocks_destroy_the_autocorrelation_a_trend_series_has():
    """A trending series has fat tails only if the blocks keep its runs intact."""
    trend = [0.01 if (i // 60) % 2 == 0 else -0.01 for i in range(600)]
    long_blocks = block_bootstrap(trend, resamples=5_000, block_days=91, horizon_days=91)
    short_blocks = block_bootstrap(trend, resamples=5_000, block_days=1, horizon_days=91)
    long_spread = long_blocks[95.0] - long_blocks[5.0]
    short_spread = short_blocks[95.0] - short_blocks[5.0]
    # Observed ~1.95x on this series. The claim is that keeping runs intact makes
    # the tails materially fatter, which is why the kill rule uses 91-day blocks;
    # an i.i.d. resample would hand the operator a reassuringly narrow p05.
    assert long_spread > short_spread * 1.5


def test_percentiles_are_monotone():
    rng = np.random.default_rng(5)
    d = block_bootstrap(rng.normal(0.0, 0.02, 500).tolist(), resamples=4_000)
    keys = sorted(d)
    values = [d[k] for k in keys]
    assert values == sorted(values)


def test_empty_or_degenerate_input_returns_nothing_rather_than_a_fake_number():
    assert block_bootstrap([]) == {}
    assert block_bootstrap([0.01], resamples=0) == {}
    assert block_bootstrap([float("nan"), float("inf")]) == {}


def test_percentile_of_locates_an_observed_quarter_in_the_distribution():
    d = {5.0: -500.0, 50.0: 0.0, 95.0: 500.0}
    assert percentile_of(-1000.0, d) == 5.0        # clamped at the bottom
    assert percentile_of(1000.0, d) == 95.0        # clamped at the top
    assert percentile_of(0.0, d) == pytest.approx(50.0)
    assert percentile_of(-250.0, d) == pytest.approx(27.5, abs=0.1)
    assert percentile_of(0.0, {}) is None
