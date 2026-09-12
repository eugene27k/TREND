"""US-T09 AC 3-4 and US-T10: persistence, slicing, reduce-only, the window, risk cuts."""

from __future__ import annotations

import pytest

from aegis.core.clock import FakeClock, day_of
from aegis.core.config import AppConfig
from aegis.core.context import Context
from aegis.core.errors import ExchangeUnreachable, OrderRejected
from aegis.core.types import (
    PlannedOrder,
    RebalanceStatus,
    Side,
    TimeInForce,
)
from aegis.gateway.fake import FakeGateway
from aegis.rebalance.executor import (
    ALERT_ILLIQUID,
    ALERT_REDUCE_ONLY_REJECTED,
    ALERT_WINDOW_END,
    RebalanceExecutor,
)
from aegis.storage.db import json_loads
from aegis.storage.repositories import Repositories
from tests.rebalance.conftest import BTC, ETH, fill_hook

HOUR_MS = 3_600_000


def buy(symbol: str, qty: float, mark: float, *, seq: int = 0, n_slices: int = 1) -> PlannedOrder:
    return PlannedOrder(
        symbol=symbol,
        side=Side.BUY,
        delta_notional=qty * mark,
        delta_qty=qty,
        current_qty=0.0,
        target_qty=qty,
        reduce_only=False,
        risk_reducing=False,
        sequence=seq,
        n_slices=n_slices,
    )


def close(symbol: str, qty: float, mark: float, *, seq: int = 0) -> PlannedOrder:
    return PlannedOrder(
        symbol=symbol,
        side=Side.SELL if qty > 0 else Side.BUY,
        delta_notional=-qty * mark,
        delta_qty=-qty,
        current_qty=qty,
        target_qty=0.0,
        reduce_only=True,
        risk_reducing=True,
        sequence=seq,
        n_slices=1,
    )


def mids() -> dict[str, float]:
    return {BTC: 100.0, ETH: 50.0}


def alert_codes(repos: Repositories) -> list[str]:
    return [row["code"] for row in repos.alerts.recent()]


# --------------------------------------------------------------------------- #
# US-T09 AC 3 — the plan is persisted before any order exists
# --------------------------------------------------------------------------- #


