"""US-11 (CARRY, reused for TREND) — income ledger sync."""

from __future__ import annotations

import pytest

from aegis.accounting.ledger import DEFAULT_OVERLAP_MS, LEDGER_SYNC_FAILED, LedgerService
from aegis.core.clock import FakeClock
from aegis.core.context import Context
from aegis.core.errors import ExchangeUnreachable
from aegis.core.types import IncomeType, Side
from aegis.gateway.fake import FakeGateway
from tests.accounting.conftest import HOUR_MS, T0, alert_codes, raw_income


def _busy_day(gateway: FakeGateway) -> None:
    """A day with one trade, one funding settlement and one deposit."""
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=10.0, price=100.0, ts_ms=T0 + HOUR_MS)
    gateway.settle_funding("BTCUSDT", 0.0001, T0 + 8 * HOUR_MS)
    gateway.sim.transfer(500.0, T0 + 10 * HOUR_MS)


# --------------------------------------------------------------------------- #
# AC 1 — idempotence
# --------------------------------------------------------------------------- #


def test_us_11_ac1_resyncing_the_same_window_does_not_double_count(
    ctx: Context, gateway: FakeGateway
) -> None:
    _busy_day(gateway)
    service = LedgerService(ctx)
    now = T0 + 12 * HOUR_MS

    first = service.sync(now)
    rows_after_first = ctx.repos.ledger.count()
    second = service.sync(now)

    assert first == rows_after_first > 0
    assert second == 0
    assert ctx.repos.ledger.count() == rows_after_first


def test_us_11_ac1_a_replayed_window_books_nothing_even_from_scratch(
    ctx: Context, gateway: FakeGateway
) -> None:
    """Re-reading from the epoch (a restored cursor) must still not duplicate."""
    _busy_day(gateway)
    service = LedgerService(ctx)
    now = T0 + 12 * HOUR_MS
    service.sync(now)
    count = ctx.repos.ledger.count()

    assert service.backfill(0) == 0
    assert ctx.repos.ledger.count() == count


# --------------------------------------------------------------------------- #
# AC 1 — the cursor and its overlap
# --------------------------------------------------------------------------- #


def test_us_11_ac1_cursor_is_rewound_by_the_overlap(ctx: Context, gateway: FakeGateway) -> None:
    _busy_day(gateway)
    now = T0 + 12 * HOUR_MS

    LedgerService(ctx).sync(now)

    last_ts = ctx.repos.ledger.last_ts()
    assert last_ts == T0 + 10 * HOUR_MS
    assert ctx.repos.ledger.sync_cursor() == last_ts - DEFAULT_OVERLAP_MS


def test_us_11_ac1_settlement_written_late_inside_the_overlap_is_still_booked(
    ctx: Context, gateway: FakeGateway
) -> None:
    _busy_day(gateway)
    service = LedgerService(ctx)
    service.sync(T0 + 12 * HOUR_MS)
    before = ctx.repos.ledger.count()

    # The venue back-dates a funding settlement into a window we already read.
    raw_income(gateway, "FUNDING_FEE", -0.25, T0 + 10 * HOUR_MS - 1800_000, symbol="BTCUSDT")

    assert service.sync(T0 + 13 * HOUR_MS) == 1
    assert ctx.repos.ledger.count() == before + 1


def test_us_11_ac1_an_empty_response_never_advances_the_cursor(ctx: Context) -> None:
    """An empty answer is not proof of no income — it is also how an outage looks."""
    service = LedgerService(ctx)

    assert service.sync(T0 + HOUR_MS) == 0
    assert ctx.repos.ledger.sync_cursor() is None


