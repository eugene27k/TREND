"""US-T11 AC 1: drift between rebalances, and position changes we did not cause."""

from __future__ import annotations

import json

import pytest

from aegis.core.clock import FakeClock, day_of
from aegis.core.context import Context
from aegis.core.types import Fill, Position, Severity, Side
from aegis.gateway.fake import FakeGateway
from aegis.rebalance.drift import ALERT_DRIFT, ALERT_UNEXPLAINED, DriftMonitor
from aegis.rebalance.executor import RebalanceExecutor
from aegis.storage.repositories import Repositories
from tests.rebalance.conftest import BTC, fill_hook, targets

MINUTE_MS = 60_000


def _no_constants(name: str) -> float:
    """A JSON parser that refuses NaN/Infinity, the way a browser does."""
    raise AssertionError(f"{name} is not valid JSON")


def with_last_target(repos: Repositories, clock: FakeClock, notional: float) -> None:
    """Persist a rebalance and its targets — the monitor's reference point."""
    repos.rebalances.create("reb-1", day_of(clock.now_ms()), clock.now_ms())
    repos.targets.save("reb-1", targets({BTC: notional}))


def record_own_fill(repos: Repositories, ts_ms: int, qty: float) -> None:
    repos.fills.add_many(
        [
            Fill(
                trade_id="T1",
                order_id="1",
                symbol=BTC,
                side=Side.BUY,
                qty=qty,
                price=100.0,
                fee=0.02,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=ts_ms,
                rebalance_id="reb-1",
            )
        ]
    )


def test_us_t11_ac1_drift_beyond_the_band_outside_the_window_warns(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 13.0, 100.0)  # 1 300 vs a 1 000 target: 30 %

    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)

    assert len(found) == 1
    assert found[0]["symbol"] == BTC
    assert found[0]["code"] == ALERT_DRIFT
    assert found[0]["severity"] == str(Severity.WARN)
    assert found[0]["drift_frac"] == pytest.approx(0.30)
    assert found[0]["reconcile"] is False
    assert [a["code"] for a in repos.alerts.recent()] == [ALERT_DRIFT]


def test_us_t11_ac1_drift_inside_the_band_is_not_reported(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 11.5, 100.0)  # 15 % — inside drift_frac

    assert DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False) == []


def test_us_t11_ac1_drift_is_suppressed_inside_the_rebalance_window(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 13.0, 100.0)

    assert DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=True) == []
    assert repos.alerts.recent() == []


def test_us_t11_ac1_check_runs_at_most_once_per_drift_interval(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 13.0, 100.0)
    monitor = DriftMonitor(ctx)

    assert monitor.check(clock.now_ms(), in_rebalance_window=False)
    clock.advance(minutes=59)
    assert monitor.check(clock.now_ms(), in_rebalance_window=False) == []
    clock.advance(minutes=1)
    assert monitor.check(clock.now_ms(), in_rebalance_window=False)


def test_us_t11_ac1_unexplained_position_change_is_critical_and_asks_for_reconciliation(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    repos.positions.replace_all(
        [Position(symbol=BTC, qty=10.0, entry_price=100.0, mark_price=100.0)], clock.now_ms()
    )
    venue.set_position(BTC, 5.0, 100.0)  # halved by an ADL: no order of ours

    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)

    assert len(found) == 1
    assert found[0]["code"] == ALERT_UNEXPLAINED
    assert found[0]["severity"] == str(Severity.CRITICAL)
    assert found[0]["previous_qty"] == pytest.approx(10.0)
    assert found[0]["current_qty"] == pytest.approx(5.0)
    assert found[0]["reconcile"] is True
    assert [a["code"] for a in repos.alerts.recent()] == [ALERT_UNEXPLAINED]


def test_us_t11_ac1_position_change_explained_by_our_own_fill_is_not_unexplained(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    repos.positions.replace_all(
        [Position(symbol=BTC, qty=10.0, entry_price=100.0, mark_price=100.0)], clock.now_ms()
    )
    record_own_fill(repos, clock.now_ms() - 10 * MINUTE_MS, 3.0)
    venue.set_position(BTC, 13.0, 100.0)

    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)

    assert [f["code"] for f in found] == [ALERT_DRIFT]  # drift only, no CRITICAL
    assert ALERT_UNEXPLAINED not in [a["code"] for a in repos.alerts.recent()]


def test_first_pass_without_a_persisted_baseline_does_not_cry_wolf(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 10.0, 100.0)  # exactly on target, but no snapshot yet
    monitor = DriftMonitor(ctx)

    assert monitor.check(clock.now_ms(), in_rebalance_window=False) == []

    # The baseline is now established, so a later unexplained move is caught.
    clock.advance(minutes=61)
    venue.set_position(BTC, 4.0, 100.0)
    found = monitor.check(clock.now_ms(), in_rebalance_window=False)
    assert [f["code"] for f in found] == [ALERT_UNEXPLAINED]


def test_a_position_with_no_target_at_all_is_full_drift(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    with_last_target(repos, clock, 0.0)
    repos.positions.replace_all(
        [Position(symbol=BTC, qty=10.0, entry_price=100.0, mark_price=100.0)], clock.now_ms()
    )
    venue.set_position(BTC, 10.0, 100.0)

    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)

    assert [f["code"] for f in found] == [ALERT_DRIFT]
    # 100 % of the position should not be there. Deliberately finite: the record
    # is stored as JSON and rendered by the API, and `Infinity` is not JSON — a
    # strict renderer raises rather than serialising it.
    assert found[0]["drift_frac"] == pytest.approx(1.0)
    stored = json.loads(repos.alerts.recent()[0]["context_json"], parse_constant=_no_constants)
    assert stored["drift_frac"] == pytest.approx(1.0)


def test_a_risk_cut_does_not_erase_the_reference_targets(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    """US-T11 AC 1 compares against the *last target*, and a risk cut sets none.

    ``flatten_all`` / ``reduce_by`` write a ``rebalances`` row of their own and
    never write ``targets``, so "the newest rebalance" stopped being the newest
    row that sized a book: every position read as having a zero target and the
    monitor warned about all of them, for the rest of the day.
    """
    with_last_target(repos, clock, 1_000.0)
    venue.set_position(BTC, 10.0, 100.0)  # exactly on its 1 000 USDT target
    venue.set_fill_policy("callable", fill_hook())

    clock.advance(minutes=5)
    RebalanceExecutor(ctx).reduce_by({BTC: 0.1}, "margin_amber", clock.now_ms())

    assert repos.targets.latest_by_symbol() == {BTC: pytest.approx(1_000.0)}
    clock.advance(minutes=61)
    # 900 against a 1 000 target is 10 % — inside the band, so nothing to report.
    assert DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False) == []


def test_an_open_order_counts_as_our_own_activity(
    ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock
) -> None:
    from aegis.core.types import Order, OrderStatus, OrderType, TimeInForce

    with_last_target(repos, clock, 1_000.0)
    repos.positions.replace_all(
        [Position(symbol=BTC, qty=10.0, entry_price=100.0, mark_price=100.0)], clock.now_ms()
    )
    repos.orders.upsert(
        Order(
            order_id="1",
            client_order_id="c1",
            symbol=BTC,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=1.0,
            price=99.99,
            time_in_force=TimeInForce.GTX,
            reduce_only=False,
            status=OrderStatus.PARTIALLY_FILLED,
            created_ts_ms=clock.now_ms(),
        )
    )
    venue.set_position(BTC, 10.5, 100.0)

    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)
    assert ALERT_UNEXPLAINED not in [f["code"] for f in found]
