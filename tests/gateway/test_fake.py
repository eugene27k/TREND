"""FakeGateway — the programmable venue every other module's tests rely on.

The fake is only useful if it refuses what Binance refuses, so most of what is
asserted here is a rejection. US-T10 AC 2 in particular ("a test asserts an
exchange rejection is raised, not a flip") is only true of the *engine* if it is
first true of the venue the engine is tested against.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from aegis.core.clock import FakeClock
from aegis.core.errors import (
    ExchangeUnreachable,
    GatewayError,
    InsufficientMargin,
    OrderRejected,
    PermissionChanged,
    RateLimited,
)
from aegis.core.types import (
    FundingRate,
    IncomeType,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Strategy,
    SymbolInfo,
    TimeInForce,
)
from aegis.gateway.base import ExchangeGateway
from aegis.gateway.fake import FakeGateway

TOL = 1e-9
DAY_MS = 86_400_000

PROTOCOL_METHODS = sorted(
    name
    for name, value in vars(ExchangeGateway).items()
    if not name.startswith("_") and inspect.isfunction(value)
)


def assert_matches_protocol(impl: type) -> None:
    """Every ``ExchangeGateway`` method exists on ``impl`` with an identical signature.

    ``isinstance`` against a runtime-checkable Protocol only checks that the
    names exist; this is what stops a future protocol change from diverging
    silently in a simulated gateway.
    """
    missing = [name for name in PROTOCOL_METHODS if not callable(getattr(impl, name, None))]
    assert missing == [], f"{impl.__name__} is missing {missing}"
    for name in PROTOCOL_METHODS:
        expected = inspect.signature(getattr(ExchangeGateway, name))
        actual = inspect.signature(getattr(impl, name))
        assert str(actual) == str(expected), f"{impl.__name__}.{name}{actual} != protocol {expected}"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock("2026-09-12T00:05:00Z")


@pytest.fixture
def gw(clock: FakeClock) -> FakeGateway:
    g = FakeGateway(clock)
    g.set_symbol_info("BTCUSDT", tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=5.0)
    g.set_book("BTCUSDT", 99.9, 100.1)
    return g


def _req(**kwargs) -> OrderRequest:
    base = {
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "qty": 1.0,
        "order_type": OrderType.LIMIT,
        "price": 99.9,
        "time_in_force": TimeInForce.GTX,
    }
    base.update(kwargs)
    return OrderRequest(**base)


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_fake_gateway_satisfies_exchange_gateway_isinstance(gw: FakeGateway) -> None:
    assert isinstance(gw, ExchangeGateway)


def test_fake_gateway_signatures_match_the_protocol_exactly() -> None:
    assert_matches_protocol(FakeGateway)


def test_protocol_method_list_is_not_empty() -> None:
    """Guards the conformance test itself: an empty list would pass vacuously."""
    assert len(PROTOCOL_METHODS) >= 20
    assert "place_order" in PROTOCOL_METHODS


# --------------------------------------------------------------------------- #
# Programmable market data
# --------------------------------------------------------------------------- #


def test_set_symbol_info_builds_defaults_and_amends_in_place(gw: FakeGateway) -> None:
    info = gw.exchange_info()["BTCUSDT"]
    assert info.base_asset == "BTC"
    assert info.is_tradeable_perp
    amended = gw.set_symbol_info("BTCUSDT", status="SETTLING")
    assert amended.status == "SETTLING"
    assert amended.tick_size == 0.1  # untouched fields survive
    assert gw.exchange_info()["BTCUSDT"].status == "SETTLING"


def test_set_symbol_info_accepts_a_ready_made_symbol_info(gw: FakeGateway) -> None:
    info = SymbolInfo(
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        status="TRADING",
        contract_type="PERPETUAL",
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        min_notional=5.0,
        price_precision=2,
        quantity_precision=3,
    )
    assert gw.set_symbol_info(info) is info
    assert gw.exchange_info()["ETHUSDT"] is info


def test_exchange_info_refresh_is_counted(gw: FakeGateway) -> None:
    gw.exchange_info()
    assert gw.exchange_info_refreshes == 0
    gw.exchange_info(refresh=True)
    assert gw.exchange_info_refreshes == 1


def test_set_book_sets_mark_to_the_mid_unless_overridden(gw: FakeGateway) -> None:
    assert gw.book_ticker("BTCUSDT").mid == pytest.approx(100.0)
    assert gw.mark_price("BTCUSDT") == pytest.approx(100.0)
    gw.set_book("BTCUSDT", 99.9, 100.1, mark=101.0)
    assert gw.mark_price("BTCUSDT") == pytest.approx(101.0)


def test_book_ticker_without_a_configured_book_raises_gateway_error(clock: FakeClock) -> None:
    with pytest.raises(GatewayError):
        FakeGateway(clock).book_ticker("NOPEUSDT")


def test_daily_bars_filter_by_start_end_and_limit(gw: FakeGateway) -> None:
    gw.set_closes("BTCUSDT", [100.0 + i for i in range(10)], start_day=date(2026, 1, 1))
    assert len(gw.daily_bars("BTCUSDT")) == 10
    windowed = gw.daily_bars("BTCUSDT", start=date(2026, 1, 3), end=date(2026, 1, 5))
    assert [b.day for b in windowed] == [date(2026, 1, 3), date(2026, 1, 4), date(2026, 1, 5)]
    assert [b.day for b in gw.daily_bars("BTCUSDT", limit=2)] == [date(2026, 1, 9), date(2026, 1, 10)]
    assert gw.daily_bars("UNSETUSDT") == []


def test_set_bars_sorts_ascending_by_day(gw: FakeGateway) -> None:
    rows = gw.set_closes("BTCUSDT", [1.0, 2.0, 3.0], start_day=date(2026, 3, 1))
    reversed_rows = gw.set_bars("BTCUSDT", reversed(rows))
    assert [b.day for b in reversed_rows] == [date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)]


def test_funding_history_accepts_tuples_and_filters_by_window(gw: FakeGateway) -> None:
    gw.set_funding("BTCUSDT", [(1_000, 0.0001), (2_000, -0.0002), (3_000, 0.0003)])
    assert [f.rate for f in gw.funding_history("BTCUSDT")] == [0.0001, -0.0002, 0.0003]
    assert [f.funding_time_ms for f in gw.funding_history("BTCUSDT", start_ms=2_000)] == [2_000, 3_000]
    assert [f.funding_time_ms for f in gw.funding_history("BTCUSDT", end_ms=2_000)] == [1_000, 2_000]


def test_predicted_funding_defaults_to_zero_and_annualises_by_interval(gw: FakeGateway) -> None:
    assert gw.predicted_funding("BTCUSDT").rate == 0.0
    fr = gw.set_predicted_funding("BTCUSDT", 0.0001, interval_hours=4.0)
    assert gw.predicted_funding("BTCUSDT") is fr
    assert fr.annualised() == pytest.approx(0.0001 * 8760 / 4.0)


def test_server_time_offset_simulates_clock_drift(gw: FakeGateway, clock: FakeClock) -> None:
    assert gw.server_time_ms() == clock.now_ms()
    gw.set_server_time_offset_ms(1_500)
    assert gw.server_time_ms() - clock.now_ms() == 1_500


def test_mark_price_falls_back_to_the_book_mid(clock: FakeClock) -> None:
    g = FakeGateway(clock)
    assert g.mark_price("BTCUSDT") == 0.0
    g._books["BTCUSDT"] = g.set_book("BTCUSDT", 10.0, 12.0)
    g.sim.marks.pop("BTCUSDT")
    assert g.mark_price("BTCUSDT") == pytest.approx(11.0)


# --------------------------------------------------------------------------- #
# US-T10 AC 2 — reduce-only never flips
# --------------------------------------------------------------------------- #


def test_us_t10_ac2_reduce_only_over_reduction_is_rejected_not_flipped(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 1.0, entry_price=100.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.SELL, qty=2.0, price=100.1, reduce_only=True))
    assert excinfo.value.code == -2022
    assert excinfo.value.is_reduce_only_violation
    assert gw.positions()["BTCUSDT"].qty == pytest.approx(1.0)
    assert gw.open_orders() == []
    assert gw.placed == []


def test_us_t10_ac2_reduce_only_on_a_flat_position_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.SELL, qty=1.0, price=100.1, reduce_only=True))
    assert excinfo.value.code == -2022


def test_us_t10_ac2_reduce_only_in_the_same_direction_is_rejected(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 1.0, entry_price=100.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.BUY, qty=0.5, price=99.9, reduce_only=True))
    assert excinfo.value.code == -2022


def test_us_t10_ac2_reduce_only_short_cannot_be_over_bought(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", -1.0, entry_price=100.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.BUY, qty=1.5, price=99.9, reduce_only=True))
    assert excinfo.value.code == -2022
    assert gw.positions()["BTCUSDT"].qty == pytest.approx(-1.0)


def test_us_t10_ac2_reduce_only_exact_close_is_accepted(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 1.0, entry_price=100.0)
    order = gw.place_order(_req(side=Side.SELL, qty=1.0, price=100.1, reduce_only=True))
    assert order.status is OrderStatus.NEW
    assert order.reduce_only


def test_us_t10_ac2_a_non_reduce_only_order_may_still_flip(gw: FakeGateway) -> None:
    """The protection is the flag, not the gateway second-guessing the strategy."""
    gw.set_position("BTCUSDT", 1.0, entry_price=100.0)
    order = gw.place_order(_req(side=Side.SELL, qty=2.0, price=100.1, reduce_only=False))
    gw.fill_order(order.order_id)
    assert gw.positions()["BTCUSDT"].qty == pytest.approx(-1.0)


# --------------------------------------------------------------------------- #
# Post-only and the exchange filters
# --------------------------------------------------------------------------- #


def test_gtx_buy_that_would_cross_the_book_is_rejected_5022(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.BUY, qty=1.0, price=100.1))
    assert excinfo.value.code == -5022
    assert excinfo.value.is_post_only_violation


def test_gtx_sell_that_would_cross_the_book_is_rejected_5022(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.SELL, qty=1.0, price=99.9))
    assert excinfo.value.code == -5022


def test_gtx_at_the_passive_best_is_accepted(gw: FakeGateway) -> None:
    assert gw.place_order(_req(side=Side.BUY, price=99.9)).status is OrderStatus.NEW
    assert gw.place_order(_req(side=Side.SELL, price=100.1)).status is OrderStatus.NEW


def test_ioc_taker_order_is_not_subject_to_the_post_only_check(gw: FakeGateway) -> None:
    order = gw.place_order(_req(side=Side.BUY, price=100.1, time_in_force=TimeInForce.IOC))
    assert order.status is OrderStatus.NEW  # fill_policy="none": the test drives fills


def test_qty_below_min_qty_is_rejected(gw: FakeGateway) -> None:
    gw.set_symbol_info("BTCUSDT", min_qty=0.5, min_notional=0.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(qty=0.1))
    assert excinfo.value.code == -1013


def test_notional_below_min_notional_is_rejected(gw: FakeGateway) -> None:
    gw.set_symbol_info("BTCUSDT", min_qty=0.001, min_notional=500.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(qty=1.0, price=99.9))
    assert excinfo.value.code == -1013


def test_qty_off_the_step_grid_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(qty=1.00042))
    assert excinfo.value.code == -1111


def test_price_off_the_tick_grid_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(price=99.93))
    assert excinfo.value.code == -1111


def test_non_positive_qty_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(qty=0.0))
    assert excinfo.value.code == -1111


def test_unknown_symbol_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(symbol="NOPEUSDT"))
    assert excinfo.value.code == -1121


def test_rejection_carries_the_client_order_id(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(symbol="NOPEUSDT", client_order_id="trend-1"))
    assert excinfo.value.client_order_id == "trend-1"


def test_market_order_without_a_price_skips_the_tick_check(gw: FakeGateway) -> None:
    order = gw.place_order(_req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.IOC))
    assert order.price is None
    assert order.status is OrderStatus.NEW


# --------------------------------------------------------------------------- #
# Fill policies
# --------------------------------------------------------------------------- #


def test_default_fill_policy_leaves_orders_resting_as_new(gw: FakeGateway) -> None:
    order = gw.place_order(_req())
    assert order.status is OrderStatus.NEW
    assert gw.open_orders("BTCUSDT") == [order]
    assert gw.positions() == {}


def test_immediate_policy_fills_a_market_order_at_the_aggressive_book_price(gw: FakeGateway) -> None:
    gw.set_fill_policy("immediate")
    order = gw.place_order(_req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.IOC))
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == pytest.approx(100.1)
    assert gw.user_trades("BTCUSDT")[0].is_maker is False


def test_immediate_policy_fills_a_crossing_limit_as_taker(gw: FakeGateway) -> None:
    gw.set_fill_policy("immediate")
    order = gw.place_order(_req(side=Side.BUY, price=100.2, time_in_force=TimeInForce.IOC))
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == pytest.approx(100.1)


def test_immediate_policy_leaves_a_passive_gtx_order_resting(gw: FakeGateway) -> None:
    gw.set_fill_policy("immediate")
    assert gw.place_order(_req(side=Side.BUY, price=99.9)).status is OrderStatus.NEW


def test_immediate_policy_expires_a_non_crossing_ioc(gw: FakeGateway) -> None:
    gw.set_fill_policy("immediate")
    order = gw.place_order(_req(side=Side.BUY, price=99.8, time_in_force=TimeInForce.IOC))
    assert order.status is OrderStatus.EXPIRED
    assert gw.open_orders() == []


def test_callable_policy_hands_the_order_to_the_hook(gw: FakeGateway) -> None:
    seen: list[Order] = []

    def hook(gateway: FakeGateway, order: Order) -> None:
        seen.append(order)
        gateway.fill_order(order.order_id, qty=order.qty / 2)

    gw.set_fill_policy("callable", hook)
    order = gw.place_order(_req(qty=2.0))
    assert [o.order_id for o in seen] == [order.order_id]
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == pytest.approx(1.0)


def test_unknown_fill_policy_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(GatewayError):
        gw.set_fill_policy("teleport")


def test_callable_policy_without_a_hook_is_rejected(gw: FakeGateway) -> None:
    with pytest.raises(GatewayError):
        gw.set_fill_policy("callable")


# --------------------------------------------------------------------------- #
# Manual fills
# --------------------------------------------------------------------------- #


def test_partial_fills_accumulate_and_reach_filled_at_exactly_the_full_qty(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0, price=99.9))
    after_first = gw.fill_order(order.order_id, qty=0.4, price=99.9)
    assert after_first.status is OrderStatus.PARTIALLY_FILLED
    assert after_first.filled_qty == pytest.approx(0.4)
    after_second = gw.fill_order(order.order_id, qty=0.3, price=100.9)
    assert after_second.filled_qty == pytest.approx(0.7)
    assert after_second.avg_price == pytest.approx((0.4 * 99.9 + 0.3 * 100.9) / 0.7)
    final = gw.fill_order(order.order_id)
    assert final.status is OrderStatus.FILLED
    assert final.filled_qty == 1.0
    assert final.remaining_qty == 0.0
    assert gw.open_orders() == []
    assert len(gw.user_trades("BTCUSDT")) == 3


def test_fill_beyond_the_remaining_qty_is_refused(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0))
    with pytest.raises(GatewayError):
        gw.fill_order(order.order_id, qty=1.5)


def test_filling_a_terminal_order_is_refused(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0))
    gw.fill_order(order.order_id)
    with pytest.raises(GatewayError):
        gw.fill_order(order.order_id)


def test_non_positive_fill_qty_is_refused(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0))
    with pytest.raises(GatewayError):
        gw.fill_order(order.order_id, qty=0.0)


def test_filling_an_unknown_order_raises_order_rejected(gw: FakeGateway) -> None:
    with pytest.raises(OrderRejected):
        gw.fill_order("999999")


def test_market_order_fills_at_the_book_when_no_price_is_supplied(gw: FakeGateway) -> None:
    order = gw.place_order(_req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.IOC))
    filled = gw.fill_order(order.order_id, is_maker=False)
    assert filled.avg_price == pytest.approx(100.1)


def test_a_fill_without_any_price_source_is_refused(clock: FakeClock) -> None:
    g = FakeGateway(clock)
    g.set_symbol_info("XUSDT", min_notional=0.0)
    order = g.place_order(
        OrderRequest(symbol="XUSDT", side=Side.BUY, qty=1.0, order_type=OrderType.MARKET, price=None)
    )
    with pytest.raises(GatewayError):
        g.fill_order(order.order_id)


# --------------------------------------------------------------------------- #
# Bookkeeping: entry price, realised P&L, fees, wallet, income
# --------------------------------------------------------------------------- #


def test_entry_price_is_the_weighted_average_of_adding_fills(gw: FakeGateway) -> None:
    first = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(first.order_id, price=100.0)
    second = gw.place_order(_req(qty=3.0, price=99.9))
    gw.fill_order(second.order_id, price=104.0)
    pos = gw.positions()["BTCUSDT"]
    assert pos.qty == pytest.approx(4.0)
    assert pos.entry_price == pytest.approx((1.0 * 100.0 + 3.0 * 104.0) / 4.0)


def test_maker_fill_pays_the_maker_fee_and_moves_the_wallet(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(order.order_id, price=100.0, is_maker=True)
    fill = gw.user_trades("BTCUSDT")[0]
    assert fill.fee == pytest.approx(100.0 * 0.0002)
    assert gw.account().wallet_balance == pytest.approx(10_000.0 - 0.02)


def test_taker_fill_pays_the_taker_fee(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(order.order_id, price=100.0, is_maker=False)
    assert gw.user_trades("BTCUSDT")[0].fee == pytest.approx(100.0 * 0.0005)
    assert gw.account().wallet_balance == pytest.approx(10_000.0 - 0.05)


def test_set_commission_overrides_the_rate_for_one_symbol_only(gw: FakeGateway) -> None:
    gw.set_commission(0.0, 0.001, symbol="BTCUSDT")
    assert gw.commission_rate("BTCUSDT") == (0.0, 0.001)
    assert gw.commission_rate("ETHUSDT") == (0.0002, 0.0005)
    gw.set_commission(0.0001, 0.0004)
    assert gw.commission_rate("ETHUSDT") == (0.0001, 0.0004)


def test_reducing_fill_realises_pnl_and_reconciles_the_wallet(gw: FakeGateway) -> None:
    opening = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(opening.order_id, price=100.0, is_maker=True)
    gw.set_book("BTCUSDT", 109.9, 110.1)
    closing = gw.place_order(_req(side=Side.SELL, qty=1.0, price=110.1, reduce_only=True))
    gw.fill_order(closing.order_id, price=110.0, is_maker=False)

    assert gw.positions() == {}
    assert gw.sim.realized_pnl == pytest.approx(10.0)
    fees = 100.0 * 0.0002 + 110.0 * 0.0005
    assert gw.sim.fees_paid == pytest.approx(fees)
    assert gw.account().wallet_balance == pytest.approx(10_000.0 + 10.0 - fees)


def test_short_side_realises_pnl_with_the_opposite_sign(gw: FakeGateway) -> None:
    opening = gw.place_order(_req(side=Side.SELL, qty=2.0, price=100.1))
    gw.fill_order(opening.order_id, price=100.0)
    closing = gw.place_order(_req(side=Side.BUY, qty=2.0, price=99.9, reduce_only=True))
    gw.fill_order(closing.order_id, price=95.0)
    assert gw.sim.realized_pnl == pytest.approx(2.0 * (100.0 - 95.0))


def test_a_flip_realises_the_old_leg_and_reprices_the_new_one(gw: FakeGateway) -> None:
    opening = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(opening.order_id, price=100.0)
    flipping = gw.place_order(_req(side=Side.SELL, qty=3.0, price=100.1))
    gw.fill_order(flipping.order_id, price=110.0)
    pos = gw.positions()["BTCUSDT"]
    assert pos.qty == pytest.approx(-2.0)
    assert pos.entry_price == pytest.approx(110.0)
    assert gw.sim.realized_pnl == pytest.approx(10.0)


def test_income_records_commission_always_and_realized_pnl_only_when_reducing(gw: FakeGateway) -> None:
    opening = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(opening.order_id, price=100.0)
    types = [r["incomeType"] for r in gw.income(0)]
    assert types == [str(IncomeType.COMMISSION)]

    closing = gw.place_order(_req(side=Side.SELL, qty=1.0, price=100.1, reduce_only=True))
    gw.fill_order(closing.order_id, price=110.0)
    rows = gw.income(0)
    assert [r["incomeType"] for r in rows] == [
        str(IncomeType.COMMISSION),
        str(IncomeType.REALIZED_PNL),
        str(IncomeType.COMMISSION),
    ]
    assert float(rows[1]["income"]) == pytest.approx(10.0)
    assert all(r["asset"] == "USDT" for r in rows)
    assert all(r["symbol"] == "BTCUSDT" for r in rows)


def test_income_rows_sum_to_the_wallet_change(gw: FakeGateway) -> None:
    """The ledger identity, proved against the venue that produced the rows."""
    gw.set_fill_policy("immediate")
    gw.place_order(_req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.IOC, qty=2.0))
    gw.settle_funding("BTCUSDT", 0.0001)
    gw.set_book("BTCUSDT", 109.9, 110.1)
    gw.place_order(
        _req(
            side=Side.SELL,
            order_type=OrderType.MARKET,
            price=None,
            time_in_force=TimeInForce.IOC,
            qty=2.0,
            reduce_only=True,
        )
    )
    total = sum(float(r["income"]) for r in gw.income(0))
    assert gw.account().wallet_balance - 10_000.0 == pytest.approx(total)


def test_income_window_and_limit_are_applied(gw: FakeGateway) -> None:
    order = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(order.order_id, price=100.0)
    ts = gw.income(0)[0]["time"]
    assert gw.income(ts + 1) == []
    assert gw.income(0, end_ms=ts - 1) == []
    assert len(gw.income(0, limit=0)) == 0


def test_user_trades_filter_by_symbol_and_start(gw: FakeGateway, clock: FakeClock) -> None:
    order = gw.place_order(_req(qty=1.0, price=99.9))
    gw.fill_order(order.order_id, price=100.0)
    later = clock.now_ms() + 10
    assert len(gw.user_trades("BTCUSDT")) == 1
    assert gw.user_trades("BTCUSDT", start_ms=later) == []
    assert gw.user_trades("ETHUSDT") == []
    assert len(gw.user_trades("BTCUSDT", limit=0)) == 0


def test_set_position_seeds_state_without_fees_or_income(gw: FakeGateway) -> None:
    pos = gw.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=110.0)
    assert pos.unrealized_pnl == pytest.approx(20.0)
    assert gw.income(0) == []
    assert gw.user_trades("BTCUSDT") == []
    assert gw.account().margin_balance == pytest.approx(10_020.0)


def test_account_derives_margin_and_available_from_open_positions(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    state = gw.account()
    assert state.initial_margin == pytest.approx(200.0 / 5.0)
    assert state.maint_margin == pytest.approx(200.0 * 0.005)
    assert state.available_balance == pytest.approx(10_000.0 - 40.0)
    assert state.margin_ratio == pytest.approx(1.0 / 10_000.0)


def test_set_account_pins_the_fields_a_test_names(gw: FakeGateway) -> None:
    state = gw.set_account(wallet_balance=5_000.0, maint_margin=1_000.0)
    assert state.wallet_balance == pytest.approx(5_000.0)
    assert state.maint_margin == pytest.approx(1_000.0)
    assert gw.account().margin_ratio == pytest.approx(0.2)


def test_set_account_can_pin_a_field_without_moving_the_wallet(gw: FakeGateway) -> None:
    state = gw.set_account(maint_margin=2_000.0)
    assert state.maint_margin == pytest.approx(2_000.0)
    assert state.wallet_balance == pytest.approx(10_000.0)
    assert gw.sim.wallet_balance == pytest.approx(10_000.0)


# --------------------------------------------------------------------------- #
# Funding sign convention (Section 5.6)
# --------------------------------------------------------------------------- #


def test_a_long_pays_funding_when_the_rate_is_positive(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    amount = gw.settle_funding("BTCUSDT", 0.0001)
    assert amount == pytest.approx(-0.02)
    assert gw.account().wallet_balance == pytest.approx(10_000.0 - 0.02)
    row = gw.income(0)[-1]
    assert row["incomeType"] == str(IncomeType.FUNDING_FEE)
    assert float(row["income"]) == pytest.approx(-0.02)


def test_a_short_receives_funding_when_the_rate_is_positive(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", -2.0, entry_price=100.0, mark_price=100.0)
    amount = gw.settle_funding("BTCUSDT", 0.0001)
    assert amount == pytest.approx(0.02)
    assert gw.account().wallet_balance == pytest.approx(10_000.0 + 0.02)


def test_a_long_receives_funding_when_the_rate_is_negative(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    assert gw.settle_funding("BTCUSDT", -0.0001) == pytest.approx(0.02)


def test_settle_funding_on_a_flat_book_is_a_no_op(gw: FakeGateway) -> None:
    assert gw.settle_funding("BTCUSDT", 0.0001) == 0.0
    assert gw.settle_funding("BTCUSDT", 0.0) == 0.0
    assert gw.income(0) == []


def test_settle_funding_appends_to_the_funding_history(gw: FakeGateway, clock: FakeClock) -> None:
    gw.set_predicted_funding("BTCUSDT", 0.0001, interval_hours=4.0)
    gw.set_position("BTCUSDT", 1.0, entry_price=100.0, mark_price=100.0)
    gw.settle_funding("BTCUSDT", 0.0002)
    history = gw.funding_history("BTCUSDT")
    assert [f.rate for f in history] == [0.0002]
    assert history[0].funding_time_ms == clock.now_ms()
    assert history[0].interval_hours == 4.0


# --------------------------------------------------------------------------- #
# Order lifecycle
# --------------------------------------------------------------------------- #


def test_cancel_order_moves_it_to_canceled_and_out_of_open_orders(gw: FakeGateway) -> None:
    order = gw.place_order(_req())
    cancelled = gw.cancel_order("BTCUSDT", order.order_id)
    assert cancelled.status is OrderStatus.CANCELED
    assert gw.open_orders() == []
    assert gw.get_order("BTCUSDT", order.order_id).status is OrderStatus.CANCELED


def test_cancelling_twice_is_rejected(gw: FakeGateway) -> None:
    order = gw.place_order(_req())
    gw.cancel_order("BTCUSDT", order.order_id)
    with pytest.raises(OrderRejected):
        gw.cancel_order("BTCUSDT", order.order_id)


def test_order_lookup_with_the_wrong_symbol_is_rejected(gw: FakeGateway) -> None:
    order = gw.place_order(_req())
    with pytest.raises(OrderRejected):
        gw.get_order("ETHUSDT", order.order_id)
    with pytest.raises(OrderRejected):
        gw.cancel_order("ETHUSDT", order.order_id)


def test_cancel_all_only_touches_the_named_symbol(gw: FakeGateway) -> None:
    gw.set_symbol_info("ETHUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0)
    gw.set_book("ETHUSDT", 49.9, 50.1)
    btc = gw.place_order(_req())
    eth = gw.place_order(_req(symbol="ETHUSDT", price=49.9))
    gw.cancel_all("BTCUSDT")
    assert gw.get_order("BTCUSDT", btc.order_id).status is OrderStatus.CANCELED
    assert gw.get_order("ETHUSDT", eth.order_id).status is OrderStatus.NEW
    assert [o.order_id for o in gw.open_orders()] == [eth.order_id]


def test_order_carries_the_request_provenance(gw: FakeGateway) -> None:
    order = gw.place_order(
        _req(strategy=Strategy.TREND, rebalance_id="rb-1", slice_id="s-3", intent="rebalance")
    )
    assert (order.strategy, order.rebalance_id, order.slice_id, order.intent) == (
        Strategy.TREND,
        "rb-1",
        "s-3",
        "rebalance",
    )
    assert order.client_order_id.startswith("fake-")
    assert gw.placed[-1].symbol == "BTCUSDT"


def test_client_order_id_is_preserved_when_supplied(gw: FakeGateway) -> None:
    assert gw.place_order(_req(client_order_id="trend-42")).client_order_id == "trend-42"


def test_set_leverage_and_margin_type_are_recorded(gw: FakeGateway) -> None:
    gw.set_leverage("BTCUSDT", 5)
    gw.set_margin_type("BTCUSDT", "CROSSED")
    assert gw.leverage == {"BTCUSDT": 5}
    assert gw.margin_type == {"BTCUSDT": "CROSSED"}


def test_poll_advances_nothing(gw: FakeGateway) -> None:
    order = gw.place_order(_req())
    gw.set_book("BTCUSDT", 110.0, 110.1)  # the market ran away from our bid
    for _ in range(5):
        gw.poll()
    assert gw.get_order("BTCUSDT", order.order_id).status is OrderStatus.NEW
    assert gw.positions() == {}
    assert gw.calls["poll"] == 5


def test_close_is_idempotent_and_recorded(gw: FakeGateway) -> None:
    gw.close()
    gw.close()
    assert gw.closed is True
    assert gw.calls["close"] == 2


# --------------------------------------------------------------------------- #
# Identity and permissions
# --------------------------------------------------------------------------- #


def test_permissions_and_identity_are_programmable(gw: FakeGateway) -> None:
    assert gw.key_permissions() == {"futures": True, "withdraw": False, "ip_restricted": True}
    assert gw.sub_account_name() == "trend-01"
    gw.set_permissions(futures=False, withdraw=True, ip_restricted=False)
    assert gw.key_permissions() == {"futures": False, "withdraw": True, "ip_restricted": False}
    gw.set_sub_account_name(None)
    assert gw.sub_account_name() is None


def test_bnb_balance_is_programmable(gw: FakeGateway) -> None:
    assert gw.bnb_balance() == pytest.approx(1.0)
    gw.set_bnb_balance(0.0)
    assert gw.bnb_balance() == 0.0


# --------------------------------------------------------------------------- #
# Chaos injection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "args", "exc"),
    [
        ("account", (), ExchangeUnreachable("boom")),
        ("book_ticker", ("BTCUSDT",), RateLimited("slow down", retry_after_s=2.0)),
        ("key_permissions", (), PermissionChanged("futures revoked")),
        ("place_order", (_req(),), InsufficientMargin("not enough margin")),
        ("positions", (), ExchangeUnreachable("boom")),
        ("cancel_all", ("BTCUSDT",), ExchangeUnreachable("boom")),
    ],
)
def test_injected_errors_propagate_with_their_own_type(
    gw: FakeGateway, method: str, args: tuple, exc: Exception
) -> None:
    gw.inject_error(method, exc)
    with pytest.raises(type(exc)):
        getattr(gw, method)(*args)


def test_injected_place_order_error_is_raised_before_any_state_changes(gw: FakeGateway) -> None:
    gw.inject_error("place_order", InsufficientMargin("no margin"))
    with pytest.raises(InsufficientMargin):
        gw.place_order(_req())
    assert gw.open_orders() == []
    assert gw.placed == []


def test_injected_errors_persist_until_cleared(gw: FakeGateway) -> None:
    gw.inject_error("account", ExchangeUnreachable("down"))
    for _ in range(3):
        with pytest.raises(ExchangeUnreachable):
            gw.account()
    gw.clear_errors("account")
    assert gw.account().wallet_balance == pytest.approx(10_000.0)


def test_fail_next_raises_once_then_recovers(gw: FakeGateway) -> None:
    gw.fail_next("book_ticker", RateLimited("429", retry_after_s=0.5))
    with pytest.raises(RateLimited) as excinfo:
        gw.book_ticker("BTCUSDT")
    assert excinfo.value.retry_after_s == 0.5
    assert gw.book_ticker("BTCUSDT").mid == pytest.approx(100.0)


def test_fail_next_can_queue_several_failures(gw: FakeGateway) -> None:
    gw.fail_next("server_time_ms", ExchangeUnreachable("timeout"), times=2)
    for _ in range(2):
        with pytest.raises(ExchangeUnreachable):
            gw.server_time_ms()
    assert gw.server_time_ms() > 0


def test_clear_errors_without_a_method_clears_everything(gw: FakeGateway) -> None:
    gw.inject_error("account", ExchangeUnreachable("a"))
    gw.fail_next("positions", ExchangeUnreachable("b"))
    gw.clear_errors()
    assert gw.account() is not None
    assert gw.positions() == {}


# --------------------------------------------------------------------------- #
# Venue metadata overlays and remaining programmable surface
# --------------------------------------------------------------------------- #


def test_adl_quantile_and_liquidation_price_overlay_the_position(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    assert gw.positions()["BTCUSDT"].adl_quantile == 0
    gw.set_adl_quantile("BTCUSDT", 4)
    gw.set_liquidation_price("BTCUSDT", 80.0)
    pos = gw.positions()["BTCUSDT"]
    assert pos.adl_quantile == 4
    assert pos.liquidation_price == pytest.approx(80.0)
    assert pos.qty == pytest.approx(2.0)  # the sim state is untouched


def test_set_mark_overrides_the_mid_without_touching_the_book(gw: FakeGateway) -> None:
    gw.set_mark("BTCUSDT", 123.0)
    assert gw.mark_price("BTCUSDT") == pytest.approx(123.0)
    assert gw.book_ticker("BTCUSDT").mid == pytest.approx(100.0)


def test_set_funding_accepts_ready_made_funding_rate_rows(gw: FakeGateway) -> None:
    rows = gw.set_funding(
        "BTCUSDT",
        [FundingRate(symbol="BTCUSDT", funding_time_ms=5_000, rate=0.0003, interval_hours=4.0)],
    )
    assert rows[0].interval_hours == 4.0
    assert gw.funding_history("BTCUSDT")[0].rate == pytest.approx(0.0003)


def test_set_closes_tolerates_a_zero_close(gw: FakeGateway) -> None:
    bars = gw.set_closes("BTCUSDT", [0.0, 10.0], start_day=date(2026, 5, 1))
    assert bars[0].volume == 0.0
    assert bars[1].volume == pytest.approx(50_000_000.0 / 10.0)


def test_immediate_policy_leaves_a_non_crossing_gtc_limit_resting(gw: FakeGateway) -> None:
    gw.set_fill_policy("immediate")
    order = gw.place_order(_req(side=Side.BUY, price=99.8, time_in_force=TimeInForce.GTC))
    assert order.status is OrderStatus.NEW


def test_immediate_policy_cannot_fill_a_market_order_with_no_price_source(clock: FakeClock) -> None:
    g = FakeGateway(clock, fill_policy="immediate")
    g.set_symbol_info("XUSDT", min_notional=0.0)
    order = g.place_order(
        OrderRequest(symbol="XUSDT", side=Side.BUY, qty=1.0, order_type=OrderType.MARKET, price=None)
    )
    assert order.status is OrderStatus.NEW
    assert g.user_trades("XUSDT") == []


# --------------------------------------------------------------------------- #
# Bookkeeping corners (aegis.gateway.simulation)
# --------------------------------------------------------------------------- #


def test_reduce_only_sell_against_a_short_is_rejected(gw: FakeGateway) -> None:
    gw.set_position("BTCUSDT", -1.0, entry_price=100.0)
    with pytest.raises(OrderRejected) as excinfo:
        gw.place_order(_req(side=Side.SELL, qty=0.5, price=100.1, reduce_only=True))
    assert excinfo.value.code == -2022


def test_a_zero_step_size_disables_the_grid_check(gw: FakeGateway) -> None:
    gw.set_symbol_info("BTCUSDT", step_size=0.0, tick_size=0.0, min_qty=0.0, min_notional=0.0)
    assert gw.place_order(_req(qty=0.123456789, price=99.87654321)).status is OrderStatus.NEW


def test_a_partial_reduce_keeps_the_original_entry_price(gw: FakeGateway) -> None:
    opening = gw.place_order(_req(qty=4.0, price=99.9))
    gw.fill_order(opening.order_id, price=100.0)
    closing = gw.place_order(_req(side=Side.SELL, qty=1.0, price=100.1, reduce_only=True))
    gw.fill_order(closing.order_id, price=110.0)
    pos = gw.positions()["BTCUSDT"]
    assert pos.qty == pytest.approx(3.0)
    assert pos.entry_price == pytest.approx(100.0)
    assert gw.sim.realized_pnl == pytest.approx(10.0)
    assert gw.sim.position("BTCUSDT").is_flat is False


def test_a_transfer_moves_the_wallet_without_touching_pnl(gw: FakeGateway, clock: FakeClock) -> None:
    gw.sim.transfer(2_500.0, clock.now_ms())
    assert gw.account().wallet_balance == pytest.approx(12_500.0)
    assert gw.sim.realized_pnl == 0.0
    row = gw.income(0)[-1]
    assert row["incomeType"] == str(IncomeType.TRANSFER)
    assert IncomeType.parse(row["incomeType"]).is_transfer
    assert row["symbol"] == ""


def test_daily_bars_limit_truncates_the_way_binance_does(gw: FakeGateway) -> None:
    """Forward from a start: the oldest ``limit``; open-ended: the newest ones."""
    gw.set_closes("BTCUSDT", [100.0 + i for i in range(10)], start_day=date(2026, 1, 1))
    assert [b.day for b in gw.daily_bars("BTCUSDT", start=date(2026, 1, 1), limit=2)] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
    ]
    assert [b.day for b in gw.daily_bars("BTCUSDT", limit=2)] == [date(2026, 1, 9), date(2026, 1, 10)]
    assert gw.daily_bars("BTCUSDT", limit=0) == []
