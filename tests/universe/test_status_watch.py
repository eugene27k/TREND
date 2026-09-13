"""US-T11 AC 2/3 — Section 5.10 status, delisting and funding-interval watch."""

from __future__ import annotations

import pytest

from aegis.core.clock import DAY_MS, FakeClock
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import Strategy, UniverseEntry, UniverseResult
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories
from aegis.universe.status_watch import StatusWatch

NOW = "2026-09-08T12:00:00Z"
UNIVERSE = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def ctx(clock: FakeClock) -> Context:
    cfg = load_config("config/trend.yaml", use_env=False)
    repos = Repositories(open_db(":memory:"), Strategy.TREND)
    ctx = Context(
        cfg=cfg,
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )
    repos.universe.save(
        UniverseResult(
            month="2026-09",
            entries=tuple(
                UniverseEntry(
                    symbol=s,
                    rank=i + 1,
                    median_quote_volume_30d=1e8,
                    history_days=400,
                    included=True,
                    reason="top-16 by volume",
                )
                for i, s in enumerate(UNIVERSE)
            ),
        ),
        ctx.now_ms(),
    )
    for symbol in UNIVERSE:
        ctx.gateway.set_symbol_info(symbol)
    return ctx


@pytest.fixture
def watch(ctx: Context) -> StatusWatch:
    return StatusWatch(ctx)


def alert(ctx: Context, code: str) -> dict | None:
    return ctx.repos.alerts.last_of_code(code)


def delist(ctx: Context, symbol: str) -> None:
    """Drop a symbol from exchangeInfo entirely — FakeGateway can add but not remove."""
    ctx.gateway._symbols.pop(symbol)


# --------------------------------------------------------------------------- #
# AC 2 — status other than TRADING
# --------------------------------------------------------------------------- #


def test_us_t11_ac2_all_trading_returns_nothing(ctx: Context, watch: StatusWatch) -> None:
    assert watch.check(ctx.now_ms()) == []
    assert ctx.repos.alerts.recent() == []


@pytest.mark.parametrize(
    "status",
    ["SETTLING", "PRE_DELIVERING", "DELIVERING", "DELIVERED", "PRE_SETTLE", "CLOSE", "PENDING_TRADING"],
)
def test_us_t11_ac2_non_trading_status_is_returned_for_closure(
    ctx: Context, watch: StatusWatch, status: str
) -> None:
    ctx.gateway.set_symbol_info("BBBUSDT", status=status)

    closures = watch.check(ctx.now_ms())

    assert closures == ["BBBUSDT"]
    warn = alert(ctx, "SYMBOL_STATUS")
    assert warn is not None
    assert warn["severity"] == "WARN"
    assert status in warn["message"]


def test_us_t11_ac2_the_status_read_is_never_a_cached_one(ctx: Context, watch: StatusWatch) -> None:
    # 5.10 is a *fresh* exchangeInfo every 60 minutes; the live gateway caches the
    # payload unless asked to refresh, so a cached read would make the watch blind
    # to the one event it exists for.
    watch.check(ctx.now_ms())

    assert ctx.gateway.exchange_info_refreshes == 1


def test_us_t11_ac2_two_symbols_changing_at_once_both_alert(ctx: Context, watch: StatusWatch) -> None:
    ctx.gateway.set_symbol_info("AAAUSDT", status="SETTLING")
    ctx.gateway.set_symbol_info("BBBUSDT", status="CLOSE")

    assert watch.check(ctx.now_ms()) == ["AAAUSDT", "BBBUSDT"]
    codes = [r["code"] for r in ctx.repos.alerts.recent()]
    assert codes.count("SYMBOL_STATUS") == 2, "by-code suppression must not swallow the second"


def test_us_t11_ac2_the_same_status_is_not_realerted_but_stays_closeable(
    ctx: Context, watch: StatusWatch, clock: FakeClock
) -> None:
    ctx.gateway.set_symbol_info("BBBUSDT", status="SETTLING")
    watch.check(ctx.now_ms())

    clock.advance(hours=1)
    closures = watch.check(ctx.now_ms())

    assert closures == ["BBBUSDT"], "still not tradeable, still to be closed"
    codes = [r["code"] for r in ctx.repos.alerts.recent()]
    assert codes.count("SYMBOL_STATUS") == 1


def test_us_t11_ac2_a_symbol_back_to_trading_is_dropped(
    ctx: Context, watch: StatusWatch, clock: FakeClock
) -> None:
    ctx.gateway.set_symbol_info("BBBUSDT", status="PENDING_TRADING")
    watch.check(ctx.now_ms())

    ctx.gateway.set_symbol_info("BBBUSDT", status="TRADING")
    clock.advance(hours=1)

    assert watch.check(ctx.now_ms()) == []


