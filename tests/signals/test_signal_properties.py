"""US-T04 AC 3 — the properties Section 5.3 promises, including blow-off protection."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from aegis.core.config import SignalConfig
from aegis.signals.engine import compute_signal, compute_signal_series, response

C1_LIKE = SignalConfig(pairs=((2, 6), (4, 12), (8, 24)), price_std_window=10, y_std_window=20)
PROD_LIKE = SignalConfig(pairs=((8, 24), (16, 48), (32, 96)), price_std_window=63, y_std_window=120)


def drift_series(mu: float, n: int = 400, sd: float = 0.02, seed: int = 5) -> list[float]:
    """A trending path: constant drift plus seeded noise (deterministic, offline)."""
    rng = np.random.default_rng(seed)
    return list(100.0 * np.exp(np.cumsum(mu + rng.normal(0.0, sd, n))))


def test_us_t04_ac3_monotone_drift_drives_signal_towards_plus_one() -> None:
    result = compute_signal(drift_series(+0.003), PROD_LIKE, "BTCUSDT")
    assert result.warm
    assert result.signal > 0.9


def test_us_t04_ac3_mirrored_drift_drives_signal_towards_minus_one() -> None:
    rng = np.random.default_rng(5)
    steps = 0.003 + rng.normal(0.0, 0.02, 400)
    up = compute_signal(list(100.0 * np.exp(np.cumsum(steps))), PROD_LIKE)
    down = compute_signal(list(100.0 * np.exp(np.cumsum(-steps))), PROD_LIKE)
    assert down.signal < -0.9
    assert down.signal == pytest.approx(-up.signal, abs=1e-3)  # the score is sign-antisymmetric


def test_us_t04_ac3_flat_series_signal_below_five_percent() -> None:
    for n in (120, 400):
        result = compute_signal([250.0] * n, C1_LIKE)
        assert abs(result.signal) < 0.05
        assert result.signal == 0.0
        assert not result.warm


def test_us_t04_ac3_spike_after_a_long_trend_decreases_u_of_the_fastest_pair() -> None:
    """A 20 % blow-off day pushes z past sqrt(2), so the response shrinks the position."""
    prices = drift_series(+0.004, 400, sd=0.01, seed=0)
    before = compute_signal(prices, PROD_LIKE)
    after = compute_signal([*prices[:-1], prices[-2] * 1.20], PROD_LIKE)
    assert before.warm and after.warm
    assert after.z[0] > before.z[0] > math.sqrt(2.0)  # further out on the decaying arm
    assert abs(after.u[0]) < abs(before.u[0])


def test_us_t04_ac3_response_peaks_at_sqrt_two_and_decays() -> None:
    peak = math.sqrt(2.0)
    assert response(peak) == pytest.approx(max(response(z) for z in np.linspace(0.0, 6.0, 6001)), rel=1e-6)
    assert response(peak) == pytest.approx(1.0, abs=0.05)  # the 0.89 normaliser scales the peak to ~1
    grid = np.linspace(peak, 8.0, 200)
    values = [response(float(z)) for z in grid]
    assert all(b < a for a, b in itertools.pairwise(values))
    assert response(-peak) == pytest.approx(-response(peak))
    assert response(0.0) == 0.0
    assert math.isnan(response(float("nan")))


def test_us_t04_ac3_overextended_trend_gets_a_smaller_u_than_a_moderate_one() -> None:
    moderate, overextended = 1.4, 3.0
    assert abs(response(overextended)) < abs(response(moderate))
    assert abs(response(6.0)) < 0.01  # a runaway z is sized to nothing


def test_us_t04_ac3_signal_never_exceeds_the_clip_on_a_random_walk() -> None:
    rng = np.random.default_rng(20260907)
    prices = list(100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.05, 600))))
    for bar in compute_signal_series(prices, C1_LIKE):
        assert -1.0 <= bar.signal <= 1.0
        assert math.isfinite(bar.signal)
        if not bar.warm:
            assert bar.signal == 0.0


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    prices=st.lists(
        st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=120,
    )
)
def test_us_t04_ac3_signal_is_bounded_by_one_for_arbitrary_price_paths(prices: list[float]) -> None:
    result = compute_signal(prices, C1_LIKE, "ANYUSDT")
    assert math.isfinite(result.signal)
    assert abs(result.signal) <= 1.0
    if not result.warm:
        assert result.signal == 0.0


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    prices=st.lists(
        st.floats(min_value=1.0, max_value=1e4, allow_nan=False, allow_infinity=False),
        min_size=40,
        max_size=90,
    ),
    scale=st.floats(min_value=0.01, max_value=100.0),
)
def test_us_t04_ac3_signal_is_scale_invariant(prices: list[float], scale: float) -> None:
    """Doubling every price cannot change a trend score — x, y and z all rescale."""
    base = compute_signal(prices, C1_LIKE)
    scaled = compute_signal([p * scale for p in prices], C1_LIKE)
    assert scaled.warm == base.warm
    if base.warm:
        assert scaled.signal == pytest.approx(base.signal, abs=1e-6)
