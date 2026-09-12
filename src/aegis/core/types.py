"""Shared value types for the Aegis engine.

Every module in the repository speaks these types. They are deliberately plain
(frozen dataclasses + str enums) so that they serialise to SQLite and JSON
without adapters, and so that pure strategy functions stay free of I/O.

Money and notional values are USDT floats. Order quantities and prices are
floats in the domain layer and are only converted to exchange-precision strings
at the gateway boundary (``aegis.core.precision``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Strategy(StrEnum):
    """Sleeve identifier. Stamped on every shared-layer row (US-T01 AC 2)."""

    CARRY = "CARRY"
    TREND = "TREND"


class Mode(StrEnum):
    """Execution mode (Locked Decision: modes backtest/paper/demo/live)."""

    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"

    @property
    def sends_real_orders(self) -> bool:
        """True when orders reach an exchange (demo = Binance testnet, live = mainnet)."""
        return self in (Mode.DEMO, Mode.LIVE)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"  # post-only ("good till crossing") — rejected if it would take


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


class EngineState(StrEnum):
    """Per-strategy state machine (Section 5.11)."""

    IDLE = "IDLE"
    COMPUTING = "COMPUTING"
    REBALANCING = "REBALANCING"
    RISK_ACTION = "RISK_ACTION"
    HALTED_RISK = "HALTED_RISK"
    SAFE_MODE = "SAFE_MODE"
    STOPPED = "STOPPED"


class Phase(StrEnum):
    """Capital phase gates (Section 7)."""

    P0_BACKTEST = "P0_BACKTEST"
    P1_PAPER = "P1_PAPER"
    P2_MICRO_LIVE = "P2_MICRO_LIVE"
    P3_SCALED = "P3_SCALED"


class Severity(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class RiskStatus(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class SliceOutcome(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RebalanceStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETE = "complete"
    WINDOW_END = "window_end"
    ABORTED = "aborted"


class IncomeType(StrEnum):
    """Binance ``/fapi/v1/income`` incomeType values that we book."""

    REALIZED_PNL = "REALIZED_PNL"
    FUNDING_FEE = "FUNDING_FEE"
    COMMISSION = "COMMISSION"
    TRANSFER = "TRANSFER"
    INSURANCE_CLEAR = "INSURANCE_CLEAR"
    REFERRAL_KICKBACK = "REFERRAL_KICKBACK"
    COMMISSION_REBATE = "COMMISSION_REBATE"
    WELCOME_BONUS = "WELCOME_BONUS"
    CROSS_COLLATERAL_TRANSFER = "CROSS_COLLATERAL_TRANSFER"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    COIN_SWAP_DEPOSIT = "COIN_SWAP_DEPOSIT"
    COIN_SWAP_WITHDRAW = "COIN_SWAP_WITHDRAW"
    POSITION_LIMIT_INCREASE_FEE = "POSITION_LIMIT_INCREASE_FEE"
    OTHER = "OTHER"

    @classmethod
    def parse(cls, raw: str) -> IncomeType:
        try:
            return cls(raw)
        except ValueError:
            return cls.OTHER

    @property
    def is_transfer(self) -> bool:
        """Capital in/out — excluded from P&L, subtracted in the ledger identity."""
        return self in (
            IncomeType.TRANSFER,
            IncomeType.INTERNAL_TRANSFER,
            IncomeType.CROSS_COLLATERAL_TRANSFER,
        )


# --------------------------------------------------------------------------- #
# Market / instrument data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """Static instrument metadata from ``exchangeInfo`` plus the account's fees."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str  # TRADING, SETTLING, PRE_DELIVERING, DELIVERING, CLOSE, ...
    contract_type: str  # PERPETUAL, CURRENT_QUARTER, ...
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float
    price_precision: int
    quantity_precision: int
    onboard_date_ms: int = 0
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    funding_interval_hours: float = 8.0

    @property
    def is_tradeable_perp(self) -> bool:
        return self.status == "TRADING" and self.contract_type == "PERPETUAL"


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One 00:00 UTC daily kline of a perpetual."""

    symbol: str
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    open_time_ms: int
    close_time_ms: int
    source: str = "rest"  # rest | archive | backfill | synthetic
    filled: bool = False  # forward-filled placeholder (signal use only, never P&L)


@dataclass(frozen=True, slots=True)
class BookTicker:
    """Best bid/ask snapshot used for passive pegging and decision mids."""

    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    ts_ms: int

    @property
    def mid(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return 0.0 if m <= 0 else (self.ask_price - self.bid_price) / m * 10_000.0

    def passive_price(self, side: Side) -> float:
        """Best price on our own side of the book (post-only peg)."""
        return self.bid_price if side is Side.BUY else self.ask_price

    def aggressive_price(self, side: Side) -> float:
        """Best price on the other side of the book (IOC taker)."""
        return self.ask_price if side is Side.BUY else self.bid_price


@dataclass(frozen=True, slots=True)
class FundingRate:
    """One realised funding settlement, or the current predicted rate."""

    symbol: str
    funding_time_ms: int
    rate: float
    interval_hours: float = 8.0

    def annualised(self) -> float:
        """Annualised funding (Section 5.6): ``rate x 8760 / interval_hours``."""
        if self.interval_hours <= 0:
            return 0.0
        return self.rate * (8760.0 / self.interval_hours)


# --------------------------------------------------------------------------- #
# Account / positions / orders
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AccountState:
    """Sub-account snapshot from ``/fapi/v2/account`` (USDT single-asset mode)."""

    ts_ms: int
    wallet_balance: float
    margin_balance: float  # equity E used everywhere in Section 5.5
    unrealized_pnl: float
    available_balance: float
    maint_margin: float
    initial_margin: float

    @property
    def equity(self) -> float:
        return self.margin_balance

    @property
    def margin_ratio(self) -> float:
        """Binance margin ratio = maintenance margin / margin balance (US-T12 AC 1)."""
        if self.margin_balance <= 0:
            return 1.0
        return self.maint_margin / self.margin_balance


@dataclass(frozen=True, slots=True)
class Position:
    """One-way-mode position for a symbol. ``qty`` > 0 long, < 0 short."""

    symbol: str
    qty: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float = 0.0
    leverage: float = 5.0
    liquidation_price: float = 0.0
    adl_quantile: int = 0
    ts_ms: int = 0

    @property
    def notional(self) -> float:
        """Signed notional at the mark price."""
        return self.qty * self.mark_price

    @property
    def side(self) -> PositionSide:
        if self.qty > 0:
            return PositionSide.LONG
        if self.qty < 0:
            return PositionSide.SHORT
        return PositionSide.FLAT

    @property
    def is_flat(self) -> bool:
        return self.qty == 0.0


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An order as the strategy wants it, before precision rounding."""

    symbol: str
    side: Side
    qty: float  # always positive
    order_type: OrderType = OrderType.LIMIT
    price: float | None = None
    time_in_force: TimeInForce = TimeInForce.GTX
    reduce_only: bool = False
    client_order_id: str | None = None
    # Provenance — written straight through to the ``orders`` table.
    strategy: Strategy = Strategy.TREND
    rebalance_id: str | None = None
    slice_id: str | None = None
    intent: str = ""  # rebalance | governor_cut | kill_flatten | delisting | drift | adl