def test_us_11_ac1_rows_older_than_the_cursor_are_booked_without_rewinding_it(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    _busy_day(gateway)
    service = LedgerService(ctx)
    clock.advance(hours=12)
    service.sync(T0 + 12 * HOUR_MS)
    cursor = ctx.repos.ledger.sync_cursor()
    ctx.repos.ledger.db.execute("DELETE FROM ledger")

    booked = service.backfill(0)

    assert booked == ctx.repos.ledger.count() > 0
    assert ctx.repos.ledger.sync_cursor() == cursor


# --------------------------------------------------------------------------- #
# AC 2 — mapping and pagination
# --------------------------------------------------------------------------- #


def test_us_11_ac2_unknown_income_type_is_booked_as_other(ctx: Context, gateway: FakeGateway) -> None:
    raw_income(gateway, "SOME_NEW_BINANCE_TYPE", -1.5, T0 + HOUR_MS, symbol="BTCUSDT")

    assert LedgerService(ctx).sync(T0 + 2 * HOUR_MS) == 1

    row = ctx.repos.ledger.between(T0, T0 + 2 * HOUR_MS)[0]
    assert row["income_type"] == str(IncomeType.OTHER)
    assert row["amount"] == pytest.approx(-1.5)
    assert row["symbol"] == "BTCUSDT"


def test_us_11_ac2_transfers_are_booked_and_flagged_as_capital_flow(
    ctx: Context, gateway: FakeGateway
) -> None:
    gateway.sim.transfer(-250.0, T0 + HOUR_MS)

    LedgerService(ctx).sync(T0 + 2 * HOUR_MS)

    rows = ctx.repos.ledger.between(T0, T0 + 2 * HOUR_MS)
    assert [IncomeType.parse(r["income_type"]).is_transfer for r in rows] == [True]
    assert rows[0]["amount"] == pytest.approx(-250.0)


def test_us_11_ac2_pagination_collects_every_row_when_the_venue_caps_the_page(
    ctx: Context, gateway: FakeGateway
) -> None:
    for i in range(7):
        raw_income(gateway, "FUNDING_FEE", -0.1, T0 + i * HOUR_MS, symbol="BTCUSDT")

    booked = LedgerService(ctx, page_limit=2).sync(T0 + 8 * HOUR_MS)

    assert booked == 7
    assert ctx.repos.ledger.count() == 7


def test_us_11_ac2_a_full_page_sharing_one_timestamp_still_terminates(
    ctx: Context, gateway: FakeGateway
) -> None:
    """More rows in one millisecond than the page holds must not be dropped."""
    for i in range(4):
        raw_income(gateway, "COMMISSION", -0.01, T0 + HOUR_MS, symbol="BTCUSDT", tran_id=str(500 + i))

    assert LedgerService(ctx, page_limit=2).sync(T0 + 2 * HOUR_MS) == 4


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


def test_us_11_ac3_gateway_failure_alerts_and_propagates(ctx: Context, gateway: FakeGateway) -> None:
    gateway.inject_error("income", ExchangeUnreachable("income endpoint down"))

    with pytest.raises(ExchangeUnreachable):
        LedgerService(ctx).sync(T0 + HOUR_MS)

    assert LEDGER_SYNC_FAILED in alert_codes(ctx)


def test_us_11_ac3_rows_read_before_a_mid_page_failure_are_still_booked(
    ctx: Context, gateway: FakeGateway
) -> None:
    for i in range(4):
        raw_income(gateway, "FUNDING_FEE", -0.1, T0 + i * HOUR_MS, symbol="BTCUSDT")

    healthy = gateway.income
    calls = {"n": 0}

    def flaky(start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ExchangeUnreachable("connection dropped between pages")
        return healthy(start_ms, end_ms, limit)

    gateway.income = flaky  # type: ignore[method-assign]
    service = LedgerService(ctx, page_limit=2)

    with pytest.raises(ExchangeUnreachable):
        service.sync(T0 + 8 * HOUR_MS)
    assert ctx.repos.ledger.count() == 2  # the first page survived the failure

    gateway.income = healthy  # type: ignore[method-assign]
    assert service.sync(T0 + 8 * HOUR_MS) == 2
    assert ctx.repos.ledger.count() == 4


def test_us_11_ac2_a_malformed_income_row_is_booked_rather_than_crashing_the_sync(
    ctx: Context, gateway: FakeGateway
) -> None:
    """An unparseable amount must not stop the rest of the day from being booked."""
    broken = raw_income(gateway, "FUNDING_FEE", -0.1, T0 + HOUR_MS, symbol="BTCUSDT")
    broken["income"] = "not-a-number"
    raw_income(gateway, "FUNDING_FEE", -0.2, T0 + 2 * HOUR_MS, symbol="ETHUSDT")

    assert LedgerService(ctx).sync(T0 + 3 * HOUR_MS) == 2

    amounts = sorted(r["amount"] for r in ctx.repos.ledger.between(0, T0 + 3 * HOUR_MS))
    assert amounts == [pytest.approx(-0.2), pytest.approx(0.0)]
