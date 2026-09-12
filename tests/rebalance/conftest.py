"""Builders shared by the rebalance tests.

Everything here is data: a ``Targets`` value, a ``SymbolInfo`` grid, a position.
The fixtures in ``tests/conftest.py`` supply the offline ``Context``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aegis.core.types import (
    Position,
    SymbolInfo,
    SymbolTarget,
    Targets,
    TimeInForce,
)
from aegis.gateway.fake import FakeGateway

BTC = "BTCUSDT"
ETH = "ETHUSDT"
SOL = "SOLUSDT"


def info(
    symbol: str,
    *,
    tick_size: float = 0.01,
    step_size: float = 0.001,
    min_qty: float = 0.001,
    min_notional: float = 5.0,
) -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        status="TRADING",
        contract_type="PERPETUAL",
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
        price_precision=2,
        quantity_precision=3,
    )


def position(symbol: str, qty: float, mark: float) -> Position:
    return Position(symbol=symbol, qty=qty, entry_price=mark, mark_price=mark)


def targets(notionals: Mapping[str, float], equity: float = 10_000.0, g: float = 1.0) -> Targets:
    rows = tuple(
        SymbolTarget(
            symbol=symbol,
            signal=1.0 if notional >= 0 else -1.0,
            vol=0.5,
            raw=notional,
            target_notional=notional,
        )
        for symbol, notional in sorted(notionals.items())
    )
    return Targets(
        targets=rows, sigma_p=0.2, conv=0.5, sigma_eff=0.2, s=1.0, g=g, equity=equity
    )


def fill_hook(*, maker: bool = True):
    """Fill every order the executor places, the instant it is placed.

    GTX fills as a maker at the posted price, IOC as a taker at the book —
    which is what the venue would do to an order that is marketable on arrival.
    """

    def hook(gateway: FakeGateway, order) -> None:
        if order.time_in_force is TimeInForce.IOC:
            book = gateway.book_ticker(order.symbol)
            gateway.fill_order(order.order_id, price=book.aggressive_price(order.side), is_maker=False)
        else:
            gateway.fill_order(order.order_id, is_maker=maker)

    return hook


@pytest.fixture
def venue(gateway: FakeGateway) -> FakeGateway:
    """A two-symbol venue with round numbers: BTC at 100.00, ETH at 50.00."""
    gateway.set_symbol_info(info(BTC))
    gateway.set_symbol_info(info(ETH))
    gateway.set_book(BTC, bid=99.99, ask=100.01)
    gateway.set_book(ETH, bid=49.99, ask=50.01)
    gateway.set_account(wallet_balance=10_000.0)
    return gateway