def test_us_t09_ac3_plan_is_persisted_before_the_first_order(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.inject_error("place_order", ExchangeUnreachable("venue down"))
    plan = [buy(BTC, 1.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]

    with pytest.raises(ExchangeUnreachable):
        RebalanceExecutor(ctx).execute(
            "reb-1", plan, decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS
        )

    row = repos.rebalances.get("reb-1")
    assert row is not None
    assert row["status"] == str(RebalanceStatus.RUNNING)
    assert row["cursor"] == 0
    assert [p["symbol"] for p in json_loads(row["order_plan_json"], [])] == [BTC, ETH]
    assert row["planned_notional"] == pytest.approx(200.0)
    assert json_loads(row["decision_mids_json"], {}) == mids()


def test_us_t09_ac3_cursor_advances_only_after_an_entry_completes(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    plan = [buy(BTC, 1.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]
    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS)
    assert repos.rebalances.get("reb-1")["cursor"] == 2


# --------------------------------------------------------------------------- #
# US-T09 AC 4 — resume
# --------------------------------------------------------------------------- #


def test_us_t09_ac4_resume_continues_from_the_cursor_and_does_not_retrade(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    """A window that closes between the two waves leaves the second one to resume.

    Symbols inside a wave are worked concurrently, so an interruption is
    constructed across the wave boundary: the risk-reducing wave runs first and
    consumes the window, and the risk-increasing wave never starts (5.8).
    """

    def slow_fill(gateway: FakeGateway, order) -> None:
        fill_hook()(gateway, order)
        gateway.clock.advance(minutes=40)  # the window closes under us

    venue.set_position(BTC, qty=1.0, entry_price=100.0)
    venue.set_fill_policy("callable", slow_fill)
    start = clock.now_ms()
    plan = [close(BTC, 1.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]

    first = RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=start + 30 * 60_000)
    assert first.status is RebalanceStatus.WINDOW_END
    assert repos.rebalances.get("reb-1")["cursor"] == 1
    assert venue.positions().get(BTC) is None or venue.positions()[BTC].qty == pytest.approx(0.0)
    assert ETH not in venue.positions()

    placed_before = len(venue.placed)
    venue.set_fill_policy("callable", fill_hook())
    resumed = RebalanceExecutor(ctx).resume("reb-1", clock.now_ms() + HOUR_MS)

    new_requests = venue.placed[placed_before:]
    assert [r.symbol for r in new_requests] == [ETH]  # BTC is done; it is not touched again
    assert venue.positions()[ETH].qty == pytest.approx(2.0)
    assert resumed.status is RebalanceStatus.COMPLETE
    assert repos.rebalances.get("reb-1")["cursor"] == 2


def test_symbols_inside_a_wave_are_worked_concurrently(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    """Both orders must reach the venue before either escalates.

    Sequentially the second symbol would not be touched until the first had run
    its whole five-minute escalation, which is what makes a sixteen-symbol
    rebalance impossible inside the 55-minute window.
    """
    venue.set_fill_policy("none")
    start = clock.now_ms()
    plan = [buy(BTC, 1.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]
    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=start + HOUR_MS)

    placed = [(r.symbol, r.time_in_force) for r in venue.placed]
    passive = [s for s, tif in placed if tif is TimeInForce.GTX]
    assert passive[:2] == [BTC, ETH], f"both passive orders should go out first, got {placed}"
    first_taker = next(i for i, (_, tif) in enumerate(placed) if tif is TimeInForce.IOC)
    assert first_taker >= 2, "no symbol may escalate before every symbol has been quoted"


def test_resume_of_an_unknown_rebalance_raises(ctx: Context) -> None:
    from aegis.core.errors import AegisError

    with pytest.raises(AegisError):
        RebalanceExecutor(ctx).resume("nope", 0)


# --------------------------------------------------------------------------- #
# US-T10 AC 2 — reduce-only
# --------------------------------------------------------------------------- #


def test_us_t10_ac2_reduce_only_is_rejected_and_does_not_flip_the_position(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.set_position(BTC, 1.0, 100.0)
    # A stale target that would sell through zero — the venue must refuse it.
    hostile = PlannedOrder(
        symbol=BTC,
        side=Side.SELL,
        delta_notional=-200.0,
        delta_qty=-2.0,
        current_qty=1.0,
        target_qty=-1.0,
        reduce_only=True,
        risk_reducing=True,
        sequence=0,
        n_slices=1,
    )

    RebalanceExecutor(ctx).execute(
        "reb-1", [hostile], decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS
    )

    assert venue.positions()[BTC].qty == pytest.approx(1.0)  # not flipped, not reduced
    assert all(r.reduce_only for r in venue.placed)  # never retried without the flag
    slices = repos.slices.for_rebalance("reb-1")
    assert [s["outcome"] for s in slices] == ["rejected"]
    assert ALERT_REDUCE_ONLY_REJECTED in alert_codes(repos)


def test_us_t10_ac2_reduce_only_flag_comes_from_the_plan(
    ctx: Context, venue: FakeGateway, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.set_position(BTC, 2.0, 100.0)
    plan = [close(BTC, 2.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]

    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS)

    by_symbol = {r.symbol: r for r in venue.placed}
    assert by_symbol[BTC].reduce_only is True
    assert by_symbol[ETH].reduce_only is False


# --------------------------------------------------------------------------- #
# US-T10 AC 3 — TWAP slicing that adapts to fills
# --------------------------------------------------------------------------- #


def test_us_t10_ac3_twenty_five_slices_spread_evenly_over_the_twap_window(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock, cfg: AppConfig
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    start = clock.now_ms()
    plan = [buy(BTC, 2.5, 100.0, n_slices=25)]

    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=start + 2 * HOUR_MS)

    rows = repos.slices.for_rebalance("reb-1")
    assert len(rows) == 25 <= cfg.exec.max_slices
    spacing = {rows[i + 1]["placed_ts"] - rows[i]["placed_ts"] for i in range(len(rows) - 1)}
    assert spacing == {cfg.exec.max_twap_min * 60_000 // 25}  # evenly spaced
    assert rows[-1]["placed_ts"] - start <= cfg.exec.max_twap_min * 60_000
    assert venue.positions()[BTC].qty == pytest.approx(2.5)


def test_us_t10_ac3_slices_adapt_to_fills_and_never_over_trade(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    def over_fill(gateway: FakeGateway, order) -> None:
        fill_hook()(gateway, order)
        # Price improvement: the first slice ends up taking the whole target.
        gateway.set_position(BTC, 4.0, 100.0)

    venue.set_fill_policy("callable", over_fill)
    plan = [buy(BTC, 4.0, 100.0, n_slices=4)]

    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS)

    assert len(venue.placed) == 1  # the remaining three slices have nothing to do
    assert venue.positions()[BTC].qty == pytest.approx(4.0)
    assert len(repos.slices.for_rebalance("reb-1")) == 1


def test_slice_smaller_than_min_notional_is_merged_into_one_order(
    ctx: Context, venue: FakeGateway, clock: FakeClock
) -> None:
    venue.set_symbol_info(BTC, min_notional=50.0)
    venue.set_fill_policy("callable", fill_hook())
    # 100 USDT split 10 ways would be 10 USDT a slice — under the venue floor.
    plan = [buy(BTC, 1.0, 100.0, n_slices=10)]

    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS)

    assert [r.qty for r in venue.placed] == [pytest.approx(1.0)]


# --------------------------------------------------------------------------- #
# US-T10 AC 1 — slice lifecycle: re-peg, escalate
# --------------------------------------------------------------------------- #


def test_us_t10_ac1_slice_repegs_when_the_passive_best_moves(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock, cfg: AppConfig
) -> None:
    moves: list[int] = []

    def move_book_once(gateway: FakeGateway, order) -> None:
        if order.time_in_force is TimeInForce.IOC:
            fill_hook()(gateway, order)
            return
        if not moves:
            moves.append(1)
            gateway.set_book(BTC, bid=100.49, ask=100.51)

    venue.set_fill_policy("callable", move_book_once)
    start = clock.now_ms()

    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=start + HOUR_MS
    )

    row = repos.slices.for_rebalance("reb-1")[0]
    assert row["repegs"] == 1
    assert row["outcome"] == "escalated"
    assert row["taker"] == 1

    prices = [r.price for r in venue.placed]
    assert prices[0] == pytest.approx(99.99)  # first passive peg: the old bid
    assert prices[1] == pytest.approx(100.49)  # re-pegged to the new bid
    assert venue.placed[-1].time_in_force is TimeInForce.IOC
    assert venue.placed[-1].price == pytest.approx(100.51)  # taker crosses the ask


def test_us_t10_ac1_unfilled_slice_escalates_to_ioc_after_escalate_s(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock, cfg: AppConfig
) -> None:
    start = clock.now_ms()

    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=start + HOUR_MS
    )

    ioc = [o for o in repos.orders.for_rebalance("reb-1") if o.time_in_force is TimeInForce.IOC]
    assert len(ioc) == 1
    assert ioc[0].created_ts_ms - start == cfg.exec.escalate_s * 1000
    assert repos.slices.for_rebalance("reb-1")[0]["outcome"] == "escalated"


def test_us_t10_ac1_post_only_rejection_repegs_one_tick_away(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.fail_next("place_order", OrderRejected("would take", -5022))

    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS
    )

    assert [r.price for r in venue.placed] == [pytest.approx(99.98)]  # one tick below the bid
    assert all(r.time_in_force is TimeInForce.GTX for r in venue.placed)  # no early escalation
    assert repos.slices.for_rebalance("reb-1")[0]["repegs"] == 1


# --------------------------------------------------------------------------- #
# US-T10 AC 4 — the stored summary
# --------------------------------------------------------------------------- #


def test_us_t10_ac4_summary_records_notional_fees_slippage_maker_ratio_and_completion(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook(maker=True))
    outcome = RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids={BTC: 100.0}, end_ts_ms=clock.now_ms() + HOUR_MS
    )

    assert outcome.status is RebalanceStatus.COMPLETE
    assert outcome.traded_notional == pytest.approx(99.99)  # filled at the bid
    assert outcome.planned_notional == pytest.approx(100.0)
    assert outcome.fees == pytest.approx(99.99 * 0.0002)
    assert outcome.maker_ratio == pytest.approx(1.0)
    assert outcome.completion_pct == pytest.approx(99.99)
    assert outcome.avg_slippage_bps == pytest.approx(-1.0)  # bought 1 bp below the mid
    assert outcome.duration_s >= 0.0

    row = repos.rebalances.get("reb-1")
    assert row["status"] == str(RebalanceStatus.COMPLETE)
    assert row["traded_notional"] == pytest.approx(99.99)
    assert row["maker_ratio"] == pytest.approx(1.0)
    assert row["avg_slippage_bps"] == pytest.approx(-1.0)


def test_us_t10_ac4_fills_carry_the_decision_mid_and_maker_flag(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook(maker=True))
    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids={BTC: 100.0}, end_ts_ms=clock.now_ms() + HOUR_MS
    )

    fills = repos.fills.for_rebalance("reb-1")
    assert len(fills) == 1
    assert fills[0].decision_mid == pytest.approx(100.0)
    assert fills[0].slippage_bps == pytest.approx(-1.0)
    assert fills[0].is_maker is True
    assert fills[0].slice_id == "reb-1:BTCUSDT:0"


# --------------------------------------------------------------------------- #
# US-T10 AC 5 — the window ends at 01:00
# --------------------------------------------------------------------------- #


def test_us_t10_ac5_window_end_cancels_orders_and_logs_residuals(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    start = clock.now_ms()
    plan = [buy(BTC, 1.0, 100.0, seq=0), buy(ETH, 2.0, 50.0, seq=1)]

    outcome = RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids=mids(), end_ts_ms=start + 60_000)

    assert outcome.status is RebalanceStatus.WINDOW_END
    assert venue.open_orders() == []
    assert {r["symbol"] for r in outcome.residuals} == {BTC, ETH}
    assert {r["reason"] for r in outcome.residuals} == {"window_end"}
    assert outcome.completion_pct == pytest.approx(0.0)
    assert repos.rebalances.get("reb-1")["status"] == str(RebalanceStatus.WINDOW_END)
    assert ALERT_WINDOW_END in alert_codes(repos)


# --------------------------------------------------------------------------- #
# 5.9 step 6 — illiquid symbols
# --------------------------------------------------------------------------- #


def test_three_consecutive_failures_flag_the_symbol_illiquid(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    from datetime import timedelta

    today = day_of(clock.now_ms())
    repos.illiquid.record_failure(BTC, today - timedelta(days=1), "window_end")
    repos.illiquid.record_failure(BTC, today - timedelta(days=2), "window_end")

    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=clock.now_ms() + 60_000
    )

    assert repos.illiquid.active_symbols(clock.now_ms()) == {BTC}
    assert ALERT_ILLIQUID in alert_codes(repos)


def test_a_single_failure_does_not_flag_the_symbol(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=clock.now_ms() + 60_000
    )
    assert repos.illiquid.active_symbols(clock.now_ms()) == set()
    assert ALERT_ILLIQUID not in alert_codes(repos)


# --------------------------------------------------------------------------- #
# 5.9 step 7 — risk cuts
# --------------------------------------------------------------------------- #


def test_flatten_all_closes_every_position_reduce_only(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.set_position(BTC, 2.0, 100.0)
    venue.set_position(ETH, -3.0, 50.0)

    outcome = RebalanceExecutor(ctx).flatten_all("kill_rule", clock.now_ms())

    assert venue.positions() == {}
    assert all(r.reduce_only for r in venue.placed)
    # Largest exposure first: BTC 200 before ETH 150.
    assert [r.symbol for r in venue.placed] == [BTC, ETH]
    assert repos.rebalances.get(outcome.rebalance_id)["kind"] == "flatten"


def test_risk_cut_escalates_after_the_shortened_sixty_second_deadline(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock, cfg: AppConfig
) -> None:
    venue.set_position(BTC, 2.0, 100.0)
    start = clock.now_ms()

    outcome = RebalanceExecutor(ctx).flatten_all("governor_cut", start)

    ioc = [o for o in repos.orders.for_rebalance(outcome.rebalance_id) if o.time_in_force is TimeInForce.IOC]
    assert len(ioc) == 1
    assert ioc[0].created_ts_ms - start == cfg.exec.risk_escalate_s * 1000 == 60_000


def test_reduce_by_cuts_the_requested_fraction_of_each_position(
    ctx: Context, venue: FakeGateway, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.set_position(BTC, 10.0, 100.0)
    venue.set_position(ETH, -4.0, 50.0)

    RebalanceExecutor(ctx).reduce_by({BTC: 0.25}, "margin_red", clock.now_ms())

    assert venue.positions()[BTC].qty == pytest.approx(7.5)
    assert venue.positions()[ETH].qty == pytest.approx(-4.0)  # untouched
    assert [(r.symbol, r.side, r.qty, r.reduce_only) for r in venue.placed] == [
        (BTC, Side.SELL, pytest.approx(2.5), True)
    ]


def test_reduce_by_ignores_symbols_without_a_position(
    ctx: Context, venue: FakeGateway, clock: FakeClock
) -> None:
    outcome = RebalanceExecutor(ctx).reduce_by({BTC: 0.5}, "margin_red", clock.now_ms())
    assert venue.placed == []
    assert outcome.planned_notional == 0.0
    assert outcome.completion_pct == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Defensive paths
# --------------------------------------------------------------------------- #


def test_us_t10_ac3_position_already_past_the_target_stops_the_symbol(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    def overshoot(gateway: FakeGateway, order) -> None:
        fill_hook()(gateway, order)
        gateway.set_position(BTC, 5.0, 100.0)  # past the 4.0 target

    venue.set_fill_policy("callable", overshoot)

    RebalanceExecutor(ctx).execute(
        "reb-1",
        [buy(BTC, 4.0, 100.0, n_slices=4)],
        decision_mids=mids(),
        end_ts_ms=clock.now_ms() + HOUR_MS,
    )

    # Selling back down to the target would be the engine increasing risk on its
    # own initiative; it simply stops instead.
    assert len(venue.placed) == 1
    assert venue.positions()[BTC].qty == pytest.approx(5.0)


def test_partial_fill_is_repegged_for_the_remainder_only(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    placements: list[int] = []

    def half_fill_then_move(gateway: FakeGateway, order) -> None:
        placements.append(1)
        if len(placements) == 1:
            gateway.fill_order(order.order_id, qty=0.5, is_maker=True)
            gateway.set_book(BTC, bid=100.49, ask=100.51)

    venue.set_fill_policy("callable", half_fill_then_move)

    RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS
    )

    assert venue.placed[0].qty == pytest.approx(1.0)
    assert venue.placed[1].qty == pytest.approx(0.5)  # only what is left
    assert repos.slices.for_rebalance("reb-1")[0]["fill_qty"] == pytest.approx(0.5)


def test_repeated_post_only_rejections_abandon_the_slice_without_escalating(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.inject_error("place_order", OrderRejected("would take", -5022))

    outcome = RebalanceExecutor(ctx).execute(
        "reb-1", [buy(BTC, 1.0, 100.0)], decision_mids=mids(), end_ts_ms=clock.now_ms() + HOUR_MS
    )

    row = repos.slices.for_rebalance("reb-1")[0]
    assert row["outcome"] == "rejected"
    assert row["repegs"] == 3  # one per attempt, each a tick further out
    assert repos.orders.for_rebalance("reb-1") == []  # nothing ever rested
    assert outcome.traded_notional == 0.0


def test_planned_symbol_without_instrument_metadata_is_skipped_not_guessed(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    unknown = buy("XRPUSDT", 100.0, 1.0)

    outcome = RebalanceExecutor(ctx).execute(
        "reb-1", [unknown], decision_mids={}, end_ts_ms=clock.now_ms() + HOUR_MS
    )

    assert venue.placed == []
    assert "ORDER_REJECTED" in alert_codes(repos)
    assert outcome.status is RebalanceStatus.COMPLETE


def test_two_risk_cuts_in_the_same_millisecond_get_distinct_ids(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    venue.set_fill_policy("callable", fill_hook())
    venue.set_position(BTC, 1.0, 100.0)
    executor = RebalanceExecutor(ctx)
    now = clock.now_ms()

    first = executor.flatten_all("kill_rule", now)
    second = executor.flatten_all("kill_rule", now)

    assert first.rebalance_id != second.rebalance_id
    assert repos.rebalances.get(second.rebalance_id) is not None
