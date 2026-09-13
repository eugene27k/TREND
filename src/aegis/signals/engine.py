"""Section 5.3 trend signal: the three-speed EMA-crossover score of Baz et al. (2015).

The engine is deliberately a pure function of a price series and a ``SignalConfig``
(Invariant: ``signals/`` has no clock, no I/O, no positions — US-T04 AC 5), so that
the live rebalance and the backtester run the *same* arithmetic and every position
traces back to a formula.

Two numerical decisions are load-bearing:

* EMAs use ``pandas.ewm(span=n, adjust=False)`` — the recursion pinned by the PRD
  and by the Appendix C.1 vectors — which is a forward recursion and therefore
  gives identical values whether it is run over a prefix or over the full history.
* Rolling standard deviations are recomputed per window instead of using pandas'
  online (add/remove) accumulator. The online form carries rounding error from the
  observations it has already dropped, so ``compute_signal(prices[:i+1])`` and
  ``compute_signal_series(prices)[i]`` would drift apart by ~1e-13. The backtester
  relies on those two being the same number, so we pay O(n*w) for exactness.

A window that is not yet full, a NaN price, or a zero rolling standard deviation
(a perfectly flat series) all yield NaN intermediates. Such a bar is *not warm* and
its signal is forced to 0.0: idle is valid (Invariant 3), and an infinity must never
reach the sizing step.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd

from aegis.core.config import SignalConfig
from aegis.core.errors import ConfigError
from aegis.core.types import SignalResult

_NAN = float("nan")

#: A rolling std at or below this fraction of the window's magnitude is treated as flat.
_FLAT_REL_EPS = 1e-12


def _response(z: np.ndarray, norm: float) -> np.ndarray:
    """Vectorised ``z * exp(-z^2 / 4) / norm`` — the *only* implementation.

    ``_intermediates`` and :func:`response` share it so that the blow-off
    protection the property tests exercise is the arithmetic that sizes
    positions. A runaway ``z`` overflows to ``exp(-inf) = 0`` and therefore to
    ``u = 0``; a non-finite ``z`` stays NaN and the bar is not warm.
    """
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        return z * np.exp(-(z**2) / 4.0) / norm


def response(z: float, norm: float = 0.89) -> float:
    """Blow-off-protected response ``z * exp(-z^2 / 4) / norm`` (Section 5.3).

    Peaks at ``|z| = sqrt(2)`` and decays beyond it, so an over-extended trend is
    sized *smaller* than a moderate one. ``norm`` (0.89) scales the peak to ~1.
    """
    if norm <= 0:
        raise ConfigError(f"signal.response_norm must be > 0, got {norm}")
    if not math.isfinite(z):
        return _NAN
    return float(_response(np.asarray(float(z)), norm))


def rolling_std(values: Sequence[float] | np.ndarray, window: int, ddof: int = 1) -> np.ndarray:
    """Trailing sample standard deviation; NaN until ``window`` observations exist.

    Each window is evaluated on its own slice (see the module docstring) so the
    value at index ``i`` depends only on ``values[i - window + 1 : i + 1]``.
    """
    if window < 1:
        raise ConfigError(f"rolling window must be >= 1, got {window}")
    arr = np.asarray(values, dtype=float)
    out = np.full(arr.shape[0], _NAN, dtype=float)
    if window <= ddof:
        return out
    denom = float(window - ddof)
    for i in range(window - 1, arr.shape[0]):
        w = arr[i - window + 1 : i + 1]
        mean = float(np.sum(w)) / window
        sd = math.sqrt(float(np.sum((w - mean) ** 2)) / denom)
        # A window of identical prices need not give *exactly* zero: rounding in the
        # mean leaves a ~1e-14 residue, which would divide into a huge y and size a
        # position out of float noise. Anything at that level is flat by definition.
        scale = float(np.max(np.abs(w)))
        out[i] = 0.0 if sd <= _FLAT_REL_EPS * scale else sd
    return out


def _as_prices(prices: Sequence[float] | pd.Series) -> np.ndarray:
    try:  # non-numeric input is a programming error, and it is ours to name
        arr = (
            prices.to_numpy(dtype=float, copy=True)
            if isinstance(prices, pd.Series)
            else np.asarray(prices, dtype=float)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"prices must be numeric, got {type(prices).__name__}") from exc
    if arr.ndim > 1:
        # Flattening a matrix would silently invent a price history out of columns.
        raise ConfigError(f"prices must be a single series, got shape {arr.shape}")
    return arr.ravel()


def _ema(prices: np.ndarray, span: int) -> np.ndarray:
    """``pandas.ewm(span, adjust=False)`` — alpha = 2/(span+1), seeded at obs 0."""
    return pd.Series(prices).ewm(span=span, adjust=False).mean().to_numpy(dtype=float)


def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """Elementwise division where a zero or non-finite denominator yields NaN.

    A flat price series has a zero rolling std; without this guard it would emit
    +/-inf and size a maximal position from a series that never moved.
    """
    out = np.full(num.shape, _NAN, dtype=float)
    ok = np.isfinite(den) & (den != 0.0) & np.isfinite(num)
    out[ok] = num[ok] / den[ok]
    return out


def _validate(params: SignalConfig) -> None:
    if not params.pairs:
        raise ConfigError("signal.pairs must not be empty")
    for short, long in params.pairs:
        if short < 1 or long < 1:
            raise ConfigError(f"signal pair {(short, long)}: EMA spans must be >= 1")
        if short >= long:
            raise ConfigError(f"signal pair {(short, long)}: short span must be < long span")
    if params.price_std_window < 2 or params.y_std_window < 2:
        raise ConfigError("signal std windows must be >= 2 for a ddof=1 sample std")
    if params.response_norm <= 0:
        raise ConfigError("signal.response_norm must be > 0")
    if params.clip <= 0:
        raise ConfigError("signal.clip must be > 0")


def _intermediates(
    prices: np.ndarray, params: SignalConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-pair (rows) x per-bar (columns) x, y, z, u matrices."""
    n = prices.shape[0]
    n_pairs = len(params.pairs)
    shape = (n_pairs, n)
    x = np.full(shape, _NAN, dtype=float)
    y = np.full(shape, _NAN, dtype=float)
    z = np.full(shape, _NAN, dtype=float)
    u = np.full(shape, _NAN, dtype=float)
    # No n < 2 special case: with one price both EMAs sit on the seed, so x = 0 and
    # every rolling window is still empty -> y/z/u NaN, warm False. That keeps a
    # one-bar prefix identical to bar 0 of the full series.
    price_sd = rolling_std(prices, params.price_std_window, params.ddof)
    for k, (short, long) in enumerate(params.pairs):
        x[k] = _ema(prices, short) - _ema(prices, long)
        y[k] = _safe_div(x[k], price_sd)
        z[k] = _safe_div(y[k], rolling_std(y[k], params.y_std_window, params.ddof))
        u[k] = _response(z[k], params.response_norm)
    return x, y, z, u


