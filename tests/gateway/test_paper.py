"""PaperGateway — real market data, locally simulated fills (US-T10 AC 6).

Two things make paper mode trustworthy and both are asserted here: it must never
send an order to the venue it reads prices from, and its costs must be the ones
the backtest charged — the TREND slippage table (Locked Decision 8) plus the
account's real commission rates.
"""

from __future__ import annotations

from datetime import date

import pytest

from aegis.core.clock import FakeClock
from aegis.core.config import AppConfig
from aegis.core.errors import ExchangeUnreachable, GatewayError, OrderRejected
from aegis.core.types import (
    IncomeType,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from aegis.gateway.base import ExchangeGateway
from aegis.gateway.fake import FakeGateway
from aegis.gateway.paper import PaperGateway
from tests.gateway.test_fake import assert_matches_protocol

BPS = 10_000.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock("2026-09-12T00:05:00Z")


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


@pytest.fixture
def inner(clock: FakeClock) -> FakeGateway:
    g = FakeGateway(clock)
    for symbol in ("BTCUSDT", "SOLUSDT"):
        g.set_symbol_info(symbol, tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=5.0)
    g.set_book("BTCUSDT", 99.9, 100.1)
    g.set_book("SOLUSDT", 199.9, 200.1)
    return g


@pytest.fixture
def paper(inner: FakeGateway, clock: FakeClock, cfg: AppConfig) -> PaperGateway:
    return PaperGateway(inner, clock, cfg, starting_balance=10_000.0)


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


def _ioc(**kwargs) -> OrderRequest:
    return _req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.IOC, **kwargs)


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_paper_gateway_satisfies_exchange_gateway_isinstance(paper: PaperGateway) -> None:
    assert isinstance(paper, ExchangeGateway)


def test_paper_gateway_signatures_match_the_protocol_exactly() -> None:
    assert_matches_protocol(PaperGateway)


# --------------------------------------------------------------------------- #
# Market data comes from the real venue
# --------------------------------------------------------------------------- #


def test_market_data_is_read_from_the_inner_gateway(paper: PaperGateway, inner: FakeGateway) -> None:
    inner.set_closes("BTCUSDT", [100.0, 101.0, 102.0], start_day=date(2026, 1, 1))
    inner.set_funding("BTCUSDT", [(1_000, 0.0001)])
    inner.set_predicted_funding("BTCUSDT", 0.0002)

    assert paper.book_ticker("BTCUSDT").mid == pytest.approx(100.0)
    assert paper.mark_price("BTCUSDT") == pytest.approx(100.0)
    assert [b.close for b in paper.daily_bars("BTCUSDT")] == [100.0, 101.0, 102.0]
    assert [f.rate for f in paper.funding_history("BTCUSDT")] == [0.0001]
    assert paper.predicted_funding("BTCUSDT").rate == pytest.approx(0.0002)
    assert paper.server_time_ms() == paper.clock.now_ms()
    assert set(paper.exchange_info()) == {"BTCUSDT", "SOLUSDT"}


