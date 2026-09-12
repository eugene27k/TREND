"""US-T02 AC 1 / AC 4 — eligibility, history gate, forced inclusion, tie-breaks, point-in-time."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.core.config import UniverseConfig
from aegis.core.errors import ConfigError
from aegis.core.types import DailyBar, SymbolInfo
from aegis.universe import select_universe
from aegis.universe.select import UNRANKED

PARAMS = UniverseConfig()
MONTH = "2022-03"
MONTH_START = date(2022, 3, 1)


def make_symbol(
    symbol: str,
    *,
    base: str | None = None,
    quote: str = "USDT",
    status: str = "TRADING",
    contract_type: str = "PERPETUAL",
) -> SymbolInfo:
    """A minimal tradeable-perp SymbolInfo; only the selection fields vary."""
    return SymbolInfo(
        symbol=symbol,
        base_asset=base if base is not None else symbol.removesuffix(quote),
        quote_asset=quote,
        status=status,
        contract_type=contract_type,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        min_notional=5.0,
        price_precision=2,
        quantity_precision=3,
    )


def make_bars(
    symbol: str,
    *,
    n: int,
    quote_volume: float | list[float],
    last_day: date = MONTH_START - timedelta(days=1),
) -> list[DailyBar]:
    """``n`` consecutive daily bars ending on ``last_day`` (inclusive)."""
    volumes = quote_volume if isinstance(quote_volume, list) else [quote_volume] * n
    assert len(volumes) == n
    out = []
    for i, qv in enumerate(volumes):
        day = last_day - timedelta(days=n - 1 - i)
        ts = int(day.toordinal()) * 86_400_000
        out.append(
            DailyBar(
                symbol=symbol,
                day=day,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=qv / 100.0,
                quote_volume=qv,
                open_time_ms=ts,
                close_time_ms=ts + 86_399_999,
            )
        )
    return out


def universe_of(specs: dict[str, dict], *, days: int = 400, month: str = MONTH, params=PARAMS):
    """Build exchange_info + volume_history from ``{symbol: {"vol": ..., **kwargs}}``."""
    info: dict[str, SymbolInfo] = {}
    hist: dict[str, list[DailyBar]] = {}
    for symbol, spec in specs.items():
        spec = dict(spec)
        vol = spec.pop("vol", 1_000_000.0)
        n = spec.pop("days", days)
        last_day = spec.pop("last_day", MONTH_START - timedelta(days=1))
        info[symbol] = make_symbol(symbol, **spec)
        hist[symbol] = make_bars(symbol, n=n, quote_volume=vol, last_day=last_day)
    return select_universe(info, hist, params, month)


def liquid_field(n: int, *, prefix: str = "ALT") -> dict[str, dict]:
    """``n`` eligible symbols with strictly decreasing volume: A0 is the most liquid."""
    return {f"{prefix}{i:02d}USDT": {"vol": 1_000_000.0 - i} for i in range(n)}


# --------------------------------------------------------------------------- #
# AC 1 — eligibility
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("base", PARAMS.exclude_bases)
def test_us_t02_ac1_stable_pegged_bases_are_excluded(base: str) -> None:
    result = universe_of({f"{base}USDT": {"vol": 9e9}, "BTCUSDT": {}, "ETHUSDT": {}})
    assert f"{base}USDT" not in result.symbols
    assert result.entry(f"{base}USDT").reason == "excluded: stable-pegged base"
    assert result.entry(f"{base}USDT").rank == UNRANKED


def test_us_t02_ac1_non_usdt_quote_and_non_perpetual_are_excluded() -> None:
    result = universe_of(
        {
            "BTCUSDC": {"vol": 9e9, "quote": "USDC", "base": "BTC"},
            "ETHUSDT_220325": {"vol": 9e9, "base": "ETH", "contract_type": "CURRENT_QUARTER"},
            "BTCUSDT": {},
            "ETHUSDT": {},
        }
    )
    assert set(result.symbols) == {"BTCUSDT", "ETHUSDT"}
    assert result.entry("BTCUSDC").reason == "excluded: quote asset USDC"
    assert result.entry("ETHUSDT_220325").reason == "excluded: contract type CURRENT_QUARTER"


def test_us_t02_ac1_non_trading_status_is_excluded_with_the_status_in_the_reason() -> None:
    result = universe_of({"XRPUSDT": {"vol": 9e9, "status": "SETTLING"}, "BTCUSDT": {}})
    assert "XRPUSDT" not in result.symbols
    assert result.entry("XRPUSDT").reason == "excluded: status SETTLING"


# --------------------------------------------------------------------------- #
# AC 1 — the 400-day history requirement
# --------------------------------------------------------------------------- #


def test_us_t02_ac1_399_days_of_history_is_excluded_400_is_included() -> None:
    result = universe_of({"AAAUSDT": {"vol": 9e9, "days": 399}, "BBBUSDT": {"vol": 8e9, "days": 400}})
    assert result.symbols == ("BBBUSDT",)
    assert result.entry("AAAUSDT").reason == "excluded: 399 < 400 days history"
    assert result.entry("AAAUSDT").history_days == 399
    assert result.entry("BBBUSDT").history_days == 400


def test_us_t02_ac1_duplicate_bars_for_one_day_count_once() -> None:
    bars = make_bars("AAAUSDT", n=400, quote_volume=1e6)
    result = select_universe(
        {"AAAUSDT": make_symbol("AAAUSDT")}, {"AAAUSDT": bars + bars[-1:]}, PARAMS, MONTH
    )
    assert result.entry("AAAUSDT").history_days == 400


# --------------------------------------------------------------------------- #
# AC 1 — ranking, size and tie-breaking
# --------------------------------------------------------------------------- #


def test_us_t02_ac1_exactly_16_selected_from_40_candidates_in_volume_order() -> None:
    specs = liquid_field(40)
    specs["BTCUSDT"] = {"vol": 5e9}
    specs["ETHUSDT"] = {"vol": 4e9}
    result = universe_of(specs)

    assert len(result.symbols) == PARAMS.size == 16
    assert result.symbols[:2] == ("BTCUSDT", "ETHUSDT")
    assert result.symbols[2:] == tuple(f"ALT{i:02d}USDT" for i in range(14))
    assert [e.rank for e in result.entries] == list(range(1, 43))
    assert result.entry("ALT13USDT").reason == "top-16 by volume"
    assert result.entry("ALT14USDT").reason == "rank 17 > 16"
    assert len(result.entries) == 42  # every considered symbol, winners and losers


def test_us_t02_ac1_equal_median_volume_breaks_by_symbol_name_ascending() -> None:
    specs = {name: {"vol": 1e6} for name in ("CCCUSDT", "AAAUSDT", "BBBUSDT")}
    result = universe_of(specs, params=UniverseConfig(size=2, force_include=()))
    assert result.symbols == ("AAAUSDT", "BBBUSDT")
    assert [e.symbol for e in result.entries] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]


def test_us_t02_ac1_median_of_even_window_is_the_mean_of_the_two_middle_values() -> None:
    # The last 30 bars are 15 zeros then 15 tens: the middle pair is (0, 10) -> 5.0.
    volumes = [9e9] * 370 + [0.0] * 15 + [10.0] * 15
    result = universe_of({"AAAUSDT": {"vol": volumes}})
    assert result.entry("AAAUSDT").median_quote_volume_30d == pytest.approx(5.0)


def test_us_t02_ac1_only_the_last_30_bars_feed_the_median() -> None:
    volumes = [9e9] * 370 + [7.0] * 30
    result = universe_of({"AAAUSDT": {"vol": volumes}})
    assert result.entry("AAAUSDT").median_quote_volume_30d == pytest.approx(7.0)


def test_us_t02_ac1_fewer_eligible_than_size_includes_all_without_padding() -> None:
    specs = liquid_field(3)
    specs["DEADUSDT"] = {"vol": 9e9, "status": "CLOSE"}
    result = universe_of(specs, params=UniverseConfig(force_include=()))
    assert len(result.symbols) == 3


def test_us_t02_ac1_empty_exchange_info_yields_an_empty_result() -> None:
    result = select_universe({}, {}, UniverseConfig(force_include=()), MONTH)
    assert result.symbols == ()
    assert result.entries == ()
    assert result.month == MONTH


# --------------------------------------------------------------------------- #
# AC 1 — forced inclusion
# --------------------------------------------------------------------------- #


def test_us_t02_ac1_forced_btc_eth_ranked_20th_displace_the_weakest_and_size_stays_16() -> None:
    specs = liquid_field(20)
    specs["BTCUSDT"] = {"vol": 1.0}  # a data glitch: BTC looks like the least liquid
    specs["ETHUSDT"] = {"vol": 2.0}
    result = universe_of(specs)

    assert len(result.symbols) == 16
    assert {"BTCUSDT", "ETHUSDT"} <= set(result.symbols)
    assert result.entry("BTCUSDT").reason == "forced include"
    assert result.entry("BTCUSDT").rank == 22
    # The two weakest non-forced names step aside, not the ones just below the cut.
    assert "ALT14USDT" not in result.symbols
    assert "ALT15USDT" not in result.symbols
    assert result.entry("ALT15USDT").reason == "rank 16 displaced by forced include"
    assert "ALT13USDT" in result.symbols
    assert result.symbols[-2:] == ("ETHUSDT", "BTCUSDT")


def test_us_t02_ac1_forced_symbol_already_in_the_top_16_does_not_displace_anyone() -> None:
    specs = liquid_field(20)
    specs["BTCUSDT"] = {"vol": 9e9}
    specs["ETHUSDT"] = {"vol": 8e9}
    result = universe_of(specs)
    assert result.symbols[:2] == ("BTCUSDT", "ETHUSDT")
    assert all(e.reason == "top-16 by volume" for e in result.entries if e.included)


def test_us_t02_ac1_forced_inclusion_refused_when_btc_status_is_not_trading() -> None:
    specs = liquid_field(20)
    specs["BTCUSDT"] = {"vol": 9e9, "status": "SETTLING"}
    specs["ETHUSDT"] = {"vol": 8e9}
    result = universe_of(specs)

    assert "BTCUSDT" not in result.symbols
    assert result.entry("BTCUSDT").included is False
    assert result.entry("BTCUSDT").reason == "excluded: status SETTLING"
    assert len(result.symbols) == 16


def test_us_t02_ac1_forced_inclusion_refused_when_eth_lacks_400_days_of_history() -> None:
    specs = liquid_field(20)
    specs["ETHUSDT"] = {"vol": 9e9, "days": 120}
    result = universe_of(specs)
    assert "ETHUSDT" not in result.symbols
    assert result.entry("ETHUSDT").reason == "excluded: 120 < 400 days history"


def test_us_t02_ac1_forced_symbol_missing_from_exchange_info_is_reported_not_included() -> None:
    result = universe_of(liquid_field(20))
    assert "BTCUSDT" not in result.symbols
    entry = result.entry("BTCUSDT")
    assert entry.included is False
    assert entry.reason == "excluded: not listed in exchangeInfo"
    assert entry.rank == UNRANKED


# --------------------------------------------------------------------------- #
# AC 4 — point-in-time
# --------------------------------------------------------------------------- #


def test_us_t02_ac4_symbol_listed_in_2025_never_appears_in_a_2022_universe() -> None:
    specs = liquid_field(5)
    info = {s: make_symbol(s) for s in specs}
    hist = {s: make_bars(s, n=400, quote_volume=spec["vol"]) for s, spec in specs.items()}
    # Listed 2025: 800 huge-volume bars, none of them before 2022-03-01.
    info["NEWUSDT"] = make_symbol("NEWUSDT")
    hist["NEWUSDT"] = make_bars("NEWUSDT", n=800, quote_volume=9e9, last_day=date(2025, 6, 30))

    past = select_universe(info, hist, UniverseConfig(force_include=()), "2022-03")
    assert "NEWUSDT" not in past.symbols
    assert past.entry("NEWUSDT").history_days == 0
    assert past.entry("NEWUSDT").reason == "excluded: 0 < 400 days history"

    present = select_universe(info, hist, UniverseConfig(force_include=()), "2025-07")
    assert present.symbols[0] == "NEWUSDT"


def test_us_t02_ac4_bars_on_the_first_of_the_month_are_not_consulted() -> None:
    bars = make_bars("AAAUSDT", n=401, quote_volume=1e6, last_day=MONTH_START)
    result = select_universe({"AAAUSDT": make_symbol("AAAUSDT")}, {"AAAUSDT": bars}, PARAMS, MONTH)
    assert result.entry("AAAUSDT").history_days == 400  # the 2022-03-01 bar is dropped


def test_us_t02_ac4_selection_is_deterministic_across_dict_ordering() -> None:
    specs = liquid_field(20)
    reversed_specs = dict(reversed(list(specs.items())))
    assert universe_of(specs).entries == universe_of(reversed_specs).entries


def test_us_t02_ac1_missing_volume_history_is_treated_as_no_history() -> None:
    result = select_universe({"AAAUSDT": make_symbol("AAAUSDT")}, {}, PARAMS, MONTH)
    assert result.entry("AAAUSDT").reason == "excluded: 0 < 400 days history"


@pytest.mark.parametrize("bad", ["2022", "2022-13-01x", "march", ""])
def test_us_t02_ac1_malformed_month_raises_config_error(bad: str) -> None:
    with pytest.raises(ConfigError):
        select_universe({}, {}, PARAMS, bad)