def _day_ts_ms(bar_day: date | None) -> int:
    if bar_day is None:
        return 0
    return int(datetime(bar_day.year, bar_day.month, bar_day.day, tzinfo=UTC).timestamp() * 1000)


def _result_at(
    i: int,
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    u: np.ndarray,
    params: SignalConfig,
    symbol: str,
    bar_day: date | None,
) -> SignalResult:
    xs = tuple(float(v) for v in x[:, i])
    ys = tuple(float(v) for v in y[:, i])
    zs = tuple(float(v) for v in z[:, i])
    us = tuple(float(v) for v in u[:, i])
    warm = all(math.isfinite(v) for v in (*xs, *ys, *zs, *us))
    signal = 0.0
    if warm:
        signal = float(np.clip(sum(us) / len(us), -params.clip, params.clip))
    return SignalResult(
        symbol=symbol,
        x=xs,
        y=ys,
        z=zs,
        u=us,
        signal=signal,
        bar_day=bar_day,
        bar_ts_ms=_day_ts_ms(bar_day),
        warm=warm,
    )


def _empty_result(params: SignalConfig, symbol: str, bar_day: date | None) -> SignalResult:
    nans = tuple(_NAN for _ in params.pairs)
    return SignalResult(
        symbol=symbol,
        x=nans,
        y=nans,
        z=nans,
        u=nans,
        signal=0.0,
        bar_day=bar_day,
        bar_ts_ms=_day_ts_ms(bar_day),
        warm=False,
    )


def compute_signal(
    prices: Sequence[float] | pd.Series,
    params: SignalConfig,
    symbol: str = "",
    bar_day: date | None = None,
) -> SignalResult:
    """Signal and intermediates at the last observation (US-T04 AC 1)."""
    _validate(params)
    arr = _as_prices(prices)
    if arr.shape[0] == 0:
        return _empty_result(params, symbol, bar_day)
    x, y, z, u = _intermediates(arr, params)
    return _result_at(arr.shape[0] - 1, x, y, z, u, params, symbol, bar_day)


def compute_signal_series(
    prices: Sequence[float] | pd.Series,
    params: SignalConfig,
    symbol: str = "",
    bar_days: Sequence[date] | None = None,
) -> tuple[SignalResult, ...]:
    """One ``SignalResult`` per bar — the backtester's whole-history form.

    Identical, bar for bar, to calling :func:`compute_signal` on every prefix.
    """
    _validate(params)
    arr = _as_prices(prices)
    n = arr.shape[0]
    if bar_days is not None and len(bar_days) != n:
        raise ConfigError(f"bar_days has {len(bar_days)} entries for {n} prices")
    if n == 0:
        return ()
    x, y, z, u = _intermediates(arr, params)
    days: Sequence[date | None] = list(bar_days) if bar_days is not None else [None] * n
    return tuple(_result_at(i, x, y, z, u, params, symbol, days[i]) for i in range(n))


def signal_snapshot_row(result: SignalResult, params: SignalConfig, **extra: Any) -> dict[str, Any]:
    """Flatten a result for the ``signal_snapshots`` table (US-T04 AC 4).

    The keys are the table's own column names (PRD Section 9 / ``002_trend.sql``:
    ``day``, ``symbol``, ``x1..x3``, ``y1..y3``, ``z1..z3``, ``u1..u3``,
    ``signal``, ``warm``, ``bar_ts``), so the row can be written as it stands.
    Storage owns the write; the engine only names the columns so that the pure
    layer stays free of the database. ``extra`` may add columns but never
    overwrite a computed one — every number in the row comes from ``result``.
    """
    row: dict[str, Any] = {
        "symbol": result.symbol,
        "day": result.bar_day.isoformat() if result.bar_day else None,
        "bar_ts": result.bar_ts_ms,
        "signal": result.signal,
        "warm": result.warm,
    }
    for k in range(len(params.pairs)):
        row[f"x{k + 1}"] = result.x[k]
        row[f"y{k + 1}"] = result.y[k]
        row[f"z{k + 1}"] = result.z[k]
        row[f"u{k + 1}"] = result.u[k]
    return {**extra, **row}


__all__ = [
    "compute_signal",
    "compute_signal_series",
    "response",
    "rolling_std",
    "signal_snapshot_row",
]