@dataclass(frozen=True, slots=True)
class Order:
    """An order acknowledged by the venue (or by the paper simulator)."""

    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    price: float | None
    time_in_force: TimeInForce
    reduce_only: bool
    status: OrderStatus
    filled_qty: float = 0.0
    avg_price: float = 0.0
    created_ts_ms: int = 0
    updated_ts_ms: int = 0
    strategy: Strategy = Strategy.TREND
    rebalance_id: str | None = None
    slice_id: str | None = None
    intent: str = ""

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)


@dataclass(frozen=True, slots=True)
class Fill:
    """One trade print against one of our orders."""

    trade_id: str
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    fee_asset: str
    is_maker: bool
    ts_ms: int
    realized_pnl: float = 0.0
    strategy: Strategy = Strategy.TREND
    rebalance_id: str | None = None
    slice_id: str | None = None
    decision_mid: float | None = None  # 5.9 step 1 — slippage reference
    slippage_bps: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.price


# --------------------------------------------------------------------------- #
# Strategy artefacts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SignalResult:
    """Per-symbol trend signal with every intermediate (US-T04 AC 1)."""

    symbol: str
    x: tuple[float, ...]
    y: tuple[float, ...]
    z: tuple[float, ...]
    u: tuple[float, ...]
    signal: float
    bar_day: date | None = None
    bar_ts_ms: int = 0
    warm: bool = True  # False when windows are not yet filled (signal forced to 0)


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    symbol: str
    rank: int
    median_quote_volume_30d: float
    history_days: int
    included: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class UniverseResult:
    """Output of the pure ``select_universe`` function (US-T02 AC 1)."""

    month: str  # YYYY-MM
    entries: tuple[UniverseEntry, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(e.symbol for e in self.entries if e.included)

    def entry(self, symbol: str) -> UniverseEntry | None:
        return next((e for e in self.entries if e.symbol == symbol), None)


@dataclass(frozen=True, slots=True)
class RiskModel:
    """Daily volatility and covariance estimates (Section 5.4)."""

    symbols: tuple[str, ...]
    vols: dict[str, float]  # annualised, floored/capped
    corr: tuple[tuple[float, ...], ...]  # correlation matrix in ``symbols`` order
    avg_corr: float
    n_obs: dict[str, int] = field(default_factory=dict)

    def cov_matrix(self) -> list[list[float]]:
        """Annualised covariance ``Sigma = diag(sigma) . Corr . diag(sigma)``."""
        n = len(self.symbols)
        v = [self.vols[s] for s in self.symbols]
        return [[v[i] * v[j] * self.corr[i][j] for j in range(n)] for i in range(n)]


@dataclass(frozen=True, slots=True)
class SymbolTarget:
    """Per-symbol sizing output with every intermediate (US-T06 AC 4)."""

    symbol: str
    signal: float
    vol: float
    raw: float
    target_notional: float
    funding_ann: float = 0.0
    funding_haircut: float = 1.0
    caps_applied: tuple[str, ...] = ()
    target_qty: float = 0.0
    current_qty: float = 0.0
    delta_notional: float = 0.0
    traded: bool = False


@dataclass(frozen=True, slots=True)
class Targets:
    """Whole-book sizing output (Section 5.5)."""

    targets: tuple[SymbolTarget, ...]
    sigma_p: float
    conv: float
    sigma_eff: float
    s: float
    g: float
    equity: float

    def by_symbol(self) -> dict[str, SymbolTarget]:
        return {t.symbol: t for t in self.targets}

    @property
    def gross(self) -> float:
        return sum(abs(t.target_notional) for t in self.targets)

    @property
    def net(self) -> float:
        return sum(t.target_notional for t in self.targets)


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    """One entry of the persisted rebalance order plan (US-T09 AC 3)."""

    symbol: str
    side: Side
    delta_notional: float
    delta_qty: float
    current_qty: float
    target_qty: float
    reduce_only: bool
    risk_reducing: bool
    sequence: int
    n_slices: int = 1
    clip_qty: float = 0.0


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """Risk supervisor reading (US-T12 AC 1)."""

    ts_ms: int
    equity: float
    gross: float
    net: float
    largest_abs: float
    largest_symbol: str
    margin_ratio: float
    available_balance: float
    n_long: int
    n_short: int
    status: RiskStatus
    breaches: tuple[str, ...] = ()
    survivable_move: float = 0.0

    @property
    def gross_x(self) -> float:
        return self.gross / self.equity if self.equity > 0 else 0.0

    @property
    def net_x(self) -> float:
        return self.net / self.equity if self.equity > 0 else 0.0


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One cash-affecting event, keyed by the exchange's own income id."""

    strategy: Strategy
    ts_ms: int
    income_type: IncomeType
    asset: str
    amount: float
    symbol: str | None = None
    tran_id: str = ""
    trade_id: str | None = None
    info: str = ""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One point on the time-weighted equity path (governor peak, US-T08 AC 3)."""

    ts_ms: int
    equity: float
    net_transfer: float = 0.0
    twr_factor: float = 1.0  # (E_t - transfer) / E_{t-1}
    twr_index: float = 1.0  # cumulative product — the drawdown reference


@dataclass(frozen=True, slots=True)
class MetricValue:
    strategy: Strategy
    name: str
    period: str  # 7d | 30d | 90d | mtd | ytd | since_inception | all
    value: float | None
    as_of_ts_ms: int
    n_obs: int = 0
    std_error: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Alert:
    strategy: Strategy
    ts_ms: int
    severity: Severity
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AccountState",
    "Alert",
    "BookTicker",
    "DailyBar",
    "EngineState",
    "EquityPoint",
    "ExposureSnapshot",
    "Fill",
    "FundingRate",
    "IncomeType",
    "LedgerEntry",
    "MetricValue",
    "Mode",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Phase",
    "PlannedOrder",
    "Position",
    "PositionSide",
    "RebalanceStatus",
    "RiskModel",
    "RiskStatus",
    "Severity",
    "Side",
    "SignalResult",
    "SliceOutcome",
    "Strategy",
    "SymbolInfo",
    "SymbolTarget",
    "Targets",
    "TimeInForce",
    "UniverseEntry",
    "UniverseResult",
]
