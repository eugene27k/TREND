"""Regressions for two defects the end-to-end run exposed.

Both were invisible to unit tests because each component was individually
correct: the bugs lived in the interaction between the window, the escalation
timer and the venue's price grid.
"""

from __future__ import annotations

import pytest

from aegis.core.clock import to_ms
from aegis.core.precision import round_price, round_price_marketable
from aegis.core.types import Side, SymbolInfo, TimeInForce
from aegis.gateway.simulation import crosses_book
from aegis.strategy_trend.runner import TrendRunner

INFO = SymbolInfo("XUSDT", "X", "USDT", "TRADING", "PERPETUAL", 0.001, 0.001, 0.001, 5.0, 3, 3)


# --------------------------------------------------------------------------- #
# 1. A taker IOC must cross, not touch.
# --------------------------------------------------------------------------- #


def test_a_taker_price_rounds_through_the_touch_not_to_the_nearest_tick():
    """An off-grid ask rounded to the nearest tick lands *below* itself.

    The IOC then expires instead of filling, and the 5-minute escalation appears
    to happen while doing nothing at all.
    """
    from aegis.core.types import BookTicker

    ask, bid = 73.48930, 73.41550
    book = BookTicker("XUSDT", bid, 1.0, ask, 1.0, 0)

    naive = round_price(ask, INFO)  # nearest tick -> 73.489
    assert naive < ask
    assert not crosses_book(Side.BUY, naive, book), "this is the bug"

    marketable = round_price_marketable(ask, INFO, Side.BUY)
    assert marketable >= ask
    assert crosses_book(Side.BUY, marketable, book)


def test_the_same_holds_for_a_sell():
    from aegis.core.types import BookTicker

    bid = 73.41559
    book = BookTicker("XUSDT", bid, 1.0, 73.5, 1.0, 0)
    assert not crosses_book(Side.SELL, round_price(bid, INFO), book)
    assert crosses_book(Side.SELL, round_price_marketable(bid, INFO, Side.SELL), book)


def test_marketable_rounding_is_a_no_op_on_an_on_grid_price():
    """The normal case: a venue quoting on its own grid pays nothing for this."""
    assert round_price_marketable(73.489, INFO, Side.BUY) == pytest.approx(73.489)
    assert round_price_marketable(73.489, INFO, Side.SELL) == pytest.approx(73.489)


def test_every_escalated_slice_actually_fills(world):
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:02:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    rebalance_id = world.repos.rebalances.recent(1)[0]["rebalance_id"]
    slices = world.repos.slices.for_rebalance(rebalance_id)
    escalated = [s for s in slices if s["outcome"] == "escalated"]
    assert escalated, "the fixture's passive orders never fill, so all should escalate"
    unfilled = [s["symbol"] for s in escalated if s["fill_qty"] <= 0]
    assert unfilled == [], f"escalated but never filled: {unfilled}"

    ioc = [o for o in world.repos.orders.for_rebalance(rebalance_id) if o.time_in_force is TimeInForce.IOC]
    assert ioc
    assert all(o.filled_qty > 0 for o in ioc), "an IOC that expires is a silent no-op"


# --------------------------------------------------------------------------- #
# 2. Symbols must be worked concurrently.
# --------------------------------------------------------------------------- #


def test_sixteen_symbols_fit_in_the_window(world):
    """Sequentially this needs 16 x 5 = 80 minutes against a 55-minute window."""
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:02:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    row = world.repos.rebalances.recent(1)[0]
    elapsed_min = (row["ended_ts"] - row["started_ts"]) / 60_000
    n_symbols = len({s["symbol"] for s in world.repos.slices.for_rebalance(row["rebalance_id"])})
    assert n_symbols >= 10
    escalate_min = world.cfg.exec.escalate_s / 60
    assert elapsed_min < n_symbols * escalate_min / 2, (
        f"{elapsed_min:.0f} min for {n_symbols} symbols looks sequential"
    )
    assert row["completion_pct"] == pytest.approx(100.0, abs=1.0)


def test_reductions_are_worked_before_increases(world):
    """Two waves: the second must not start until the first is done (5.8)."""
    runner = TrendRunner(world)
    runner.start(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:02:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-08T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    # Day two: yesterday's book exists, so some symbols reduce and some increase.
    world.gateway.set_fill_policy("immediate")
    world.clock.set(to_ms("2026-09-09T00:02:00Z"))
    runner.tick(world.clock.now_ms())
    world.clock.set(to_ms("2026-09-09T00:05:00Z"))
    runner.tick(world.clock.now_ms())

    import json

    for row in world.repos.rebalances.recent(3):
        plan = json.loads(row["order_plan_json"])
        if not plan:
            continue
        reducing = [p["sequence"] for p in plan if p["risk_reducing"]]
        increasing = [p["sequence"] for p in plan if not p["risk_reducing"]]
        if reducing and increasing:
            assert max(reducing) < min(increasing)
            slices = world.repos.slices.for_rebalance(row["rebalance_id"])
            by_symbol = {s["symbol"]: s["placed_ts"] for s in slices}
            reduce_symbols = {p["symbol"] for p in plan if p["risk_reducing"]}
            increase_symbols = {p["symbol"] for p in plan if not p["risk_reducing"]}
            placed_reducing = [by_symbol[s] for s in reduce_symbols if s in by_symbol]
            placed_increasing = [by_symbol[s] for s in increase_symbols if s in by_symbol]
            if placed_reducing and placed_increasing:
                assert max(placed_reducing) <= min(placed_increasing)
