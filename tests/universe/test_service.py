"""US-T02 AC 2/3 and US-T11 AC 4 — the scheduled universe refresh."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.core.clock import DAY_MS, FakeClock, to_ms
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import Strategy, UniverseEntry, UniverseResult
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories
from aegis.universe.service import UniverseService

SEPT_8 = "2026-09-08T00:30:00Z"
LAST_CLOSED = date(2026, 9, 7)

#: Four candidates, ranked by median quote volume — small numbers so the test
#: states the expected ranking outright instead of recomputing it.
VOLUMES = {"AAAUSDT": 400e6, "BBBUSDT": 300e6, "CCCUSDT": 200e6, "DDDUSDT": 100e6}


def build_ctx(clock: FakeClock, **universe: object) -> Context:
    cfg = load_config(
        "config/trend.yaml",
        use_env=False,
        overrides={
            "universe": {
                "size": 3,
                "min_history_days": 5,
                "volume_window_days": 3,
                "force_include": [],
                **universe,
            }
        },
    )
    repos = Repositories(open_db(":memory:"), Strategy.TREND)
    return Context(
        cfg=cfg,
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(SEPT_8)


@pytest.fixture
def ctx(clock: FakeClock) -> Context:
    ctx = build_ctx(clock)
    seed_venue(ctx)
    return ctx


def seed_venue(ctx: Context, volumes: dict[str, float] | None = None, days: int = 40) -> None:
    for symbol, volume in (volumes or VOLUMES).items():
        ctx.gateway.set_symbol_info(symbol)
        ctx.gateway.set_closes(
            symbol, [100.0] * days, start_day=LAST_CLOSED - timedelta(days=days - 1), quote_volume=volume
        )


def service(ctx: Context) -> UniverseService:
    return UniverseService(ctx)


def alert(ctx: Context, code: str) -> dict | None:
    return ctx.repos.alerts.last_of_code(code)


def save_month(ctx: Context, month: str, symbols: list[str]) -> None:
    """Pin a stored universe without running the selector (the 'mocked change')."""
    ctx.repos.universe.save(
        UniverseResult(
            month=month,
            entries=tuple(
                UniverseEntry(
                    symbol=s,
                    rank=i + 1,
                    median_quote_volume_30d=1e6,
                    history_days=400,
                    included=True,
                    reason="top-3 by volume",
                )
                for i, s in enumerate(symbols)
            ),
        ),
        ctx.now_ms(),
    )


# --------------------------------------------------------------------------- #
# US-T02 AC 2 — the refresh itself
# --------------------------------------------------------------------------- #


def test_us_t02_ac2_first_start_refreshes_even_off_schedule(ctx: Context) -> None:
    svc = service(ctx)

    result = svc.refresh_if_due(ctx.now_ms())

    assert result is not None
    assert result.month == "2026-09"
    assert list(result.symbols) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert ctx.repos.universe.symbols("2026-09") == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    # Every considered symbol is persisted with its reason (dashboard input).
    stored = ctx.repos.universe.month("2026-09")
    assert stored is not None
    assert stored.entry("DDDUSDT").included is False
    assert stored.entry("DDDUSDT").reason == "rank 4 > 3"
    assert alert(ctx, "UNIVERSE_REFRESH") is not None


def test_us_t02_ac2_refresh_is_idempotent_within_the_month(ctx: Context) -> None:
    svc = service(ctx)
    svc.refresh_if_due(ctx.now_ms())
    alerts_after_first = len(ctx.repos.alerts.recent())

    assert svc.refresh_if_due(ctx.now_ms()) is None
    assert len(ctx.repos.alerts.recent()) == alerts_after_first, "no double-alerting"


def test_us_t02_ac2_force_reselects_a_month_already_stored(ctx: Context) -> None:
    svc = service(ctx)
    svc.refresh_if_due(ctx.now_ms())

    assert svc.refresh_if_due(ctx.now_ms(), force=True) is not None


def test_us_t02_ac2_refresh_waits_for_the_first_of_the_month(clock: FakeClock) -> None:
    ctx = build_ctx(clock)
    seed_venue(ctx)
    save_month(ctx, "2026-08", ["AAAUSDT"])
    svc = service(ctx)

    clock.set(to_ms("2026-09-01T00:04:00Z"))
    assert svc.refresh_if_due(clock.now_ms()) is None, "before universe.refresh_time_utc"

    clock.set(to_ms("2026-09-01T00:05:00Z"))
    assert svc.refresh_if_due(clock.now_ms()) is not None


def test_us_t02_ac2_a_refresh_missed_while_down_still_runs(clock: FakeClock) -> None:
    ctx = build_ctx(clock)
    seed_venue(ctx)
    save_month(ctx, "2026-08", ["AAAUSDT"])

    # The engine was offline on the 1st; it is now the 8th and September has no
    # universe. The cut-off is still 2026-09-01, so the answer is unchanged.
    assert service(ctx).refresh_if_due(ctx.now_ms()) is not None


def test_us_t02_ac2_entrants_and_leavers_are_alerted_with_reasons(ctx: Context) -> None:
    save_month(ctx, "2026-08", ["AAAUSDT", "BBBUSDT", "ZZZUSDT"])

    service(ctx).refresh_if_due(ctx.now_ms())

    entry = alert(ctx, "UNIVERSE_ENTRY")
    exit_ = alert(ctx, "UNIVERSE_EXIT")
    assert entry is not None and "CCCUSDT" in entry["message"]
    assert exit_ is not None and "ZZZUSDT" in exit_["message"]
    assert "AAAUSDT" not in entry["message"], "a symbol that stayed is not an entrant"


def test_us_t02_ac2_first_ever_selection_does_not_announce_entrants(ctx: Context) -> None:
    service(ctx).refresh_if_due(ctx.now_ms())

    assert alert(ctx, "UNIVERSE_ENTRY") is None
    assert alert(ctx, "UNIVERSE_EXIT") is None


def test_us_t02_ac2_refresh_clears_illiquid_flags(ctx: Context) -> None:
    now = ctx.now_ms()
    ctx.repos.illiquid.flag("AAAUSDT", now - DAY_MS, now + DAY_MS, "clip too small")
    assert ctx.repos.illiquid.active_symbols(now) == {"AAAUSDT"}

    service(ctx).refresh_if_due(now)

    assert ctx.repos.illiquid.active_symbols(now) == set()


def test_us_t02_ac2_refresh_stores_symbol_meta_for_the_venue(ctx: Context) -> None:
    service(ctx).refresh_if_due(ctx.now_ms())

    assert set(ctx.repos.symbol_meta.all()) == set(VOLUMES)


def test_us_t02_history_gate_is_enforced_through_the_bar_store(clock: FakeClock) -> None:
    ctx = build_ctx(clock, min_history_days=30)
    seed_venue(ctx)
    # Only 6 sessions before 2026-09-01 — short of the 30-day gate.
    ctx.gateway.set_closes("BBBUSDT", [100.0] * 12, start_day=date(2026, 8, 26), quote_volume=300e6)

    result = service(ctx).refresh_if_due(ctx.now_ms())

    assert result is not None
    assert "BBBUSDT" not in result.symbols
    assert "days history" in result.entry("BBBUSDT").reason


# --------------------------------------------------------------------------- #
# US-T02 AC 3 — leavers
# --------------------------------------------------------------------------- #


def test_us_t02_ac3_leavers_lists_last_months_symbols_that_dropped_out(ctx: Context) -> None:
    save_month(ctx, "2026-08", ["AAAUSDT", "ZZZUSDT", "YYYUSDT"])
    service(ctx).refresh_if_due(ctx.now_ms())

    assert service(ctx).leavers(ctx.now_ms()) == ["YYYUSDT", "ZZZUSDT"]


def test_us_t02_ac3_no_previous_month_means_no_leavers(ctx: Context) -> None:
    svc = service(ctx)
    svc.refresh_if_due(ctx.now_ms())

    assert svc.leavers(ctx.now_ms()) == []
    assert service(build_ctx(FakeClock(SEPT_8))).leavers(ctx.now_ms()) == []


# --------------------------------------------------------------------------- #
# US-T11 AC 4 — illiquid flags are excluded from sizing, not from the book
# --------------------------------------------------------------------------- #


def test_us_t11_ac4_current_symbols_excludes_illiquid_flagged(ctx: Context) -> None:
    now = ctx.now_ms()
    svc = service(ctx)
    svc.refresh_if_due(now)
    ctx.repos.illiquid.flag("BBBUSDT", now, now + 3 * DAY_MS, "no fill in 3 rebalances")

    assert svc.current_symbols(now) == ["AAAUSDT", "CCCUSDT"]
    assert svc.all_symbols(now) == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


def test_us_t11_ac4_an_expired_flag_lets_the_symbol_back(ctx: Context) -> None:
    now = ctx.now_ms()
    svc = service(ctx)
    svc.refresh_if_due(now)
    ctx.repos.illiquid.flag("BBBUSDT", now - 4 * DAY_MS, now - DAY_MS, "stale")

    assert "BBBUSDT" in svc.current_symbols(now)


# --------------------------------------------------------------------------- #
# Reading the universe
# --------------------------------------------------------------------------- #


def test_effective_month_falls_back_to_last_month_before_the_refresh(clock: FakeClock) -> None:
    ctx = build_ctx(clock)
    save_month(ctx, "2026-08", ["AAAUSDT", "BBBUSDT"])
    clock.set(to_ms("2026-09-01T00:01:00Z"))
    svc = service(ctx)

    assert svc.effective_month(clock.now_ms()) == "2026-08"
    assert svc.all_symbols(clock.now_ms()) == ["AAAUSDT", "BBBUSDT"]


def test_an_empty_store_has_no_universe(clock: FakeClock) -> None:
    svc = service(build_ctx(clock))

    assert svc.effective_month(clock.now_ms()) is None
    assert svc.all_symbols(clock.now_ms()) == []
    assert svc.current_symbols(clock.now_ms()) == []


def test_result_for_returns_the_persisted_selection(ctx: Context) -> None:
    svc = service(ctx)
    svc.refresh_if_due(ctx.now_ms())

    assert svc.result_for("2026-09") is not None
    assert svc.result_for("2020-01") is None


def test_us_t02_ac3_a_pure_exit_alerts_without_an_entry(clock: FakeClock) -> None:
    ctx = build_ctx(clock, size=4)
    seed_venue(ctx)
    save_month(ctx, "2026-08", ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "ZZZUSDT"])

    service(ctx).refresh_if_due(ctx.now_ms())

    assert alert(ctx, "UNIVERSE_ENTRY") is None
    exit_ = alert(ctx, "UNIVERSE_EXIT")
    assert exit_ is not None and "ZZZUSDT" in exit_["message"]