def test_us_t11_ac2_a_symbol_removed_from_exchange_info_is_delisted_critical(
    ctx: Context, watch: StatusWatch
) -> None:
    delist(ctx, "CCCUSDT")

    closures = watch.check(ctx.now_ms())

    assert closures == ["CCCUSDT"]
    critical = alert(ctx, "SYMBOL_DELISTED")
    assert critical is not None
    assert critical["severity"] == "CRITICAL"
    assert alert(ctx, "SYMBOL_STATUS") is None


def test_us_t11_ac2_illiquid_flagged_symbols_are_still_watched(ctx: Context, watch: StatusWatch) -> None:
    now = ctx.now_ms()
    ctx.repos.illiquid.flag("BBBUSDT", now, now + 3 * DAY_MS, "no fill")
    ctx.gateway.set_symbol_info("BBBUSDT", status="SETTLING")

    # A flag stops us building a position; it does not mean we hold none.
    assert watch.check(now) == ["BBBUSDT"]


def test_us_t11_ac2_an_empty_universe_asks_the_venue_nothing(clock: FakeClock) -> None:
    repos = Repositories(open_db(":memory:"), Strategy.TREND)
    ctx = Context(
        cfg=load_config("config/trend.yaml", use_env=False),
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )

    assert StatusWatch(ctx).check(ctx.now_ms()) == []
    assert ctx.gateway.calls["exchange_info"] == 0


def test_us_t11_ac2_check_cadence_is_the_configured_status_watch_minutes(
    ctx: Context, watch: StatusWatch, clock: FakeClock
) -> None:
    assert watch.is_due(ctx.now_ms()) is True
    watch.check(ctx.now_ms())

    assert watch.is_due(ctx.now_ms()) is False
    clock.advance(minutes=ctx.cfg.universe.status_watch_minutes - 1)
    assert watch.is_due(clock.now_ms()) is False
    clock.advance(minutes=1)
    assert watch.is_due(clock.now_ms()) is True


# --------------------------------------------------------------------------- #
# AC 3 — funding interval
# --------------------------------------------------------------------------- #


def test_us_t11_ac3_first_observation_is_stored_without_an_alert(ctx: Context, watch: StatusWatch) -> None:
    changed = watch.check_funding_intervals(ctx.now_ms())

    assert changed == {}
    assert alert(ctx, "FUNDING_INTERVAL_CHANGE") is None
    assert ctx.repos.symbol_meta.get("AAAUSDT").funding_interval_hours == 8.0


def test_us_t11_ac3_a_changed_interval_updates_symbol_meta_and_logs_info(
    ctx: Context, watch: StatusWatch, clock: FakeClock
) -> None:
    watch.check_funding_intervals(ctx.now_ms())
    ctx.gateway.set_symbol_info("BBBUSDT", funding_interval_hours=4.0)
    clock.advance(hours=1)

    changed = watch.check_funding_intervals(clock.now_ms())

    assert changed == {"BBBUSDT": 4.0}
    # Stored before the next overlay evaluation reads it.
    assert ctx.repos.symbol_meta.get("BBBUSDT").funding_interval_hours == 4.0
    info = alert(ctx, "FUNDING_INTERVAL_CHANGE")
    assert info is not None
    assert info["severity"] == "INFO"
    assert "BBBUSDT" in info["message"]


def test_us_t11_ac3_an_unchanged_interval_is_silent(
    ctx: Context, watch: StatusWatch, clock: FakeClock
) -> None:
    watch.check_funding_intervals(ctx.now_ms())
    clock.advance(hours=1)

    assert watch.check_funding_intervals(clock.now_ms()) == {}
    assert alert(ctx, "FUNDING_INTERVAL_CHANGE") is None


def test_us_t11_ac3_a_delisted_symbol_is_skipped(ctx: Context, watch: StatusWatch) -> None:
    delist(ctx, "CCCUSDT")

    watch.check_funding_intervals(ctx.now_ms())

    assert ctx.repos.symbol_meta.get("CCCUSDT") is None


def test_us_t11_ac3_an_empty_universe_is_a_no_op(clock: FakeClock) -> None:
    repos = Repositories(open_db(":memory:"), Strategy.TREND)
    ctx = Context(
        cfg=load_config("config/trend.yaml", use_env=False),
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )

    assert StatusWatch(ctx).check_funding_intervals(ctx.now_ms()) == {}
