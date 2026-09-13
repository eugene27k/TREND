"""US-T16 AC 1 / US-T02 AC 4 — survivorship-free monthly universes."""

from __future__ import annotations

from datetime import date, timedelta

from aegis.backtest_trend.universe_builder import (
    PointInTimeUniverse,
    months_between,
    synthesise_symbol_info,
)
from aegis.core.clock import month_key
from aegis.core.types import DailyBar


def bars_for(symbol: str, start: date, days: int, volume: float = 1e9) -> list[DailyBar]:
    return [
        DailyBar(symbol, start + timedelta(days=i), 100.0, 101.0, 99.0, 100.0, 1.0, volume, 0, 0)
        for i in range(days)
    ]


def test_months_between_is_inclusive():
    assert months_between("2021-11", "2022-02") == ["2021-11", "2021-12", "2022-01", "2022-02"]
    assert months_between("2022-01", "2022-01") == ["2022-01"]


def test_status_comes_from_the_file_inventory_not_from_today():
    trading = synthesise_symbol_info("BTCUSDT", ["2021-12", "2022-01"], "2022-02")
    delisted = synthesise_symbol_info("OLDUSDT", ["2021-05"], "2022-02")
    assert trading.status == "TRADING"
    assert delisted.status == "DELISTED"
    assert trading.base_asset == "BTC" and trading.quote_asset == "USDT"
    assert trading.contract_type == "PERPETUAL"


def test_us_t16_ac1_a_delisted_symbol_is_in_the_universes_of_the_months_it_existed(bt_cfg):
    start = date(2021, 1, 1)
    live = {f"S{i:02d}USDT": bars_for(f"S{i:02d}USDT", start, 900, 1e9 - i) for i in range(16)}
    # DEADUSDT traded until 2022-06 with the largest volume, then vanished.
    dead_days = (date(2022, 7, 1) - start).days
    live["DEADUSDT"] = bars_for("DEADUSDT", start, dead_days, 9e9)
    inventory = {s: sorted({month_key(b.day) for b in bl}) for s, bl in live.items()}

    universes = PointInTimeUniverse(inventory, live, bt_cfg.universe).build(
        months_between("2022-05", "2022-10")
    )
    assert "DEADUSDT" in universes["2022-05"].symbols, "it was tradeable then"
    assert "DEADUSDT" not in universes["2022-09"].symbols, "it was gone by then"


def test_us_t02_ac4_a_symbol_listed_later_never_appears_earlier(bt_cfg):
    start = date(2021, 1, 1)
    bars = {f"S{i:02d}USDT": bars_for(f"S{i:02d}USDT", start, 1400, 1e9 - i) for i in range(16)}
    bars["NEWUSDT"] = bars_for("NEWUSDT", date(2025, 1, 1), 500, 9e9)
    inventory = {s: sorted({month_key(b.day) for b in bl}) for s, bl in bars.items()}

    universes = PointInTimeUniverse(inventory, bars, bt_cfg.universe).build(
        months_between("2022-01", "2022-12")
    )
    for month, result in universes.items():
        assert "NEWUSDT" not in result.symbols, month


def test_every_month_selects_at_most_the_configured_size(bt_cfg):
    start = date(2021, 1, 1)
    bars = {f"S{i:02d}USDT": bars_for(f"S{i:02d}USDT", start, 900, 1e9 - i * 1e6) for i in range(30)}
    inventory = {s: sorted({month_key(b.day) for b in bl}) for s, bl in bars.items()}
    universes = PointInTimeUniverse(inventory, bars, bt_cfg.universe).build(
        months_between("2022-05", "2022-08")
    )
    for month, result in universes.items():
        assert len(result.symbols) <= bt_cfg.universe.size, month


def test_the_first_months_select_nothing_because_history_is_too_short(bt_cfg):
    start = date(2021, 1, 1)
    bars = {f"S{i:02d}USDT": bars_for(f"S{i:02d}USDT", start, 900) for i in range(20)}
    inventory = {s: sorted({month_key(b.day) for b in bl}) for s, bl in bars.items()}
    universes = PointInTimeUniverse(inventory, bars, bt_cfg.universe).build(["2021-03"])
    assert universes["2021-03"].symbols == (), "400 days of history is required"
