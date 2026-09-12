"""US-T15 — the metric engine: every documented metric, every standard period.

The engine is exercised against an in-memory database seeded with a full set of
rows (equity curve, attribution, fills, rebalances, governor, signals, trades,
BTC bars, tracking) so that the assertions are about what the engine computes
and stores, not about what data happens to exist.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import pytest

from aegis.analytics import metrics as m
from aegis.analytics.engine import DOCUMENTED_METRIC_NAMES, METRIC_NAMES, MetricsEngine
from aegis.core.clock import FakeClock, to_ms
from aegis.core.config import AppConfig, load_config
from aegis.core.context import Context
from aegis.core.types import (
    AccountState,
    DailyBar,
    EquityPoint,
    Fill,
    Side,
    SignalResult,
    Strategy,
    SymbolInfo,
)
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories
from tests.fixtures.appendix_c import (
    C4_AVG_EQUITY,
    C4_BETA,
    C4_BETA_BTC_SD,
    C4_BETA_DAYS,
    C4_BETA_EPS_SD,
    C4_BETA_SEED,
    C4_BETA_TOL,
    C4_CORR,
    C4_CORR_TOL,
    C4_DAYS,
    C4_TRADED_NOTIONAL,
    C4_TURNOVER_ANNUALISED,
    C4_TURNOVER_PERIOD,
)

DAY_MS = 86_400_000
END_DAY = date(2026, 9, 30)
NOW_MS = to_ms("2026-09-30T01:10:00Z")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


# --------------------------------------------------------------------------- #
# Fixtures and seeding
# --------------------------------------------------------------------------- #


@pytest.fixture
def config() -> AppConfig:
    return load_config("config/trend.yaml", use_env=False)


def _context(config: AppConfig, repos: Repositories, clock: FakeClock) -> Context:
    return Context(
        cfg=config,
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )


@pytest.fixture
def engine_env(config: AppConfig):
    db = open_db(":memory:")
    repos = Repositories(db, Strategy.TREND)
    clock = FakeClock(NOW_MS)
    yield _context(config, repos, clock), repos
    db.close()


def _days(n: int, end: date = END_DAY) -> list[date]:
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def _seed_equity(
    repos: Repositories, days: list[date], returns: list[float], start_equity: float = 10_000.0
) -> dict[date, float]:
    """Write an equity curve whose twr returns are exactly ``returns``.

    ``returns[0]`` is the return *on* ``days[1]``; the first day only seeds the
    index, which is how the real curve behaves (US-T08 AC 3).
    """
    index = 1.0
    equity = start_equity
    peak = 1.0
    out: dict[date, float] = {}
    for i, day in enumerate(days):
        factor = 1.0 + returns[i - 1] if i > 0 else 1.0
        index *= factor
        equity *= factor
        peak = max(peak, index)
        repos.equity.upsert(
            day,
            EquityPoint(ts_ms=to_ms(day), equity=equity, twr_factor=factor, twr_index=index),
            peak_index=peak,
            drawdown=(peak - index) / peak,
        )
        if i > 0:
            out[day] = returns[i - 1]
    return out


def _seed_bars(repos: Repositories, days: list[date], closes: list[float], symbol: str = "BTCUSDT") -> None:
    repos.bars.upsert_many(
        [
            DailyBar(
                symbol=symbol,
                day=day,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1.0,
                quote_volume=close,
                open_time_ms=to_ms(day),
                close_time_ms=to_ms(day) + DAY_MS - 1,
            )
            for day, close in zip(days, closes, strict=True)
        ]
    )


def _seed_pnl(repos: Repositories, days: list[date]) -> None:
    for i, day in enumerate(days):
        rows = []
        for j, symbol in enumerate(SYMBOLS):
            sign = 1.0 if (i + j) % 3 else -1.0
            rows.append(
                {
                    "symbol": symbol,
                    "side": "long" if (i + j) % 2 == 0 else "short",
                    "avg_notional": 1_000.0 + 100.0 * j,
                    "price_pnl": 5.0 * sign,
                    "funding": -0.5,
                    "fees": 0.3,
                    "slippage": 0.2,
                    "net_pnl": 5.0 * sign - 0.5 - 0.3 - 0.2,
                    "traded_notional": 200.0,
                    "signal": 0.6 * sign,
                }
            )
        repos.symbol_pnl.upsert_many(day, rows)


def _seed_fills(
    repos: Repositories, days: list[date], *, notional_per_fill: float = 400.0, every: int = 5
) -> None:
    fills = []
    for i, day in enumerate(days):
        if i % every:
            continue
        for j, symbol in enumerate(SYMBOLS):
            price = 100.0 + j
            fills.append(
                Fill(
                    trade_id=f"{symbol}-{i}",
                    order_id=f"o-{symbol}-{i}",
                    symbol=symbol,
                    side=Side.BUY if j % 2 == 0 else Side.SELL,
                    qty=notional_per_fill / price,
                    price=price,
                    fee=notional_per_fill * 0.0002,
                    fee_asset="USDT",
                    is_maker=j != 0,
                    ts_ms=to_ms(day) + 3_600_000,
                    slippage_bps=1.5,
                )
            )
    repos.fills.add_many(fills)


def _finish_rebalance(
    repos: Repositories, rebalance_id: str, day: date, completion_pct: float, *, kind: str = "scheduled"
) -> None:
    repos.rebalances.create(rebalance_id, day, to_ms(day) + 300_000, kind=kind, equity=10_000.0)
    repos.rebalances.finish(
        rebalance_id,
        ended_ts=to_ms(day) + 3_600_000,
        status="complete",
        completion_pct=completion_pct,
        traded_notional=1_200.0,
        fees=0.5,
        avg_slippage_bps=1.5,
        maker_ratio=0.7,
        residuals=[],
    )


def _seed_rebalances(repos: Repositories, days: list[date]) -> None:
    for i, day in enumerate(days):
        _finish_rebalance(repos, f"rb-{day.isoformat()}", day, 100.0 - (i % 3) * 5.0)


def _seed_signals(repos: Repositories, days: list[date]) -> None:
    for i, day in enumerate(days):
        results = [
            SignalResult(
                symbol=s, x=(), y=(), z=(), u=(), signal=round(0.6 - 0.1 * ((i + j) % 4), 3), bar_day=day
            )
            for j, s in enumerate(SYMBOLS)
        ]
        repos.signals.save_many(day, results, to_ms(day))


def _seed_trades(repos: Repositories, days: list[date]) -> None:
    for i, day in enumerate(days[::10]):
        repos.trades.upsert(
            {
                "trade_key": f"T{i}",
                "symbol": SYMBOLS[i % 3],
                "side": "long",
                "open_ts": to_ms(day) - 5 * DAY_MS,
                "close_ts": to_ms(day) + 3_600_000,
                "days": 5.0 + i,
                "pnl": 40.0 if i % 2 == 0 else -25.0,
                "mae": -15.0 - i,
                "max_notional": 1_000.0,
                "entry_signal": 0.7,
                "exit_signal": 0.1,
            }
        )


def _seed_governor(repos: Repositories, days: list[date]) -> None:
    mid = days[len(days) // 2]
    repos.governor.record(to_ms(days[0]), 0.0, 1.0, 1.0, "start")
    repos.governor.record(to_ms(mid), 0.13, 1.0, 0.5, "dd_12")


def _seed_tracking(repos: Repositories, days: list[date], returns: dict[date, float]) -> None:
    for day in days[1:]:
        ref = returns.get(day, 0.0) * 0.9 * 10_000.0
        repos.tracking.upsert(
            day,
            live_pnl=returns.get(day, 0.0) * 10_000.0,
            ref_pnl=ref,
            cum_live=0.0,
            cum_ref=0.0,
            cum_diff_frac=0.0,
        )


def _wave(n: int) -> list[float]:
    """Deterministic, non-degenerate daily returns (no RNG, no wall clock)."""
    return [0.004 * math.sin(i / 3.0) + 0.002 * math.cos(i / 7.0) - 0.0003 for i in range(n)]


def _seed_all(repos: Repositories, n_days: int = 120) -> tuple[list[date], dict[date, float]]:
    days = _days(n_days)
    returns = _seed_equity(repos, days, _wave(n_days))
    closes = [30_000.0]
    for i in range(1, n_days):
        closes.append(closes[-1] * (1.0 + 0.006 * math.sin(i / 4.0)))
    _seed_bars(repos, days, closes)
    _seed_pnl(repos, days)
    _seed_fills(repos, days)
    _seed_rebalances(repos, days)
    _seed_signals(repos, days)
    _seed_trades(repos, days)
    _seed_governor(repos, days)
    _seed_tracking(repos, days, returns)
    return days, returns


def _by_name(values, period: str) -> dict[str, object]:
    return {v.name: v for v in values if v.period == period}


# --------------------------------------------------------------------------- #
# AC 2 — every documented metric, for every standard period
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_every_documented_metric_is_emitted_for_every_period(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = MetricsEngine(ctx).compute_all(NOW_MS)
    for period in ctx.cfg.metrics.periods:
        emitted = {v.name for v in values if v.period == period}
        missing = set(DOCUMENTED_METRIC_NAMES) - emitted
        assert not missing, f"{period} is missing {sorted(missing)}"


def test_us_t15_ac2_the_documented_names_match_the_services_contract(engine_env) -> None:
    """The list in docs/SERVICES.md, copied here so a rename fails loudly."""
    contract = {
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
        "var_95",
        "skew",
        "kurtosis",
        "realised_vol",
        "vol_ratio",
        "gross_exposure",
        "net_exposure",
        "turnover",
        "cost_per_unit_bps",
        "execution_alpha",
        "maker_ratio",
        "rebalance_completion",
        "beta_btc",
        "corr_btc",
        "corr_carry",
        "long_pnl",
        "short_pnl",
        "hit_rate_long",
        "hit_rate_short",
        "governor_time_g1",
        "governor_time_g05",
        "governor_time_g025",
        "funding_share",
        "vol_target_adherence",
        "concentration",
        "trade_count",
        "avg_holding_days",
        "win_rate",
        "information_ratio",
        "net_of_infra",
        "cash_alternative",
    }
    assert contract == set(DOCUMENTED_METRIC_NAMES)
    assert contract <= set(METRIC_NAMES)


def test_us_t15_ac2_every_engine_metric_name_is_emitted(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = MetricsEngine(ctx).compute_all(NOW_MS)
    for period in ctx.cfg.metrics.periods:
        assert {v.name for v in values if v.period == period} == set(METRIC_NAMES)


def test_us_t15_ac2_no_metric_name_is_emitted_twice_per_period(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = MetricsEngine(ctx).compute_all(NOW_MS)
    keys = [(v.name, v.period) for v in values]
    assert len(keys) == len(set(keys))


def test_us_t15_ac2_values_are_persisted_and_readable(engine_env) -> None:
    ctx, repos = engine_env
    _seed_all(ctx.repos)
    MetricsEngine(ctx).compute_all(NOW_MS)
    stored = repos.metrics.latest("30d")
    assert {k.split(":")[0] for k in stored} == set(METRIC_NAMES)
    assert stored["sharpe:30d"]["strategy"] == "TREND"
    assert stored["sharpe:30d"]["n_obs"] == 30


def test_us_t15_ac2_all_standard_periods_are_computed(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = MetricsEngine(ctx).compute_all(NOW_MS)
    assert {v.period for v in values} == set(ctx.cfg.metrics.periods)


def test_us_t15_ac2_period_windows_have_the_documented_lengths(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    engine = MetricsEngine(ctx)
    assert engine.window_for("7d", NOW_MS).days == 7
    assert engine.window_for("30d", NOW_MS).days == 30
    assert engine.window_for("90d", NOW_MS).days == 90
    assert engine.window_for("mtd", NOW_MS).start_day == date(2026, 9, 1)
    assert engine.window_for("ytd", NOW_MS).start_day == date(2026, 1, 1)
    assert engine.window_for("since_inception", NOW_MS).start_day == END_DAY - timedelta(days=119)


def test_us_t15_ac2_short_windows_are_written_with_n_obs_not_dropped(engine_env) -> None:
    """PRD 10.5: below 20 active days the value is greyed, never silently absent."""
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    sharpe = values["sharpe"]
    assert sharpe.n_obs == 7
    assert sharpe.extra["below_min_active"] is True
    assert sharpe.extra["min_active_days"] == ctx.cfg.metrics.min_active_days
    assert sharpe.value is not None


def test_us_t15_ac2_long_windows_are_not_flagged(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    values = _by_name(MetricsEngine(ctx).compute_period("90d", NOW_MS), "90d")
    assert values["sharpe"].extra["below_min_active"] is False


def test_us_t15_ac2_an_empty_database_yields_none_not_a_crash(engine_env) -> None:
    ctx, _ = engine_env
    values = MetricsEngine(ctx).compute_all(NOW_MS)
    assert {v.name for v in values if v.period == "30d"} == set(METRIC_NAMES)
    assert all(v.value is None for v in values if v.name == "sharpe")
    assert all(v.n_obs == 0 for v in values if v.name == "sharpe")


def test_us_t15_ac2_no_stored_value_is_nan(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    for v in MetricsEngine(ctx).compute_all(NOW_MS):
        assert v.value is None or math.isfinite(v.value)


def test_us_t15_ac2_recomputation_is_deterministic(engine_env) -> None:
    ctx, _ = engine_env
    _seed_all(ctx.repos)
    first = MetricsEngine(ctx).compute_all(NOW_MS)
    second = MetricsEngine(ctx).compute_all(NOW_MS)
    assert [(v.name, v.period, v.value) for v in first] == [(v.name, v.period, v.value) for v in second]


def test_us_t15_ac2_rows_are_written_under_the_bound_strategy_only(engine_env) -> None:
    ctx, repos = engine_env
    _seed_all(ctx.repos)
    MetricsEngine(ctx).compute_all(NOW_MS)
    carry = Repositories(repos.db, Strategy.CARRY)
    assert carry.metrics.latest() == {}


# --------------------------------------------------------------------------- #
# AC 1 — the Appendix C.4 vectors, through the engine
# --------------------------------------------------------------------------- #


def test_us_t15_ac1_engine_turnover_reproduces_the_c4_vector(engine_env) -> None:
    """12 000 traded over 30 days on 10 000 average equity -> 1.2, annualised 14.6."""
    ctx, repos = engine_env
    days = _days(C4_DAYS)
    _seed_equity(repos, days, [0.0] * C4_DAYS, start_equity=C4_AVG_EQUITY)
    per_fill = C4_TRADED_NOTIONAL / len(days)
    repos.fills.add_many(
        [
            Fill(
                trade_id=f"f{i}",
                order_id=f"o{i}",
                symbol="BTCUSDT",
                side=Side.BUY,
                qty=per_fill / 100.0,
                price=100.0,
                fee=0.0,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=to_ms(day) + 1_000,
            )
            for i, day in enumerate(days)
        ]
    )
    value = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")["turnover"]
    assert value.extra["traded_notional"] == pytest.approx(C4_TRADED_NOTIONAL)
    assert value.extra["avg_equity"] == pytest.approx(C4_AVG_EQUITY)
    assert value.value == pytest.approx(C4_TURNOVER_PERIOD, abs=1e-9)
    assert value.extra["annualised"] == pytest.approx(C4_TURNOVER_ANNUALISED, abs=1e-9)


def test_us_t15_ac1_engine_beta_to_btc_reproduces_the_c4_vector(engine_env) -> None:
    """The C.4 synthetic pair, fed in as an equity curve and a BTC bar series."""
    ctx, repos = engine_env
    rng = np.random.default_rng(C4_BETA_SEED)
    btc = list(rng.normal(0.0, C4_BETA_BTC_SD, C4_BETA_DAYS))
    eps = rng.normal(0.0, C4_BETA_EPS_SD, C4_BETA_DAYS)
    strategy = [0.3 * b + e for b, e in zip(btc, eps, strict=True)]

    days = _days(C4_BETA_DAYS + 1)
    _seed_equity(repos, days, strategy)
    closes = [30_000.0]
    for r in btc:
        closes.append(closes[-1] * (1.0 + r))
    _seed_bars(repos, days, closes)

    values = _by_name(MetricsEngine(ctx).compute_period("since_inception", NOW_MS), "since_inception")
    assert values["beta_btc"].n_obs == C4_BETA_DAYS
    assert values["beta_btc"].value == pytest.approx(C4_BETA, abs=C4_BETA_TOL)
    assert values["corr_btc"].value == pytest.approx(C4_CORR, abs=C4_CORR_TOL)


# --------------------------------------------------------------------------- #
# AC 3 — the shared metrics come out of the shared functions unchanged
# --------------------------------------------------------------------------- #


def test_us_t15_ac3_shared_metrics_equal_the_pure_functions_on_the_same_window(engine_env) -> None:
    ctx, repos = engine_env
    days, returns = _seed_all(repos)
    window_returns = [returns[d] for d in days[-30:]]

    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    rf = ctx.cfg.bench.rf_annual
    expected_sharpe, expected_se = m.sharpe(window_returns, rf)
    assert values["sharpe"].value == pytest.approx(expected_sharpe, rel=1e-12)
    assert values["sharpe"].std_error == pytest.approx(expected_se, rel=1e-12)
    assert values["sortino"].value == pytest.approx(m.sortino(window_returns, rf), rel=1e-12)
    assert values["var_95"].value == pytest.approx(m.var(window_returns), rel=1e-12)
    assert values["cvar_95"].value == pytest.approx(m.cvar(window_returns), rel=1e-12)
    assert values["skew"].value == pytest.approx(m.skew(window_returns), rel=1e-12)
    assert values["kurtosis"].value == pytest.approx(m.kurtosis(window_returns), rel=1e-12)
    assert values["calmar"].value == pytest.approx(
        m.calmar(window_returns, values["max_drawdown"].value), rel=1e-12
    )


def test_us_t15_ac3_cash_alternative_and_net_of_infra_use_the_configured_inputs(engine_env, config) -> None:
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    cash = values["cash_alternative"]
    assert cash.value == pytest.approx(
        m.cash_alternative(cash.extra["equity0"], config.bench.rf_annual, 30), rel=1e-12
    )
    infra = values["net_of_infra"]
    assert infra.value == pytest.approx(
        m.net_of_infra(infra.extra["net_pnl"], config.infra.monthly_cost_eur, 30), rel=1e-12
    )


def test_us_t15_ac3_information_ratio_is_measured_against_the_backtest_reference(engine_env) -> None:
    """``tracking.ref_pnl`` becomes a return by dividing by the previous day's equity.

    The reference is seeded at exactly half the live P&L, so the active return is
    half the strategy return and the ratio is computable with ``statistics``.
    """
    ctx, repos = engine_env
    days = _days(40)
    returns = _seed_equity(repos, days, _wave(40))
    equity = {date.fromisoformat(r["day"]): float(r["equity"]) for r in repos.equity.all()}
    for prev, day in pairwise(days):
        repos.tracking.upsert(day, ref_pnl=0.5 * returns[day] * equity[prev])

    window = days[-30:]
    active = [returns[d] * 0.5 for d in window]
    expected = statistics.fmean(active) / statistics.stdev(active) * math.sqrt(365)

    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["information_ratio"].n_obs == 30
    assert values["information_ratio"].value == pytest.approx(expected, rel=1e-9)


def test_us_t15_ac3_information_ratio_is_none_without_a_reference_run(engine_env) -> None:
    ctx, repos = engine_env
    days = _days(40)
    _seed_equity(repos, days, _wave(40))
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["information_ratio"].value is None
    assert values["information_ratio"].n_obs == 0


# --------------------------------------------------------------------------- #
# TREND additions through the engine
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_realised_vol_is_compared_against_the_configured_target(engine_env, config) -> None:
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("90d", NOW_MS), "90d")
    target = config.sizing.sigma_target_portfolio
    assert values["realised_vol"].extra["target"] == pytest.approx(target)
    assert values["vol_ratio"].value == pytest.approx(values["realised_vol"].value / target)


def test_us_t15_ac2_exposure_falls_back_to_attribution_when_no_snapshot_exists(engine_env) -> None:
    """Three hand-written days on a flat 10 000 book, so every number is arithmetic.

    gross x = 0.30 / 0.20 / 0.25, net x = +0.10 / +0.10 / -0.25.
    """
    ctx, repos = engine_env
    days = _days(3)
    _seed_equity(repos, days, [0.0, 0.0], start_equity=10_000.0)
    book = {
        days[0]: (("BTCUSDT", "long", 2_000.0), ("ETHUSDT", "short", 1_000.0)),
        days[1]: (("BTCUSDT", "long", 1_500.0), ("ETHUSDT", "short", 500.0)),
        days[2]: (("BTCUSDT", "short", 2_500.0),),
    }
    for day, rows in book.items():
        repos.symbol_pnl.upsert_many(
            day,
            [
                {"symbol": s, "side": side, "avg_notional": n, "net_pnl": 0.0}
                for s, side, n in rows
            ],
        )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    gross, net = values["gross_exposure"], values["net_exposure"]
    assert gross.n_obs == 3
    assert gross.value == pytest.approx((0.30 + 0.20 + 0.25) / 3)
    assert gross.extra["max"] == pytest.approx(0.30)
    assert gross.extra["current"] == pytest.approx(0.25)
    assert net.value == pytest.approx((0.10 + 0.10 - 0.25) / 3)
    assert net.extra["max"] == pytest.approx(-0.25)  # most extreme reading, sign kept
    assert net.extra["current"] == pytest.approx(-0.25)


def test_us_t15_ac2_exposure_prefers_the_snapshot_series_when_present(engine_env) -> None:
    ctx, repos = engine_env
    days, _ = _seed_all(repos)
    for day in days[-5:]:
        account = AccountState(
            ts_ms=to_ms(day) + 60_000,
            wallet_balance=10_000.0,
            margin_balance=10_000.0,
            unrealized_pnl=0.0,
            available_balance=9_000.0,
            maint_margin=100.0,
            initial_margin=500.0,
        )
        repos.snapshots.add(account, [], gross=25_000.0, net=5_000.0)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["gross_exposure"].value == pytest.approx(2.5)
    assert values["net_exposure"].value == pytest.approx(0.5)
    assert values["gross_exposure"].n_obs == 5


def test_us_t15_ac2_execution_alpha_compares_realised_cost_with_the_model(engine_env, config) -> None:
    """Two 10 000 fills: 6.00 of fees and 4.00 of slippage -> 5 bps realised.

    The conservative model is the taker fee plus each symbol's slippage bps
    (Locked Decision 8), notional-weighted: BTC 2 bps and SOL 6 bps over equal
    clips, so ``alpha = (model - 5) / 10 000 x 20 000``.
    """
    ctx, repos = engine_env
    days = _days(3)
    _seed_equity(repos, days, [0.0, 0.0], start_equity=10_000.0)
    repos.fills.add_many(
        [
            Fill(
                trade_id="f-btc",
                order_id="o-btc",
                symbol="BTCUSDT",
                side=Side.BUY,
                qty=100.0,
                price=100.0,
                fee=1.0,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=to_ms(days[-1]) + 1_000,
                slippage_bps=1.0,
            ),
            Fill(
                trade_id="f-sol",
                order_id="o-sol",
                symbol="SOLUSDT",
                side=Side.SELL,
                qty=100.0,
                price=100.0,
                fee=5.0,
                fee_asset="USDT",
                is_maker=False,
                ts_ms=to_ms(days[-1]) + 2_000,
                slippage_bps=3.0,
            ),
        ]
    )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    cost, alpha = values["cost_per_unit_bps"], values["execution_alpha"]
    assert cost.extra["fees"] == pytest.approx(6.0)
    assert cost.extra["slippage"] == pytest.approx(4.0)
    assert cost.extra["traded_notional"] == pytest.approx(20_000.0)
    assert cost.value == pytest.approx(5.0)

    taker_bps = config.exec.taker_fee_fallback * 10_000.0
    model = taker_bps + (config.exec.slippage_for("BTCUSDT") + config.exec.slippage_for("SOLUSDT")) / 2
    assert alpha.extra["model_bps"] == pytest.approx(model)
    assert alpha.value == pytest.approx((model - 5.0) / 10_000.0 * 20_000.0)
    assert values["maker_ratio"].value == pytest.approx(0.5)


def test_us_t15_ac2_maker_ratio_reflects_the_fills(engine_env) -> None:
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["maker_ratio"].value == pytest.approx(2 / 3)  # one taker symbol in three


def test_us_t15_ac2_rebalance_completion_averages_scheduled_rebalances(engine_env) -> None:
    """100 / 90 / 80 scheduled -> 90.0; the risk cut beside them is not a rebalance."""
    ctx, repos = engine_env
    days = _days(3)
    _seed_equity(repos, days, [0.0, 0.0])
    for day, pct in zip(days, (100.0, 90.0, 80.0), strict=True):
        _finish_rebalance(repos, f"rb-{day}", day, pct)
    _finish_rebalance(repos, "cut", days[-1], 0.0, kind="risk_cut")

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    assert values["rebalance_completion"].n_obs == 3
    assert values["rebalance_completion"].value == pytest.approx(90.0)


def test_us_t15_ac2_rebalance_completion_of_the_seeded_month_is_exact(engine_env) -> None:
    """The seed cycles 100 / 95 / 90, so 30 days average to exactly 95."""
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["rebalance_completion"].n_obs == 30
    assert values["rebalance_completion"].value == pytest.approx(95.0)


def test_us_t15_ac2_side_attribution_and_concentration_reach_the_metric_table(engine_env) -> None:
    """Four hand-written symbol-days: BTC +12 then -6 long, ETH -4 then +8 short."""
    ctx, repos = engine_env
    days = _days(2)
    _seed_equity(repos, days, [0.0])
    repos.symbol_pnl.upsert_many(
        days[0],
        [
            {"symbol": "BTCUSDT", "side": "long", "avg_notional": 1_000.0, "net_pnl": 12.0},
            {"symbol": "ETHUSDT", "side": "short", "avg_notional": 1_000.0, "net_pnl": -4.0},
        ],
    )
    repos.symbol_pnl.upsert_many(
        days[1],
        [
            {"symbol": "BTCUSDT", "side": "long", "avg_notional": 1_000.0, "net_pnl": -6.0},
            {"symbol": "ETHUSDT", "side": "short", "avg_notional": 1_000.0, "net_pnl": 8.0},
        ],
    )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    assert values["long_pnl"].value == pytest.approx(6.0) and values["long_pnl"].n_obs == 2
    assert values["short_pnl"].value == pytest.approx(4.0) and values["short_pnl"].n_obs == 2
    assert values["hit_rate_long"].value == pytest.approx(0.5)
    assert values["hit_rate_short"].value == pytest.approx(0.5)
    concentration = values["concentration"]
    assert concentration.extra["shares"] == pytest.approx({"BTCUSDT": 0.6, "ETHUSDT": 0.4})
    assert concentration.value == pytest.approx(0.6)


def test_us_t15_ac2_governor_time_in_state_is_time_weighted(engine_env) -> None:
    """The seeded cut to g = 0.5 lands 60 days before the end of a 90-day window."""
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("90d", NOW_MS), "90d")
    assert values["governor_time_g1"].value == pytest.approx(30 / 90)
    assert values["governor_time_g05"].value == pytest.approx(60 / 90)
    assert values["governor_time_g025"].value == pytest.approx(0.0)
    assert values["governor_time_g025"].extra["other"] == pytest.approx(0.0)


def test_us_t15_ac2_governor_state_before_the_window_is_carried_in(engine_env) -> None:
    """The cut happened 90 days before the 30-day window: all of it sits at 0.5."""
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["governor_time_g05"].value == pytest.approx(1.0)
    assert values["governor_time_g1"].value == pytest.approx(0.0)


def test_us_t15_ac2_trade_statistics_are_the_closed_episodes_of_the_window(engine_env) -> None:
    """Three trades: 4/8/12 days, +30/-10/+20, MAE 5/15/25 (US-T14 AC 4)."""
    ctx, repos = engine_env
    days = _days(5)
    _seed_equity(repos, days, [0.0] * 4)
    for i, (held, pnl, mae) in enumerate(((4.0, 30.0, -5.0), (8.0, -10.0, -15.0), (12.0, 20.0, -25.0))):
        repos.trades.upsert(
            {
                "trade_key": f"T{i}",
                "symbol": SYMBOLS[i],
                "side": "long",
                "open_ts": to_ms(days[0]) - 20 * DAY_MS,
                "close_ts": to_ms(days[-1]) + 3_600_000,
                "days": held,
                "pnl": pnl,
                "mae": mae,
                "max_notional": 1_000.0,
            }
        )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    assert values["trade_count"].value == pytest.approx(3.0)
    assert values["avg_holding_days"].value == pytest.approx(8.0)
    assert values["win_rate"].value == pytest.approx(2 / 3)
    assert values["avg_win"].value == pytest.approx(25.0)
    assert values["avg_loss"].value == pytest.approx(-10.0)
    assert values["trade_count"].extra["mae_median"] == pytest.approx(15.0)
    assert values["trade_count"].extra["mae_max"] == pytest.approx(25.0)


def test_us_t15_ac2_signal_statistics_are_computed_over_the_window(engine_env) -> None:
    """Two days, two symbols: mean |s| 0.5, turnover 0.4, strong fraction 0.5."""
    ctx, repos = engine_env
    days = _days(2)
    _seed_equity(repos, days, [0.0])
    for day, values_by_symbol in (
        (days[0], {"BTCUSDT": 0.8, "ETHUSDT": -0.2}),
        (days[1], {"BTCUSDT": 0.4, "ETHUSDT": -0.6}),
    ):
        repos.signals.save_many(
            day,
            [
                SignalResult(symbol=s, x=(), y=(), z=(), u=(), signal=v, bar_day=day)
                for s, v in values_by_symbol.items()
            ],
            to_ms(day),
        )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    assert values["signal_mean_abs"].value == pytest.approx((0.8 + 0.2 + 0.4 + 0.6) / 4)
    assert values["signal_turnover"].value == pytest.approx(0.4)
    assert values["signal_strong_frac"].value == pytest.approx(0.5)
    assert values["signal_mean_abs"].n_obs == 4


def test_us_t15_ac2_regime_table_is_stored_with_its_buckets(engine_env) -> None:
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("since_inception", NOW_MS), "since_inception")
    buckets = values["regime_table"].extra["buckets"]
    assert set(buckets) == {"down", "flat", "up"}
    assert sum(b["months"] for b in buckets.values()) == values["regime_table"].n_obs


def test_us_t15_ac2_funding_share_uses_pre_cost_pnl(engine_env) -> None:
    """Price 100 + funding 25 = gross 125, so funding is a fifth of it — fees excluded."""
    ctx, repos = engine_env
    days = _days(2)
    _seed_equity(repos, days, [0.0])
    repos.symbol_pnl.upsert_many(
        days[-1],
        [
            {"symbol": "BTCUSDT", "side": "long", "price_pnl": 60.0, "funding": 15.0,
             "fees": 5.0, "slippage": 2.0, "net_pnl": 68.0},
            {"symbol": "ETHUSDT", "side": "short", "price_pnl": 40.0, "funding": 10.0,
             "fees": 3.0, "slippage": 1.0, "net_pnl": 46.0},
        ],
    )
    share = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")["funding_share"]
    assert share.extra["gross_pnl"] == pytest.approx(125.0)
    assert share.value == pytest.approx(0.2)


def _alternating(n: int, size: float) -> list[float]:
    """+size, -size, ... — every 30-day window has the same stdev, by construction."""
    return [size if i % 2 == 0 else -size for i in range(n)]


def _daily_for_annual_vol(annual: float, window: int) -> float:
    """The +/-c that makes a ``window``-long alternating series annualise to ``annual``."""
    return annual / math.sqrt(365.0) / math.sqrt(window / (window - 1.0))


def test_us_t15_ac2_vol_target_adherence_counts_every_day_on_target(engine_env, config) -> None:
    """A book running exactly at the 20 % target is inside 0.5x-1.5x on every day."""
    ctx, repos = engine_env
    target = config.sizing.sigma_target_portfolio
    days = _days(60)
    c = _daily_for_annual_vol(target, config.metrics.vol_window_days)
    _seed_equity(repos, days, _alternating(60, c))

    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["realised_vol"].value == pytest.approx(target, rel=1e-6)
    assert values["vol_ratio"].value == pytest.approx(1.0, rel=1e-6)
    assert values["vol_target_adherence"].n_obs == 30
    assert values["vol_target_adherence"].value == pytest.approx(1.0)


def test_us_t15_ac2_vol_target_adherence_counts_no_day_when_the_book_runs_hot(
    engine_env, config
) -> None:
    """Twice the target is outside the 1.5x band on every day — the fraction is 0."""
    ctx, repos = engine_env
    target = config.sizing.sigma_target_portfolio
    days = _days(60)
    c = _daily_for_annual_vol(2.0 * target, config.metrics.vol_window_days)
    _seed_equity(repos, days, _alternating(60, c))

    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["vol_ratio"].value == pytest.approx(2.0, rel=1e-6)
    assert values["vol_target_adherence"].n_obs == 30
    assert values["vol_target_adherence"].value == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Correlation with the CARRY sleeve
# --------------------------------------------------------------------------- #


def test_us_t15_ac2_corr_carry_is_none_when_the_sleeve_is_absent(engine_env) -> None:
    ctx, repos = engine_env
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["corr_carry"].value is None
    assert values["corr_carry"].n_obs == 0


def test_us_t15_ac2_corr_carry_is_computed_from_a_combined_database(engine_env) -> None:
    ctx, repos = engine_env
    days, returns = _seed_all(repos)
    carry = Repositories(repos.db, Strategy.CARRY)
    carry_returns = [-r for r in _wave(len(days))]
    _seed_equity(carry, days, carry_returns)

    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["corr_carry"].n_obs == 30
    assert values["corr_carry"].value == pytest.approx(-1.0, abs=1e-9)
    # The TREND numbers are untouched by the CARRY rows living in the same file.
    assert values["sharpe"].value == pytest.approx(
        m.sharpe([returns[d] for d in days[-30:]], ctx.cfg.bench.rf_annual)[0], rel=1e-12
    )


def test_us_t15_ac2_corr_carry_uses_the_same_window_as_corr_btc(engine_env) -> None:
    """Section 10: "Correlation with BTC / with CARRY ... same windows".

    CARRY is seeded as TREND's mirror image for everything except the last 60
    days, where it is a copy. The 60-day headline must therefore be +1 while the
    full period, carried in ``extra``, must not be.
    """
    ctx, repos = engine_env
    days = _days(120)
    trend_returns = _wave(120)
    _seed_equity(repos, days, trend_returns)
    window = ctx.cfg.metrics.beta_window_days
    # returns[j] is the return on days[j + 1]: the last `window` days are j >= 118 - window.
    mirrored = [r if j >= len(days) - 2 - window else -r for j, r in enumerate(trend_returns)]
    _seed_equity(Repositories(repos.db, Strategy.CARRY), days, mirrored)

    corr = _by_name(MetricsEngine(ctx).compute_period("since_inception", NOW_MS), "since_inception")[
        "corr_carry"
    ]
    assert corr.extra["window"] == window
    assert corr.value == pytest.approx(1.0, abs=1e-9)
    assert corr.extra["full_period"] < 0.9


def test_us_t15_ac2_rate_metrics_use_the_days_the_sleeve_existed(engine_env) -> None:
    """A window that opens before the first equity row must not bill, credit or
    annualise over days on which there was no sleeve.

    Seeded 40 days before ``NOW``, the year-to-date window is 273 days long but
    only 40 of them are the sleeve's, so ytd must agree with since-inception on
    every rate-like number.
    """
    base, repos = engine_env
    cfg = base.cfg.model_copy(update={"infra": base.cfg.infra.model_copy(update={"monthly_cost_eur": 6.0})})
    ctx = _context(cfg, repos, FakeClock(NOW_MS))
    days = _days(40)
    _seed_all(repos, 40)

    ytd = _by_name(MetricsEngine(ctx).compute_period("ytd", NOW_MS), "ytd")
    since = _by_name(
        MetricsEngine(ctx).compute_period("since_inception", NOW_MS), "since_inception"
    )
    assert ytd["sharpe"].extra["period_days"] == 273  # the window itself is untouched
    assert ytd["sharpe"].extra["live_days"] == 40

    cash = ytd["cash_alternative"]
    assert cash.value == pytest.approx(m.cash_alternative(cash.extra["equity0"], cfg.bench.rf_annual, 40))
    assert cash.value == pytest.approx(since["cash_alternative"].value)

    infra = ytd["net_of_infra"]
    assert infra.value == pytest.approx(m.net_of_infra(infra.extra["net_pnl"], 6.0, 40))
    assert infra.value == pytest.approx(since["net_of_infra"].value)

    assert ytd["turnover"].extra["annualised"] == pytest.approx(
        ytd["turnover"].value * 365 / 40
    )
    # The governor was at g = 1 for the first half of the 40 days, not for the
    # 233 days before the sleeve existed.
    assert ytd["governor_time_g1"].value == pytest.approx(0.5)
    assert ytd["governor_time_g1"].value == pytest.approx(since["governor_time_g1"].value)
    assert days[0] == date.fromisoformat(repos.equity.all()[0]["day"])


def test_us_t15_ac2_the_carry_sleeve_never_correlates_against_itself(config) -> None:
    """Run as CARRY, ``corr_carry`` is meaningless and stays ``None``."""
    db = open_db(":memory:")
    repos = Repositories(db, Strategy.CARRY)
    clock = FakeClock(NOW_MS)
    ctx = Context(
        cfg=config.model_copy(update={"strategy": Strategy.CARRY}),
        clock=clock,
        gateway=FakeGateway(clock),
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.CARRY),
    )
    _seed_all(repos)
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["corr_carry"].value is None
    db.close()


def test_us_t15_ac2_model_cost_reads_the_symbol_fee_when_one_is_stored(engine_env, config) -> None:
    """Locked Decision 4: the fee model comes from ``commissionRate``, stored in
    ``symbol_meta``; only a symbol without metadata falls back to the config."""
    ctx, repos = engine_env
    days = _days(3)
    _seed_equity(repos, days, [0.0, 0.0])
    repos.symbol_meta.upsert_many(
        [
            SymbolInfo(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                status="TRADING",
                contract_type="PERPETUAL",
                tick_size=0.1,
                step_size=0.001,
                min_qty=0.001,
                min_notional=5.0,
                price_precision=1,
                quantity_precision=3,
                taker_fee=0.0009,
            )
        ],
        to_ms(days[0]),
    )
    repos.fills.add_many(
        [
            Fill(
                trade_id="f-btc",
                order_id="o-btc",
                symbol="BTCUSDT",
                side=Side.BUY,
                qty=100.0,
                price=100.0,
                fee=1.0,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=to_ms(days[-1]) + 1_000,
                slippage_bps=1.0,
            )
        ]
    )

    values = _by_name(MetricsEngine(ctx).compute_period("7d", NOW_MS), "7d")
    expected = 0.0009 * 10_000.0 + config.exec.slippage_for("BTCUSDT")
    assert values["execution_alpha"].extra["model_bps"] == pytest.approx(expected)


def test_us_t15_ac2_model_cost_falls_back_to_the_defaults_without_fills(engine_env, config) -> None:
    ctx, repos = engine_env
    days = _days(40)
    _seed_equity(repos, days, _wave(40))
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    expected = config.exec.taker_fee_fallback * 10_000.0 + config.exec.slippage_for("default")
    assert values["execution_alpha"].extra["model_bps"] == pytest.approx(expected)
    assert values["execution_alpha"].value is None  # nothing traded -> no alpha to claim


def test_us_t15_ac2_flat_rows_and_days_without_equity_are_excluded(engine_env) -> None:
    """Exposure is only defined where there is equity to divide by, and flat is not a side."""
    ctx, repos = engine_env
    days = _days(30)
    _seed_equity(repos, days[-20:], _wave(20))
    for day in days:
        repos.symbol_pnl.upsert_many(
            day,
            [
                {"symbol": "BTCUSDT", "side": "long", "avg_notional": 1_000.0, "net_pnl": 1.0},
                {"symbol": "ETHUSDT", "side": "flat", "avg_notional": 0.0, "net_pnl": 0.0},
            ],
        )
    repos.fills.add_many(
        [
            Fill(
                trade_id="zero",
                order_id="zero",
                symbol="BTCUSDT",
                side=Side.BUY,
                qty=0.0,
                price=0.0,
                fee=0.0,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=to_ms(days[-1]),
            ),
        ]
    )
    values = _by_name(MetricsEngine(ctx).compute_period("30d", NOW_MS), "30d")
    assert values["gross_exposure"].n_obs == 20  # only the days with an equity row
    assert values["avg_long_count"].value == pytest.approx(1.0)
    assert values["avg_short_count"].value == pytest.approx(0.0)
    assert values["execution_alpha"].value is None  # a zero-notional fill is not trading
