"""Turning targets into an ordered, persistable list of orders (PRD 5.8, 5.9 step 2).

This module is **pure**: no clock, no gateway, no database. Everything it needs —
the sized targets, the *reconciled exchange* positions (US-T09 AC 1), the
instrument grid, the marks and the liquidity estimate — is passed in. That is
what lets the same planner run inside the backtester and inside a unit test, and
it is why a plan can be persisted before a single order object exists (US-T09
AC 3): the plan is a value, not a side effect.

Three decisions here are load-bearing and easy to get wrong:

* **Rounding never rounds up.** ``notional_to_qty`` floors onto the lot grid, and
  an order that rounds to zero, or whose notional lands under the symbol's
  ``minNotional``, is dropped rather than inflated. Bumping a *reducing* order up
  to ``minNotional`` would trade more than the strategy asked for — the opposite
  of what a reduction is for. The single exception is a **close**: a position
  worth less than ``minNotional`` still has to be closed in full, so the order
  quantity is the whole position and the venue's close-position semantics carry
  it.
* **A sign flip is two orders, never one.** A single order through zero cannot
  carry ``reduce_only``: the venue would reject it (-2022) and the book would be
  left half-traded, or — worse, on the paths where Binance truncates instead of
  rejecting — only the reducing half would happen and the engine would believe it
  had flipped. So a flip is emitted as a reducing leg to flat (``reduce_only``)
  followed by a separate opening leg (not ``reduce_only``), in that order.
* **Risk-reducing first.** Invariant 1 in list form: the orders that shrink the
  book go out before the orders that grow it, largest current exposure first, so
  that a rebalance interrupted at any point has reduced more risk than it added.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping

from aegis.core.config import AppConfig
from aegis.core.precision import notional_to_qty, round_qty
from aegis.core.types import PlannedOrder, Position, Side, SymbolInfo, Targets
from aegis.portfolio.hysteresis import should_trade

#: Minutes in a day — the denominator of the "typical minute" clip (5.9 step 2).
MINUTES_PER_DAY = 1440

#: Quantities are floats on a decimal grid; compare against zero with slack.
_QTY_TOL = 1e-12


def minute_volume(avg_daily_quote_volume: float) -> float:
    """``avg_daily_quote_volume_30d / 1440`` — a symbol's typical minute in USDT.

    Callers that hold a *daily* volume (``BarService.avg_daily_quote_volume``)
    pass it through here to get the per-minute figure ``build_plan`` expects.
    """
    return avg_daily_quote_volume / MINUTES_PER_DAY if avg_daily_quote_volume > 0 else 0.0


def clip_notional(avg_minute_volume: float, cfg: AppConfig) -> float:
    """Liquidity clip in USDT: ``clip_frac_of_minute`` of a typical minute."""
    return max(0.0, cfg.exec.clip_frac_of_minute * max(0.0, avg_minute_volume))


def slice_count(delta_notional: float, clip: float, cfg: AppConfig) -> int:
    """``min(max_slices, ceil(|delta| / clip))``, never below 1.

    With no volume estimate (``clip <= 0``) there is nothing to slice against, so
    the order goes out whole rather than being chopped on a guess.
    """
    if clip <= 0.0:
        return 1
    return max(1, min(cfg.exec.max_slices, math.ceil(abs(delta_notional) / clip)))


def _sign(value: float) -> float:
    return math.copysign(1.0, value) if value != 0.0 else 0.0


def _side_of(delta_qty: float) -> Side:
    return Side.BUY if delta_qty > 0 else Side.SELL


def _tradeable(qty: float, mark: float, info: SymbolInfo) -> bool:
    """Would the venue accept this quantity at all? (minQty and minNotional)."""
    return abs(qty) >= info.min_qty - _QTY_TOL and abs(qty) * mark >= info.min_notional - _QTY_TOL


def _leg(
    *,
    symbol: str,
    delta_qty: float,
    delta_notional: float,
    current_qty: float,
    target_qty: float,
    reduce_only: bool,
    risk_reducing: bool,
    current_notional: float,
    clip: float,
    mark: float,
    info: SymbolInfo,
    cfg: AppConfig,
) -> tuple[PlannedOrder, float]:
    """Build one leg plus the sort key component ``|current_notional|``."""
    n_slices = slice_count(delta_notional, clip, cfg)
    clip_qty = abs(notional_to_qty(clip, mark, info)) if clip > 0 else 0.0
    order = PlannedOrder(
        symbol=symbol,
        side=_side_of(delta_qty),
        delta_notional=delta_notional,
        delta_qty=delta_qty,
        current_qty=current_qty,
        target_qty=target_qty,
        reduce_only=reduce_only,
        risk_reducing=risk_reducing,
        sequence=0,
        n_slices=n_slices,
        clip_qty=clip_qty,
    )
    return order, abs(current_notional)


def build_plan(
    targets: Targets,
    positions: Mapping[str, Position],
    symbol_info: Mapping[str, SymbolInfo],
    marks: Mapping[str, float],
    equity: float,
    cfg: AppConfig,
    avg_minute_volume: Mapping[str, float],
    universe: Collection[str],
) -> list[PlannedOrder]:
    """Plan the rebalance: what to trade, in which direction, in what order.

    ``positions`` must come from reconciled exchange state, never from local
    bookkeeping (US-T09 AC 1) — the planner sizes the delta against what the
    venue says we hold, so a missed fill cannot become a phantom position.

    ``avg_minute_volume`` is the symbol's average *per-minute* quote volume
    (``avg_daily_quote_volume_30d / 1440``; see :func:`minute_volume`).

    Every symbol in the universe is considered, and so is every symbol with a
    non-zero position: a symbol that has left the universe has no signal behind
    it and is always traded to zero (5.8), whatever the hysteresis band says.
    """
    by_symbol = targets.by_symbol()
    in_universe = set(universe)
    candidates = sorted(in_universe | {s for s, p in positions.items() if p.qty != 0.0} | set(by_symbol))

    legs: list[tuple[int, float, float, str, PlannedOrder]] = []
    for symbol in candidates:
        info = symbol_info.get(symbol)
        if info is None:
            continue  # an instrument we cannot round for is an instrument we cannot trade

        position = positions.get(symbol)
        current_qty = position.qty if position is not None else 0.0
        mark = marks.get(symbol) or (position.mark_price if position is not None else 0.0)
        if mark <= 0.0:
            continue
        current_notional = current_qty * mark

        target = by_symbol.get(symbol)
        # Out of the universe means out of the book: the target is zero even if a
        # stale sizing row still carries a number for this symbol.
        target_notional = target.target_notional if (target is not None and symbol in in_universe) else 0.0

        if current_qty == 0.0 and target_notional == 0.0:
            continue
        if not should_trade(target_notional, current_notional, equity, cfg.rebalance, symbol in in_universe):
            continue

        clip = clip_notional(avg_minute_volume.get(symbol, 0.0), cfg)

        if target_notional == 0.0:
            # A close. It must close the *whole* position even when what is left
            # is worth less than minNotional, so the quantity comes from the
            # position, not from the rounded delta.
            delta_qty = -current_qty
            order, key = _leg(
                symbol=symbol,
                delta_qty=delta_qty,
                delta_notional=-current_notional,
                current_qty=current_qty,
                target_qty=0.0,
                reduce_only=True,
                risk_reducing=True,
                current_notional=current_notional,
                clip=clip,
                mark=mark,
                info=info,
                cfg=cfg,
            )
            legs.append((0, -key, 0.0, symbol, order))
            continue

        delta_qty = notional_to_qty(target_notional - current_notional, mark, info)
        if abs(delta_qty) <= _QTY_TOL:
            continue
        final_qty = round_qty(current_qty + delta_qty, info)

        flips = current_qty != 0.0 and final_qty != 0.0 and _sign(final_qty) != _sign(current_qty)
        if flips:
            # Leg 1: back to flat, reduce-only. Leg 2: open the new side. A single
            # order through zero would have to drop reduce_only to be accepted,
            # and then a stale target could double the position instead of
            # flipping it — exactly what US-T10 AC 2 forbids.
            close_leg, key = _leg(
                symbol=symbol,
                delta_qty=-current_qty,
                delta_notional=-current_notional,
                current_qty=current_qty,
                target_qty=0.0,
                reduce_only=True,
                risk_reducing=True,
                current_notional=current_notional,
                clip=clip,
                mark=mark,
                info=info,
                cfg=cfg,
            )
            legs.append((0, -key, 0.0, symbol, close_leg))

            if _tradeable(final_qty, mark, info):
                open_leg, _ = _leg(
                    symbol=symbol,
                    delta_qty=final_qty,
                    delta_notional=final_qty * mark,
                    current_qty=0.0,
                    target_qty=final_qty,
                    reduce_only=False,
                    risk_reducing=False,
                    current_notional=0.0,
                    clip=clip,
                    mark=mark,
                    info=info,
                    cfg=cfg,
                )
                legs.append((1, -abs(final_qty * mark), 0.0, symbol, open_leg))
            continue

        if not _tradeable(delta_qty, mark, info):
            continue  # below the venue's floor — never bump a delta up to reach it

        reducing = abs(final_qty) < abs(current_qty)
        risk_reducing = abs(target_notional) < abs(current_notional)
        order, key = _leg(
            symbol=symbol,
            delta_qty=delta_qty,
            delta_notional=delta_qty * mark,
            current_qty=current_qty,
            target_qty=final_qty,
            reduce_only=reducing,
            risk_reducing=risk_reducing,
            current_notional=current_notional,
            clip=clip,
            mark=mark,
            info=info,
            cfg=cfg,
        )
        legs.append(
            (0 if risk_reducing else 1, -key if risk_reducing else -abs(delta_qty * mark), 0.0, symbol, order)
        )

    # Risk-reducing first by |current| descending, then risk-increasing by
    # |delta| descending; the symbol breaks ties so the plan is reproducible.
    legs.sort(key=lambda item: (item[0], item[1], item[3]))
    return [
        PlannedOrder(
            symbol=o.symbol,
            side=o.side,
            delta_notional=o.delta_notional,
            delta_qty=o.delta_qty,
            current_qty=o.current_qty,
            target_qty=o.target_qty,
            reduce_only=o.reduce_only,
            risk_reducing=o.risk_reducing,
            sequence=i,
            n_slices=o.n_slices,
            clip_qty=o.clip_qty,
        )
        for i, (_, _, _, _, o) in enumerate(legs)
    ]


def planned_notional(plan: Collection[PlannedOrder]) -> float:
    """Total absolute notional the plan intends to trade."""
    return sum(abs(p.delta_notional) for p in plan)


__all__ = [
    "MINUTES_PER_DAY",
    "build_plan",
    "clip_notional",
    "minute_volume",
    "planned_notional",
    "slice_count",
]
