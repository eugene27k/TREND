"""Drawdown governor (PRD Section 5.7, US-T08).

Why a state machine rather than a formula: the four rows of the 5.7 table are
deliberately *asymmetric*. Risk comes off at 12 % and 20 % but only goes back on
at 15 % and 8 %, so a book oscillating around a threshold cannot pump exposure
up and down every day. A stateless ``g = f(dd)`` would have exactly that
pathology. The price of hysteresis is that ``g`` depends on where it has been,
which is why the caller must persist it (``governor_state``) and pass it back in.

Only ONE transition happens per call, and a downward transition always wins over
an upward one. That is Invariant 1 ("the engine may only *reduce* risk on its
own") made mechanical: from ``g = 1.0`` a sudden 25 % drawdown steps to 0.5 now
and to 0.25 on the next evaluation, never straight to 0.25 and never upward.

Why the peak lives on a time-weighted index: equity alone confuses capital with
performance. A deposit that doubles equity would print a new peak and erase a
real drawdown; a withdrawal would print a fake one and cut the book for no
reason. ``twr_factor = (E_t - net_transfer_t) / E_{t-1}`` strips the transfer out
of the return before it ever reaches the peak (US-T08 AC 3).

This module is pure: no clock, no I/O, no positions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from aegis.core.config import GovernorConfig
from aegis.core.errors import ConfigError
from aegis.core.types import EquityPoint

#: The multiplier a fresh, in-profit book runs at.
FULL_RISK = 1.0


def governor(dd: float, current_g: float, cfg: GovernorConfig) -> float:
    """One step of the Section 5.7 table: ``(drawdown, g) -> g'``.

    ``cfg.down`` maps ``threshold -> g'`` for cuts (fires when ``dd >= threshold``)
    and ``cfg.up`` maps ``threshold -> g'`` for restorations (fires when
    ``dd < threshold``). Thresholds are configuration, not literals, so the
    Appendix A defaults can be re-stated in a backtest variant without a code
    change.

    Among the applicable cuts we take the *largest* ``g'`` — the gentlest step
    that the table allows — and among the applicable restorations the *smallest*.
    That is what makes 1.0 -> 0.5 -> 0.25 a two-call path even when the drawdown
    gaps straight through both thresholds.
    """
    if not math.isfinite(current_g) or current_g <= 0.0:
        raise ConfigError(f"governor multiplier must be a positive finite number, got {current_g!r}")
    if not math.isfinite(dd):
        # A missing or broken equity curve is not evidence that risk is safe.
        return current_g

    cuts = [g for threshold, g in cfg.down.items() if dd >= threshold and g < current_g]
    if cuts:
        return max(cuts)
    restores = [g for threshold, g in cfg.up.items() if dd < threshold and g > current_g]
    if restores:
        return min(restores)
    return current_g


def is_downward(before: float, after: float) -> bool:
    """True when a transition cuts risk.

    The caller uses this to decide *when* the new ``g`` bites: downward
    transitions trigger an immediate proportional reduction (taker escalation at
    60 s), upward ones wait for the next rebalance (US-T08 AC 2). The executor
    that acts on this lives in ``rebalance/``, not here.
    """
    return after < before


def time_weighted_points(points: Sequence[EquityPoint]) -> list[EquityPoint]:
    """Fill in ``twr_factor`` and ``twr_index`` on an equity path.

    ``points`` must be in ascending time order and carry ``equity`` and
    ``net_transfer`` (deposits positive, withdrawals negative). Any factor and
    index already on the inputs is ignored and recomputed, so the function is
    idempotent and safe to run over rows straight out of the database.

    A non-positive previous equity yields a factor of 1.0: there is no return to
    measure on capital that was not there.
    """
    out: list[EquityPoint] = []
    index = 1.0
    previous: float | None = None
    for point in points:
        equity = float(point.equity)
        transfer = float(point.net_transfer)
        if previous is None or previous <= 0.0 or not math.isfinite(previous):
            factor = 1.0
        else:
            factor = (equity - transfer) / previous
        if not math.isfinite(factor) or factor < 0.0:
            factor = 0.0
        index *= factor
        out.append(
            EquityPoint(
                ts_ms=point.ts_ms,
                equity=equity,
                net_transfer=transfer,
                twr_factor=factor,
                twr_index=index,
            )
        )
        previous = equity
    return out


def drawdown_from_curve(points: Sequence[EquityPoint]) -> float:
    """Current drawdown against the peak of the time-weighted index.

    ``dd = 1 - twr_index / running_max(twr_index)`` at the last point, clamped to
    ``[0, 1]`` — the peak is "since inception" (Section 5.7), so it can only ever
    be the running maximum of the whole path.
    """
    weighted = time_weighted_points(points)
    if not weighted:
        return 0.0
    # The first factor is always 1.0, so the peak is always >= 1.0: no zero divide.
    peak = max(p.twr_index for p in weighted)
    return min(1.0, max(0.0, 1.0 - weighted[-1].twr_index / peak))


__all__ = [
    "FULL_RISK",
    "drawdown_from_curve",
    "governor",
    "is_downward",
    "time_weighted_points",
]
