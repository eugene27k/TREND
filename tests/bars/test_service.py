"""US-T03 — daily bars and funding, proved against a programmable venue."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.bars.service import BarService
from aegis.core.clock import FakeClock, at_utc, to_ms
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.errors import DataGap, ExchangeUnreachable
from aegis.core.types import FundingRate, Strategy
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

NOW = "2026-09-08T00:02:00Z"
LAST_CLOSED = date(2026, 9, 7)
SYMBOL = "BTCUSDT"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def ctx(clock: FakeClock) -> Context:
    cfg = load_config("config/trend.yaml", use_env=False)
    repos = Repositories(open_db(":memory:"), Strategy.TREND)
    return Context(
        cfg=cfg,
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )


@pytest.fixture
def bars(ctx: Context) -> BarService:
    return BarService(ctx)


def seed(
    ctx: Context, symbol: str, days: int, *, end: date = LAST_CLOSED, quote_volume: float = 50_000_000.0
) -> list[float]:
    """``days`` daily closes on the venue, ending on ``end``."""
    closes = [100.0 + i for i in range(days)]
    ctx.gateway.set_symbol_info(symbol)
    ctx.gateway.set_closes(
        symbol, closes, start_day=end - timedelta(days=days - 1), quote_volume=quote_volume
    )
    return closes


def alert_codes(ctx: Context) -> list[str]:
    return [r["code"] for r in ctx.repos.alerts.recent()]


# --------------------------------------------------------------------------- #
# AC 1 — backfill and the bar deadline
# --------------------------------------------------------------------------- #


def test_us_t03_ac1_backfill_loads_min_history_days(ctx: Context, bars: BarService) -> None:
    seed(ctx, SYMBOL, 420)

    stored = bars.backfill([SYMBOL])

    assert stored[SYMBOL] >= ctx.cfg.universe.min_history_days == 400
    assert ctx.repos.bars.count(SYMBOL) == stored[SYMBOL]
    assert ctx.repos.bars.latest_day(SYMBOL) == LAST_CLOSED.isoformat()
    assert {b.source for b in ctx.repos.bars.series(SYMBOL)} == {"backfill"}
    # Pagination is the gateway's job: one call per symbol is the whole contract.
    assert ctx.gateway.calls["daily_bars"] == 1


def test_us_t03_ac1_backfill_skips_a_series_that_is_long_enough_and_current(
    ctx: Context, bars: BarService
) -> None:
    seed(ctx, SYMBOL, 420)
    bars.backfill([SYMBOL])

    bars.backfill([SYMBOL])

    assert ctx.gateway.calls["daily_bars"] == 1


def test_us_t03_ac1_backfill_tops_up_a_stale_series(ctx: Context, bars: BarService, clock: FakeClock) -> None:
    # 5.1 step 3 ranks on "the median of the last 30 daily quote volumes", and a
    # candidate outside the traded universe gets no daily bar of its own: if a
    # long-but-stale series were skipped, every later month would rank on the
    # volumes of the first backfill.
    seed(ctx, SYMBOL, 420)  # ... 2026-09-07
    bars.backfill([SYMBOL])
    clock.set(to_ms("2026-10-08T00:02:00Z"))
    seed(ctx, SYMBOL, 450, end=date(2026, 10, 7))  # the same window plus 30 sessions

    stored = bars.backfill([SYMBOL])

    assert ctx.gateway.calls["daily_bars"] == 2
    assert stored[SYMBOL] == 450
    assert ctx.repos.bars.latest_day(SYMBOL) == "2026-10-07"
    assert bars.closes(SYMBOL)[-1] == 100.0 + 449


def test_us_t03_ac1_forward_filled_rows_do_not_pass_for_history(ctx: Context, bars: BarService) -> None:
    # AC 2's placeholders are invisible to the selector's history gate, so they
    # must not make backfill believe the venue has already been asked.
    seed(ctx, SYMBOL, 3, end=LAST_CLOSED - timedelta(days=2))
    bars.backfill([SYMBOL], min_days=3)
    bars.forward_fill([SYMBOL], through=LAST_CLOSED)
    assert ctx.repos.bars.count(SYMBOL) == 5
    calls = ctx.gateway.calls["daily_bars"]

    assert bars.backfill([SYMBOL], min_days=5) == {SYMBOL: 3}
    assert ctx.gateway.calls["daily_bars"] == calls + 1


def test_us_t03_ac1_ensure_day_retries_until_the_bar_arrives(
    ctx: Context, bars: BarService, clock: FakeClock
) -> None:
    seed(ctx, SYMBOL, 10)
    deadline = at_utc(date(2026, 9, 8), ctx.cfg.rebalance.bar_deadline_utc)
    # The venue is unreachable for the first two attempts, then answers.
    ctx.gateway.fail_next("daily_bars", ExchangeUnreachable("boom"), times=2)

    fetched, missing = bars.ensure_day([SYMBOL], LAST_CLOSED, deadline)

    assert fetched == {SYMBOL}
    assert missing == set()
    assert len(clock.slept) == 2
    assert clock.now_ms() < deadline
    assert ctx.repos.bars.has_day(SYMBOL, LAST_CLOSED)
    assert "BAR_MISSING" not in alert_codes(ctx)


def test_us_t03_ac1_ensure_day_stops_at_the_deadline_and_alerts_bar_missing(
    ctx: Context, bars: BarService, clock: FakeClock
) -> None:
    ctx.gateway.set_symbol_info(SYMBOL)  # listed, but no bar will ever arrive
    deadline = at_utc(date(2026, 9, 8), ctx.cfg.rebalance.bar_deadline_utc)

    fetched, missing = bars.ensure_day([SYMBOL], LAST_CLOSED, deadline)

    assert fetched == set()
    assert missing == {SYMBOL}
    assert clock.now_ms() >= deadline, "the retry loop must end once the clock passes 00:04"
    assert clock.slept, "retries must go through ctx.clock.sleep, never time.sleep"
    warn = ctx.repos.alerts.last_of_code("BAR_MISSING")
    assert warn is not None and warn["severity"] == "WARN"


def test_us_t03_ac1_ensure_day_past_the_deadline_makes_exactly_one_attempt(
    ctx: Context, bars: BarService, clock: FakeClock
) -> None:
    ctx.gateway.set_symbol_info(SYMBOL)

    _, missing = bars.ensure_day([SYMBOL], LAST_CLOSED, deadline_ms=clock.now_ms() - 1)

    assert missing == {SYMBOL}
    assert clock.slept == []
    assert ctx.gateway.calls["daily_bars"] == 1


def test_us_t03_ac1_a_forward_filled_day_is_still_owed_a_real_bar(ctx: Context, bars: BarService) -> None:
    seed(ctx, SYMBOL, 3, end=LAST_CLOSED - timedelta(days=1))
    bars.backfill([SYMBOL], min_days=3)
    bars.forward_fill([SYMBOL], through=LAST_CLOSED)

    _, missing = bars.fetch_closed_day([SYMBOL], LAST_CLOSED)

    assert missing == {SYMBOL}


# --------------------------------------------------------------------------- #
# AC 2 — forward fill is for signals, never for P&L
# --------------------------------------------------------------------------- #


def gapped(ctx: Context, symbol: str = SYMBOL) -> tuple[date, list[float]]:
    """Four consecutive sessions with the third day missing entirely."""
    start = date(2026, 9, 1)
    closes = [100.0, 110.0, 121.0, 133.1]
    ctx.gateway.set_symbol_info(symbol)
    ctx.gateway.set_closes(symbol, closes, start_day=start)
    kept = [b for b in ctx.gateway.daily_bars(symbol) if b.day != date(2026, 9, 3)]
    ctx.gateway.set_bars(symbol, kept)
    return start, closes


def test_us_t03_ac2_gap_is_forward_filled_flagged_and_excluded_from_realised_bars(
    ctx: Context, bars: BarService
) -> None:
    gapped(ctx)
    bars.backfill([SYMBOL], min_days=4)
    assert not ctx.repos.bars.has_day(SYMBOL, date(2026, 9, 3))

    written = bars.forward_fill([SYMBOL], through=date(2026, 9, 4))

    assert written == 1
    filled = ctx.repos.bars.series(SYMBOL, start=date(2026, 9, 3), end=date(2026, 9, 3))[0]
    assert filled.filled is True
    assert filled.source == "synthetic"
    assert filled.close == 110.0, "the gap carries the previous close forward"
    assert filled.quote_volume == 0.0
    # Signal continuity sees it...
    assert bars.closes(SYMBOL) == [100.0, 110.0, 110.0, 133.1]
    # ...and every money question structurally cannot.
    assert [b.day for b in bars.realised_bars(SYMBOL)] == [
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 4),
    ]


def test_us_t03_ac2_log_returns_include_the_filled_day_as_zero(ctx: Context, bars: BarService) -> None:
    gapped(ctx)
    bars.backfill([SYMBOL], min_days=4)
    bars.forward_fill([SYMBOL], through=date(2026, 9, 4))

    returns = bars.log_returns(SYMBOL)

    assert len(returns) == 3
    assert returns[1] == 0.0, "no information arrived on a filled day"
    assert returns[0] == pytest.approx(0.0953101798)
    assert bars.log_returns(SYMBOL, limit=2) == pytest.approx(returns[-2:])


def test_us_t03_ac2_forward_fill_invents_nothing_before_the_first_bar(ctx: Context, bars: BarService) -> None:
    seed(ctx, SYMBOL, 3, end=date(2026, 9, 4))
    bars.backfill([SYMBOL], min_days=3)

    bars.forward_fill([SYMBOL], through=date(2026, 9, 4))

    assert ctx.repos.bars.series(SYMBOL)[0].day == date(2026, 9, 2)
    assert bars.forward_fill([SYMBOL], through=date(2026, 9, 4)) == 0


def test_us_t03_ac2_a_real_bar_replaces_the_forward_filled_placeholder(
    ctx: Context, bars: BarService
) -> None:
    gapped(ctx)
    bars.backfill([SYMBOL], min_days=4)
    bars.forward_fill([SYMBOL], through=date(2026, 9, 4))
    late = ctx.gateway.set_closes(SYMBOL, [100.0, 110.0, 121.0, 133.1], start_day=date(2026, 9, 1))

    fetched, missing = bars.fetch_closed_day([SYMBOL], date(2026, 9, 3))

    assert (fetched, missing) == ({SYMBOL}, set())
    row = ctx.repos.bars.series(SYMBOL, start=date(2026, 9, 3), end=date(2026, 9, 3))[0]
    assert row.filled is False
    assert row.close == late[2].close == 121.0
    assert len(bars.realised_bars(SYMBOL)) == 4


# --------------------------------------------------------------------------- #
# Series the rest of the engine consumes
# --------------------------------------------------------------------------- #


def test_us_t03_avg_daily_quote_volume_ignores_forward_filled_bars(ctx: Context, bars: BarService) -> None:
    gapped(ctx)
    bars.backfill([SYMBOL], min_days=4)
    bars.forward_fill([SYMBOL], through=date(2026, 9, 4))

    # Four calendar days, three of which traded 50m: a zero-volume placeholder
    # would drag the execution clip down by a quarter.
    assert bars.avg_daily_quote_volume(SYMBOL, days=30) == pytest.approx(50_000_000.0)
    assert bars.avg_daily_quote_volume("NOPEUSDT") == 0.0


def test_us_t03_volume_history_is_point_in_time(ctx: Context, bars: BarService) -> None:
    seed(ctx, SYMBOL, 40)
    bars.backfill([SYMBOL], min_days=40)

    history = bars.volume_history([SYMBOL], days=10, before=date(2026, 9, 1))

    assert len(history[SYMBOL]) == 10
    assert history[SYMBOL][-1].day == date(2026, 8, 31)


# --------------------------------------------------------------------------- #
# AC 3 / AC 4 — funding
# --------------------------------------------------------------------------- #


def settlements(symbol: str, day: date, rates: list[float]) -> list[FundingRate]:
    base = to_ms(day)
    return [
        FundingRate(symbol=symbol, funding_time_ms=base + i * 8 * 3_600_000, rate=r, interval_hours=8.0)
        for i, r in enumerate(rates)
    ]


def test_us_t03_ac3_sync_funding_is_idempotent(ctx: Context, bars: BarService) -> None:
    ctx.gateway.set_funding(SYMBOL, settlements(SYMBOL, date(2026, 9, 6), [1e-4, 2e-4, 3e-4]))

    first = bars.sync_funding([SYMBOL], ctx.now_ms())
    second = bars.sync_funding([SYMBOL], ctx.now_ms())

    assert first == {SYMBOL: 3}
    assert second == {SYMBOL: 0}, "a second sync over the same window writes nothing"
    stored = ctx.repos.funding.history(SYMBOL)
    assert [f.rate for f in stored] == [1e-4, 2e-4, 3e-4]


def test_us_t03_ac3_sync_funding_resumes_from_the_stored_cursor(ctx: Context, bars: BarService) -> None:
    rows = settlements(SYMBOL, date(2026, 9, 6), [1e-4, 2e-4])
    ctx.gateway.set_funding(SYMBOL, rows)
    bars.sync_funding([SYMBOL], ctx.now_ms())
    later = FundingRate(
        symbol=SYMBOL, funding_time_ms=rows[-1].funding_time_ms + 8 * 3_600_000, rate=5e-4, interval_hours=4.0
    )
    ctx.gateway.set_funding(SYMBOL, [*rows, later])

    added = bars.sync_funding([SYMBOL], ctx.now_ms())

    assert added == {SYMBOL: 1}
    stored = ctx.repos.funding.history(SYMBOL)
    assert len(stored) == 3
    assert stored[-1].interval_hours == 4.0


def test_us_t03_ac4_predicted_funding_annualises_with_the_symbol_interval(
    ctx: Context, bars: BarService
) -> None:
    ctx.gateway.set_predicted_funding(SYMBOL, 0.0004, interval_hours=8.0)
    ctx.gateway.set_predicted_funding("ETHUSDT", 0.0004, interval_hours=4.0)

    annualised = bars.predicted_funding([SYMBOL, "ETHUSDT"])

    assert annualised[SYMBOL] == pytest.approx(0.0004 * 8760 / 8)
    assert annualised["ETHUSDT"] == pytest.approx(0.0004 * 8760 / 4)


def test_us_t03_ac4_predicted_funding_prefers_the_stored_interval(ctx: Context, bars: BarService) -> None:
    # US-T11 AC 3: the funding-interval watch stores 4h; the overlay must use it
    # even while premiumIndex is still answering with the 8h default.
    info = ctx.gateway.set_symbol_info(SYMBOL, funding_interval_hours=4.0)
    ctx.repos.symbol_meta.upsert_many([info], ctx.now_ms())
    ctx.gateway.set_predicted_funding(SYMBOL, 0.0004, interval_hours=8.0)

    assert bars.predicted_funding([SYMBOL])[SYMBOL] == pytest.approx(0.0004 * 8760 / 4)


def test_us_t03_funding_failures_do_not_propagate(ctx: Context, bars: BarService) -> None:
    ctx.gateway.inject_error("funding_history", ExchangeUnreachable("down"))
    ctx.gateway.inject_error("predicted_funding", ExchangeUnreachable("down"))

    assert bars.sync_funding([SYMBOL], ctx.now_ms()) == {SYMBOL: 0}
    assert bars.predicted_funding([SYMBOL]) == {SYMBOL: 0.0}


def test_us_t03_stored_funding_reads_back_what_sync_wrote(ctx: Context, bars: BarService) -> None:
    ctx.gateway.set_funding(SYMBOL, settlements(SYMBOL, date(2026, 9, 6), [1e-4, 2e-4]))
    bars.sync_funding([SYMBOL], ctx.now_ms())

    assert [f.rate for f in bars.stored_funding(SYMBOL)] == [1e-4, 2e-4]


def test_us_t03_backfill_survives_an_unreachable_venue(ctx: Context, bars: BarService) -> None:
    ctx.gateway.set_symbol_info(SYMBOL)
    ctx.gateway.inject_error("daily_bars", ExchangeUnreachable("down"))

    assert bars.backfill([SYMBOL], min_days=5) == {SYMBOL: 0}


def test_us_t03_last_closed_day_is_yesterday_utc(bars: BarService) -> None:
    assert bars.last_closed_day() == LAST_CLOSED


def test_us_t03_ac1_a_day_already_stored_is_not_re_fetched(ctx: Context, bars: BarService) -> None:
    seed(ctx, SYMBOL, 3)
    bars.backfill([SYMBOL], min_days=3)
    calls = ctx.gateway.calls["daily_bars"]

    fetched, missing = bars.fetch_closed_day([SYMBOL], LAST_CLOSED)

    assert (fetched, missing) == ({SYMBOL}, set())
    assert ctx.gateway.calls["daily_bars"] == calls


def test_us_t03_ac1_backfill_of_an_unknown_symbol_stores_nothing(ctx: Context, bars: BarService) -> None:
    assert bars.backfill(["NOPEUSDT"], min_days=3) == {"NOPEUSDT": 0}


def test_us_t03_ac2_forward_fill_skips_symbols_with_no_bars(bars: BarService) -> None:
    assert bars.forward_fill(["NOPEUSDT"], through=LAST_CLOSED) == 0


def test_us_t03_ac2_a_non_positive_close_is_a_data_gap_not_a_silent_zero(
    ctx: Context, bars: BarService
) -> None:
    ctx.gateway.set_symbol_info(SYMBOL)
    ctx.gateway.set_closes(SYMBOL, [100.0, 0.0], start_day=date(2026, 9, 6))
    bars.backfill([SYMBOL], min_days=2)

    with pytest.raises(DataGap):
        bars.log_returns(SYMBOL)
