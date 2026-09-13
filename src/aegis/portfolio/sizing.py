"""Sizing, scaling and caps (PRD Section 5.5, US-T06) — Invariant 8 lives here.

"Exposure is bounded by construction" means the bound is applied *here*, to
numbers, before an ``OrderRequest`` exists anywhere in the process. Nothing
downstream may increase a target: the planner subtracts positions, the executor
slices, and both can only move toward what this function returned. If a cap is
ever breached in production, the bug is in this file.

Why the caps are ordered and re-checked rather than solved jointly:

* **single** first, independently per symbol. One conviction spike must not be
  allowed to consume the whole book, and clipping one leg changes both of the
  aggregates below it — so it has to run first or the aggregates are computed on
  a book that will not be traded.
* **net** second, scaling only the *dominant* side. A net breach is directional
  risk, and the fix is to shrink the side that caused it; shrinking the hedging
  side too would raise net exposure, not lower it.
* **gross** last, scaling everything. It is the only cap whose fix is uniform,
  and scaling everything down by one factor cannot re-break single (every
  ``|target|`` shrinks) or net (``|net|`` shrinks by the same factor). That
  ordering is what makes the post-condition — all three caps hold — provable
  rather than hopeful, and ``check_caps`` re-asserts it on the way out.

The two steps that follow the caps (the funding haircut and the zeroing floor)
shrink individual legs, which leaves single and gross intact but can lift ``net``
when the shrunk legs were on the minority side — so the net cap is applied once
more on the final book. The post-condition is on what leaves this function.

``N`` is the *fixed* universe size (16), not the count of symbols that happen to
have a signal today. Dividing by the live count would quietly lever the book up
on days when most symbols are flat — exactly the days when the remaining signals
are least diversified.

This module is pure: no clock, no I/O, no positions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from aegis.core.config import AppConfig
from aegis.core.errors import ConfigError
from aegis.core.types import RiskModel, SymbolTarget, Targets
from aegis.portfolio.funding_overlay import NO_HAIRCUT, apply_funding_overlay

CAP_SINGLE = "single"
CAP_NET = "net"
CAP_GROSS = "gross"

#: Order is the specification, not a detail — see the module docstring.
CAP_ORDER = (CAP_SINGLE, CAP_NET, CAP_GROSS)


@dataclass(slots=True)
class _Leg:
    """Mutable per-symbol working state; frozen into a ``SymbolTarget`` at the end."""

    symbol: str
    signal: float
    vol: float
    raw: float = 0.0
    target: float = 0.0
    funding: float = 0.0
    haircut: float = NO_HAIRCUT
    caps: list[str] = field(default_factory=list)

    def mark(self, cap: str) -> None:
        """Record a cap application; ``_freeze`` normalises the list to CAP_ORDER."""
        if cap not in self.caps:
            self.caps.append(cap)


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _validate(cfg: AppConfig, governor_g: float) -> None:
    """Reject configuration that would *increase* risk. Nothing else raises here."""
    if not math.isfinite(governor_g) or governor_g < 0.0:
        raise ConfigError(f"governor_g must be a finite, non-negative multiplier, got {governor_g!r}")
    if cfg.sizing_divisor <= 0:
        raise ConfigError(f"sizing divisor N must be positive, got {cfg.sizing_divisor!r}")
    for name, value in (
        (CAP_SINGLE, cfg.caps.single),
        (CAP_NET, cfg.caps.net),
        (CAP_GROSS, cfg.caps.gross),
    ):
        if not math.isfinite(value) or value < 0.0:
            raise ConfigError(f"caps.{name} must be finite and non-negative, got {value!r}")
    if cfg.sizing.s_max < 0.0 or not math.isfinite(cfg.sizing.s_max):
        raise ConfigError(f"sizing.s_max must be finite and non-negative, got {cfg.sizing.s_max!r}")


def _covariance(risk_model: RiskModel, legs: list[_Leg]) -> list[list[float]]:
    """Sigma restricted to ``legs``, in ``legs`` order.

    Pairs the risk model covers come straight from ``RiskModel.cov_matrix()`` so
    that the diagonal used by ``sigma_p`` is the very same floored/capped vol the
    raw sizing divided by. A symbol the risk model has never seen (too little
    history for the covariance window) falls back to its own vol and the average
    pairwise correlation — the same substitution ``ewma_cov`` makes internally
    (US-T05 AC 2) — rather than being assigned zero risk.
    """
    n = len(legs)
    cov = risk_model.cov_matrix() if risk_model.symbols else []
    index = {s: i for i, s in enumerate(risk_model.symbols)}
    avg_corr = _finite(risk_model.avg_corr)
    sigma = [[0.0] * n for _ in range(n)]
    for a in range(n):
        ia = index.get(legs[a].symbol)
        for b in range(a, n):
            ib = index.get(legs[b].symbol)
            if ia is not None and ib is not None:
                value = _finite(cov[ia][ib])
            else:
                corr = 1.0 if a == b else avg_corr
                value = legs[a].vol * legs[b].vol * corr
            sigma[a][b] = value
            sigma[b][a] = value
    return sigma


def _portfolio_vol(weights: list[float], sigma: list[list[float]]) -> float:
    """``sqrt(w' Sigma w)``, floored at 0 — a rounding-negative variance is still flat."""
    n = len(weights)
    variance = 0.0
    for a in range(n):
        wa = weights[a]
        if wa == 0.0:
            continue
        for b in range(n):
            variance += wa * sigma[a][b] * weights[b]
    if not math.isfinite(variance) or variance <= 0.0:
        return 0.0
    return math.sqrt(variance)


# --------------------------------------------------------------------------- #
# Caps — applied in CAP_ORDER, each on the book the previous one left behind.
# --------------------------------------------------------------------------- #


def _apply_single_cap(legs: list[_Leg], limit: float) -> None:
    for leg in legs:
        if abs(leg.target) > limit:
            leg.target = math.copysign(limit, leg.target)
            leg.mark(CAP_SINGLE)


def _apply_net_cap(legs: list[_Leg], limit: float) -> None:
    """Scale the side whose sign matches the excess; leave the other side alone."""
    net = sum(leg.target for leg in legs)
    if abs(net) <= limit:
        return
    longs = sum(leg.target for leg in legs if leg.target > 0.0)
    shorts = sum(leg.target for leg in legs if leg.target < 0.0)
    if net > limit:
        dominant, other = longs, shorts
        sign = 1.0
    else:
        dominant, other = shorts, longs
        sign = -1.0
    if dominant == 0.0:  # pragma: no cover - unreachable while limit >= 0
        return
    # Solve k * dominant + other = sign * limit for the dominant side only.
    scale = (sign * limit - other) / dominant
    scale = min(1.0, max(0.0, scale))
    for leg in legs:
        if leg.target != 0.0 and math.copysign(1.0, leg.target) == sign:
            leg.target *= scale
            leg.mark(CAP_NET)


def _apply_gross_cap(legs: list[_Leg], limit: float) -> None:
    gross = sum(abs(leg.target) for leg in legs)
    if gross <= limit:
        return
    scale = limit / gross if gross > 0.0 else 0.0
    for leg in legs:
        if leg.target != 0.0:
            leg.target *= scale
            leg.mark(CAP_GROSS)


def check_caps(targets: Targets, cfg: AppConfig, *, tol: float = 1e-6) -> tuple[str, ...]:
    """Cap names currently breached by ``targets`` — empty tuple is the invariant.

    ``size_targets`` calls this on its own output; the risk supervisor calls it
    again on *filled* positions (Invariant 8: "re-checked after fills"). ``tol``
    is absolute USDT, sized for float noise on an equity-scaled comparison.
    """
    equity = targets.equity
    if equity <= 0.0:
        return () if targets.gross <= tol else (CAP_GROSS,)
    breached: list[str] = []
    largest = max((abs(t.target_notional) for t in targets.targets), default=0.0)
    if largest > cfg.caps.single * equity + tol:
        breached.append(CAP_SINGLE)
    if abs(targets.net) > cfg.caps.net * equity + tol:
        breached.append(CAP_NET)
    if targets.gross > cfg.caps.gross * equity + tol:
        breached.append(CAP_GROSS)
    return tuple(breached)


# --------------------------------------------------------------------------- #
# The one public entry point
# --------------------------------------------------------------------------- #


def size_targets(
    signals: Mapping[str, float],
    vols: Mapping[str, float],
    risk_model: RiskModel,
    equity: float,
    governor_g: float,
    cfg: AppConfig,
    funding_ann: Mapping[str, float] | None = None,
    min_notionals: Mapping[str, float] | None = None,
    prices: Mapping[str, float] | None = None,
) -> Targets:
    """Section 5.5 -> 5.6, literally and in order (US-T06).

    ``signals`` defines the book: one ``SymbolTarget`` comes back per key, in
    sorted order, whether or not it ends up with a position. ``vols`` are the
    annualised, floored/capped estimates from ``riskmodel``; a symbol with no
    usable vol is reported with ``raw = 0`` rather than dropped, because the
    persisted ``targets`` row is the audit trail for why a symbol was not traded.

    ``funding_ann``, ``min_notionals`` and ``prices`` are optional overlays:
    without them the funding haircut is 1.0, the zeroing floor is the
    ``min_target_frac`` rule alone, and ``target_qty`` stays 0.0. Lot rounding is
    deliberately *not* done here — that is the rebalance planner's job, against
    the symbol's ``stepSize``.

    Raises ``ConfigError`` only for parameters that could raise risk (a negative
    ``governor_g``, a negative cap, ``N <= 0``). Bad *market* inputs — no signal,
    a zero vol, a degenerate covariance, zero equity — produce a flat book, never
    an exception: idle is valid (Invariant 3).
    """
    _validate(cfg, governor_g)

    legs = [
        _Leg(symbol=symbol, signal=_finite(signals[symbol]), vol=max(0.0, _finite(vols.get(symbol, 0.0))))
        for symbol in sorted(signals)
    ]

    # Conviction is the mean over the symbols we were handed signals for, so a
    # book of three strong signals is not diluted by the thirteen the universe
    # could have had. (N, which does dilute, is applied separately in raw.)
    conv = sum(abs(leg.signal) for leg in legs) / len(legs) if legs else 0.0
    full = cfg.sizing.conviction_full
    ratio = 1.0 if full <= 0.0 else conv / full
    sigma_eff = cfg.sizing.sigma_target_portfolio * min(1.0, ratio)

    equity = _finite(equity)
    if equity <= 0.0 or not legs:
        # No capital, no book. Flat is the only safe answer and it is not an error.
        return _freeze(
            legs,
            sigma_p=0.0,
            conv=conv,
            sigma_eff=sigma_eff,
            s=0.0,
            g=governor_g,
            equity=equity,
            prices=prices,
        )

    n_divisor = cfg.sizing_divisor
    for leg in legs:
        if leg.vol > 0.0:
            leg.raw = leg.signal * (cfg.sizing.sigma_target_asset / leg.vol) / n_divisor * equity

    sigma = _covariance(risk_model, legs)
    sigma_p = _portfolio_vol([leg.raw / equity for leg in legs], sigma)

    # sigma_p == 0 means no signal, or a covariance that says this book carries no
    # risk. Either way there is nothing to scale to the vol target.
    s = 0.0 if sigma_p <= 0.0 else min(cfg.sizing.s_max, max(0.0, sigma_eff / sigma_p))

    for leg in legs:
        leg.target = leg.raw * s * governor_g

    _apply_single_cap(legs, cfg.caps.single * equity)
    _apply_net_cap(legs, cfg.caps.net * equity)
    _apply_gross_cap(legs, cfg.caps.gross * equity)

    funding = funding_ann or {}
    for leg in legs:
        leg.funding = _finite(funding.get(leg.symbol, 0.0))
        leg.target, leg.haircut = apply_funding_overlay(leg.target, leg.funding, cfg.funding)

    floor_frac = cfg.sizing.min_target_frac * equity
    mins = min_notionals or {}
    for leg in legs:
        floor = max(floor_frac, max(0.0, _finite(mins.get(leg.symbol, 0.0))))
        if abs(leg.target) < floor:
            leg.target = 0.0

    # The funding haircut and the zeroing floor only ever *shrink* individual
    # legs, which cannot re-break single or gross — but they can break net,
    # because shrinking the minority side of a directional book raises net
    # exposure (halve the only short in a net-long book and net goes up).
    # Invariant 8 is a
    # post-condition on what leaves this function, not on an intermediate book, so
    # the net cap runs once more on the final one. A leg the re-scale pushes back
    # under min_notional is left for the planner to drop: a cap is a risk bound
    # and outranks a dust rule.
    _apply_net_cap(legs, cfg.caps.net * equity)

    out = _freeze(
        legs, sigma_p=sigma_p, conv=conv, sigma_eff=sigma_eff, s=s, g=governor_g, equity=equity, prices=prices
    )
    breached = check_caps(out, cfg)
    if breached:  # pragma: no cover - the ordering proof above makes this unreachable
        raise ConfigError(f"cap post-condition violated after sizing: {breached}")
    return out


def _freeze(
    legs: list[_Leg],
    *,
    sigma_p: float,
    conv: float,
    sigma_eff: float,
    s: float,
    g: float,
    equity: float,
    prices: Mapping[str, float] | None,
) -> Targets:
    price_map = prices or {}
    out: list[SymbolTarget] = []
    for leg in legs:
        price = _finite(price_map.get(leg.symbol, 0.0))
        out.append(
            SymbolTarget(
                symbol=leg.symbol,
                signal=leg.signal,
                vol=leg.vol,
                raw=leg.raw,
                target_notional=leg.target,
                funding_ann=leg.funding,
                funding_haircut=leg.haircut,
                caps_applied=tuple(cap for cap in CAP_ORDER if cap in leg.caps),
                target_qty=leg.target / price if price > 0.0 else 0.0,
            )
        )
    return Targets(
        targets=tuple(out),
        sigma_p=sigma_p,
        conv=conv,
        sigma_eff=sigma_eff,
        s=s,
        g=g,
        equity=equity,
    )


__all__ = [
    "CAP_GROSS",
    "CAP_NET",
    "CAP_ORDER",
    "CAP_SINGLE",
    "check_caps",
    "size_targets",
]
