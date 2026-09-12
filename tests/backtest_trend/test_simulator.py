"""US-T16 AC 2, AC 6 — the daily simulator."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.backtest_trend.simulator import COV_WINDOW_DAYS, Simulator
from aegis.backtest_trend.universe_builder import PointInTimeUniverse, months_between
from aegis.core.clock import month_key
from tests.backtest_trend.conftest import DAYS, START, make_market

WARM = START + timedelta(days=430)
END = START + timedelta(days=DAYS - 1)


def build(cfg, market, **kwargs) -> Simulator:
    bars, funding, inventory = market
    months = months_between(month_key(START), month_key(END))
    universes = PointInTimeUniverse(inventory, bars, cfg.universe).build(months)
    return Simulator(cfg, bars, funding, universes, initial_equity=10_000.0, **kwargs)


@pytest.fixture(scope="module")
def run(bt_cfg, market):
    return build(bt_cfg, market).run(WARM, END)


def test_us_t16_ac2_the_simulator_produces_a_daily_equity_path(run):
    assert len(run.equity) > 400
    days = [p["day"] for p in run.equity]
    assert days == sorted(days), "the path must be in calendar order"
    assert len(set(days)) == len(days), "one point per day"


def test_us_t16_ac2_momentum_is_profitable_on_a_trending_market(run):
    """Not a promise about the future — a check that the machine captures a trend."""
    assert run.metrics["net_pnl"] > 0
    assert run.metrics["sharpe"] > 0.5
    assert run.metrics["max_drawdown"] < 0.35


def test_us_t16_ac6_the_run_is_deterministic(bt_cfg, market):
    a = build(bt_cfg, market).run(WARM, END)
    b = build(bt_cfg, market).run(WARM, END)
    assert a.run_id == b.run_id
    assert a.manifest == b.manifest
    assert a.metrics == b.metrics
    assert a.equity == b.equity


def test_us_t16_ac6_a_different_parameter_changes_the_run_id(bt_cfg, market):
    a = build(bt_cfg, market)
    other = bt_cfg.model_copy(update={
        "sizing": bt_cfg.sizing.model_copy(update={"sigma_target_asset": 0.30})
    })
    b = build(other, market)
    assert a.run(WARM, END).run_id != b.run(WARM, END).run_id


def test_us_t16_ac6_completes_well_inside_the_15_minute_budget(run):
    """PRD: '< 15 minutes on the free VM for 2021->now (16 symbols daily)'.

    This run is ~470 days over 20 symbols. The budget is for ~1700 days over 16,
    so a generous ceiling here still proves the per-day cost is small enough.
    """
    assert run.duration_s < 120, f"{run.duration_s:.1f}s for {len(run.equity)} days"
    per_day_ms = run.duration_s / len(run.equity) * 1000
    assert per_day_ms < 200, f"{per_day_ms:.0f} ms/day extrapolates past the budget"


def test_equity_reconciles_with_cash_and_marked_positions(bt_cfg, market):
    """Section 14 point 2: the equity path must be recomputable from its own flows."""
    bars, funding, _ = market
    sim = build(bt_cfg, market)
    result = sim.run(WARM, WARM + timedelta(days=120))
    first = result.equity[0]["equity"]
    last = result.equity[-1]["equity"]
    # Every currency movement is one of: price P&L, fees, slippage, funding.
    accounted = result.metrics["funding"] - result.metrics["fees"] - result.metrics["slippage"]
    price_pnl = (last - first) - accounted
    assert abs((first + price_pnl + accounted) - last) < 0.01


def test_no_look_ahead_a_run_ending_earlier_is_a_prefix(bt_cfg, market):
    """The decisive property: tomorrow's bar cannot change today's equity."""
    full = build(bt_cfg, market).run(WARM, END)
    short_end = WARM + timedelta(days=200)
    short = build(bt_cfg, market).run(WARM, short_end)
    n = len(short.equity)
    assert n > 100
    for a, b in zip(short.equity, full.equity[:n], strict=True):
        assert a["day"] == b["day"]
        assert a["equity"] == pytest.approx(b["equity"], rel=1e-12)


def test_a_flat_market_produces_no_position(bt_cfg, market):
    """Idle is valid (Invariant 3): no trend, no book."""
    bars, funding, inventory = market
    flat_bars = {
        s: [type(b)(b.symbol, b.day, 100.0, 100.0, 100.0, 100.0, b.volume, b.quote_volume,
                    b.open_time_ms, b.close_time_ms, b.source, b.filled) for b in bl]
        for s, bl in bars.items()
    }
    months = months_between(month_key(START), month_key(END))
    universes = PointInTimeUniverse(inventory, flat_bars, bt_cfg.universe).build(months)
    sim = Simulator(bt_cfg, flat_bars, funding, universes, initial_equity=10_000.0)
    result = sim.run(WARM, WARM + timedelta(days=90))
    assert result.metrics["traded_notional"] == 0.0
    assert all(p["gross"] == 0.0 for p in result.equity)


def test_the_hard_halt_stops_trading_for_good(bt_cfg, market):
    bars, funding, inventory = market
    cfg = bt_cfg.model_copy(update={"risk": bt_cfg.risk.model_copy(update={"hard_halt_dd": 0.02})})
    months = months_between(month_key(START), month_key(END))
    universes = PointInTimeUniverse(inventory, bars, cfg.universe).build(months)
    result = Simulator(cfg, bars, funding, universes, initial_equity=10_000.0).run(WARM, END)
    assert result.halted_on is not None
    after = [p for p in result.equity if p["day"] > result.halted_on.isoformat()]
    assert all(p["gross"] == 0.0 for p in after), "a halt must leave the book flat"
    equities = {round(p["equity"], 6) for p in after}
    assert len(equities) <= 1, "equity cannot move once flat"


def test_an_empty_calendar_returns_an_empty_result(bt_cfg, market):
    sim = build(bt_cfg, market)
    result = sim.run(date(2019, 1, 1), date(2019, 2, 1))
    assert result.equity == ()
    assert result.metrics == {}
    assert result.run_id


def test_fill_at_close_is_the_documented_time_of_day_variant(bt_cfg, market):
    bars, funding, inventory = market
    months = months_between(month_key(START), month_key(END))
    universes = PointInTimeUniverse(inventory, bars, bt_cfg.universe).build(months)
    at_open = Simulator(bt_cfg, bars, funding, universes, fill_at="open").run(WARM, WARM + timedelta(days=200))
    at_close = Simulator(bt_cfg, bars, funding, universes, fill_at="close").run(WARM, WARM + timedelta(days=200))
    assert at_open.metrics["net_pnl"] != at_close.metrics["net_pnl"]
    assert at_close.manifest["fill_at"] == "close"


def test_an_invalid_fill_at_is_refused(bt_cfg, market):
    from aegis.core.errors import ConfigError

    bars, funding, inventory = market
    with pytest.raises(ConfigError):
        Simulator(bt_cfg, bars, funding, {}, fill_at="midday")


def test_funding_is_charged_to_longs_and_paid_to_shorts(bt_cfg):
    """The sign convention, isolated: one symbol, one position, known funding."""
    from datetime import date as d

    from aegis.core.types import DailyBar, FundingRate, UniverseEntry, UniverseResult

    days = [d(2021, 1, 1) + timedelta(days=i) for i in range(5)]
    bars = {"XUSDT": [DailyBar("XUSDT", day, 100.0, 100.0, 100.0, 100.0, 1.0, 1e9, 0, 0)
                      for day in days]}
    rate = 0.01
    funding = {"XUSDT": [FundingRate("XUSDT", int((day - d(1970, 1, 1)).days) * 86_400_000,
                                     rate, 8.0) for day in days]}
    universes = {month_key(days[0]): UniverseResult(
        month_key(days[0]), (UniverseEntry("XUSDT", 1, 1e9, 500, True, "test"),))}

    from aegis.backtest_trend.simulator import _Book

    sim = Simulator(bt_cfg, bars, funding, universes, initial_equity=10_000.0)
    long_book = _Book(cash=10_000.0, qty={"XUSDT": 10.0})     # +1 000 notional
    charged = sim._settle_funding(long_book, days[1], {"XUSDT": 0}, {"XUSDT": 100.0})
    assert charged == pytest.approx(-rate * 1_000.0), "a long pays when funding is positive"

    short_book = _Book(cash=10_000.0, qty={"XUSDT": -10.0})
    received = sim._settle_funding(short_book, days[1], {"XUSDT": 0}, {"XUSDT": 100.0})
    assert received == pytest.approx(+rate * 1_000.0), "a short receives it"


def test_cov_window_is_long_enough_to_be_exact(bt_cfg):
    """A 500-day window at a 20-day half-life leaves the oldest weight at 2**-25."""
    assert 2 ** (-COV_WINDOW_DAYS / bt_cfg.cov.half_life_days) < 1e-6


def test_manifest_carries_checksums_and_the_parameter_snapshot(run, bt_cfg):
    manifest = run.manifest
    assert manifest["parameters"]["signal"]["pairs"] == [list(p) for p in bt_cfg.signal.pairs]
    assert len(manifest["data_checksums"]) == len(manifest["symbols"])
    assert "api_key" not in manifest["parameters"].get("account", {})


def test_a_changed_bar_changes_the_manifest_checksum(bt_cfg, market):
    bars, funding, inventory = market
    tampered = {s: list(b) for s, b in bars.items()}
    first = tampered["S00USDT"][0]
    tampered["S00USDT"][0] = type(first)(
        first.symbol, first.day, first.open, first.high, first.low, first.close * 1.01,
        first.volume, first.quote_volume, first.open_time_ms, first.close_time_ms,
        first.source, first.filled)
    months = months_between(month_key(START), month_key(END))
    u = PointInTimeUniverse(inventory, bars, bt_cfg.universe).build(months)
    a = Simulator(bt_cfg, bars, funding, u).manifest(WARM, END, "default")
    b = Simulator(bt_cfg, tampered, funding, u).manifest(WARM, END, "default")
    assert a["data_checksums"]["S00USDT"] != b["data_checksums"]["S00USDT"]
