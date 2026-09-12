"""Shared performance metrics — pure functions over daily returns and equity.

Three decisions shape every function in this file.

* **Purity.** Nothing here reads a clock, a database or a gateway. The metric
  engine loads the rows; these functions only do arithmetic. That is what makes
  the Appendix C.4 vectors reproducible and the dashboard numbers auditable.
* **``None`` rather than ``NaN`` or ``0``.** A metric computed from too few
  observations is *unknown*. Returning ``0.0`` would put a lie on the dashboard
  and ``NaN`` would propagate silently through JSON and SQLite (which stores it
  as NULL anyway). Every function therefore returns ``None`` when the input
  cannot support it, and the caller stores ``n_obs`` next to the value so the UI
  can grey it (PRD Section 10.5).
* **A 365-day year.** Crypto perpetuals trade every day, so annualisation is
  ``x 365`` / ``x sqrt(365)`` everywhere (Locked Decision 4). The ``periods``
  argument exists to make that explicit at every call site, not to be tuned.

NaN handling is *pairwise*: a non-finite observation is dropped from a single
series, and a pair is dropped from a two-series metric when either side is
non-finite. Two series of different length are a caller bug, not missing data,
and raise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from aegis.core.errors import AegisError

YEAR_DAYS = 365
"""Crypto year — markets never close (Locked Decision 4)."""


class MetricInputError(AegisError, ValueError):
    """Malformed metric input (mismatched series lengths, impossible level).

    It is an ``AegisError`` because the house rule is that the engine raises
    only its own exceptions, and a ``ValueError`` because that is what a caller
    of a numeric helper catches.
    """


# --------------------------------------------------------------------------- #
# Small numeric helpers — shared with trend_metrics.py
# --------------------------------------------------------------------------- #


def finite(values: Sequence[float] | None) -> list[float]:
    """The finite observations of ``values``; ``None``/NaN/inf are dropped."""
    out: list[float] = []
    for v in values or ():
        if v is None:
            continue
        f = float(v)
        if math.isfinite(f):
            out.append(f)
    return out


def paired(a: Sequence[float] | None, b: Sequence[float] | None) -> tuple[list[float], list[float]]:
    """Aligned finite pairs of two equal-length series.

    A pair is kept only when both sides are finite, so a gap in either series
    removes the observation from both.
    """
    xs = list(a or ())
    ys = list(b or ())
    if len(xs) != len(ys):
        raise MetricInputError(f"series lengths differ: {len(xs)} vs {len(ys)}")
    ka: list[float] = []
    kb: list[float] = []
    for x, y in zip(xs, ys, strict=True):
        if x is None or y is None:
            continue
        fx, fy = float(x), float(y)
        if math.isfinite(fx) and math.isfinite(fy):
            ka.append(fx)
            kb.append(fy)
    return ka, kb


def mean(values: Sequence[float]) -> float | None:
    vs = finite(values)
    return sum(vs) / len(vs) if vs else None


def stdev(values: Sequence[float], ddof: int = 1) -> float | None:
    """Sample standard deviation (``ddof=1``), or ``None`` below ``ddof + 1`` points."""
    vs = finite(values)
    n = len(vs)
    if n - ddof <= 0:
        return None
    m = sum(vs) / n
    return math.sqrt(sum((v - m) ** 2 for v in vs) / (n - ddof))


def quantile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolation quantile (the numpy default), ``p`` in [0, 1]."""
    vs = sorted(finite(values))
    if not vs:
        return None
    if not 0.0 <= p <= 1.0:
        raise MetricInputError(f"quantile p must be in [0, 1], got {p}")
    if len(vs) == 1:
        return vs[0]
    pos = (len(vs) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vs[lo]
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


# --------------------------------------------------------------------------- #
# Risk-adjusted return
# --------------------------------------------------------------------------- #


def sharpe(returns: Sequence[float], rf_annual: float = 0.0,
           periods: int = YEAR_DAYS) -> tuple[float | None, float | None]:
    """Annualised Sharpe ratio and its standard error.

    ``SE = sqrt((1 + 0.5 S^2) / n)`` — the Lo (2002) i.i.d. approximation. It is
    reported next to every Sharpe because a 90-day Sharpe without its error bar
    is an invitation to over-read noise.
    """
    r = finite(returns)
    n = len(r)
    if n < 2:
        return None, None
    sd = stdev(r)
    if not sd:
        return None, None
    excess = sum(r) / n - rf_annual / periods
    value = excess / sd * math.sqrt(periods)
    return value, math.sqrt((1.0 + 0.5 * value * value) / n)


def downside_deviation(returns: Sequence[float], target: float = 0.0,
                       periods: int = YEAR_DAYS) -> float | None:
    """Annualised deviation of the returns that fell below ``target``.

    The denominator is ``n - 1`` over *all* observations (not only the downside
    ones), matching the sample convention used by :func:`sharpe`, so a book that
    never lost money reports ``0.0`` rather than an undefined average.
    """
    r = finite(returns)
    n = len(r)
    if n < 2:
        return None
    sq = sum(min(0.0, x - target) ** 2 for x in r)
    return math.sqrt(sq / (n - 1)) * math.sqrt(periods)


def sortino(returns: Sequence[float], rf_annual: float = 0.0,
            periods: int = YEAR_DAYS) -> float | None:
    """Annualised excess return divided by the annualised downside deviation."""
    r = finite(returns)
    n = len(r)
    if n < 2:
        return None
    target = rf_annual / periods
    dd = downside_deviation(r, target=target, periods=periods)
    if not dd:
        return None
    return (sum(r) / n - target) * periods / dd


def max_drawdown(equity_or_index: Sequence[float]) -> float | None:
    """Largest peak-to-trough fall, as a positive fraction of the peak."""
    values = finite(equity_or_index)
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def calmar(returns: Sequence[float], max_dd: float | None,
           periods: int = YEAR_DAYS) -> float | None:
    """Geometric annualised return divided by the maximum drawdown."""
    r = finite(returns)
    n = len(r)
    if n < 2 or max_dd is None or max_dd <= 0:
        return None
    growth = 1.0
    for x in r:
        growth *= 1.0 + x
    if growth <= 0:
        return None
    return (growth ** (periods / n) - 1.0) / max_dd


# --------------------------------------------------------------------------- #
# Tail and shape
# --------------------------------------------------------------------------- #


def var(returns: Sequence[float], level: float = 0.95) -> float | None:
    """Historical value at risk — the ``1 - level`` return quantile, sign-flipped.

    Positive means a loss, so ``var(..., 0.95) == 0.03`` reads "on the worst 5 %
    of days the book lost at least 3 %".
    """
    r = finite(returns)
    if len(r) < 2:
        return None
    if not 0.0 < level < 1.0:
        raise MetricInputError(f"var level must be in (0, 1), got {level}")
    q = quantile(r, 1.0 - level)
    return None if q is None else -q


def cvar(returns: Sequence[float], level: float = 0.95) -> float | None:
    """Mean of the worst ``1 - level`` tail, sign-flipped (positive = loss)."""
    r = sorted(finite(returns))
    n = len(r)
    if n < 2:
        return None
    if not 0.0 < level < 1.0:
        raise MetricInputError(f"cvar level must be in (0, 1), got {level}")
    k = max(1, math.floor(n * (1.0 - level)))
    tail = r[:k]
    return -sum(tail) / len(tail)


def skew(returns: Sequence[float]) -> float | None:
    """Adjusted Fisher-Pearson sample skewness (``G1``); needs three points."""
    r = finite(returns)
    n = len(r)
    if n < 3:
        return None
    sd = stdev(r)
    if not sd:
        return None
    m = sum(r) / n
    acc = sum(((x - m) / sd) ** 3 for x in r)
    return n / ((n - 1) * (n - 2)) * acc


def kurtosis(returns: Sequence[float]) -> float | None:
    """Excess (Fisher) sample kurtosis ``G2``; needs four points."""
    r = finite(returns)
    n = len(r)
    if n < 4:
        return None
    sd = stdev(r)
    if not sd:
        return None
    m = sum(r) / n
    acc = sum(((x - m) / sd) ** 4 for x in r)
    first = n * (n + 1) / ((n - 1) * (n - 2) * (n - 3)) * acc
    second = 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))
    return first - second


