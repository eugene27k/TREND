"""US-11 (CARRY, reused) — snapshots; US-T08 AC 3 — the time-weighted equity curve."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.accounting.snapshots import SnapshotService
from aegis.core.clock import DAY_MS, FakeClock, day_start_ms
from aegis.core.context import Context
from aegis.core.errors import DataGap
from aegis.core.types import IncomeType, LedgerEntry, Strategy
from aegis.gateway.fake import FakeGateway
from tests.accounting.conftest import DAY, T0

DAY0 = DAY


def _book_transfer(ctx: Context, amount: float, ts_ms: int) -> None:
    ctx.repos.ledger.add_many(
        [
            LedgerEntry(
                strategy=Strategy.TREND,
                ts_ms=ts_ms,
                income_type=IncomeType.TRANSFER,
                asset="USDT",
                amount=amount,
                tran_id=f"tr-{ts_ms}-{amount}",
            )
        ]
    )


def _walk(
    ctx: Context,
    gateway: FakeGateway,
    clock: FakeClock,
    equities: list[float],
    transfers: dict[int, float] | None = None,
) -> list[date]:
    """Snapshot one day at a time at 00:00, with optional same-day transfers."""
    service = SnapshotService(ctx)
    days: list[date] = []
    for i, equity in enumerate(equities):
        day = DAY0 + timedelta(days=i)
        ts = day_start_ms(day)
        clock.set(ts)
        if transfers and i in transfers:
            _book_transfer(ctx, transfers[i], ts + 1000)
        gateway.set_account(wallet_balance=equity)
        service.take(ts)
        service.update_equity_curve(day, ts)
        days.append(day)
    return days


# --------------------------------------------------------------------------- #
# take()
# --------------------------------------------------------------------------- #


def test_us_11_ac1_take_persists_account_positions_and_signed_exposure(
    ctx: Context, gateway: FakeGateway
) -> None:
    gateway.set_position("BTCUSDT", 10.0, entry_price=100.0, mark_price=110.0)
    gateway.set_position("ETHUSDT", -20.0, entry_price=50.0, mark_price=49.0)

    account = SnapshotService(ctx).take(T0)

    row = ctx.repos.snapshots.latest()
    assert row is not None
    assert row["ts"] == T0 == account.ts_ms
    assert row["gross_notional"] == pytest.approx(10 * 110 + 20 * 49)
    assert row["net_notional"] == pytest.approx(10 * 110 - 20 * 49)
    assert set(ctx.repos.positions.all()) == {"BTCUSDT", "ETHUSDT"}
    assert ctx.repos.positions.all()["ETHUSDT"].qty == pytest.approx(-20.0)


def test_us_11_ac1_take_replaces_the_local_position_table(ctx: Context, gateway: FakeGateway) -> None:
    gateway.set_position("BTCUSDT", 10.0, entry_price=100.0, mark_price=100.0)
    service = SnapshotService(ctx)
    service.take(T0)

    gateway.sim.set_position("BTCUSDT", 0.0, 0.0, mark_price=100.0)
    service.take(T0 + 60_000)

    assert ctx.repos.positions.all() == {}


def test_us_11_ac1_update_equity_curve_without_a_snapshot_raises(ctx: Context) -> None:
    with pytest.raises(DataGap):
        SnapshotService(ctx).update_equity_curve(DAY0, T0)


# --------------------------------------------------------------------------- #
# US-T08 AC 3 — transfers must not move the peak
# --------------------------------------------------------------------------- #


def test_us_t08_ac3_mid_period_deposit_creates_no_fake_peak_and_keeps_the_drawdown(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """10 000 -> 9 000 (a real 10 % drawdown) -> +5 000 deposited.

    Raw equity of 14 000 is an all-time high; time-weighted, nothing happened.
    """
    service = SnapshotService(ctx)
    days = _walk(ctx, gateway, clock, [10_000.0, 9_000.0, 14_000.0], transfers={2: 5_000.0})

    rows = {r["day"]: r for r in ctx.repos.equity.all()}
    last = rows[days[2].isoformat()]

    assert last["net_transfer"] == pytest.approx(5_000.0)
    assert last["twr_factor"] == pytest.approx(1.0)  # (14000 - 5000) / 9000
    assert last["twr_index"] == pytest.approx(0.9)
    assert last["peak_index"] == pytest.approx(1.0)  # the deposit did not print a peak
    assert last["drawdown"] == pytest.approx(0.10)
    assert service.drawdown(day_start_ms(days[2])) == pytest.approx(0.10)


def test_us_t08_ac3_withdrawal_does_not_invent_a_drawdown(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    days = _walk(ctx, gateway, clock, [10_000.0, 11_000.0, 6_000.0], transfers={2: -5_000.0})

    last = {r["day"]: r for r in ctx.repos.equity.all()}[days[2].isoformat()]

    assert last["twr_factor"] == pytest.approx(1.0)  # (6000 + 5000) / 11000
    assert last["drawdown"] == pytest.approx(0.0)
    assert SnapshotService(ctx).drawdown(day_start_ms(days[2])) == pytest.approx(0.0)


def test_us_t08_ac3_real_losses_still_produce_a_drawdown(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    days = _walk(ctx, gateway, clock, [10_000.0, 12_000.0, 9_600.0])

    last = {r["day"]: r for r in ctx.repos.equity.all()}[days[2].isoformat()]

    assert last["peak_index"] == pytest.approx(1.2)
    assert last["drawdown"] == pytest.approx(0.20)


def test_us_11_ac1_the_curve_is_idempotent_and_recomputed_in_order(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """Re-running a day (and writing days out of order) must not change the path."""
    days = _walk(ctx, gateway, clock, [10_000.0, 9_000.0, 9_500.0])
    before = ctx.repos.equity.all()

    service = SnapshotService(ctx)
    for day in reversed(days):
        service.update_equity_curve(day, day_start_ms(day) + DAY_MS - 1)

    assert ctx.repos.equity.all() == before


def test_us_11_ac1_drawdown_ignores_points_after_now(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    days = _walk(ctx, gateway, clock, [10_000.0, 8_000.0, 10_000.0])
    service = SnapshotService(ctx)

    assert service.drawdown(day_start_ms(days[1])) == pytest.approx(0.20)
    assert service.drawdown(day_start_ms(days[2])) == pytest.approx(0.0)


def test_us_11_ac1_transfers_are_read_from_the_ledger_for_the_day_only(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    _book_transfer(ctx, 1_000.0, T0 + 3_600_000)
    _book_transfer(ctx, 250.0, T0 + DAY_MS + 3_600_000)
    service = SnapshotService(ctx)
    gateway.set_account(wallet_balance=11_000.0)
    service.take(T0 + DAY_MS - 1)

    point = service.update_equity_curve(DAY0, T0 + DAY_MS - 1)

    assert point.net_transfer == pytest.approx(1_000.0)
    assert service.net_transfer(T0, T0 + 2 * DAY_MS) == pytest.approx(1_250.0)