def test_exchange_info_is_cached_until_refresh_is_requested(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.exchange_info()
    paper.exchange_info()
    assert inner.calls["exchange_info"] == 1
    paper.exchange_info(refresh=True)
    assert inner.calls["exchange_info"] == 2
    assert inner.exchange_info_refreshes == 1


def test_identity_and_permissions_pass_through(paper: PaperGateway, inner: FakeGateway) -> None:
    inner.set_permissions(futures=True, withdraw=False, ip_restricted=True)
    inner.set_sub_account_name("trend-01")
    inner.set_bnb_balance(0.25)
    assert paper.key_permissions()["futures"] is True
    assert paper.sub_account_name() == "trend-01"
    assert paper.bnb_balance() == pytest.approx(0.25)


def test_closing_paper_closes_the_inner_gateway(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.close()
    assert paper.closed is True
    assert inner.closed is True


# --------------------------------------------------------------------------- #
# US-T10 AC 6 — orders never reach the venue
# --------------------------------------------------------------------------- #


def test_us_t10_ac6_orders_are_never_forwarded_to_the_inner_gateway(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    paper.place_order(_ioc(qty=2.0))
    paper.place_order(_req(qty=1.0, price=99.9))
    paper.cancel_all("BTCUSDT")
    paper.poll()
    assert inner.placed == []
    assert inner.calls["place_order"] == 0
    assert inner.calls["cancel_all"] == 0
    assert inner.positions() == {}
    assert paper.positions()["BTCUSDT"].qty == pytest.approx(2.0)


def test_us_t10_ac6_leverage_and_margin_type_are_recorded_locally_only(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    paper.set_leverage("BTCUSDT", 5)
    paper.set_margin_type("BTCUSDT", "CROSSED")
    assert paper.leverage == {"BTCUSDT": 5}
    assert paper.margin_type == {"BTCUSDT": "CROSSED"}
    assert inner.leverage == {}
    assert inner.margin_type == {}


# --------------------------------------------------------------------------- #
# Taker fills pay the slippage table
# --------------------------------------------------------------------------- #


def test_taker_buy_pays_exactly_the_symbol_slippage_above_the_ask(paper: PaperGateway) -> None:
    order = paper.place_order(_ioc(side=Side.BUY, qty=1.0))
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == pytest.approx(100.1 * (1.0 + 2.0 / BPS))
    assert order.avg_price > 100.1


def test_taker_sell_pays_exactly_the_symbol_slippage_below_the_bid(paper: PaperGateway) -> None:
    order = paper.place_order(_ioc(side=Side.SELL, qty=1.0))
    assert order.avg_price == pytest.approx(99.9 * (1.0 - 2.0 / BPS))
    assert order.avg_price < 99.9


def test_default_slippage_applies_to_symbols_outside_the_table(paper: PaperGateway) -> None:
    assert paper.slippage_bps("BTCUSDT") == pytest.approx(2.0)
    assert paper.slippage_bps("SOLUSDT") == pytest.approx(6.0)
    order = paper.place_order(_ioc(symbol="SOLUSDT", side=Side.BUY, qty=1.0))
    assert order.avg_price == pytest.approx(200.1 * (1.0 + 6.0 / BPS))


def test_a_taker_fill_is_flagged_taker_and_is_immediate(paper: PaperGateway) -> None:
    paper.place_order(_ioc(qty=1.0))
    fill = paper.user_trades("BTCUSDT")[0]
    assert fill.is_maker is False
    assert paper.open_orders() == []


def test_an_ioc_limit_order_is_treated_as_a_taker(paper: PaperGateway) -> None:
    order = paper.place_order(_req(price=100.2, time_in_force=TimeInForce.IOC))
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == pytest.approx(100.1 * (1.0 + 2.0 / BPS))


def test_a_taker_order_without_a_book_is_refused(clock: FakeClock, cfg: AppConfig) -> None:
    bare = FakeGateway(clock)
    bare.set_symbol_info("BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=0.0)
    gw = PaperGateway(bare, clock, cfg)
    with pytest.raises(GatewayError):
        gw.place_order(_ioc(qty=1.0))


# --------------------------------------------------------------------------- #
# Post-only fills only when the market trades through
# --------------------------------------------------------------------------- #


def test_a_resting_post_only_buy_does_not_fill_while_the_ask_stays_above_it(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    order = paper.place_order(_req(side=Side.BUY, price=99.9))
    for _ in range(3):
        paper.poll()
    assert paper.get_order("BTCUSDT", order.order_id).status is OrderStatus.NEW
    inner.set_book("BTCUSDT", 100.5, 100.7)  # market ran away
    paper.poll()
    assert paper.get_order("BTCUSDT", order.order_id).status is OrderStatus.NEW


def test_a_post_only_buy_fills_at_its_own_price_as_maker_when_traded_through(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    order = paper.place_order(_req(side=Side.BUY, price=99.9, qty=2.0))
    inner.set_book("BTCUSDT", 99.5, 99.7)
    paper.poll()
    done = paper.get_order("BTCUSDT", order.order_id)
    assert done.status is OrderStatus.FILLED
    assert done.avg_price == pytest.approx(99.9)  # our limit, no slippage
    fill = paper.user_trades("BTCUSDT")[0]
    assert fill.is_maker is True
    assert fill.price == pytest.approx(99.9)
    assert paper.positions()["BTCUSDT"].qty == pytest.approx(2.0)


def test_a_post_only_sell_fills_when_the_bid_reaches_it(paper: PaperGateway, inner: FakeGateway) -> None:
    order = paper.place_order(_req(side=Side.SELL, price=100.1, qty=1.0))
    paper.poll()
    assert paper.get_order("BTCUSDT", order.order_id).status is OrderStatus.NEW
    inner.set_book("BTCUSDT", 100.1, 100.3)
    paper.poll()
    filled = paper.get_order("BTCUSDT", order.order_id)
    assert filled.status is OrderStatus.FILLED
    assert filled.avg_price == pytest.approx(100.1)
    assert paper.user_trades("BTCUSDT")[0].is_maker is True


def test_a_cancelled_resting_order_never_fills(paper: PaperGateway, inner: FakeGateway) -> None:
    order = paper.place_order(_req(side=Side.BUY, price=99.9))
    paper.cancel_order("BTCUSDT", order.order_id)
    inner.set_book("BTCUSDT", 99.0, 99.2)
    paper.poll()
    assert paper.get_order("BTCUSDT", order.order_id).status is OrderStatus.CANCELED
    assert paper.user_trades("BTCUSDT") == []


def test_cancel_all_clears_the_resting_book(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.place_order(_req(side=Side.BUY, price=99.9))
    paper.place_order(_req(symbol="SOLUSDT", side=Side.BUY, price=199.9))
    paper.cancel_all("BTCUSDT")
    assert [o.symbol for o in paper.open_orders()] == ["SOLUSDT"]
    inner.set_book("BTCUSDT", 99.0, 99.2)
    paper.poll()
    assert paper.positions() == {}


def test_cancelling_an_unknown_or_mismatched_order_is_rejected(paper: PaperGateway) -> None:
    order = paper.place_order(_req())
    with pytest.raises(OrderRejected):
        paper.cancel_order("SOLUSDT", order.order_id)
    with pytest.raises(OrderRejected):
        paper.get_order("SOLUSDT", order.order_id)
    paper.cancel_order("BTCUSDT", order.order_id)
    with pytest.raises(OrderRejected):
        paper.cancel_order("BTCUSDT", order.order_id)


# --------------------------------------------------------------------------- #
# The venue's admission rules apply in paper too
# --------------------------------------------------------------------------- #


def test_us_t10_ac2_reduce_only_over_reduction_is_rejected_in_paper(paper: PaperGateway) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=1.0))
    with pytest.raises(OrderRejected) as excinfo:
        paper.place_order(_ioc(side=Side.SELL, qty=2.0, reduce_only=True))
    assert excinfo.value.code == -2022
    assert paper.positions()["BTCUSDT"].qty == pytest.approx(1.0)


def test_gtx_that_would_cross_is_rejected_in_paper(paper: PaperGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        paper.place_order(_req(side=Side.BUY, price=100.1))
    assert excinfo.value.code == -5022


def test_exchange_filters_are_enforced_from_the_inner_exchange_info(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    inner.set_symbol_info("BTCUSDT", min_notional=1_000.0)
    with pytest.raises(OrderRejected) as excinfo:
        paper.place_order(_req(qty=1.0, price=99.9))
    assert excinfo.value.code == -1013


def test_an_unknown_symbol_is_rejected_in_paper(paper: PaperGateway) -> None:
    with pytest.raises(OrderRejected) as excinfo:
        paper.place_order(_req(symbol="NOPEUSDT"))
    assert excinfo.value.code == -1121


# --------------------------------------------------------------------------- #
# Fees and the wallet
# --------------------------------------------------------------------------- #


def test_maker_and_taker_fees_come_from_the_inner_commission_rate(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    inner.set_commission(0.0001, 0.0004, symbol="BTCUSDT")
    assert paper.commission_rate("BTCUSDT") == (0.0001, 0.0004)

    taker = paper.place_order(_ioc(side=Side.BUY, qty=1.0))
    taker_fill = paper.user_trades("BTCUSDT")[0]
    assert taker_fill.fee == pytest.approx(taker.avg_price * 0.0004)

    resting = paper.place_order(_req(side=Side.BUY, price=99.9, qty=1.0))
    inner.set_book("BTCUSDT", 99.5, 99.7)
    paper.poll()
    maker_fill = paper.user_trades("BTCUSDT")[-1]
    assert maker_fill.order_id == resting.order_id
    assert maker_fill.fee == pytest.approx(99.9 * 0.0001)

    expected = 10_000.0 - taker_fill.fee - maker_fill.fee
    assert paper.account().wallet_balance == pytest.approx(expected)


def test_commission_falls_back_to_config_when_the_venue_will_not_say(
    paper: PaperGateway, inner: FakeGateway, cfg: AppConfig
) -> None:
    inner.set_commission(0.0, 0.001, symbol="BTCUSDT")
    inner.inject_error("commission_rate", ExchangeUnreachable("down"))
    assert paper.commission_rate("BTCUSDT") == (
        cfg.exec.maker_fee_fallback,
        cfg.exec.taker_fee_fallback,
    )


def test_commission_rate_is_cached_per_symbol(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.commission_rate("BTCUSDT")
    paper.commission_rate("BTCUSDT")
    assert inner.calls["commission_rate"] == 1


def test_round_trip_realises_pnl_net_of_both_fees(paper: PaperGateway, inner: FakeGateway) -> None:
    opening = paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    inner.set_book("BTCUSDT", 109.9, 110.1)
    closing = paper.place_order(_ioc(side=Side.SELL, qty=2.0, reduce_only=True))

    expected_pnl = 2.0 * (closing.avg_price - opening.avg_price)
    fees = sum(f.fee for f in paper.user_trades("BTCUSDT"))
    assert paper.sim.realized_pnl == pytest.approx(expected_pnl)
    assert paper.positions() == {}
    assert paper.account().wallet_balance == pytest.approx(10_000.0 + expected_pnl - fees)


def test_income_rows_reconcile_with_the_wallet(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    inner.settle_funding("BTCUSDT", 0.0001, ts_ms=paper.clock.now_ms() + 1_000)
    paper.clock.advance(seconds=2)
    paper.poll()
    inner.set_book("BTCUSDT", 109.9, 110.1)
    paper.place_order(_ioc(side=Side.SELL, qty=2.0, reduce_only=True))

    total = sum(float(r["income"]) for r in paper.income(0))
    assert paper.account().wallet_balance - 10_000.0 == pytest.approx(total)
    assert {r["incomeType"] for r in paper.income(0)} == {
        str(IncomeType.COMMISSION),
        str(IncomeType.FUNDING_FEE),
        str(IncomeType.REALIZED_PNL),
    }


def test_starting_balance_defaults_to_the_configured_phase_capital(
    inner: FakeGateway, clock: FakeClock
) -> None:
    cfg = AppConfig(phase={"current": "P1_PAPER", "capital_usdt": 2_500.0})
    gw = PaperGateway(inner, clock, cfg)
    assert gw.account().wallet_balance == pytest.approx(2_500.0)
    assert PaperGateway(inner, clock, AppConfig()).account().wallet_balance == pytest.approx(10_000.0)


# --------------------------------------------------------------------------- #
# Funding on poll()
# --------------------------------------------------------------------------- #


def test_a_long_pays_funding_on_poll_when_the_rate_is_positive(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    before = paper.account().wallet_balance
    inner.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, 0.0001)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.account().wallet_balance == pytest.approx(before - 0.0001 * 2.0 * 100.0)
    row = paper.income(0)[-1]
    assert row["incomeType"] == str(IncomeType.FUNDING_FEE)
    assert float(row["income"]) == pytest.approx(-0.02)


def test_a_short_receives_funding_on_poll_when_the_rate_is_positive(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    paper.place_order(_ioc(side=Side.SELL, qty=2.0))
    before = paper.account().wallet_balance
    inner.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, 0.0001)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.account().wallet_balance == pytest.approx(before + 0.0001 * 2.0 * 100.0)


def test_a_long_receives_funding_when_the_rate_is_negative(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    before = paper.account().wallet_balance
    inner.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, -0.0002)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.account().wallet_balance == pytest.approx(before + 0.0002 * 2.0 * 100.0)


def test_each_funding_settlement_is_booked_exactly_once(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    t0 = clock.now_ms()
    inner.set_funding("BTCUSDT", [(t0 + 1_000, 0.0001), (t0 + 2_000, 0.0001)])
    clock.advance(seconds=3)
    for _ in range(4):
        paper.poll()
    funding_rows = [r for r in paper.income(0) if r["incomeType"] == str(IncomeType.FUNDING_FEE)]
    assert len(funding_rows) == 2
    assert paper.sim.funding_paid == pytest.approx(-0.04)


def test_settlements_older_than_the_position_are_not_booked(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    inner.set_funding("BTCUSDT", [(clock.now_ms() - 10_000, 0.01)])
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    paper.poll()
    assert [r for r in paper.income(0) if r["incomeType"] == str(IncomeType.FUNDING_FEE)] == []


def test_funding_is_not_booked_while_flat(paper: PaperGateway, inner: FakeGateway, clock: FakeClock) -> None:
    inner.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, 0.0001)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.income(0) == []
    assert paper.sim.funding_paid == 0.0


# --------------------------------------------------------------------------- #
# Positions and marks
# --------------------------------------------------------------------------- #


def test_positions_are_marked_from_the_inner_gateway(paper: PaperGateway, inner: FakeGateway) -> None:
    entry = paper.place_order(_ioc(side=Side.BUY, qty=2.0)).avg_price
    inner.set_book("BTCUSDT", 119.9, 120.1)
    pos = paper.positions()["BTCUSDT"]
    assert pos.mark_price == pytest.approx(120.0)
    assert pos.unrealized_pnl == pytest.approx(2.0 * (120.0 - entry))
    assert paper.account().margin_balance == pytest.approx(
        paper.account().wallet_balance + pos.unrealized_pnl
    )


def test_partial_positions_accumulate_a_weighted_entry_price(paper: PaperGateway, inner: FakeGateway) -> None:
    first = paper.place_order(_ioc(side=Side.BUY, qty=1.0)).avg_price
    inner.set_book("BTCUSDT", 199.9, 200.1)
    second = paper.place_order(_ioc(side=Side.BUY, qty=3.0)).avg_price
    pos = paper.positions()["BTCUSDT"]
    assert pos.qty == pytest.approx(4.0)
    assert pos.entry_price == pytest.approx((first + 3.0 * second) / 4.0)


def test_user_trades_and_open_orders_are_local(paper: PaperGateway, inner: FakeGateway) -> None:
    paper.place_order(_ioc(qty=1.0))
    resting = paper.place_order(_req(price=99.9))
    assert [o.order_id for o in paper.open_orders("BTCUSDT")] == [resting.order_id]
    assert len(paper.user_trades("BTCUSDT")) == 1
    assert inner.user_trades("BTCUSDT") == []


# --------------------------------------------------------------------------- #
# poll() corners
# --------------------------------------------------------------------------- #


def test_a_market_order_is_a_taker_whatever_its_time_in_force(paper: PaperGateway) -> None:
    """Nothing can rest without a limit price, so MARKET always crosses at once."""
    order = paper.place_order(_req(order_type=OrderType.MARKET, price=None, time_in_force=TimeInForce.GTX))
    assert order.status is OrderStatus.FILLED
    assert paper.user_trades("BTCUSDT")[0].is_maker is False
    assert paper.open_orders() == []


def test_a_resting_order_on_a_symbol_with_no_book_is_skipped(clock: FakeClock, cfg: AppConfig) -> None:
    bare = FakeGateway(clock)
    bare.set_symbol_info("BTCUSDT", tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=0.0)
    gw = PaperGateway(bare, clock, cfg)
    order = gw.place_order(_req(price=99.9))
    gw.poll()
    assert gw.get_order("BTCUSDT", order.order_id).status is OrderStatus.NEW


def test_funding_is_skipped_for_a_symbol_that_has_been_closed_out(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=1.0))
    paper.place_order(_ioc(side=Side.SELL, qty=1.0, reduce_only=True))
    assert paper.sim.position_qty("BTCUSDT") == 0.0
    inner.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, 0.01)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.sim.funding_paid == 0.0


def test_a_gateway_that_ignores_the_start_window_still_books_funding_once(
    clock: FakeClock, cfg: AppConfig
) -> None:
    """The guard against double-booking is the applied-watermark, not the query."""

    class LooseFunding(FakeGateway):
        def funding_history(self, symbol, start_ms=None, end_ms=None):
            return super().funding_history(symbol)

    loose = LooseFunding(clock)
    loose.set_symbol_info("BTCUSDT", tick_size=0.1, step_size=0.001, min_qty=0.001, min_notional=5.0)
    loose.set_book("BTCUSDT", 99.9, 100.1)
    gw = PaperGateway(loose, clock, cfg, starting_balance=10_000.0)
    gw.place_order(_ioc(side=Side.BUY, qty=2.0))
    loose.set_funding("BTCUSDT", [(clock.now_ms() + 1_000, 0.0001)])
    clock.advance(seconds=2)
    gw.poll()
    gw.poll()
    assert gw.sim.funding_paid == pytest.approx(-0.02)


# --------------------------------------------------------------------------- #
# Regressions: the fill model may never open risk on its own (Invariant 1)
# --------------------------------------------------------------------------- #


def test_us_t10_ac2_a_resting_reduce_only_order_cannot_open_a_position(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    """5.9 step 4 — "a stale target can never flip a position by accident"."""
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    resting = paper.place_order(_req(side=Side.SELL, price=100.1, qty=2.0, reduce_only=True))
    paper.place_order(_ioc(side=Side.SELL, qty=2.0, reduce_only=True))  # closed another way
    assert paper.sim.position_qty("BTCUSDT") == 0.0

    inner.set_book("BTCUSDT", 100.3, 100.5)  # the market trades through the resting sell
    paper.poll()

    assert paper.sim.position_qty("BTCUSDT") == 0.0
    assert paper.get_order("BTCUSDT", resting.order_id).status is OrderStatus.CANCELED


def test_a_resting_reduce_only_fill_is_truncated_to_the_remaining_position(
    paper: PaperGateway, inner: FakeGateway
) -> None:
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    resting = paper.place_order(_req(side=Side.SELL, price=100.1, qty=2.0, reduce_only=True))
    paper.place_order(_ioc(side=Side.SELL, qty=1.5, reduce_only=True))  # most of it went elsewhere

    inner.set_book("BTCUSDT", 100.3, 100.5)
    paper.poll()

    assert paper.sim.position_qty("BTCUSDT") == pytest.approx(0.0)
    assert paper.get_order("BTCUSDT", resting.order_id).filled_qty == pytest.approx(0.5)
    assert paper.user_trades("BTCUSDT")[-1].qty == pytest.approx(0.5)


def test_funding_that_settled_while_flat_is_not_charged_to_a_reopened_position(
    paper: PaperGateway, inner: FakeGateway, clock: FakeClock
) -> None:
    """Funding is owed by the position that was held, not by the symbol."""
    t0 = clock.now_ms()
    paper.place_order(_ioc(side=Side.BUY, qty=2.0))
    inner.set_funding("BTCUSDT", [(t0 + 1_000, 0.0001)])
    clock.advance(seconds=2)
    paper.poll()
    assert paper.sim.funding_paid == pytest.approx(-0.02)

    paper.place_order(_ioc(side=Side.SELL, qty=2.0, reduce_only=True))
    inner.set_funding("BTCUSDT", [(t0 + 1_000, 0.0001), (t0 + 5_000, 0.01)])  # settled while flat
    clock.advance(seconds=10)
    paper.poll()

    paper.place_order(_ioc(side=Side.BUY, qty=2.0))  # re-open
    paper.poll()
    assert paper.sim.funding_paid == pytest.approx(-0.02)


def test_a_refused_taker_order_leaves_nothing_resting(clock: FakeClock, cfg: AppConfig) -> None:
    bare = FakeGateway(clock)
    bare.set_symbol_info("BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=0.0)
    gw = PaperGateway(bare, clock, cfg)
    with pytest.raises(GatewayError):
        gw.place_order(_ioc(qty=1.0))
    assert gw.open_orders() == []
    bare.set_book("BTCUSDT", 99.9, 100.1)
    gw.poll()
    assert gw.positions() == {}


def test_a_limit_priced_through_the_touch_is_a_taker_not_a_resting_maker(paper: PaperGateway) -> None:
    order = paper.place_order(_req(side=Side.BUY, price=100.5, time_in_force=TimeInForce.GTC))
    assert order.status is OrderStatus.FILLED
    assert order.avg_price == pytest.approx(100.1 * (1.0 + 2.0 / BPS))
    assert paper.user_trades("BTCUSDT")[0].is_maker is False


def test_a_passive_gtc_limit_still_rests(paper: PaperGateway) -> None:
    order = paper.place_order(_req(side=Side.BUY, price=99.8, time_in_force=TimeInForce.GTC))
    assert order.status is OrderStatus.NEW
    assert paper.open_orders() == [order]


def test_exchange_info_hands_out_a_copy_of_the_cache(paper: PaperGateway) -> None:
    info = paper.exchange_info()
    info.pop("BTCUSDT")
    assert "BTCUSDT" in paper.exchange_info()
