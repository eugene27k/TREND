"""US-T04 AC 1 / AC 2 — canonical vectors, series equivalence and config-driven windows."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from aegis.core.config import SignalConfig
from aegis.core.errors import ConfigError
from aegis.signals.engine import (
    compute_signal,
    compute_signal_series,
    response,
    rolling_std,
    signal_snapshot_row,
)
from tests.fixtures.appendix_c import (
    C1_EXPECTED,
    C1_PARAMS,
    C1_PRICES,
    C1_SIGNAL,
    TOL,
)

PROD = SignalConfig()


def c1_config(**overrides: object) -> SignalConfig:
    """Test-only config from the Appendix C.1 parameters (shorter than production)."""
    return SignalConfig(**{**C1_PARAMS, **overrides})


def trending_prices(n: int = 500, mu: float = 0.003, sd: float = 0.02, seed: int = 5) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100.0 * np.exp(np.cumsum(mu + rng.normal(0.0, sd, n))))


def same(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all((math.isnan(p) and math.isnan(q)) or p == q for p, q in zip(a, b, strict=True))


# --------------------------------------------------------------------------- #
# AC 1 — Appendix C.1
# --------------------------------------------------------------------------- #


def test_us_t04_ac1_appendix_c1_intermediates_reproduced() -> None:
    result = compute_signal(C1_PRICES, c1_config(), "BTCUSDT")
    assert result.warm
    for k, (pair, x, y, z, u) in enumerate(C1_EXPECTED):
        assert c1_config().pairs[k] == pair
        assert result.x[k] == pytest.approx(x, abs=TOL)
        assert result.y[k] == pytest.approx(y, abs=TOL)
        assert result.z[k] == pytest.approx(z, abs=TOL)
        assert result.u[k] == pytest.approx(u, abs=TOL)


def test_us_t04_ac1_appendix_c1_signal_reproduced() -> None:
    result = compute_signal(C1_PRICES, c1_config(), "BTCUSDT", date(2026, 1, 2))
    assert result.signal == pytest.approx(C1_SIGNAL, abs=TOL)
    assert result.symbol == "BTCUSDT"
    assert result.bar_day == date(2026, 1, 2)
    assert result.bar_ts_ms == 1767312000000  # 2026-01-02T00:00:00Z


def test_us_t04_ac1_values_are_at_the_last_observation() -> None:
    """Dropping the last bar must change the answer — we report t = len(prices) - 1."""
    full = compute_signal(C1_PRICES, c1_config())
    shorter = compute_signal(C1_PRICES[:-1], c1_config())
    assert full.signal != shorter.signal
    assert compute_signal_series(C1_PRICES, c1_config())[-1] == full


def test_us_t04_ac1_accepts_pandas_series() -> None:
    pd = pytest.importorskip("pandas")
    from_series = compute_signal(pd.Series(C1_PRICES), c1_config())
    assert from_series.signal == pytest.approx(C1_SIGNAL, abs=TOL)


def test_us_t04_ac1_series_matches_prefix_calls_over_200_days() -> None:
    prices = trending_prices(260, seed=2)
    cfg = c1_config()
    series = compute_signal_series(prices, cfg, "ETHUSDT")
    assert len(series) == len(prices)
    warm_bars = 0
    for i, bar in enumerate(series, start=1):
        one = compute_signal(prices[:i], cfg, "ETHUSDT")
        assert one.warm == bar.warm
        assert one.signal == pytest.approx(bar.signal, abs=1e-12)
        for a, b in ((one.x, bar.x), (one.y, bar.y), (one.z, bar.z), (one.u, bar.u)):
            assert same(a, b)
        warm_bars += bar.warm
    assert warm_bars > 100  # the equivalence is exercised on real, warm values


def test_us_t04_ac1_series_carries_bar_days() -> None:
    prices = C1_PRICES[:5]
    days = [date(2026, 1, d) for d in range(1, 6)]
    series = compute_signal_series(prices, c1_config(), "BTCUSDT", days)
    assert [r.bar_day for r in series] == days
    assert series[0].bar_ts_ms == 1767225600000


def test_us_t04_ac1_series_rejects_mismatched_bar_days() -> None:
    with pytest.raises(ConfigError):
        compute_signal_series(C1_PRICES[:5], c1_config(), "BTCUSDT", [date(2026, 1, 1)])


def test_us_t04_ac1_snapshot_row_carries_every_intermediate() -> None:
    cfg = c1_config()
    row = signal_snapshot_row(compute_signal(C1_PRICES, cfg, "BTCUSDT", date(2026, 1, 2)), cfg, source="rest")
    assert row["symbol"] == "BTCUSDT"
    assert row["bar_day"] == "2026-01-02"
    assert row["source"] == "rest"
    assert row["signal"] == pytest.approx(C1_SIGNAL, abs=TOL)
    assert row["x_2_6"] == pytest.approx(C1_EXPECTED[0][1], abs=TOL)
    assert row["u_8_24"] == pytest.approx(C1_EXPECTED[2][4], abs=TOL)
    assert signal_snapshot_row(compute_signal(C1_PRICES, cfg), cfg)["bar_day"] is None


# --------------------------------------------------------------------------- #
# AC 2 — everything is configuration-driven
# --------------------------------------------------------------------------- #


def test_us_t04_ac2_production_defaults_match_section_5_3() -> None:
    assert PROD.pairs == ((8, 24), (16, 48), (32, 96))
    assert PROD.price_std_window == 63
    assert PROD.y_std_window == 250
    assert PROD.response_norm == 0.89
    assert PROD.clip == 1.0
    assert PROD.ddof == 1
    assert PROD.warmup_days == 63 + 250 + 96


def _prod_like(**overrides: object) -> SignalConfig:
    """Production spans with a shorter y window, so 500 bars are warm."""
    base = {"pairs": PROD.pairs, "price_std_window": PROD.price_std_window, "y_std_window": 120}
    return SignalConfig(**{**base, **overrides})


def test_us_t04_ac2_changing_pairs_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    other = compute_signal(prices, _prod_like(pairs=((4, 12), (16, 48), (32, 96))))
    assert base.warm and other.warm
    assert base.x[0] != other.x[0]
    assert base.signal != other.signal


def test_us_t04_ac2_changing_price_std_window_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    other = compute_signal(prices, _prod_like(price_std_window=30))
    assert base.warm and other.warm
    assert base.y[0] != other.y[0]
    assert base.signal != other.signal


def test_us_t04_ac2_changing_y_std_window_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    other = compute_signal(prices, _prod_like(y_std_window=60))
    assert base.warm and other.warm
    assert base.z[0] != other.z[0]
    assert base.signal != other.signal


def test_us_t04_ac2_changing_response_norm_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    other = compute_signal(prices, _prod_like(response_norm=1.78))
    assert other.z == base.z  # the normaliser only rescales u
    for u_base, u_other in zip(base.u, other.u, strict=True):
        assert u_other == pytest.approx(u_base / 2.0, rel=1e-12)
    assert other.signal == pytest.approx(base.signal / 2.0, rel=1e-12)


def test_us_t04_ac2_changing_clip_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    assert abs(base.signal) > 0.5
    clipped = compute_signal(prices, _prod_like(clip=0.5))
    assert clipped.signal == pytest.approx(0.5)
    assert clipped.u == base.u  # clipping happens after the average


def test_us_t04_ac2_changing_ddof_changes_the_output() -> None:
    prices = trending_prices()
    base = compute_signal(prices, _prod_like())
    other = compute_signal(prices, _prod_like(ddof=0))
    assert base.warm and other.warm
    assert base.y[0] != other.y[0]
    assert base.signal != other.signal


def test_us_t04_ac2_ema_is_seeded_at_the_first_observation() -> None:
    """adjust=False with alpha = 2/(span+1), seeded at obs 0 — checked by hand."""
    prices = [100.0, 110.0, 90.0]
    cfg = SignalConfig(pairs=((2, 3),), price_std_window=2, y_std_window=2)
    short_alpha, long_alpha = 2.0 / 3.0, 2.0 / 4.0
    s = l = 100.0
    for p in prices[1:]:
        s += short_alpha * (p - s)
        l += long_alpha * (p - l)
    assert compute_signal(prices, cfg).x[0] == pytest.approx(s - l, abs=1e-12)


def test_us_t04_ac2_rolling_std_is_a_sample_std_with_ddof_one() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    out = rolling_std(values, 3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(1.0)  # ddof=1 over (1,2,3)
    assert rolling_std(values, 3, ddof=0)[2] == pytest.approx(math.sqrt(2.0 / 3.0))
    assert np.isnan(rolling_std(values, 5)).all()


# --------------------------------------------------------------------------- #
# Degenerate inputs — idle is valid (Invariant 3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prices", [[], [100.0], [100.0, 101.0]])
def test_us_t04_ac1_short_history_is_not_warm_and_signals_zero(prices: list[float]) -> None:
    result = compute_signal(prices, PROD, "BTCUSDT")
    assert not result.warm
    assert result.signal == 0.0
    assert len(result.x) == len(PROD.pairs)
    assert all(math.isnan(v) for v in result.y)


def test_us_t04_ac1_empty_series_is_empty() -> None:
    assert compute_signal_series([], PROD) == ()


def test_us_t04_ac3_constant_prices_give_zero_signal_not_infinity() -> None:
    """rolling_std = 0 must not divide into an infinite position."""
    for price in (100.0, 113.347906, 0.00012345):
        result = compute_signal([price] * 400, c1_config())
        assert not result.warm
        assert result.signal == 0.0
        assert all(math.isnan(v) for v in (*result.y, *result.z, *result.u))
        assert all(math.isfinite(v) for v in result.x)


def test_us_t04_ac3_flat_tail_after_a_trend_is_not_warm() -> None:
    prices = trending_prices(300) + [0.0] * 80
    prices[300:] = [prices[299]] * 80
    result = compute_signal(prices, _prod_like())
    assert not result.warm
    assert result.signal == 0.0


def test_us_t04_ac1_nan_prices_never_crash_and_never_size() -> None:
    prices = list(C1_PRICES)
    prices[55] = float("nan")
    result = compute_signal(prices, c1_config())
    assert not result.warm
    assert result.signal == 0.0
    assert math.isnan(result.y[0])


def test_us_t04_ac1_nan_early_in_history_clears_once_the_window_passes() -> None:
    prices = list(C1_PRICES)
    prices[5] = float("nan")
    series = compute_signal_series(prices, c1_config())
    assert not series[10].warm
    assert series[-1].warm  # the NaN is far outside every trailing window
    assert math.isfinite(series[-1].signal)


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_std_window": 1},
        {"y_std_window": 1},
        {"clip": 0.0},
    ],
)
def test_us_t04_ac2_invalid_config_raises_config_error(overrides: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        compute_signal(C1_PRICES, c1_config(**overrides))
    with pytest.raises(ConfigError):
        compute_signal_series(C1_PRICES, c1_config(**overrides))


def test_us_t04_ac2_empty_pairs_and_bad_norm_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        compute_signal(C1_PRICES, c1_config(pairs=()))
    with pytest.raises(ConfigError):
        response(1.0, norm=0.0)
    with pytest.raises(ConfigError):
        rolling_std([1.0, 2.0], 0)


def test_us_t04_ac1_non_numeric_prices_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        compute_signal(["not-a-price", "either"], PROD)
