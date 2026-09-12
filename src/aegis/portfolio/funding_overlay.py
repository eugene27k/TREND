"""Funding overlay (PRD Section 5.6, US-T07).

Why a blunt 50 % haircut rather than a smooth function of funding: the overlay
is not a carry model. It exists because a trend that is *already crowded* pays
the crowd to hold it, and a trend position that has to fund itself at 30 %/yr
needs a much larger move to break even. A step function is auditable — the
dashboard shows ``haircut = 0.5 | 1.0`` and an operator can tell at a glance
which legs were touched — whereas a continuous taper would silently re-price
the book with every funding tick.

The asymmetry matters: a long is only haircut by *positive* funding (longs pay
shorts) and a short only by *negative* funding. A short sitting in +50 % funding
is being paid, so it is left alone.

This module is pure: no clock, no I/O, no positions.
"""

from __future__ import annotations

import math

from aegis.core.config import FundingConfig

#: Hours in a funding year (Section 5.6: ``rate x 8760 / interval_hours``).
HOURS_PER_YEAR = 8760.0

#: Returned as the second element of ``apply_funding_overlay`` when nothing was cut.
NO_HAIRCUT = 1.0


def annualise_funding(rate: float, interval_hours: float) -> float:
    """Annualise one funding rate: ``rate x 8760 / interval_hours``.

    A non-positive or non-finite interval means the venue has not told us how
    often the symbol settles; annualising by a guess would either invent or hide
    a haircut, so we return 0.0 and leave the position untouched.
    """
    if not math.isfinite(rate) or not math.isfinite(interval_hours) or interval_hours <= 0:
        return 0.0
    return rate * (HOURS_PER_YEAR / interval_hours)


def apply_funding_overlay(
    target_notional: float,
    funding_ann: float,
    cfg: FundingConfig,
) -> tuple[float, float]:
    """Halve a target that is on the paying side of extreme funding.

    Returns ``(new_notional, haircut_applied)`` where ``haircut_applied`` is
    ``cfg.haircut`` when the overlay bit and ``1.0`` otherwise, so the caller can
    persist it per symbol (US-T07 AC 2).

    The threshold comparison is *strict* (Section 5.6 says ``f > +30 %``), so a
    symbol sitting exactly on the threshold is left alone.
    """
    if not math.isfinite(target_notional):
        return 0.0, NO_HAIRCUT
    if target_notional == 0.0 or not math.isfinite(funding_ann):
        return target_notional, NO_HAIRCUT

    threshold = cfg.haircut_threshold
    crowded_long = target_notional > 0.0 and funding_ann > threshold
    crowded_short = target_notional < 0.0 and funding_ann < -threshold
    if crowded_long or crowded_short:
        return target_notional * cfg.haircut, cfg.haircut
    return target_notional, NO_HAIRCUT


__all__ = ["HOURS_PER_YEAR", "NO_HAIRCUT", "annualise_funding", "apply_funding_overlay"]
