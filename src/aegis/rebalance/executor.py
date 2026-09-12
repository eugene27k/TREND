"""Passive, sliced, reduce-only-aware execution (PRD 5.9, US-T09 AC 3-4, US-T10).

This is the only module in the engine that places orders, so every design choice
here is defensive.

*The plan is persisted before the first order exists.* ``execute`` writes the
whole ordered plan and a ``cursor`` to the ``rebalances`` row, then flips the row
to ``running``, and only then sends anything. A process that dies at 00:30 comes
back, reads the plan, and continues from the cursor (``resume``) instead of
recomputing targets against a book it has half-traded (US-T09 AC 3-4).

*Slices adapt to fills, they do not assume them.* Before every slice the executor
re-reads the **exchange** position and recomputes what is left to trade. Price
improvement on an early slice therefore shrinks the later ones instead of
over-trading, and a remaining quantity whose sign no longer matches the planned
direction stops the symbol outright — the engine never trades back through its
own target (US-T10 AC 3).

*Reduce-only is a fact about the order, not a retry policy.* It comes from the
``PlannedOrder`` and is never dropped. A ``-2022`` rejection means our view of
the position was wrong; re-sending without the flag would turn a stale target
into a position flip, which is precisely what the flag exists to prevent, so the
slice is recorded and abandoned (US-T10 AC 2).

*The window is hard.* At ``end_ts_ms`` (01:00 UTC) working orders are cancelled
and the residual deltas are logged with reason ``window_end``; the hysteresis
band makes carrying them to the next day harmless (US-T10 AC 5).

Risk cuts (``flatten_all``, ``reduce_by``) run the same algorithm with the taker
escalation shortened to ``exec.risk_escalate_s`` and ``reduce_only`` always set.
They only ever shrink the book, which is why the risk path may call them while
the engine is blocked (Invariant 1).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as _date
from decimal import ROUND_HALF_UP
from typing import Any

from aegis.core.clock import add_months, at_utc, day_of
from aegis.core.context import Context
from aegis.core.errors import AegisError, GatewayError, OrderRejected
from aegis.core.precision import (
    round_price,
    round_price_marketable,
    round_qty,
    round_step,
    slippage_bps,
)
from aegis.core.types import (
    Fill,
    Order,
    OrderRequest,
    OrderType,
    PlannedOrder,
    RebalanceStatus,
    Side,
    SliceOutcome,
    SymbolInfo,
    TimeInForce,
)
from aegis.portfolio.hysteresis import should_trade
from aegis.storage.db import json_loads

#: Alert codes. Stable — the dashboard filters on them.
ALERT_ILLIQUID = "ILLIQUID_SYMBOL"
ALERT_ORDER_REJECTED = "ORDER_REJECTED"
ALERT_REDUCE_ONLY_REJECTED = "REDUCE_ONLY_REJECTED"
ALERT_WINDOW_END = "REBALANCE_WINDOW_END"

#: ``rebalances.kind`` values this module writes.
KIND_SCHEDULED = "scheduled"
KIND_RISK_CUT = "risk_cut"
KIND_FLATTEN = "flatten"

#: Residual reasons (US-T10 AC 5).
REASON_WINDOW_END = "window_end"
REASON_UNFILLED = "unfilled"

#: A post-only order that would cross is re-pegged this many times before the
#: slice gives up and waits for the escalation instead of fighting the book.
MAX_POST_ONLY_RETRIES = 3

#: Belt-and-braces bound on the per-slice wait loop; the clock always advances by
#: at least ``repeg_s`` per iteration, so this can only bite if a config is absurd.
MAX_SLICE_ITERATIONS = 1000

_QTY_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class RebalanceOutcome:
    """What one rebalance (or risk cut) actually did — US-T10 AC 4."""

    rebalance_id: str
    status: RebalanceStatus
    completion_pct: float
    traded_notional: float
    planned_notional: float
    fees: float
    avg_slippage_bps: float
    maker_ratio: float
    residuals: tuple[dict, ...]
    duration_s: float


@dataclass(slots=True)
class _SliceResult:
    filled_qty: float
    abandon: bool = False


class RebalanceExecutor:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._info: dict[str, SymbolInfo] = {}
        self._seen_trades: set[str] = set()
        self._slice_orders: dict[str, set[str]] = {}
        self._coid_seq = 0
        self._window_hit = False

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    def execute(
        self,
        rebalance_id: str,
        plan: Sequence[PlannedOrder],
        *,
        decision_mids: Mapping[str, float],
        end_ts_ms: int,
        escalate_s: int | None = None,
        kind: str = KIND_SCHEDULED,
    ) -> RebalanceOutcome:
        """Persist the plan, then work it in sequence order until it or time runs out."""
        ordered = sorted(plan, key=lambda p: p.sequence)
        started_ts = self.ctx.clock.now_ms()
        planned = sum(abs(p.delta_notional) for p in ordered)
        mids = dict(decision_mids)

        # Before any order exists (US-T09 AC 3).
        self.ctx.repos.rebalances.create(
            rebalance_id,
            day=day_of(started_ts),
            started_ts=started_ts,
            kind=kind,
            equity=self._equity(),
            governor_g=self.ctx.repos.governor.current_g(),
            order_plan=ordered,
            decision_mids=mids,
            planned_notional=planned,
        )
        self.ctx.repos.rebalances.set_status(rebalance_id, str(RebalanceStatus.RUNNING))
        return self._work(
            rebalance_id,
            ordered,
            mids,
            end_ts_ms=end_ts_ms,
            escalate_s=escalate_s,
            started_ts=started_ts,
            cursor=0,
            planned_notional=planned,
            kind=kind,
        )

    def resume(self, rebalance_id: str, end_ts_ms: int) -> RebalanceOutcome:
        """Continue a persisted plan from its stored cursor (US-T09 AC 4)."""
        row = self.ctx.repos.rebalances.get(rebalance_id)
        if row is None:
            raise AegisError(f"cannot resume unknown rebalance {rebalance_id!r}")
        plan = _plan_from_rows(json_loads(row["order_plan_json"], []))
        mids = {str(k): float(v) for k, v in (json_loads(row["decision_mids_json"], {}) or {}).items()}
        cursor = int(row["cursor"] or 0)
        kind = str(row["kind"])
        # The escalation is a property of *why* we are trading, and that is what
        # ``kind`` records — a resumed risk cut stays a risk cut.
        escalate_s = self.ctx.cfg.exec.risk_escalate_s if kind in (KIND_RISK_CUT, KIND_FLATTEN) else None
        self.ctx.repos.rebalances.set_status(rebalance_id, str(RebalanceStatus.RUNNING))
        return self._work(
            rebalance_id,
            plan,
            mids,
            end_ts_ms=end_ts_ms,
            escalate_s=escalate_s,
            started_ts=int(row["started_ts"]),
            cursor=cursor,
            planned_notional=float(row["planned_notional"] or 0.0),
            kind=kind,
        )

    def flatten_all(self, reason: str, now_ms: int) -> RebalanceOutcome:
        """Close every position, reduce-only, with the 60 s taker escalation."""
        plan: list[PlannedOrder] = []
        positions = sorted(
            (p for p in self.ctx.gateway.positions().values() if p.qty != 0.0),
            key=lambda p: (-abs(p.notional), p.symbol),
        )
        for seq, pos in enumerate(positions):
            plan.append(
                PlannedOrder(
                    symbol=pos.symbol,
                    side=Side.SELL if pos.qty > 0 else Side.BUY,
                    delta_notional=-pos.notional,
                    delta_qty=-pos.qty,
                    current_qty=pos.qty,
                    target_qty=0.0,
                    reduce_only=True,
                    risk_reducing=True,
                    sequence=seq,
                    n_slices=1,
                )
            )
        return self._risk_cut(plan, reason, now_ms, KIND_FLATTEN)

    def reduce_by(self, fractions: Mapping[str, float], reason: str, now_ms: int) -> RebalanceOutcome:
        """Cut each named position by a fraction of its current size, reduce-only."""
        positions = self.ctx.gateway.positions()
        entries: list[tuple[float, str, PlannedOrder]] = []
        for symbol, fraction in fractions.items():
            pos = positions.get(symbol)
            if pos is None or pos.qty == 0.0 or fraction <= 0.0:
                continue
            info = self._symbol_info(symbol)
            if info is None:
                continue
            keep = pos.qty * (1.0 - min(1.0, fraction))
            target_qty = round_qty(keep, info)
            delta_qty = target_qty - pos.qty
            if abs(delta_qty) <= _QTY_TOL:
                continue
            mark = pos.mark_price or self.ctx.gateway.mark_price(symbol)
            entries.append(
                (
                    abs(pos.notional),
                    symbol,
                    PlannedOrder(
                        symbol=symbol,
                        side=Side.SELL if delta_qty < 0 else Side.BUY,
                        delta_notional=delta_qty * mark,
                        delta_qty=delta_qty,
                        current_qty=pos.qty,
                        target_qty=target_qty,
                        reduce_only=True,
                        risk_reducing=True,
                        sequence=0,
                        n_slices=1,
                    ),
                )
            )
        entries.sort(key=lambda e: (-e[0], e[1]))
        plan = [replace(e[2], sequence=i) for i, e in enumerate(entries)]
        return self._risk_cut(plan, reason, now_ms, KIND_RISK_CUT)

    # ------------------------------------------------------------------ #
    # The main loop
    # ------------------------------------------------------------------ #

    def _risk_cut(
        self, plan: Sequence[PlannedOrder], reason: str, now_ms: int, kind: str
    ) -> RebalanceOutcome:
        rebalance_id = self._risk_cut_id(kind, now_ms)
        mids = {p.symbol: self._decision_mid(p.symbol) for p in plan}
        # A risk cut is bounded by the same TWAP horizon so it can never run
        # unbounded, but the 60 s escalation means it is a taker long before then.
        end_ts_ms = now_ms + self.ctx.cfg.exec.max_twap_min * 60_000
        outcome = self.execute(
            rebalance_id,
            plan,
            decision_mids=mids,
            end_ts_ms=end_ts_ms,
            escalate_s=self.ctx.cfg.exec.risk_escalate_s,
            kind=kind,
        )
        self.ctx.alerts.info(
            "RISK_CUT",
            f"{kind} ({reason}): {outcome.completion_pct:.1f}% of {outcome.planned_notional:.2f} USDT",
            {"rebalance_id": rebalance_id, "reason": reason, "symbols": [p.symbol for p in plan]},
        )
        return outcome

    def _work(
        self,
        rebalance_id: str,
        plan: Sequence[PlannedOrder],
        decision_mids: dict[str, float],
        *,
        end_ts_ms: int,
        escalate_s: int | None,
        started_ts: int,
        cursor: int,
        planned_notional: float,
        kind: str,
    ) -> RebalanceOutcome:
        escalate = escalate_s if escalate_s is not None else self.ctx.cfg.exec.escalate_s
        self._window_hit = False

        # Symbols are worked CONCURRENTLY, in two waves. Sequentially, each symbol
        # holds the engine for its full escalation window (5 minutes), so sixteen
        # of them need eighty minutes against a fifty-five minute window and the
        # rebalance could never reach the 95 % completion P1 requires — while
        # Appendix D's own example is "100 % in 23 min" over 17 orders.
        #
        # Two waves rather than one pool, because 5.8 requires risk-reducing
        # deltas to execute first: reductions free margin and shrink the book
        # before anything grows it, which a single interleaved pool would not
        # guarantee.
        remaining_entries = [(i, plan[i]) for i in range(cursor, len(plan))]
        completed: set[int] = set(range(cursor))
        for risk_reducing in (True, False):
            wave = [(i, p) for i, p in remaining_entries if bool(p.risk_reducing) is risk_reducing]
            if not wave:
                continue
            if self.ctx.clock.now_ms() >= end_ts_ms:
                self._window_hit = True
                break
            self._run_wave(
                rebalance_id,
                wave,
                decision_mids,
                end_ts_ms,
                escalate,
                completed=completed,
                plan_len=len(plan),
            )

        self._cancel_working({p.symbol for p in plan})
        return self._finish(
            rebalance_id,
            plan,
            started_ts=started_ts,
            planned_notional=planned_notional,
            kind=kind,
        )

    def _run_wave(
        self,
        rebalance_id: str,
        wave: Sequence[tuple[int, PlannedOrder]],
        decision_mids: dict[str, float],
        end_ts_ms: int,
        escalate_s: int,
        *,
        completed: set[int],
        plan_len: int,
    ) -> None:
        """Work every symbol in the wave at once, stepping each when it is due.

        Each worker is a generator that yields the timestamp it next wants to be
        looked at — the point where a sequential executor would have slept. The
        scheduler advances whichever workers are due and then sleeps to the
        earliest outstanding wake-up, so the wall-clock cost of the wave is the
        slowest single symbol rather than the sum of all of them.
        """
        now = self.ctx.clock.now_ms()
        workers: list[list[Any]] = [
            [index, self._plan_worker(rebalance_id, planned, decision_mids, end_ts_ms, escalate_s), now]
            for index, planned in wave
        ]

        while workers:
            now = self.ctx.clock.now_ms()
            if now >= end_ts_ms:
                self._window_hit = True
                return
            due = [w for w in workers if w[2] <= now]
            if not due:
                self._sleep_until(min(min(w[2] for w in workers), end_ts_ms))
                continue
            for worker in due:
                try:
                    worker[2] = max(next(worker[1]), now)
                except StopIteration:
                    workers.remove(worker)
                    completed.add(worker[0])
                    # The cursor stays a prefix: a resumed plan replays completed
                    # entries harmlessly (their remaining quantity is already 0),
                    # but it must never skip one that is still outstanding.
                    cursor = 0
                    while cursor < plan_len and cursor in completed:
                        cursor += 1
                    self.ctx.repos.rebalances.set_cursor(rebalance_id, cursor)

    def _plan_worker(
        self,
        rebalance_id: str,
        planned: PlannedOrder,
        decision_mids: dict[str, float],
        end_ts_ms: int,
        escalate_s: int,
    ) -> Iterator[int]:
        info = self._symbol_info(planned.symbol)
        if info is None:
            self.ctx.alerts.warn(
                ALERT_ORDER_REJECTED,
                f"no instrument metadata for {planned.symbol}; skipped",
                {"symbol": planned.symbol, "rebalance_id": rebalance_id},
            )
            return

        mid = decision_mids.get(planned.symbol) or self._decision_mid(planned.symbol)
        decision_mids.setdefault(planned.symbol, mid)

        n_slices = max(1, planned.n_slices)
        slice_start = self.ctx.clock.now_ms()
        twap_ms = min(self.ctx.cfg.exec.max_twap_min * 60_000, max(0, end_ts_ms - slice_start))
        interval_ms = twap_ms / n_slices

        for i in range(n_slices):
            now = self.ctx.clock.now_ms()
            if now >= end_ts_ms:
                self._window_hit = True
                return

            remaining = self._remaining_qty(planned)
            if abs(remaining) <= _QTY_TOL:
                return

            qty = self._slice_qty(remaining, n_slices - i, info, planned)
            if qty <= 0.0:
                return

            result = yield from self._run_slice(
                rebalance_id=rebalance_id,
                planned=planned,
                info=info,
                seq=i,
                side=Side.BUY if remaining > 0 else Side.SELL,
                qty=qty,
                decision_mid=mid,
                end_ts_ms=end_ts_ms,
                escalate_s=escalate_s,
            )
            if result.abandon:
                return

            if i < n_slices - 1:
                yield min(slice_start + int((i + 1) * interval_ms), end_ts_ms)

    def _slice_qty(
        self, remaining: float, slices_left: int, info: SymbolInfo, planned: PlannedOrder
    ) -> float:
        """Even split of what is *actually* left, floored onto the lot grid.

        A split so small the venue would refuse it (minQty / minNotional) is
        collapsed into one order for the whole remainder — slicing is a cost
        optimisation, and it must never stop the plan from executing. A closing
        order is sent whole whatever its notional: the position has to go.
        """
        # Nearest-tick rather than floor: the target was already floored onto the
        # grid by the planner, so what is left here is float noise, and flooring
        # it again would shave a lot off every slice and leave a dust position.
        # The result is still clamped to the remainder, so it cannot over-trade.
        rest = round_step(abs(remaining), info.step_size, mode=ROUND_HALF_UP)
        qty = min(round_step(rest / max(1, slices_left), info.step_size, mode=ROUND_HALF_UP), rest)
        price = self._reference_price(planned.symbol)
        closing = planned.target_qty == 0.0

        if qty < info.min_qty - _QTY_TOL or (price > 0 and qty * price < info.min_notional - _QTY_TOL):
            qty = rest  # too small to be accepted on its own: send the remainder whole
        if qty <= 0.0:
            return 0.0
        if closing:
            return qty
        if qty < info.min_qty - _QTY_TOL or (price > 0 and qty * price < info.min_notional - _QTY_TOL):
            return 0.0
        return qty

    def _remaining_qty(self, planned: PlannedOrder) -> float:
        """Signed quantity still to trade, measured against the exchange position.

        The guard on direction is the anti-over-trade rule (US-T10 AC 3): if the
        book has already reached or passed the target — because an earlier slice
        filled better than expected, or because someone else moved the position —
        there is nothing left to do, and trading the "negative remainder" would
        walk the position back through the target.
        """
        position = self.ctx.gateway.positions().get(planned.symbol)
        current = position.qty if position is not None else 0.0
        remaining = planned.target_qty - current
        direction = math.copysign(1.0, planned.delta_qty) if planned.delta_qty != 0.0 else 0.0
        if direction == 0.0 or math.copysign(1.0, remaining) != direction:
            return 0.0
        return remaining

    # ------------------------------------------------------------------ #
    # One slice: post-only, re-pegged, escalated
    # ------------------------------------------------------------------ #

    def _run_slice(
        self,
        *,
        rebalance_id: str,
        planned: PlannedOrder,
        info: SymbolInfo,
        seq: int,
        side: Side,
        qty: float,
        decision_mid: float,
        end_ts_ms: int,
        escalate_s: int,
    ) -> Iterator[int]:
        """Yields the timestamp it next wants to run at; returns its ``_SliceResult``."""
        repos = self.ctx.repos
        clock = self.ctx.clock
        slice_id = f"{rebalance_id}:{planned.symbol}:{seq}"
        placed_ts = clock.now_ms()
        repos.slices.create(
            slice_id, rebalance_id, planned.symbol, seq, side, qty, planned.reduce_only, placed_ts
        )

        deadline = placed_ts + escalate_s * 1000
        taker = False
        outcome = SliceOutcome.CANCELLED

        try:
            order = self._place_passive(planned, info, side, qty, slice_id, rebalance_id)
        except OrderRejected as exc:
            return self._slice_rejected(slice_id, planned, exc)

        if order is None:
            repos.slices.finish(slice_id, str(SliceOutcome.REJECTED), 0.0, 0.0, False, clock.now_ms())
            return _SliceResult(0.0, abandon=False)

        waited = False
        for _ in range(MAX_SLICE_ITERATIONS):
            order = self._refresh(order)
            if order.status.is_terminal:
                outcome = SliceOutcome.FILLED if order.filled_qty > 0 else SliceOutcome.CANCELLED
                break

            now = clock.now_ms()
            if now >= end_ts_ms:
                order = self._cancel(order)
                self._window_hit = True
                outcome = SliceOutcome.CANCELLED
                break

            if now >= deadline:
                order = self._cancel(order)
                remaining = round_qty(order.remaining_qty, info)
                if remaining > 0 and self._tradeable(remaining, planned.symbol, info):
                    try:
                        order = self._place_taker(planned, info, side, remaining, slice_id, rebalance_id)
                    except OrderRejected as exc:
                        return self._slice_rejected(slice_id, planned, exc)
                    order = self._refresh(order)
                    if not order.status.is_terminal:
                        # An IOC is terminal at a real venue the moment it lands;
                        # cancelling is how a simulator and a stuck order agree.
                        order = self._cancel(order)
                    taker = True
                outcome = SliceOutcome.ESCALATED
                break

            # The re-peg only applies to an order that has actually been resting:
            # checking it the instant after placing would fight our own price.
            if waited:
                book = self.ctx.gateway.book_ticker(planned.symbol)
                passive = round_price(book.passive_price(side), info, side)
                if order.price is not None and passive != order.price:
                    order = self._cancel(order)
                    repos.slices.bump_repegs(slice_id)
                    remaining = round_qty(order.remaining_qty, info)
                    if remaining <= 0 or not self._tradeable(remaining, planned.symbol, info):
                        outcome = SliceOutcome.FILLED if order.filled_qty > 0 else SliceOutcome.CANCELLED
                        break
                    try:
                        replacement = self._place_passive(
                            planned, info, side, remaining, slice_id, rebalance_id
                        )
                    except OrderRejected as exc:
                        return self._slice_rejected(slice_id, planned, exc)
                    if replacement is None:
                        outcome = SliceOutcome.CANCELLED
                        break
                    order = replacement
                    continue

            yield min(clock.now_ms() + self.ctx.cfg.exec.repeg_s * 1000, deadline, end_ts_ms)
            waited = True

        filled, avg_price = self._record_fills(planned, slice_id, rebalance_id, decision_mid, placed_ts)
        repos.slices.finish(slice_id, str(outcome), filled, avg_price, taker, clock.now_ms())
        return _SliceResult(filled)

    def _slice_rejected(self, slice_id: str, planned: PlannedOrder, exc: OrderRejected) -> _SliceResult:
        """Record the refusal and abandon the symbol. Never retry without reduce_only."""
        self.ctx.repos.slices.finish(
            slice_id, str(SliceOutcome.REJECTED), 0.0, 0.0, False, self.ctx.clock.now_ms()
        )
        code = ALERT_REDUCE_ONLY_REJECTED if exc.is_reduce_only_violation else ALERT_ORDER_REJECTED
        self.ctx.alerts.warn(
            code,
            f"{planned.symbol} slice rejected ({exc.code}): {exc}",
            {
                "symbol": planned.symbol,
                "slice_id": slice_id,
                "code": exc.code,
                "reduce_only": planned.reduce_only,
            },
        )
        return _SliceResult(0.0, abandon=True)

    # ------------------------------------------------------------------ #
    # Order plumbing
    # ------------------------------------------------------------------ #

    def _place_passive(
        self,
        planned: PlannedOrder,
        info: SymbolInfo,
        side: Side,
        qty: float,
        slice_id: str,
        rebalance_id: str,
    ) -> Order | None:
        """Post-only at the passive best, stepping away a tick per GTX refusal.

        A ``-5022`` means the book moved under us between the read and the send.
        Escalating to a taker on that would pay the spread for a race we can win
        by simply quoting one tick further away (5.9 step 3).
        """
        book = self.ctx.gateway.book_ticker(planned.symbol)
        price = round_price(book.passive_price(side), info, side)
        step = info.tick_size if side is Side.SELL else -info.tick_size

        for _ in range(MAX_POST_ONLY_RETRIES):
            try:
                return self._send(planned, side, qty, price, TimeInForce.GTX, slice_id, rebalance_id)
            except OrderRejected as exc:
                if not exc.is_post_only_violation:
                    raise
                self.ctx.repos.slices.bump_repegs(slice_id)
                # One tick further from the touch. Snapped half-up because the
                # arithmetic result is already a grid multiple bar float noise,
                # which a directional rounding would turn into a second tick.
                price = round_step(price + step, info.tick_size, mode=ROUND_HALF_UP)
        return None

    def _place_taker(
        self,
        planned: PlannedOrder,
        info: SymbolInfo,
        side: Side,
        qty: float,
        slice_id: str,
        rebalance_id: str,
    ) -> Order:
        book = self.ctx.gateway.book_ticker(planned.symbol)
        # Rounded *towards* the book, not to the nearest tick: an IOC that lands a
        # hair on the passive side of the touch is expired, not filled, so the
        # escalation would appear to happen and quietly do nothing.
        price = round_price_marketable(book.aggressive_price(side), info, side)
        return self._send(planned, side, qty, price, TimeInForce.IOC, slice_id, rebalance_id)

    def _send(
        self,
        planned: PlannedOrder,
        side: Side,
        qty: float,
        price: float,
        tif: TimeInForce,
        slice_id: str,
        rebalance_id: str,
    ) -> Order:
        request = OrderRequest(
            symbol=planned.symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            time_in_force=tif,
            reduce_only=planned.reduce_only,
            client_order_id=self._client_order_id(),
            strategy=self.ctx.strategy,
            rebalance_id=rebalance_id,
            slice_id=slice_id,
            intent="rebalance",
        )
        order = self.ctx.gateway.place_order(request)
        self._slice_orders.setdefault(slice_id, set()).add(order.order_id)
        self.ctx.repos.orders.upsert(order)
        return order

    def _client_order_id(self) -> str:
        """Short and unique: Binance caps ``newClientOrderId`` at 36 characters."""
        self._coid_seq += 1
        return f"T{self.ctx.clock.now_ms()}-{self._coid_seq}"

    def _refresh(self, order: Order) -> Order:
        fresh = self.ctx.gateway.get_order(order.symbol, order.order_id)
        self.ctx.repos.orders.upsert(fresh)
        return fresh

    def _cancel(self, order: Order) -> Order:
        try:
            cancelled = self.ctx.gateway.cancel_order(order.symbol, order.order_id)
        except OrderRejected:
            # Already terminal at the venue — take its word for the final state.
            cancelled = self.ctx.gateway.get_order(order.symbol, order.order_id)
        self.ctx.repos.orders.upsert(cancelled)
        return cancelled

    def _cancel_working(self, symbols: set[str]) -> None:
        for order in self.ctx.gateway.open_orders():
            if order.symbol not in symbols:
                continue
            self._cancel(order)

    def _record_fills(
        self,
        planned: PlannedOrder,
        slice_id: str,
        rebalance_id: str,
        decision_mid: float,
        since_ms: int,
    ) -> tuple[float, float]:
        """Store this slice's prints with slippage against the decision mid (5.9 step 1)."""
        rows: list[Fill] = []
        # Match on our own order ids: a live venue's ``userTrades`` knows nothing
        # about slices, so the slice tag has to be re-attached here.
        our_orders = self._slice_orders.get(slice_id, set())
        for fill in self.ctx.gateway.user_trades(planned.symbol, start_ms=since_ms):
            if fill.trade_id in self._seen_trades or fill.order_id not in our_orders:
                continue
            self._seen_trades.add(fill.trade_id)
            rows.append(
                replace(
                    fill,
                    strategy=self.ctx.strategy,
                    rebalance_id=rebalance_id,
                    slice_id=slice_id,
                    decision_mid=decision_mid,
                    slippage_bps=slippage_bps(fill.price, decision_mid, fill.side),
                )
            )
        if rows:
            self.ctx.repos.fills.add_many(rows)
        qty = sum(f.qty for f in rows)
        avg = sum(f.qty * f.price for f in rows) / qty if qty else 0.0
        return qty, avg

    # ------------------------------------------------------------------ #
    # Finishing: residuals, summary, illiquidity
    # ------------------------------------------------------------------ #

    def _finish(
        self,
        rebalance_id: str,
        plan: Sequence[PlannedOrder],
        *,
        started_ts: int,
        planned_notional: float,
        kind: str,
    ) -> RebalanceOutcome:
        now = self.ctx.clock.now_ms()
        equity = self._equity()
        positions = self.ctx.gateway.positions()

        # A flip contributes two legs for one symbol; only the last one states
        # where the position was supposed to end up.
        final_leg: dict[str, PlannedOrder] = {}
        for entry in plan:
            final_leg[entry.symbol] = entry

        residual_reason = REASON_WINDOW_END if self._window_hit else REASON_UNFILLED
        residuals: list[dict] = []
        for symbol, entry in sorted(final_leg.items()):
            position = positions.get(symbol)
            current_qty = position.qty if position is not None else 0.0
            mark = (position.mark_price if position is not None else 0.0) or self._reference_price(symbol)
            residual_qty = entry.target_qty - current_qty
            target_notional = entry.target_qty * mark
            current_notional = current_qty * mark
            if not should_trade(target_notional, current_notional, equity, self.ctx.cfg.rebalance):
                continue
            residuals.append(
                {
                    "symbol": symbol,
                    "residual_qty": residual_qty,
                    "residual_notional": residual_qty * mark,
                    "target_qty": entry.target_qty,
                    "current_qty": current_qty,
                    "reason": residual_reason,
                }
            )

        fills = self.ctx.repos.fills.for_rebalance(rebalance_id)
        traded = sum(abs(f.qty * f.price) for f in fills)
        fees = sum(f.fee for f in fills)
        maker = sum(abs(f.qty * f.price) for f in fills if f.is_maker)
        avg_slip = sum(f.slippage_bps * abs(f.qty * f.price) for f in fills) / traded if traded > 0 else 0.0
        maker_ratio = maker / traded if traded > 0 else 0.0
        completion = 100.0 * traded / planned_notional if planned_notional > 0 else 100.0
        status = RebalanceStatus.WINDOW_END if self._window_hit else RebalanceStatus.COMPLETE

        for symbol in {f.symbol for f in fills}:
            self.ctx.repos.targets.mark_traded(rebalance_id, symbol)

        self.ctx.repos.rebalances.finish(
            rebalance_id,
            ended_ts=now,
            status=str(status),
            completion_pct=completion,
            traded_notional=traded,
            fees=fees,
            avg_slippage_bps=avg_slip,
            maker_ratio=maker_ratio,
            residuals=residuals,
        )

        if self._window_hit:
            self.ctx.alerts.warn(
                ALERT_WINDOW_END,
                f"{rebalance_id} hit the window end with {len(residuals)} residual delta(s)",
                {"rebalance_id": rebalance_id, "residuals": residuals},
            )
        if kind == KIND_SCHEDULED:
            self._record_illiquidity(residuals, now)

        return RebalanceOutcome(
            rebalance_id=rebalance_id,
            status=status,
            completion_pct=completion,
            traded_notional=traded,
            planned_notional=planned_notional,
            fees=fees,
            avg_slippage_bps=avg_slip,
            maker_ratio=maker_ratio,
            residuals=tuple(residuals),
            duration_s=(now - started_ts) / 1000.0,
        )

    def _record_illiquidity(self, residuals: Sequence[dict], now_ms: int) -> None:
        """Three consecutive days short of tolerance flags the symbol (5.9 step 6)."""
        cfg = self.ctx.cfg
        day = day_of(now_ms)
        window = [(day.toordinal() - offset) for offset in range(cfg.rebalance.illiquid_days)]
        days = [_ordinal_iso(o) for o in window]
        for residual in residuals:
            symbol = str(residual["symbol"])
            self.ctx.repos.illiquid.record_failure(symbol, day, str(residual["reason"]))
            streak = self.ctx.repos.illiquid.consecutive_failures(symbol, days)
            if streak < cfg.rebalance.illiquid_days:
                continue
            self.ctx.repos.illiquid.flag(symbol, now_ms, self._next_refresh_ms(now_ms), "illiquid")
            self.ctx.alerts.warn(
                ALERT_ILLIQUID,
                f"{symbol} missed tolerance {streak} days running; excluded until the universe refresh",
                {"symbol": symbol, "days": streak},
            )

    def _next_refresh_ms(self, now_ms: int) -> int:
        """Flags last until the next monthly universe refresh clears them."""
        first_next = add_months(day_of(now_ms).replace(day=1), 1)
        return at_utc(first_next, self.ctx.cfg.universe.refresh_time_utc)

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #

    def _risk_cut_id(self, kind: str, now_ms: int) -> str:
        candidate = f"{kind}-{now_ms}"
        suffix = 1
        while self.ctx.repos.rebalances.get(candidate) is not None:
            candidate = f"{kind}-{now_ms}-{suffix}"
            suffix += 1
        return candidate

    def _equity(self) -> float:
        return self.ctx.gateway.account().equity

    def _symbol_info(self, symbol: str) -> SymbolInfo | None:
        if symbol in self._info:
            return self._info[symbol]
        info = self.ctx.repos.symbol_meta.get(symbol)
        if info is None:
            info = self.ctx.gateway.exchange_info().get(symbol)
        if info is not None:
            self._info[symbol] = info
        return info

    def _decision_mid(self, symbol: str) -> float:
        try:
            return self.ctx.gateway.book_ticker(symbol).mid
        except GatewayError:
            # No book: the mark is the best reference we have for slippage.
            return self.ctx.gateway.mark_price(symbol)

    def _reference_price(self, symbol: str) -> float:
        return self._decision_mid(symbol)

    def _tradeable(self, qty: float, symbol: str, info: SymbolInfo) -> bool:
        price = self._reference_price(symbol)
        if qty < info.min_qty - _QTY_TOL:
            return False
        return price <= 0 or qty * price >= info.min_notional - _QTY_TOL

    def _sleep_until(self, ts_ms: int) -> None:
        delta = ts_ms - self.ctx.clock.now_ms()
        if delta > 0:
            self.ctx.clock.sleep(delta / 1000.0)


