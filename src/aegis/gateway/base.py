"""The exchange boundary.

``ExchangeGateway`` is the *only* way any strategy code touches a venue. Four
implementations satisfy it — Binance mainnet (live), Binance testnet (demo), a
local paper simulator on live market data, and the backtest simulator — so the
same rebalance executor and risk supervisor run unchanged in every mode. Tests
use ``FakeGateway``; no test in this repository touches the network.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from aegis.core.types import (
    AccountState,
    BookTicker,
    DailyBar,
    Fill,
    FundingRate,
    Order,
    OrderRequest,
    Position,
    SymbolInfo,
)


@runtime_checkable
class ExchangeGateway(Protocol):
    """Everything the engine needs from a venue, and nothing more."""

    # -- instrument & market data ------------------------------------------- #

    def exchange_info(self, *, refresh: bool = False) -> dict[str, SymbolInfo]:
        """All USDS-M symbols keyed by symbol, including non-TRADING ones.

        Non-TRADING symbols must be returned (not filtered) — the delisting
        watch (Section 5.10) needs to see the status change.
        """
        ...

    def daily_bars(
        self, symbol: str, start: date | None = None, end: date | None = None, limit: int = 1500
    ) -> list[DailyBar]:
        """Closed daily klines, ascending by day. Paginates internally."""
        ...

    def book_ticker(self, symbol: str) -> BookTicker:
        """Best bid/ask right now."""
        ...

    def mark_price(self, symbol: str) -> float: ...

    def predicted_funding(self, symbol: str) -> FundingRate:
        """``premiumIndex.lastFundingRate`` with the symbol's own interval (5.6)."""
        ...

    def funding_history(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[FundingRate]:
        """Realised funding settlements, ascending."""
        ...

    def server_time_ms(self) -> int: ...

    # -- account ------------------------------------------------------------ #

    def account(self) -> AccountState: ...

    def positions(self) -> dict[str, Position]:
        """Non-zero positions keyed by symbol (one-way mode)."""
        ...

    def income(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        """Raw ``/fapi/v1/income`` rows, ascending — the ledger's source of truth."""
        ...

    def commission_rate(self, symbol: str) -> tuple[float, float]:
        """``(maker, taker)`` from ``/fapi/v1/commissionRate`` (Locked Decision: fee model)."""
        ...

    def bnb_balance(self) -> float: ...

    def key_permissions(self) -> dict[str, bool]:
        """At least ``{"futures": bool, "withdraw": bool, "ip_restricted": bool}``."""
        ...

    def sub_account_name(self) -> str | None:
        """Best-effort identity of the key's account, for the US-T01 AC 4 assertion."""
        ...

    # -- trading ------------------------------------------------------------ #

    def set_leverage(self, symbol: str, leverage: int) -> None: ...

    def set_margin_type(self, symbol: str, margin_type: str) -> None: ...

    def place_order(self, request: OrderRequest) -> Order:
        """Submit one order. Raises ``OrderRejected`` on a venue refusal."""
        ...

    def cancel_order(self, symbol: str, order_id: str) -> Order: ...

    def cancel_all(self, symbol: str) -> None: ...

    def get_order(self, symbol: str, order_id: str) -> Order: ...

    def open_orders(self, symbol: str | None = None) -> list[Order]: ...

    def user_trades(self, symbol: str, start_ms: int | None = None, limit: int = 1000) -> list[Fill]:
        """Our own fills, ascending — the fee/maker-flag source of truth."""
        ...

    # -- lifecycle ---------------------------------------------------------- #

    def poll(self) -> None:
        """Advance any internal simulation (paper) or drain streams. No-op live."""
        ...

    def close(self) -> None: ...


__all__ = ["ExchangeGateway"]