# --------------------------------------------------------------------------- #
# Dispersion, hit rate, growth
# --------------------------------------------------------------------------- #


def volatility(returns: Sequence[float], periods: int = YEAR_DAYS) -> float | None:
    """Annualised standard deviation of the returns."""
    sd = stdev(returns)
    return None if sd is None else sd * math.sqrt(periods)


def hit_rate(returns: Sequence[float]) -> float | None:
    """Fraction of observations that were strictly positive (flat days count as misses)."""
    r = finite(returns)
    if not r:
        return None
    return sum(1 for x in r if x > 0) / len(r)


def cagr(equity: Sequence[float], days: float) -> float | None:
    """Compound annual growth rate between the first and last equity point."""
    e = finite(equity)
    if len(e) < 2 or days <= 0 or e[0] <= 0 or e[-1] <= 0:
        return None
    return (e[-1] / e[0]) ** (YEAR_DAYS / days) - 1.0


# --------------------------------------------------------------------------- #
# Relative to a benchmark
# --------------------------------------------------------------------------- #


def information_ratio(returns: Sequence[float], benchmark_returns: Sequence[float],
                      periods: int = YEAR_DAYS) -> float | None:
    """Annualised mean active return divided by the annualised tracking error."""
    a, b = paired(returns, benchmark_returns)
    if len(a) < 2:
        return None
    active = [x - y for x, y in zip(a, b, strict=True)]
    te = stdev(active)
    if not te:
        return None
    return (sum(active) / len(active)) / te * math.sqrt(periods)


