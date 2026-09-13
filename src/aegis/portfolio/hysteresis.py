"""Rebalance hysteresis (PRD Section 5.8, US-T09 AC 2).

Why two thresholds and not one: ``0.10 * |target|`` keeps proportional noise out
of big positions, but on its own it collapses to zero as the target shrinks —
a target of 20 USDT would trade on a 2 USDT delta and pay more in fees than the
position can earn. ``0.0025 * E`` is the absolute floor that stops that. The
rule takes the *larger* of the two, so both pathologies are excluded.

The band is on the delta, not on the target, so a position that is already close
enough is simply left alone: doing nothing is a valid outcome of a rebalance
(Invariant 3).

This module is pure: no clock, no I/O, no positions — ``current`` is passed in by
the planner, which reads it from *reconciled exchange* state (US-T09 AC 1).
"""

from __future__ import annotations

import math

from aegis.core.config import RebalanceConfig

#: ``trade_reason`` codes. Stable strings — they are logged and shown per symbol.
REASON_OUT_OF_UNIVERSE = "out_of_universe"
REASON_EXCEEDS_BAND = "delta_exceeds_band"
REASON_WITHIN_BAND = "within_band"
REASON_FLAT = "flat"


def hysteresis_threshold(target: float, equity: float, cfg: RebalanceConfig) -> float:
    """``max(hysteresis_frac * |target|, hysteresis_equity_frac * equity)``."""
    proportional = cfg.hysteresis_frac * abs(target) if math.isfinite(target) else 0.0
    absolute = cfg.hysteresis_equity_frac * equity if math.isfinite(equity) and equity > 0.0 else 0.0
    return max(proportional, absolute)


def trade_reason(
    target: float,
    current: float,
    equity: float,
    cfg: RebalanceConfig,
    in_universe: bool = True,
) -> str:
    """Why ``should_trade`` decided as it did — one of the ``REASON_*`` codes."""
    if not in_universe:
        # A delisted or dropped symbol is exited whatever the band says; leaving
        # it is an unmanaged position with no signal behind it.
        return REASON_OUT_OF_UNIVERSE if current != 0.0 else REASON_FLAT
    if target == 0.0 and current == 0.0:
        return REASON_FLAT
    delta = target - current
    if not math.isfinite(delta):
        return REASON_WITHIN_BAND
    if abs(delta) > hysteresis_threshold(target, equity, cfg):
        return REASON_EXCEEDS_BAND
    return REASON_WITHIN_BAND


def should_trade(
    target: float,
    current: float,
    equity: float,
    cfg: RebalanceConfig,
    in_universe: bool = True,
) -> bool:
    """Trade iff the delta clears the band, or the symbol has left the universe.

    All three arguments are signed USDT notionals (``+`` long, ``-`` short).
    """
    return trade_reason(target, current, equity, cfg, in_universe) in (
        REASON_OUT_OF_UNIVERSE,
        REASON_EXCEEDS_BAND,
    )


__all__ = [
    "REASON_EXCEEDS_BAND",
    "REASON_FLAT",
    "REASON_OUT_OF_UNIVERSE",
    "REASON_WITHIN_BAND",
    "hysteresis_threshold",
    "should_trade",
    "trade_reason",
]
