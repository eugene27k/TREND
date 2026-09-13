"""Block bootstrap of the backtest's daily returns (PRD 11.3, US-T16 AC 3).

Its only job is to answer one question the operator will actually ask in a
drawdown: *is this quarter unusual, or is it what a bad quarter looks like for
this strategy?* The 5th percentile of the resampled 3-month P&L is the kill
rule's threshold, so it has to be computed from blocks — daily crypto returns
are far from independent, and an i.i.d. bootstrap would produce a comfortingly
narrow distribution that the live engine then breaches every other quarter.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

DEFAULT_PERCENTILES = (1.0, 5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.0)


def block_bootstrap(
    daily_returns: Sequence[float],
    *,
    horizon_days: int = 91,
    block_days: int = 91,
    resamples: int = 10_000,
    seed: int = 20260907,
    starting_equity: float = 10_000.0,
    percentiles: Sequence[float] = DEFAULT_PERCENTILES,
) -> dict[float, float]:
    """P&L percentiles for a ``horizon_days`` window, in currency.

    Blocks are drawn with replacement from the circular return series, which
    keeps the autocorrelation and volatility clustering that make a trend
    strategy's drawdowns arrive in clumps.
    """
    returns = np.asarray([r for r in daily_returns if np.isfinite(r)], dtype=float)
    if returns.size == 0 or resamples <= 0:
        return {}

    block = max(1, min(int(block_days), returns.size))
    n_blocks = max(1, -(-int(horizon_days) // block))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, returns.size, size=(int(resamples), n_blocks))

    # Circular indexing so every start is legal without discarding the tail.
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]) % returns.size
    paths = returns[idx].reshape(int(resamples), -1)[:, : int(horizon_days)]

    growth = np.prod(1.0 + paths, axis=1)
    pnl = starting_equity * (growth - 1.0)
    return {float(p): float(np.percentile(pnl, p)) for p in percentiles}


def percentile_of(pnl: float, distribution: dict[float, float]) -> float | None:
    """Where an observed P&L sits in the bootstrap, by linear interpolation."""
    if not distribution:
        return None
    points = sorted(distribution.items(), key=lambda kv: kv[1])
    values = [v for _, v in points]
    keys = [k for k, _ in points]
    if pnl <= values[0]:
        return keys[0]
    if pnl >= values[-1]:
        return keys[-1]
    return float(np.interp(pnl, values, keys))


__all__ = ["DEFAULT_PERCENTILES", "block_bootstrap", "percentile_of"]
