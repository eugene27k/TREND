"""Position, fee and P&L bookkeeping shared by every simulated venue.

``FakeGateway`` (programmable, offline) and ``PaperGateway`` (live data, local
fills) disagree about where a price comes from and about when an order fills.
They must not disagree about what a fill *does*: the weighted-average entry
price, the realised P&L a reducing fill crystallises, the commission, the
wallet balance and the ``/fapi/v1/income`` rows that the ledger later reads back
are one model, implemented once, here. That is what lets an accounting test
reconcile against either gateway with the same arithmetic.

The venue's own order-admission rules live here too (``validate_order``) for the
same reason: a test that proves reduce-only cannot flip a position must be
proving it about the rule, not about one gateway's copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP

from aegis.core.errors import OrderRejected
from aegis.core.precision import round_step, slippage_bps
from aegis.core.types import (
    AccountState,
    BookTicker,
    Fill,
    IncomeType,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Strategy,
    SymbolInfo,
    TimeInForce,
)

#: Quantities and prices are floats; a grid check needs a tolerance that scales.
_GRID_TOL = 1e-9

#: Binance error codes we reproduce. -2022 and -5022 are load-bearing: the
#: executor branches on them (US-T10 AC 2) and ``OrderRejected`` classifies them.
CODE_REDUCE_ONLY_REJECTED = -2022
CODE_POST_ONLY_REJECTED = -5022
CODE_FILTER_FAILURE = -1013
CODE_PRECISION = -1111
CODE_UNKNOWN_SYMBOL = -1121
CODE_UNKNOWN_ORDER = -2013


def _off_grid(value: float, step: float) -> bool:
    if step <= 0:
        return False
    snapped = round_step(value, step, mode=ROUND_HALF_UP)
    return abs(snapped - value) > max(_GRID_TOL, abs(value) * _GRID_TOL)


def reduce_only_is_valid(position_qty: float, side: Side, qty: float) -> bool:
    """True when a reduce-only order strictly reduces and cannot flip the sign.

    Binance silently truncates an oversized reduce-only order on some paths and
    rejects it on others; the engine must never rely on the truncation, so both
    simulators always reject. A flat position has nothing to reduce.
    """
    if position_qty == 0.0:
        return False
    signed = qty if side is Side.BUY else -qty
    if position_qty > 0 and signed >= 0:
        return False
    if position_qty < 0 and signed <= 0:
        return False
    return qty <= abs(position_qty) + _GRID_TOL


def reduce_only_fill_qty(position_qty: float, side: Side, qty: float) -> float:
    """How much of a resting reduce-only order the venue would let fill *now*.

    A reduce-only order stays pegged to the position it protects: the venue
    truncates a fill to what is left to reduce and cancels the order once there
    is nothing, so it can never open or flip a position (5.9 step 4). Returns
    ``0.0`` when the order can no longer reduce anything.
    """
    if position_qty == 0.0:
        return 0.0
    signed = qty if side is Side.BUY else -qty
    if (position_qty > 0) == (signed > 0):
        return 0.0
    return min(qty, abs(position_qty))


def crosses_book(side: Side, price: float, book: BookTicker) -> bool:
    """True when a limit at ``price`` would take liquidity right now."""
    if side is Side.BUY:
        return book.ask_price > 0 and price >= book.ask_price
    return book.bid_price > 0 and price <= book.bid_price


def validate_order(
    request: OrderRequest,
    info: SymbolInfo | None,
    *,
    position_qty: float = 0.0,
    book: BookTicker | None = None,
    reference_price: float | None = None,
) -> None:
    """Raise ``OrderRejected`` for anything the venue would refuse.

    Ordered the way a venue orders it: the symbol must exist, the numbers must
    sit on the grid and clear the filters, and only then are the semantic flags
    (reduce-only, post-only) considered.
    """
    coid = request.client_order_id
    if info is None:
        raise OrderRejected(f"unknown symbol {request.symbol}", CODE_UNKNOWN_SYMBOL, client_order_id=coid)
    if request.qty <= 0:
        raise OrderRejected(f"qty must be positive, got {request.qty}", CODE_PRECISION, client_order_id=coid)
    if _off_grid(request.qty, info.step_size):
        raise OrderRejected(
            f"qty {request.qty} is not a multiple of stepSize {info.step_size}",
            CODE_PRECISION,
            client_order_id=coid,
        )
    if request.price is not None and _off_grid(request.price, info.tick_size):
        raise OrderRejected(
            f"price {request.price} is not a multiple of tickSize {info.tick_size}",
            CODE_PRECISION,
            client_order_id=coid,
        )
    if request.qty < info.min_qty - _GRID_TOL:
        raise OrderRejected(
            f"qty {request.qty} below minQty {info.min_qty}", CODE_FILTER_FAILURE, client_order_id=coid
        )
    px = request.price if request.price is not None else reference_price
    if px is not None and px > 0 and request.qty * px < info.min_notional - _GRID_TOL:
        raise OrderRejected(
            f"notional {request.qty * px} below minNotional {info.min_notional}",
            CODE_FILTER_FAILURE,
            client_order_id=coid,
        )
    if request.reduce_only and not reduce_only_is_valid(position_qty, request.side, request.qty):
        raise OrderRejected(
            f"reduce-only {request.side} {request.qty} against position {position_qty} would not reduce it",
            CODE_REDUCE_ONLY_REJECTED,
            client_order_id=coid,
        )
    if (
        request.time_in_force is TimeInForce.GTX
        and request.order_type is OrderType.LIMIT
        and request.price is not None
        and book is not None
        and crosses_book(request.side, request.price, book)
    ):
        raise OrderRejected(
            f"post-only {request.side} at {request.price} would cross {book.bid_price}/{book.ask_price}",
            CODE_POST_ONLY_REJECTED,
            client_order_id=coid,
        )


@dataclass
class SimPosition:
    """One-way position with the weighted-average entry the venue reports."""

    symbol: str
    qty: float = 0.0
    entry_price: float = 0.0
    leverage: float = 5.0

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0


@dataclass(frozen=True, slots=True)
class FillOutcome:
    """What one applied fill did — returned so callers need not diff the book."""

    fill: Fill
    realized_pnl: float
    fee: float
    position_qty: float
    entry_price: float
    opened: bool  # the position went from flat to non-flat


class SimAccount:
    """Wallet, positions, fills and income for a simulated sub-account.

    Every mutation goes through :meth:`apply_fill` or :meth:`settle_funding`, so
    the identity ``wallet = start + realised - fees + funding`` holds by
    construction and is asserted directly in the gateway tests.
    """

    def __init__(
        self,
        *,
        wallet_balance: float = 10_000.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.0005,
        leverage: float = 5.0,
        maint_margin_rate: float = 0.005,
    ) -> None:
        self.start_balance = wallet_balance
        self.wallet_balance = wallet_balance
        self.default_maker_fee = maker_fee
        self.default_taker_fee = taker_fee
        self.leverage = leverage
        self.maint_margin_rate = maint_margin_rate
        self.positions: dict[str, SimPosition] = {}
        self.marks: dict[str, float] = {}
        self.fills: list[Fill] = []
        self.income: list[dict] = []
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.funding_paid = 0.0
        self._commissions: dict[str, tuple[float, float]] = {}
        self._seq = 0

    # -- configuration ------------------------------------------------------ #

    def set_commission(self, maker: float, taker: float, symbol: str | None = None) -> None:
        """Per-symbol rates, or the account default when ``symbol`` is None."""
        if symbol is None:
            self.default_maker_fee, self.default_taker_fee = maker, taker
        else:
            self._commissions[symbol] = (maker, taker)

    def commission(self, symbol: str) -> tuple[float, float]:
        return self._commissions.get(symbol, (self.default_maker_fee, self.default_taker_fee))

    def set_mark(self, symbol: str, price: float) -> None:
        self.marks[symbol] = price

    def mark(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return self.marks.get(symbol, pos.entry_price if pos is not None else 0.0)

    def set_position(
        self,
        symbol: str,
        qty: float,
        entry_price: float = 0.0,
        *,
        mark_price: float | None = None,
        leverage: float | None = None,
    ) -> SimPosition:
        """Seed a position without generating fills, fees or income."""
        pos = SimPosition(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            leverage=leverage if leverage is not None else self.leverage,
        )
        self.positions[symbol] = pos
        self.marks[symbol] = (
            mark_price if mark_price is not None else (entry_price or self.marks.get(symbol, 0.0))
        )
        return pos

    def position(self, symbol: str) -> SimPosition:
        return self.positions.setdefault(symbol, SimPosition(symbol=symbol, leverage=self.leverage))

    def position_qty(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.qty if pos is not None else 0.0

    # -- derived views ------------------------------------------------------ #

    def unrealized_pnl(self) -> float:
        return sum(p.qty * (self.mark(s) - p.entry_price) for s, p in self.positions.items() if p.qty != 0.0)

    def gross_notional(self) -> float:
        return sum(abs(p.qty) * self.mark(s) for s, p in self.positions.items() if p.qty != 0.0)

    def positions_snapshot(self, ts_ms: int) -> dict[str, Position]:
        """Non-zero positions, exactly as ``ExchangeGateway.positions`` returns them."""
        out: dict[str, Position] = {}
        for symbol, pos in self.positions.items():
            if pos.qty == 0.0:
                continue
            mark = self.mark(symbol)
            out[symbol] = Position(
                symbol=symbol,
                qty=pos.qty,
                entry_price=pos.entry_price,
                mark_price=mark,
                unrealized_pnl=pos.qty * (mark - pos.entry_price),
                leverage=pos.leverage,
                ts_ms=ts_ms,
            )
        return out

    def account_state(self, ts_ms: int) -> AccountState:
        unreal = self.unrealized_pnl()
        gross = self.gross_notional()
        margin_balance = self.wallet_balance + unreal
        initial = gross / self.leverage if self.leverage > 0 else 0.0
        maint = gross * self.maint_margin_rate
        return AccountState(
            ts_ms=ts_ms,
            wallet_balance=self.wallet_balance,
            margin_balance=margin_balance,
            unrealized_pnl=unreal,
            available_balance=max(0.0, margin_balance - initial),
            maint_margin=maint,
            initial_margin=initial,
        )

    # -- mutation ----------------------------------------------------------- #

    def apply_fill(
        self,
        *,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        ts_ms: int,
        is_maker: bool = True,
        order_id: str = "",
        strategy: Strategy = Strategy.TREND,
        rebalance_id: str | None = None,
        slice_id: str | None = None,
        decision_mid: float | None = None,
    ) -> FillOutcome:
        """Book one trade print: position, entry price, realised P&L, fee, income."""
        pos = self.position(symbol)
        before = pos.qty
        signed = qty if side is Side.BUY else -qty
        maker_rate, taker_rate = self.commission(symbol)
        fee = abs(qty) * price * (maker_rate if is_maker else taker_rate)

        realized = 0.0
        if before == 0.0 or (before > 0) == (signed > 0):
            # Opening or adding — weighted-average the entry price.
            total = abs(before) + abs(signed)
            pos.entry_price = (
                (abs(before) * pos.entry_price + abs(signed) * price) / total if total else price
            )
            pos.qty = before + signed
        else:
            closed = min(abs(signed), abs(before))
            direction = 1.0 if before > 0 else -1.0
            realized = closed * (price - pos.entry_price) * direction
            pos.qty = before + signed
            if abs(signed) > abs(before):
                # Flip: the residual opens a fresh position at the fill price.
                pos.entry_price = price
            elif pos.qty == 0.0:
                pos.entry_price = 0.0

        self.marks.setdefault(symbol, price)
        self.realized_pnl += realized
        self.fees_paid += fee
        self.wallet_balance += realized - fee

        self._seq += 1
        trade_id = f"T{self._seq}"
        fill = Fill(
            trade_id=trade_id,
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            fee=fee,
            fee_asset="USDT",
            is_maker=is_maker,
            ts_ms=ts_ms,
            realized_pnl=realized,
            strategy=strategy,
            rebalance_id=rebalance_id,
            slice_id=slice_id,
            decision_mid=decision_mid,
            slippage_bps=slippage_bps(price, decision_mid, side) if decision_mid else 0.0,
        )
        self.fills.append(fill)

        if realized != 0.0 or (before != 0.0 and (before > 0) != (signed > 0)):
            self._add_income(IncomeType.REALIZED_PNL, realized, symbol, ts_ms, trade_id=trade_id)
        self._add_income(IncomeType.COMMISSION, -fee, symbol, ts_ms, trade_id=trade_id)

        return FillOutcome(
            fill=fill,
            realized_pnl=realized,
            fee=fee,
            position_qty=pos.qty,
            entry_price=pos.entry_price,
            opened=before == 0.0 and pos.qty != 0.0,
        )

    def settle_funding(
        self, symbol: str, rate: float, ts_ms: int, *, mark_price: float | None = None
    ) -> float:
        """Book one funding settlement against the held position.

        Sign convention (Section 5.6): the payment is ``-rate x notional``, so a
        **long** pays when the rate is positive and receives when it is
        negative; a short is the mirror. Returns the signed cash amount.
        """
        qty = self.position_qty(symbol)
        if qty == 0.0 or rate == 0.0:
            return 0.0
        mark = mark_price if mark_price is not None else self.mark(symbol)
        amount = -rate * qty * mark
        self.wallet_balance += amount
        self.funding_paid += amount
        self._add_income(IncomeType.FUNDING_FEE, amount, symbol, ts_ms)
        return amount

    def transfer(self, amount: float, ts_ms: int) -> None:
        """Capital in/out — the only wallet change that is not P&L."""
        self.wallet_balance += amount
        self._add_income(IncomeType.TRANSFER, amount, None, ts_ms)

    def _add_income(
        self,
        income_type: IncomeType,
        amount: float,
        symbol: str | None,
        ts_ms: int,
        *,
        trade_id: str | None = None,
    ) -> dict:
        self._seq += 1
        row = {
            "symbol": symbol or "",
            "incomeType": str(income_type),
            "income": f"{amount:.8f}",
            "asset": "USDT",
            "time": int(ts_ms),
            "info": "",
            "tranId": str(self._seq),
            "tradeId": trade_id or "",
        }
        self.income.append(row)
        return row

    # -- read-back ---------------------------------------------------------- #

    def income_rows(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        rows = [r for r in self.income if r["time"] >= start_ms and (end_ms is None or r["time"] <= end_ms)]
        rows.sort(key=lambda r: (r["time"], int(r["tranId"])))
        return rows[:limit]

    def trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000) -> list[Fill]:
        rows = [f for f in self.fills if f.symbol == symbol and (start_ms is None or f.ts_ms >= start_ms)]
        rows.sort(key=lambda f: (f.ts_ms, f.trade_id))
        return rows[:limit]


def apply_fill_to_order(order: Order, qty: float, price: float, ts_ms: int) -> Order:
    """Return ``order`` advanced by one fill, FILLED at exactly the full quantity."""
    filled = order.filled_qty + qty
    notional = order.avg_price * order.filled_qty + price * qty
    avg = notional / filled if filled else 0.0
    if filled >= order.qty - _GRID_TOL:
        filled, status = order.qty, OrderStatus.FILLED
    else:
        status = OrderStatus.PARTIALLY_FILLED
    return replace_order(order, filled_qty=filled, avg_price=avg, status=status, updated_ts_ms=ts_ms)


def replace_order(order: Order, **changes: object) -> Order:
    """``dataclasses.replace`` for the frozen ``Order``, typed for callers."""
    return replace(order, **changes)  # type: ignore[arg-type]


@dataclass
class OrderBookState:
    """The resting-order table both simulators keep."""

    orders: dict[str, Order] = field(default_factory=dict)
    _seq: int = 0

    def next_id(self) -> str:
        self._seq += 1
        return str(100_000 + self._seq)

    def add(self, order: Order) -> Order:
        self.orders[order.order_id] = order
        return order

    def get(self, order_id: str) -> Order:
        try:
            return self.orders[order_id]
        except KeyError:
            raise OrderRejected(f"unknown order {order_id}", CODE_UNKNOWN_ORDER) from None

    def open(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if not o.status.is_terminal and (symbol is None or o.symbol == symbol)
        ]


__all__ = [
    "CODE_FILTER_FAILURE",
    "CODE_POST_ONLY_REJECTED",
    "CODE_PRECISION",
    "CODE_REDUCE_ONLY_REJECTED",
    "CODE_UNKNOWN_ORDER",
    "CODE_UNKNOWN_SYMBOL",
    "FillOutcome",
    "OrderBookState",
    "SimAccount",
    "SimPosition",
    "apply_fill_to_order",
    "crosses_book",
    "reduce_only_fill_qty",
    "reduce_only_is_valid",
    "replace_order",
    "validate_order",
]
