"""Paper mode: real market data, simulated fills (US-T10 AC 6).

``PaperGateway`` wraps a real gateway and reads everything *observable* from it
— book, bars, marks, funding, exchange info, server time — but never sends it an
order. Orders, positions, wallet and income are simulated locally with the same
``SimAccount`` bookkeeping the fake venue uses, so paper P&L is produced by the
same arithmetic as every accounting test.

The fill model is deliberately small enough to state in full:

* **Post-only (GTX) and other passive limit orders.** They never take
  liquidity, so they fill only when the market comes to them. Each ``poll()``
  reads the book and fills the order *at its own limit price*, with
  ``is_maker=True`` and no slippage, once the opposite side of the book has
  reached or passed it — ``ask <= price`` for a buy, ``bid >= price`` for a
  sell. Depth and queue position are not modelled: the first poll at which the
  market trades through fills the whole remaining quantity. This is optimistic
  about *whether* we get filled and pessimistic about nothing, which is why the
  phase gates measure the realised maker ratio against it (Section 7).
  A reduce-only order stays pegged to the position it protects: its fill is
  truncated to what is left to reduce and it is cancelled once there is nothing,
  so a stale resting order can never open or flip a position (5.9 step 4).
* **IOC / FOK / MARKET, and any limit priced through the touch.** They take
  liquidity, so they fill at placement time,
  at ``aggressive_price(side)`` moved against us by the symbol's entry in the
  TREND slippage table (``cfg.exec.slippage_bps``: 2 bps for BTC/ETH, 6 bps
  otherwise — Locked Decision 8), with ``is_maker=False``.

Funding is applied on ``poll()``: any settlement in the inner gateway's funding
history that is newer than the last one applied is booked against the position
we were holding. ``poll()`` is what advances the simulation; the engine calls it
every loop.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from aegis.core.clock import Clock
from aegis.core.config import AppConfig
from aegis.core.errors import GatewayError, OrderRejected
from aegis.core.types import (
    AccountState,
    BookTicker,
    DailyBar,
    Fill,
    FundingRate,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    SymbolInfo,
    TimeInForce,
)
from aegis.gateway.base import ExchangeGateway
from aegis.gateway.simulation import (
    CODE_UNKNOWN_ORDER,
    OrderBookState,
    SimAccount,
    apply_fill_to_order,
    crosses_book,
    reduce_only_fill_qty,
    replace_order,
    validate_order,
)

_BPS = 10_000.0


class PaperGateway:
    """Simulated execution on live data. Never places an order on ``inner``."""

    def __init__(
        self,
        inner: ExchangeGateway,
        clock: Clock,
        cfg: AppConfig,
        *,
        starting_balance: float | None = None,
    ) -> None:
        self.inner = inner
        self.clock = clock
        self.cfg = cfg
        balance = starting_balance if starting_balance is not None else (cfg.phase.capital_usdt or 10_000.0)
        self.sim = SimAccount(
            wallet_balance=balance,
            maker_fee=cfg.exec.maker_fee_fallback,
            taker_fee=cfg.exec.taker_fee_fallback,
            leverage=float(cfg.account.leverage),
        )
        self.books = OrderBookState()
        self.calls: Counter[str] = Counter()
        self.closed = False
        self.leverage: dict[str, int] = {}
        self.margin_type: dict[str, str] = {}
        self._exchange_info: dict[str, SymbolInfo] | None = None
        self._last_funding_ms: dict[str, int] = {}
        self._commission_cache: dict[str, tuple[float, float]] = {}

    # ------------------------------------------------------------------ #
    # Market data — straight through to the real venue
    # ------------------------------------------------------------------ #

    def exchange_info(self, *, refresh: bool = False) -> dict[str, SymbolInfo]:
        self.calls["exchange_info"] += 1
        if refresh or self._exchange_info is None:
            self._exchange_info = self.inner.exchange_info(refresh=refresh)
        return dict(self._exchange_info)

    def daily_bars(
        self, symbol: str, start: date | None = None, end: date | None = None, limit: int = 1500
    ) -> list[DailyBar]:
        self.calls["daily_bars"] += 1
        return self.inner.daily_bars(symbol, start, end, limit)

    def book_ticker(self, symbol: str) -> BookTicker:
        self.calls["book_ticker"] += 1
        return self.inner.book_ticker(symbol)

    def mark_price(self, symbol: str) -> float:
        self.calls["mark_price"] += 1
        return self.inner.mark_price(symbol)

    def predicted_funding(self, symbol: str) -> FundingRate:
        self.calls["predicted_funding"] += 1
        return self.inner.predicted_funding(symbol)

    def funding_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[FundingRate]:
        self.calls["funding_history"] += 1
        return self.inner.funding_history(symbol, start_ms, end_ms)

    def server_time_ms(self) -> int:
        self.calls["server_time_ms"] += 1
        return self.inner.server_time_ms()

    # ------------------------------------------------------------------ #
    # Account — local simulation
    # ------------------------------------------------------------------ #

    def account(self) -> AccountState:
        self.calls["account"] += 1
        self._refresh_marks()
        return self.sim.account_state(self.clock.now_ms())

    def positions(self) -> dict[str, Position]:
        self.calls["positions"] += 1
        self._refresh_marks()
        return self.sim.positions_snapshot(self.clock.now_ms())

    def income(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        self.calls["income"] += 1
        return self.sim.income_rows(start_ms, end_ms, limit)

    def commission_rate(self, symbol: str) -> tuple[float, float]:
        """The real account's fees when the venue will say, config fallbacks otherwise."""
        self.calls["commission_rate"] += 1
        cached = self._commission_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            rates = self.inner.commission_rate(symbol)
        except GatewayError:
            rates = (self.cfg.exec.maker_fee_fallback, self.cfg.exec.taker_fee_fallback)
        self._commission_cache[symbol] = rates
        self.sim.set_commission(rates[0], rates[1], symbol)
        return rates

    def bnb_balance(self) -> float:
        self.calls["bnb_balance"] += 1
        return self.inner.bnb_balance()

    def key_permissions(self) -> dict[str, bool]:
        self.calls["key_permissions"] += 1
        return self.inner.key_permissions()

    def sub_account_name(self) -> str | None:
        self.calls["sub_account_name"] += 1
        return self.inner.sub_account_name()

    # ------------------------------------------------------------------ #
    # Trading — recorded locally, never forwarded
    # ------------------------------------------------------------------ #

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls["set_leverage"] += 1
        self.leverage[symbol] = leverage

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.calls["set_margin_type"] += 1
        self.margin_type[symbol] = margin_type

    def place_order(self, request: OrderRequest) -> Order:
        self.calls["place_order"] += 1
        info = self.exchange_info().get(request.symbol)
        book = self._book_or_none(request.symbol)
        validate_order(
            request,
            info,
            position_qty=self.sim.position_qty(request.symbol),
            book=book,
            reference_price=book.mid if book is not None else None,
        )
        taker = self._is_taker(request, book)
        if taker and book is None:
            raise GatewayError(f"paper taker order on {request.symbol} needs a book")
        now = self.clock.now_ms()
        order_id = self.books.next_id()
        order = Order(
            order_id=order_id,
            client_order_id=request.client_order_id or f"paper-{order_id}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            qty=request.qty,
            price=request.price,
            time_in_force=request.time_in_force,
            reduce_only=request.reduce_only,
            status=OrderStatus.NEW,
            created_ts_ms=now,
            updated_ts_ms=now,
            strategy=request.strategy,
            rebalance_id=request.rebalance_id,
            slice_id=request.slice_id,
            intent=request.intent,
        )
        self.books.add(order)
        if taker and book is not None:  # a taker without a book was refused above
            self._fill(order, self.taker_price(request.symbol, request.side, book), is_maker=False)
        return self.books.get(order_id)

    def cancel_order(self, symbol: str, order_id: str) -> Order:
        self.calls["cancel_order"] += 1
        order = self.books.get(order_id)
        if order.symbol != symbol:
            raise OrderRejected(f"order {order_id} is not on {symbol}", CODE_UNKNOWN_ORDER)
        if order.status.is_terminal:
            raise OrderRejected(f"order {order_id} is already {order.status}", CODE_UNKNOWN_ORDER)
        cancelled = replace_order(order, status=OrderStatus.CANCELED, updated_ts_ms=self.clock.now_ms())
        self.books.add(cancelled)
        return cancelled

    def cancel_all(self, symbol: str) -> None:
        self.calls["cancel_all"] += 1
        for order in self.books.open(symbol):
            self.books.add(
                replace_order(order, status=OrderStatus.CANCELED, updated_ts_ms=self.clock.now_ms())
            )

    def get_order(self, symbol: str, order_id: str) -> Order:
        self.calls["get_order"] += 1
        order = self.books.get(order_id)
        if order.symbol != symbol:
            raise OrderRejected(f"order {order_id} is not on {symbol}", CODE_UNKNOWN_ORDER)
        return order

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        self.calls["open_orders"] += 1
        return self.books.open(symbol)

    def user_trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000) -> list[Fill]:
        self.calls["user_trades"] += 1
        return self.sim.trades(symbol, start_ms, limit)

    # ------------------------------------------------------------------ #
    # Lifecycle — poll() is what advances the simulation
    # ------------------------------------------------------------------ #

    def poll(self) -> None:
        self.calls["poll"] += 1
        self._poll_resting_orders()
        self._poll_funding()

    def close(self) -> None:
        self.calls["close"] += 1
        self.closed = True
        self.inner.close()

    # ------------------------------------------------------------------ #
    # The fill model
    # ------------------------------------------------------------------ #

    def slippage_bps(self, symbol: str) -> float:
        return self.cfg.exec.slippage_for(symbol)

    def taker_price(self, symbol: str, side: Side, book: BookTicker) -> float:
        """Aggressive price moved against us by the symbol's slippage budget."""
        edge = self.slippage_bps(symbol) / _BPS
        base = book.aggressive_price(side)
        return base * (1.0 + edge) if side is Side.BUY else base * (1.0 - edge)

    @staticmethod
    def _is_taker(request: OrderRequest, book: BookTicker | None) -> bool:
        """Only a passive limit can rest; everything else crosses at placement.

        A limit priced through the touch is filled by the venue immediately, as
        a taker — it must not be allowed to rest and then be reported as a maker
        fill at its own (worse than market) price, because the maker ratio and
        the realised slippage are P1 gate measurements (Section 7).
        """
        if request.order_type is OrderType.MARKET or request.time_in_force in (
            TimeInForce.IOC,
            TimeInForce.FOK,
        ):
            return True
        # GTX never reaches here: validate_order rejects a crossing post-only.
        return (
            request.price is not None and book is not None and crosses_book(request.side, request.price, book)
        )

    def _poll_resting_orders(self) -> None:
        for order in list(self.books.open()):
            book = self._book_or_none(order.symbol)
            # A resting order always carries a limit price (market orders are
            # taker-filled at placement), but a book can be missing.
            if order.price is None or book is None:
                continue
            traded_through = (
                book.ask_price > 0 and book.ask_price <= order.price
                if order.side is Side.BUY
                else book.bid_price > 0 and book.bid_price >= order.price
            )
            if not traded_through:
                continue
            qty = order.remaining_qty
            if order.reduce_only:
                qty = reduce_only_fill_qty(self.sim.position_qty(order.symbol), order.side, qty)
                if qty <= 0.0:
                    # The position it was protecting is gone: the venue cancels
                    # the order rather than letting it open one (5.9 step 4).
                    self.books.add(
                        replace_order(order, status=OrderStatus.CANCELED, updated_ts_ms=self.clock.now_ms())
                    )
                    continue
            self._fill(order, order.price, qty=qty, is_maker=True)

    def _poll_funding(self) -> None:
        for symbol, pos in list(self.sim.positions.items()):
            if pos.qty == 0.0:
                continue
            last = self._last_funding_ms.get(symbol, 0)
            for settlement in self.inner.funding_history(symbol, start_ms=last + 1):
                if settlement.funding_time_ms <= last:
                    continue
                mark = self.inner.mark_price(symbol)
                self.sim.set_mark(symbol, mark)
                self.sim.settle_funding(symbol, settlement.rate, settlement.funding_time_ms, mark_price=mark)
                self._last_funding_ms[symbol] = settlement.funding_time_ms
                last = settlement.funding_time_ms

    def _fill(self, order: Order, price: float, *, is_maker: bool, qty: float | None = None) -> Order:
        qty = order.remaining_qty if qty is None else qty
        now = self.clock.now_ms()
        opened_flat = self.sim.position_qty(order.symbol) == 0.0
        maker, taker = self.commission_rate(order.symbol)
        self.sim.set_commission(maker, taker, order.symbol)
        self.sim.apply_fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            ts_ms=now,
            is_maker=is_maker,
            order_id=order.order_id,
            strategy=order.strategy,
            rebalance_id=order.rebalance_id,
            slice_id=order.slice_id,
        )
        if opened_flat:
            # Only settlements from here on belong to us. This must overwrite an
            # older watermark, not defer to it: settlements that fell while the
            # book was flat are nobody's, and charging them to the new position
            # would put funding in the ledger that the account never paid.
            self._last_funding_ms[order.symbol] = now
        updated = apply_fill_to_order(order, qty, price, now)
        self.books.add(updated)
        return updated

    def _book_or_none(self, symbol: str) -> BookTicker | None:
        try:
            return self.inner.book_ticker(symbol)
        except GatewayError:
            return None

    def _refresh_marks(self) -> None:
        for symbol, pos in self.sim.positions.items():
            if pos.qty != 0.0:
                self.sim.set_mark(symbol, self.inner.mark_price(symbol))


__all__ = ["PaperGateway"]
