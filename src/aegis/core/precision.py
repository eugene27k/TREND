"""Exchange precision helpers.

Quantities and prices must land exactly on the symbol's ``stepSize``/``tickSize``
grid or the venue rejects the order. Binary floats cannot represent 0.001
exactly, so every rounding decision is made in ``Decimal`` and only the final
string is handed to the gateway.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext

from aegis.core.types import Side, SymbolInfo


def _d(value: float | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_step(value: float, step: float, *, mode: str = ROUND_DOWN) -> float:
    """Round ``|value|`` down (default) to a multiple of ``step``, keeping the sign."""
    if step <= 0:
        return value
    with localcontext() as ctx:
        ctx.prec = 28
        d_val, d_step = _d(abs(value)), _d(step)
        quantised = (d_val / d_step).quantize(Decimal(1), rounding=mode) * d_step
        return float(math.copysign(float(quantised), value))


def round_qty(qty: float, info: SymbolInfo) -> float:
    """Floor a quantity onto the lot grid (never round up — that over-trades)."""
    return round_step(qty, info.step_size, mode=ROUND_DOWN)


def round_price(price: float, info: SymbolInfo, side: Side | None = None) -> float:
    """Snap a price onto the tick grid.

    With a ``side``, snap conservatively so a passive order stays passive:
    a buy rounds *down* (further from the ask), a sell rounds *up*.
    """
    if side is Side.BUY:
        return round_step(price, info.tick_size, mode=ROUND_DOWN)
    if side is Side.SELL:
        return round_step(price, info.tick_size, mode="ROUND_UP")
    return round_step(price, info.tick_size, mode=ROUND_HALF_UP)


def format_qty(qty: float, info: SymbolInfo) -> str:
    return f"{_d(round_qty(qty, info)):.{info.quantity_precision}f}"


def format_price(price: float, info: SymbolInfo, side: Side | None = None) -> str:
    return f"{_d(round_price(price, info, side)):.{info.price_precision}f}"


def notional_to_qty(notional: float, price: float, info: SymbolInfo) -> float:
    """Signed notional -> signed, lot-rounded quantity."""
    if price <= 0:
        return 0.0
    return round_qty(notional / price, info)


def meets_min_notional(qty: float, price: float, info: SymbolInfo) -> bool:
    return abs(qty) >= info.min_qty and abs(qty) * price >= info.min_notional


def bump_to_min_qty(qty: float, info: SymbolInfo) -> float:
    """Raise a non-zero quantity to ``min_qty`` keeping its sign, else return 0."""
    if qty == 0.0:
        return 0.0
    if abs(qty) < info.min_qty:
        return math.copysign(info.min_qty, qty)
    return qty


def slippage_bps(fill_price: float, reference: float, side: Side) -> float:
    """Cost in bps versus the decision mid — positive means we paid up."""
    if reference <= 0:
        return 0.0
    raw = (fill_price - reference) / reference * 10_000.0
    return raw if side is Side.BUY else -raw


__all__ = [
    "bump_to_min_qty",
    "format_price",
    "format_qty",
    "meets_min_notional",
    "notional_to_qty",
    "round_price",
    "round_qty",
    "round_step",
    "slippage_bps",
]