def _ordinal_iso(ordinal: int) -> str:
    return _date.fromordinal(ordinal).isoformat()


def _plan_from_rows(rows: Sequence[Mapping[str, object]]) -> list[PlannedOrder]:
    """Rebuild the plan from its persisted JSON (the resume path)."""
    plan = [
        PlannedOrder(
            symbol=str(r["symbol"]),
            side=Side(str(r["side"])),
            delta_notional=float(r["delta_notional"]),  # type: ignore[arg-type]
            delta_qty=float(r["delta_qty"]),  # type: ignore[arg-type]
            current_qty=float(r["current_qty"]),  # type: ignore[arg-type]
            target_qty=float(r["target_qty"]),  # type: ignore[arg-type]
            reduce_only=bool(r["reduce_only"]),
            risk_reducing=bool(r["risk_reducing"]),
            sequence=int(r["sequence"]),  # type: ignore[arg-type]
            n_slices=int(r.get("n_slices", 1)),  # type: ignore[arg-type,union-attr]
            clip_qty=float(r.get("clip_qty", 0.0)),  # type: ignore[arg-type,union-attr]
        )
        for r in rows
    ]
    return sorted(plan, key=lambda p: p.sequence)


__all__ = [
    "ALERT_ILLIQUID",
    "ALERT_ORDER_REJECTED",
    "ALERT_REDUCE_ONLY_REJECTED",
    "ALERT_WINDOW_END",
    "KIND_FLATTEN",
    "KIND_RISK_CUT",
    "KIND_SCHEDULED",
    "REASON_UNFILLED",
    "REASON_WINDOW_END",
    "RebalanceExecutor",
    "RebalanceOutcome",
]