def beta(returns: Sequence[float], benchmark: Sequence[float]) -> float | None:
    """OLS slope of ``returns`` on ``benchmark`` (``cov / var``)."""
    a, b = paired(returns, benchmark)
    n = len(a)
    if n < 2:
        return None
    ma = sum(a) / n
    mb = sum(b) / n
    var_b = sum((y - mb) ** 2 for y in b)
    if var_b <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return cov / var_b


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson correlation of two equal-length series."""
    xs, ys = paired(a, b)
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


# --------------------------------------------------------------------------- #
# Opportunity cost
# --------------------------------------------------------------------------- #


def cash_alternative(equity0: float, rf_annual: float, days: float) -> float | None:
    """What the starting capital would have earned sitting idle at ``rf_annual``.

    Compounded over a 365-day year, so it is directly comparable with the
    strategy's net P&L over the same window.
    """
    if days <= 0 or equity0 <= 0:
        return None
    return equity0 * ((1.0 + rf_annual) ** (days / YEAR_DAYS) - 1.0)


def net_of_infra(net_pnl: float, monthly_cost_eur: float, days: float,
                 eur_usdt: float = 1.0) -> float | None:
    """Net P&L after the hosting bill for the same window.

    The monthly cost is pro-rated on the 365-day year (``12 months / 365 days``)
    rather than on a 30-day month, so the annual figure is exact.
    """
    if days <= 0:
        return None
    return net_pnl - monthly_cost_eur * eur_usdt * days * 12.0 / YEAR_DAYS


__all__ = [
    "YEAR_DAYS",
    "MetricInputError",
    "beta",
    "cagr",
    "calmar",
    "cash_alternative",
    "correlation",
    "cvar",
    "downside_deviation",
    "finite",
    "hit_rate",
    "information_ratio",
    "kurtosis",
    "max_drawdown",
    "mean",
    "net_of_infra",
    "paired",
    "quantile",
    "sharpe",
    "skew",
    "sortino",
    "stdev",
    "var",
    "volatility",
]
