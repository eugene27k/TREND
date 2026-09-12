"""EWMA volatility and covariance estimators (PRD Section 5.4, US-T05).

Why these shapes:

* The variance recursion is *pinned by Appendix C.2*. It is seeded at ``r_0**2``
  rather than at a sample variance, and it uses ``lambda = 2**(-1/half_life)``
  rather than the RiskMetrics 0.94. Both choices are part of the specification;
  "improving" them silently re-prices the whole book.
* The floor (30 %/yr) stops a suspiciously quiet fortnight from producing an
  outsized position; the cap (300 %/yr) stops a single crash bar from producing
  no position at all. Section 5.5 divides by ``sigma_i``, so an unbounded
  estimator is an unbounded position.
* ``RiskModel`` stores the *floored/capped* vols and a separate correlation
  matrix because ``RiskModel.cov_matrix`` rebuilds
  ``Sigma = diag(sigma) Corr diag(sigma)``. The sizing step (5.5) and the
  portfolio-vol step therefore divide by, and multiply back, the exact same
  numbers — an EWMA covariance whose diagonal disagreed with the clamped vols
  would make ``s`` silently wrong.

This module is pure: no clock, no I/O, no positions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from aegis.core.config import CovConfig, VolConfig
from aegis.core.errors import ConfigError
from aegis.core.types import RiskModel

#: ``lambda = LAMBDA_FROM_HALF_LIFE_BASE ** (-1 / half_life)`` (Appendix C.2).
LAMBDA_FROM_HALF_LIFE_BASE = 2.0


def decay(half_life: float) -> float:
    """EWMA decay for a half-life in days: ``2 ** (-1 / half_life)``."""
    if not math.isfinite(half_life) or half_life <= 0:
        raise ConfigError(f"half_life must be a positive number of days, got {half_life!r}")
    return LAMBDA_FROM_HALF_LIFE_BASE ** (-1.0 / half_life)


def _clean(returns: Sequence[float], *, what: str) -> list[float]:
    """Reject NaN/inf early — one bad bar would poison every later recursion step."""
    out: list[float] = []
    for r in returns:
        value = float(r)
        if not math.isfinite(value):
            raise ConfigError(f"{what} contains a non-finite return: {r!r}")
        out.append(value)
    return out


def _check_bounds(floor: float, cap: float) -> None:
    if floor < 0.0:
        raise ConfigError(f"vol floor must be >= 0, got {floor!r}")
    if cap < floor:
        raise ConfigError(f"vol cap {cap!r} is below the floor {floor!r}")


def _check_annualisation(annualisation_days: int) -> None:
    if annualisation_days <= 0:
        raise ConfigError(f"annualisation_days must be positive, got {annualisation_days!r}")


def _variance_path(returns: Sequence[float], lam: float) -> list[float]:
    """``v_0 = r_0**2``; ``v_t = lam v_{t-1} + (1 - lam) r_t**2`` (Appendix C.2)."""
    path: list[float] = []
    v = 0.0
    for i, r in enumerate(returns):
        v = r * r if i == 0 else lam * v + (1.0 - lam) * r * r
        path.append(v)
    return path


def ewma_vol(
    returns: Sequence[float],
    half_life: float = 10.0,
    *,
    floor: float = 0.30,
    cap: float = 3.00,
    annualisation_days: int = 365,
    min_obs: int = 20,
) -> float:
    """Annualised EWMA volatility of daily log returns, clamped to ``[floor, cap]``.

    ``min_obs`` is a *reliability* marker, not a gate: the Appendix C.2 vector is
    ten observations long and must still reproduce 0.352121 exactly, so a short
    sample is estimated normally and the clamp bounds the damage. Callers that
    care read ``RiskModel.n_obs``; an empty sample has nothing to estimate from
    and falls back to the floor, which is the conservative direction (a larger
    ``sigma_i`` is a smaller position).
    """
    _check_bounds(floor, cap)
    _check_annualisation(annualisation_days)
    if min_obs < 0:
        raise ConfigError(f"min_obs must be >= 0, got {min_obs!r}")
    r = _clean(returns, what="ewma_vol")
    if not r:
        return floor
    lam = decay(half_life)
    v = _variance_path(r, lam)[-1]
    sigma = math.sqrt(max(v, 0.0)) * math.sqrt(annualisation_days)
    return min(max(sigma, floor), cap)


def ewma_vol_series(
    returns: Sequence[float],
    half_life: float = 10.0,
    *,
    floor: float = 0.30,
    cap: float = 3.00,
    annualisation_days: int = 365,
    min_obs: int = 20,
) -> list[float]:
    """``ewma_vol`` evaluated at every point of the series (for the backtester).

    Element ``t`` is the clamped annualised vol using returns ``0..t`` inclusive,
    so a backtest can size day ``t + 1`` without ever looking ahead.
    """
    _check_bounds(floor, cap)
    _check_annualisation(annualisation_days)
    if min_obs < 0:
        raise ConfigError(f"min_obs must be >= 0, got {min_obs!r}")
    r = _clean(returns, what="ewma_vol_series")
    if not r:
        return []
    lam = decay(half_life)
    scale = math.sqrt(annualisation_days)
    return [min(max(math.sqrt(max(v, 0.0)) * scale, floor), cap) for v in _variance_path(r, lam)]


def _ewma_covariance(a: Sequence[float], b: Sequence[float], lam: float) -> float:
    """Same recursion as the variance, with the cross product — seeded at ``a_0 b_0``."""
    c = 0.0
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        c = x * y if i == 0 else lam * c + (1.0 - lam) * x * y
    return c


def _pair_correlation(a: Sequence[float], b: Sequence[float], lam: float) -> float | None:
    """EWMA correlation over the common (right-aligned) sample, or None if degenerate.

    Annualisation cancels in a correlation, so it is not applied here.
    """
    cab = _ewma_covariance(a, b, lam)
    caa = _ewma_covariance(a, a, lam)
    cbb = _ewma_covariance(b, b, lam)
    denom = math.sqrt(caa * cbb)
    if denom <= 0.0 or not math.isfinite(denom):
        return None
    return min(max(cab / denom, -1.0), 1.0)


def nearest_psd(
    corr: Sequence[Sequence[float]],
    *,
    max_iter: int = 10,
) -> tuple[tuple[float, ...], ...]:
    """Nearest positive-semidefinite correlation matrix (unit diagonal).

    Why this exists: Section 5.4 tells us to substitute the *universe average*
    pairwise correlation whenever a pair has fewer than ``min_obs`` common
    observations. That substitution is done entry by entry, so the result need
    not be a valid correlation matrix at all — it can have negative eigenvalues.
    Sizing then computes ``sigma_p = sqrt(w^T Sigma w)``, and a negative
    quadratic form makes that ``sqrt`` raise (or, worse, silently produce NaN
    and flatten the book). Repairing the matrix here means the invariant holds
    for every consumer of ``RiskModel``.

    The repair is the standard eigenvalue clip: symmetrise, clip negative
    eigenvalues to zero, rebuild, then rescale to a unit diagonal. Rescaling can
    reintroduce a minuscule negative eigenvalue, so the clip is repeated until
    the matrix is PSD to within ``1e-12``. A matrix that is already PSD survives
    unchanged to well within 1e-9.
    """
    if len(corr) == 0:
        return ()
    m = np.asarray(corr, dtype=float)
    if m.ndim != 2 or m.shape[0] != m.shape[1]:
        raise ConfigError(f"correlation matrix must be square, got shape {m.shape}")
    if not np.isfinite(m).all():
        raise ConfigError("correlation matrix contains non-finite entries")

    m = 0.5 * (m + m.T)
    for _ in range(max_iter):
        eigenvalues, eigenvectors = np.linalg.eigh(m)
        if eigenvalues.min() >= -1e-12:
            break
        m = (eigenvectors * np.clip(eigenvalues, 0.0, None)) @ eigenvectors.T
        d = np.sqrt(np.clip(np.diag(m), 1e-300, None))
        m = m / np.outer(d, d)
        m = 0.5 * (m + m.T)

    np.fill_diagonal(m, 1.0)
    m = np.clip(m, -1.0, 1.0)
    return tuple(tuple(float(x) for x in row) for row in m)


def _correlation_matrix(
    symbols: Sequence[str],
    series: Mapping[str, list[float]],
    lam: float,
    min_obs: int,
) -> tuple[tuple[tuple[float, ...], ...], float]:
    """Pairwise EWMA correlations, with the universe average filling short pairs.

    The average is taken over the pairs that actually have ``>= min_obs`` common
    observations; when no pair qualifies there is nothing to borrow and the
    off-diagonal is 0.0 (independent assets — the neutral assumption, and the
    one that leaves ``sigma_p`` closest to the naive sum of variances).
    """
    n = len(symbols)
    measured: dict[tuple[int, int], float] = {}
    for i in range(n):
        a_full = series[symbols[i]]
        for j in range(i + 1, n):
            b_full = series[symbols[j]]
            common = min(len(a_full), len(b_full))
            if common < min_obs:
                continue
            # Right-align: the common sample is the most recent `common` days.
            rho = _pair_correlation(a_full[-common:], b_full[-common:], lam)
            if rho is not None:
                measured[(i, j)] = rho

    avg_corr = float(sum(measured.values()) / len(measured)) if measured else 0.0

    rows = [[1.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            rho = measured.get((i, j), avg_corr)
            rows[i][j] = rho
            rows[j][i] = rho
    return nearest_psd(rows), avg_corr


def ewma_cov(
    returns: Mapping[str, Sequence[float]],
    half_life: float = 20.0,
    *,
    min_obs: int = 60,
    annualisation_days: int = 365,
) -> RiskModel:
    """EWMA covariance of daily log returns as a ``RiskModel`` (Section 5.4).

    The per-symbol vols carried on the result come from :func:`ewma_vol` at its
    own (10-day) half-life and are floored/capped, because ``RiskModel`` is
    consumed as ``diag(sigma) Corr diag(sigma)``: the covariance half-life of 20
    days shapes the *correlations only*.
    """
    _check_annualisation(annualisation_days)
    if min_obs < 0:
        raise ConfigError(f"min_obs must be >= 0, got {min_obs!r}")
    lam = decay(half_life)
    symbols = tuple(sorted(returns))
    series = {s: _clean(returns[s], what=f"ewma_cov[{s}]") for s in symbols}
    corr, avg_corr = _correlation_matrix(symbols, series, lam, min_obs)
    vols = {s: ewma_vol(series[s], annualisation_days=annualisation_days) for s in symbols}
    return RiskModel(
        symbols=symbols,
        vols=vols,
        corr=corr,
        avg_corr=avg_corr,
        n_obs={s: len(series[s]) for s in symbols},
    )


def build_risk_model(
    returns_by_symbol: Mapping[str, Sequence[float]],
    vol_cfg: VolConfig,
    cov_cfg: CovConfig,
) -> RiskModel:
    """Everything the sizing step and ``risk_model_snapshots`` need (US-T05 AC 3).

    Symbol order is ``sorted()`` so that the correlation matrix, the persisted
    snapshot and any later reload line up row-for-row without a lookup table.
    Persistence itself belongs to the storage layer; this function is pure.
    """
    _check_annualisation(cov_cfg.annualisation_days)
    lam = decay(cov_cfg.half_life_days)
    symbols = tuple(sorted(returns_by_symbol))
    series = {s: _clean(returns_by_symbol[s], what=f"build_risk_model[{s}]") for s in symbols}
    corr, avg_corr = _correlation_matrix(symbols, series, lam, cov_cfg.min_obs)
    vols = {
        s: ewma_vol(
            series[s],
            vol_cfg.half_life_days,
            floor=vol_cfg.floor,
            cap=vol_cfg.cap,
            annualisation_days=vol_cfg.annualisation_days,
            min_obs=vol_cfg.min_obs,
        )
        for s in symbols
    }
    return RiskModel(
        symbols=symbols,
        vols=vols,
        corr=corr,
        avg_corr=avg_corr,
        n_obs={s: len(series[s]) for s in symbols},
    )


__all__ = [
    "LAMBDA_FROM_HALF_LIFE_BASE",
    "build_risk_model",
    "decay",
    "ewma_cov",
    "ewma_vol",
    "ewma_vol_series",
    "nearest_psd",
]
