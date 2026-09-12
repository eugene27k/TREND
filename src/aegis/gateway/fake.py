"""A programmable in-memory venue.

``FakeGateway`` is the venue every other module's tests run against, so it is
deliberately two things at once:

* **Programmable** — ``set_book``, ``set_bars``, ``set_position`` and friends put
  the venue into whatever state a test needs, and ``fill_order`` lets the test
  drive execution one print at a time. Nothing happens on its own: ``poll()``
  advances no simulation and the clock is external, so a test that does not move
  time sees a frozen world.
* **Strict** — it refuses exactly what Binance refuses. A reduce-only order that
  would flip a position is rejected with ``-2022`` rather than silently
  truncated, a post-only order that would cross is rejected with ``-5022``, and
  off-grid or sub-minimum quantities are rejected. Tests that assert the engine
  handles a rejection are only meaningful if the fake actually produces it
  (US-T10 AC 2).

Bookkeeping (positions, entry prices, realised P&L, fees, income) is delegated
to ``aegis.gateway.simulation.SimAccount`` so that it is identical to paper mode.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from aegis.core.clock import Clock, day_start_ms
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
    SymbolInfo,
    TimeInForce,
)
from aegis.gateway.simulation import (
    CODE_UNKNOWN_ORDER,
    OrderBookState,
    SimAccount,
    apply_fill_to_order,
    crosses_book,
    replace_order,
    validate_order,
)

FillHook = Callable[["FakeGateway", Order], None]

_QTY_TOL = 1e-9

#: Liberal defaults so a test that only cares about prices need not spell out a
#: whole ``SymbolInfo``; anything that matters to the test is passed explicitly.
_SYMBOL_DEFAULTS: dict[str, Any] = {
    "quote_asset": "USDT",
    "status": "TRADING",
    "contract_type": "PERPETUAL",
    "tick_size": 0.01,
    "step_size": 0.001,
    "min_qty": 0.001,
    "min_notional": 5.0,
    "price_precision": 2,
    "quantity_precision": 3,
}


class FakeGateway:
    """An ``ExchangeGateway`` whose every answer a test writes in advance."""

    def __init__(
        self,
        clock: Clock,
        *,
        wallet_balance: float = 10_000.0,
        fill_policy: str = "none",
        fill_hook: FillHook | None = None,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.0005,
        leverage: int = 5,
        sub_account_name: str = "trend-01",
    ) -> None:
        self.clock = clock
        self.sim = SimAccount(
            wallet_balance=wallet_balance, maker_fee=maker_fee, taker_fee=taker_fee, leverage=float(leverage)
        )
        self.books = OrderBookState()
        self.calls: Counter[str] = Counter()
        self.placed: list[OrderRequest] = []
        self.leverage: dict[str, int] = {}
        self.margin_type: dict[str, str] = {}
        self.closed = False
        self.exchange_info_refreshes = 0

        self._symbols: dict[str, SymbolInfo] = {}
        self._books: dict[str, BookTicker] = {}
        self._bars: dict[str, list[DailyBar]] = {}
        self._funding: dict[str, list[FundingRate]] = {}
        self._predicted: dict[str, FundingRate] = {}
        self._account_overrides: dict[str, float] = {}
        self._permissions = {"futures": True, "withdraw": False, "ip_restricted": True}
        self._sub_account: str | None = sub_account_name
        self._bnb = 1.0
        self._adl: dict[str, int] = {}
        self._liquidation: dict[str, float] = {}
        self._time_offset_ms = 0
        self._fill_policy = fill_policy
        self._fill_hook = fill_hook
        self._persistent_errors: dict[str, Exception] = {}
        self._one_shot_errors: dict[str, list[Exception]] = {}
        self.set_fill_policy(fill_policy, fill_hook)

    # ------------------------------------------------------------------ #
    # Programming the venue
    # ------------------------------------------------------------------ #

    def set_symbol_info(self, symbol: str | SymbolInfo, **overrides: Any) -> SymbolInfo:
        """Register (or amend) a symbol. Accepts a ready ``SymbolInfo`` or kwargs."""
        if isinstance(symbol, SymbolInfo):
            self._symbols[symbol.symbol] = symbol
            return symbol
        existing = self._symbols.get(symbol)
        if existing is not None:
            fields = {f: getattr(existing, f) for f in SymbolInfo.__slots__}
        else:
            fields = {
                "symbol": symbol,
                "base_asset": symbol.removesuffix("USDT") or symbol,
                **_SYMBOL_DEFAULTS,
            }
        fields.update(overrides)
        info = SymbolInfo(**fields)
        self._symbols[info.symbol] = info
        return info

    def set_book(
        self,
        symbol: str,
        bid: float,
        ask: float,
        *,
        bid_qty: float = 1_000.0,
        ask_qty: float = 1_000.0,
        ts_ms: int | None = None,
        mark: float | None = None,
    ) -> BookTicker:
        """Set the best bid/ask. The mark follows the mid unless given explicitly."""
        book = BookTicker(
            symbol=symbol,
            bid_price=bid,
            bid_qty=bid_qty,
            ask_price=ask,
            ask_qty=ask_qty,
            ts_ms=ts_ms if ts_ms is not None else self.clock.now_ms(),
        )
        self._books[symbol] = book
        self.sim.set_mark(symbol, mark if mark is not None else book.mid)
        return book

    def set_mark(self, symbol: str, price: float) -> None:
        self.sim.set_mark(symbol, price)

    def set_bars(self, symbol: str, bars: Iterable[DailyBar]) -> list[DailyBar]:
        rows = sorted(bars, key=lambda b: b.day)
        self._bars[symbol] = rows
        return rows

    def set_closes(
        self,
        symbol: str,
        closes: Sequence[float],
        *,
        start_day: date,
        quote_volume: float = 50_000_000.0,
    ) -> list[DailyBar]:
        """Convenience: a flat OHLC series from a list of closes, one bar per day."""
        bars = []
        for i, close in enumerate(closes):
            day = start_day + timedelta(days=i)
            open_ms = day_start_ms(day)
            bars.append(
                DailyBar(
                    symbol=symbol,
                    day=day,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=quote_volume / close if close else 0.0,
                    quote_volume=quote_volume,
                    open_time_ms=open_ms,
                    close_time_ms=open_ms + 86_399_999,
                )
            )
        return self.set_bars(symbol, bars)

    def set_position(
        self,
        symbol: str,
        qty: float,
        entry_price: float = 0.0,
        *,
        mark_price: float | None = None,
        leverage: float | None = None,
    ) -> Position:
        """Seed a position directly — no fills, no fees, no income rows."""
        self.sim.set_position(symbol, qty, entry_price, mark_price=mark_price, leverage=leverage)
        return self.sim.positions_snapshot(self.clock.now_ms())[symbol]

    def set_account(self, **overrides: float) -> AccountState:
        """Pin ``AccountState`` fields. ``wallet_balance`` also moves the sim wallet.

        Pinned fields stay pinned: the point is to let a test state an equity or a
        margin ratio outright rather than engineer positions that produce it.
        """
        if "wallet_balance" in overrides:
            self.sim.wallet_balance = float(overrides["wallet_balance"])
        self._account_overrides.update({k: float(v) for k, v in overrides.items()})
        return self.account()

    def set_funding(
        self, symbol: str, history: Iterable[FundingRate | tuple[int, float]]
    ) -> list[FundingRate]:
        rows: list[FundingRate] = []
        for item in history:
            if isinstance(item, FundingRate):
                rows.append(item)
            else:
                ts, rate = item
                rows.append(FundingRate(symbol=symbol, funding_time_ms=ts, rate=rate))
        rows.sort(key=lambda f: f.funding_time_ms)
        self._funding[symbol] = rows
        return rows

    def set_predicted_funding(self, symbol: str, rate: float, *, interval_hours: float = 8.0) -> FundingRate:
        fr = FundingRate(
            symbol=symbol, funding_time_ms=self.clock.now_ms(), rate=rate, interval_hours=interval_hours
        )
        self._predicted[symbol] = fr
        return fr

    def set_commission(self, maker: float, taker: float, symbol: str | None = None) -> None:
        self.sim.set_commission(maker, taker, symbol)

    def set_permissions(
        self, *, futures: bool = True, withdraw: bool = False, ip_restricted: bool = True
    ) -> dict[str, bool]:
        self._permissions = {"futures": futures, "withdraw": withdraw, "ip_restricted": ip_restricted}
        return dict(self._permissions)

    def set_sub_account_name(self, name: str | None) -> None:
        self._sub_account = name

    def set_bnb_balance(self, amount: float) -> None:
        self._bnb = amount

    def set_adl_quantile(self, symbol: str, quantile: int) -> None:
        """ADL quantile is venue metadata, not P&L state, so it overlays the sim."""
        self._adl[symbol] = int(quantile)

    def set_liquidation_price(self, symbol: str, price: float) -> None:
        self._liquidation[symbol] = float(price)

    def set_server_time_offset_ms(self, offset_ms: int) -> None:
        """Make the venue clock disagree with ours — for the drift check."""
        self._time_offset_ms = offset_ms

    def set_fill_policy(self, policy: str, hook: FillHook | None = None) -> None:
        if policy not in ("none", "immediate", "callable"):
            raise GatewayError(f"unknown fill policy {policy!r}")
        if policy == "callable" and hook is None and self._fill_hook is None:
            raise GatewayError("fill_policy='callable' needs a hook")
        self._fill_policy = policy
        if hook is not None:
            self._fill_hook = hook

    # ------------------------------------------------------------------ #
    # Chaos injection
    # ------------------------------------------------------------------ #

    def inject_error(self, method_name: str, exc: Exception) -> None:
        """Raise ``exc`` from ``method_name`` on every call until cleared."""
        self._persistent_errors[method_name] = exc

    def fail_next(self, method: str, exc: Exception, times: int = 1) -> None:
        """Raise ``exc`` from the next ``times`` calls of ``method``, then recover."""
        self._one_shot_errors.setdefault(method, []).extend([exc] * times)

    def clear_errors(self, method: str | None = None) -> None:
        if method is None:
            self._persistent_errors.clear()
            self._one_shot_errors.clear()
            return
        self._persistent_errors.pop(method, None)
        self._one_shot_errors.pop(method, None)

    def _check(self, method: str) -> None:
        self.calls[method] += 1
        queued = self._one_shot_errors.get(method)
        if queued:
            raise queued.pop(0)
        persistent = self._persistent_errors.get(method)
        if persistent is not None:
            raise persistent

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def exchange_info(self, *, refresh: bool = False) -> dict[str, SymbolInfo]:
        self._check("exchange_info")
        if refresh:
            self.exchange_info_refreshes += 1
        return dict(self._symbols)

    def daily_bars(
        self, symbol: str, start: date | None = None, end: date | None = None, limit: int = 1500
    ) -> list[DailyBar]:
        self._check("daily_bars")
        rows = [
            b
            for b in self._bars.get(symbol, [])
            if (start is None or b.day >= start) and (end is None or b.day <= end)
        ]
        return rows[-limit:] if limit and len(rows) > limit else rows

    def book_ticker(self, symbol: str) -> BookTicker:
        self._check("book_ticker")
        book = self._books.get(symbol)
        if book is None:
            raise GatewayError(f"no book configured for {symbol}")
        return book

    def mark_price(self, symbol: str) -> float:
        self._check("mark_price")
        mark = self.sim.marks.get(symbol)
        if mark is not None:
            return mark
        book = self._books.get(symbol)
        return book.mid if book is not None else 0.0

    def predicted_funding(self, symbol: str) -> FundingRate:
        self._check("predicted_funding")
        fr = self._predicted.get(symbol)
        if fr is not None:
            return fr
        return FundingRate(symbol=symbol, funding_time_ms=self.clock.now_ms(), rate=0.0)

    def funding_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[FundingRate]:
        self._check("funding_history")
        return [
            f
            for f in self._funding.get(symbol, [])
            if (start_ms is None or f.funding_time_ms >= start_ms)
            and (end_ms is None or f.funding_time_ms <= end_ms)
        ]

    def server_time_ms(self) -> int:
        self._check("server_time_ms")
        return self.clock.now_ms() + self._time_offset_ms

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def account(self) -> AccountState:
        self._check("account")
        state = self.sim.account_state(self.clock.now_ms())
        return replace(state, **self._account_overrides) if self._account_overrides else state

    def positions(self) -> dict[str, Position]:
        self._check("positions")
        snapshot = self.sim.positions_snapshot(self.clock.now_ms())
        if not self._adl and not self._liquidation:
            return snapshot
        return {
            s: replace(p, adl_quantile=self._adl.get(s, p.adl_quantile),
                       liquidation_price=self._liquidation.get(s, p.liquidation_price))
            for s, p in snapshot.items()
        }

    def income(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        self._check("income")
        return self.sim.income_rows(start_ms, end_ms, limit)

    def commission_rate(self, symbol: str) -> tuple[float, float]:
        self._check("commission_rate")
        return self.sim.commission(symbol)

    def bnb_balance(self) -> float:
        self._check("bnb_balance")
        return self._bnb

    def key_permissions(self) -> dict[str, bool]:
        self._check("key_permissions")
        return dict(self._permissions)

    def sub_account_name(self) -> str | None:
        self._check("sub_account_name")
        return self._sub_account

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._check("set_leverage")
        self.leverage[symbol] = leverage

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self._check("set_margin_type")
        self.margin_type[symbol] = margin_type

    def place_order(self, request: OrderRequest) -> Order:
        self._check("place_order")
        info = self._symbols.get(request.symbol)
        book = self._books.get(request.symbol)
        validate_order(
            request,
            info,
            position_qty=self.sim.position_qty(request.symbol),
            book=book,
            reference_price=self._reference_price(request.symbol),
        )
        self.placed.append(request)
        now = self.clock.now_ms()
        order_id = self.books.next_id()
        order = Order(
            order_id=order_id,
            client_order_id=request.client_order_id or f"fake-{order_id}",
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
        self._apply_fill_policy(order)
        return self.books.get(order_id)

    def fill_order(
        self,
        order_id: str,
        qty: float | None = None,
        price: float | None = None,
        *,
        is_maker: bool = True,
        ts_ms: int | None = None,
    ) -> Order:
        """Fill a resting order by hand — the main lever a test pulls.

        ``qty`` defaults to the remainder and ``price`` to the order's own limit
        (or the aggressive book price for a market order).
        """
        order = self.books.get(order_id)
        if order.status.is_terminal:
            raise GatewayError(f"order {order_id} is {order.status}, cannot fill")
        fill_qty = order.remaining_qty if qty is None else qty
        if fill_qty <= 0:
            raise GatewayError(f"fill qty must be positive, got {fill_qty}")
        if fill_qty > order.remaining_qty + _QTY_TOL:
            raise GatewayError(f"fill qty {fill_qty} exceeds remaining {order.remaining_qty}")
        fill_price = price if price is not None else self._fill_price(order)
        if fill_price <= 0:
            raise GatewayError(f"no price available to fill {order_id}")
        now = ts_ms if ts_ms is not None else self.clock.now_ms()
        self.sim.apply_fill(
            symbol=order.symbol,
            side=order.side,
            qty=fill_qty,
            price=fill_price,
            ts_ms=now,
            is_maker=is_maker,
            order_id=order.order_id,
            strategy=order.strategy,
            rebalance_id=order.rebalance_id,
            slice_id=order.slice_id,
        )
        updated = apply_fill_to_order(order, fill_qty, fill_price, now)
        self.books.add(updated)
        return updated

    def cancel_order(self, symbol: str, order_id: str) -> Order:
        self._check("cancel_order")
        order = self.books.get(order_id)
        if order.symbol != symbol:
            raise OrderRejected(f"order {order_id} is not on {symbol}", CODE_UNKNOWN_ORDER)
        if order.status.is_terminal:
            raise OrderRejected(f"order {order_id} is already {order.status}", CODE_UNKNOWN_ORDER)
        cancelled = replace_order(order, status=OrderStatus.CANCELED, updated_ts_ms=self.clock.now_ms())
        self.books.add(cancelled)
        return cancelled

    def cancel_all(self, symbol: str) -> None:
        self._check("cancel_all")
        for order in self.books.open(symbol):
            self.books.add(
                replace_order(order, status=OrderStatus.CANCELED, updated_ts_ms=self.clock.now_ms())
            )

    def get_order(self, symbol: str, order_id: str) -> Order:
        self._check("get_order")
        order = self.books.get(order_id)
        if order.symbol != symbol:
            raise OrderRejected(f"order {order_id} is not on {symbol}", CODE_UNKNOWN_ORDER)
        return order

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        self._check("open_orders")
        return self.books.open(symbol)

    def user_trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000) -> list[Fill]:
        self._check("user_trades")
        return self.sim.trades(symbol, start_ms, limit)

    # ------------------------------------------------------------------ #
    # Funding
    # ------------------------------------------------------------------ #

    def settle_funding(self, symbol: str, rate: float, ts_ms: int | None = None) -> float:
        """Settle funding against the held position and record it in history.

        ``-rate x notional``: a long pays when the rate is positive.
        """
        now = ts_ms if ts_ms is not None else self.clock.now_ms()
        interval = self._predicted.get(symbol)
        self._funding.setdefault(symbol, []).append(
            FundingRate(
                symbol=symbol,
                funding_time_ms=now,
                rate=rate,
                interval_hours=interval.interval_hours if interval is not None else 8.0,
            )
        )
        return self.sim.settle_funding(symbol, rate, now)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def poll(self) -> None:
        """No-op: the fake venue only moves when a test moves it."""
        self._check("poll")

    def close(self) -> None:
        self._check("close")
        self.closed = True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _reference_price(self, symbol: str) -> float | None:
        book = self._books.get(symbol)
        if book is not None:
            return book.mid
        return self.sim.marks.get(symbol)

    def _fill_price(self, order: Order) -> float:
        if order.price is not None:
            return order.price
        book = self._books.get(order.symbol)
        if book is not None:
            return book.aggressive_price(order.side)
        return self.sim.marks.get(order.symbol, 0.0)

    def _apply_fill_policy(self, order: Order) -> None:
        if self._fill_policy == "none":
            return
        if self._fill_policy == "callable":
            assert self._fill_hook is not None  # guarded in set_fill_policy
            self._fill_hook(self, order)
            return
        self._fill_immediate(order)

    def _fill_immediate(self, order: Order) -> None:
        """Marketable orders fill fully at the book; passive ones rest."""
        book = self._books.get(order.symbol)
        if order.order_type is OrderType.MARKET:
            price = book.aggressive_price(order.side) if book is not None else self._fill_price(order)
            if price > 0:
                self.fill_order(order.order_id, price=price, is_maker=False)
            return
        if order.time_in_force is TimeInForce.GTX or order.price is None or book is None:
            return
        if crosses_book(order.side, order.price, book):
            self.fill_order(order.order_id, price=book.aggressive_price(order.side), is_maker=False)
        elif order.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            self.books.add(
                replace_order(order, status=OrderStatus.EXPIRED, updated_ts_ms=self.clock.now_ms())
            )


__all__ = ["FakeGateway", "FillHook"]
