"""Fixtures for the dashboard API: a real SQLite file, seeded through the repositories.

The API opens files read-only, so an in-memory database cannot be shared with
it: every fixture here writes a temp file with a writer connection, closes it,
and then builds the app over the same path. Seeding goes through
``aegis.storage.repositories`` only — the tests must break if a repository's
shape changes, which is the point of not writing SQL here either.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.core.clock import FakeClock, day_start_ms, to_ms
from aegis.core.types import (
    AccountState,
    Alert,
    DailyBar,
    EquityPoint,
    Fill,
    IncomeType,
    LedgerEntry,
    MetricValue,
    Position,
    RiskModel,
    Severity,
    Side,
    SignalResult,
    Strategy,
    SymbolInfo,
    SymbolTarget,
    Targets,
    UniverseEntry,
    UniverseResult,
)
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

NOW = "2026-09-12T12:00:00Z"
NOW_MS = to_ms(NOW)
TODAY = date(2026, 9, 12)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
REBALANCE_ID = "TREND-2026-09-12"


def write_config(tmp_path: Path, strategy: str, db_path: Path) -> Path:
    """A minimal per-sleeve YAML — everything else falls back to Appendix A."""
    path = tmp_path / f"{strategy.lower()}.yaml"
    path.write_text(
        f"strategy: {strategy}\nmode: paper\nstorage:\n  db_path: {db_path}\n"
        "infra:\n  monthly_cost_eur: 0.0\n",
        encoding="utf-8",
    )
    return path


def make_db(path: Path) -> Repositories:
    return Repositories(open_db(path), Strategy.TREND)


def seed(repos: Repositories, *, days: int = 40, symbols: tuple[str, ...] = SYMBOLS) -> None:
    """A realistic day-N database: every table a page reads has rows."""
    clock = FakeClock(NOW)
    now_ms = clock.now_ms()
    start = TODAY - timedelta(days=days - 1)
    day_list = [start + timedelta(days=i) for i in range(days)]

    repos.symbol_meta.upsert_many(
        [
            SymbolInfo(
                symbol=s,
                base_asset=s[:-4],
                quote_asset="USDT",
                status="TRADING",
                contract_type="PERPETUAL",
                tick_size=0.1,
                step_size=0.001,
                min_qty=0.001,
                min_notional=5.0,
                price_precision=2,
                quantity_precision=3,
            )
            for s in symbols
        ],
        now_ms,
    )

    # --- bars, signals, risk model ---------------------------------------- #
    for i, day in enumerate(day_list):
        repos.bars.upsert_many(
            DailyBar(
                symbol=s,
                day=day,
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i + k,
                volume=1000.0,
                quote_volume=5.0e7,
                open_time_ms=day_start_ms(day),
                close_time_ms=day_start_ms(day) + 86_399_999,
            )
            for k, s in enumerate(symbols)
        )
        repos.signals.save_many(
            day,
            [
                SignalResult(
                    symbol=s,
                    x=(0.1, 0.2, 0.3),
                    y=(0.4, 0.5, 0.6),
                    z=(0.7, 0.8, 0.9),
                    u=(0.3 + k / 10, 0.2, 0.1),
                    signal=0.5 - 0.1 * k,
                    bar_day=day,
                    bar_ts_ms=day_start_ms(day),
                    warm=True,
                )
                for k, s in enumerate(symbols)
            ],
            now_ms,
        )
    repos.risk_model.save(
        TODAY,
        RiskModel(
            symbols=symbols,
            vols=dict.fromkeys(symbols, 0.6),
            corr=tuple(
                tuple(1.0 if a == b else 0.3 for b in range(len(symbols))) for a in range(len(symbols))
            ),
            avg_corr=0.3,
        ),
        now_ms,
    )

    # --- equity, snapshot, positions --------------------------------------- #
    equity = 10_000.0
    peak = 1.0
    for i, day in enumerate(day_list):
        equity *= 1.001 if i % 3 else 0.999
        index = equity / 10_000.0
        peak = max(peak, index)
        repos.equity.upsert(
            day,
            EquityPoint(ts_ms=day_start_ms(day), equity=equity, twr_factor=1.0, twr_index=index),
            peak_index=peak,
            drawdown=(peak - index) / peak,
        )
    account = AccountState(
        ts_ms=now_ms,
        wallet_balance=equity,
        margin_balance=equity,
        unrealized_pnl=12.0,
        available_balance=equity * 0.7,
        maint_margin=equity * 0.05,
        initial_margin=equity * 0.2,
    )
    positions = [
        Position(
            symbol="BTCUSDT",
            qty=0.05,
            entry_price=60_000.0,
            mark_price=61_000.0,
            unrealized_pnl=50.0,
            adl_quantile=2,
            liquidation_price=30_000.0,
            ts_ms=now_ms,
        ),
        Position(
            symbol="ETHUSDT",
            qty=-0.4,
            entry_price=3_000.0,
            mark_price=2_950.0,
            unrealized_pnl=20.0,
            adl_quantile=4,
            liquidation_price=6_000.0,
            ts_ms=now_ms,
        ),
    ]
    repos.snapshots.add(account, positions, gross=4_230.0, net=1_870.0)
    repos.positions.replace_all(positions, now_ms)

    # --- ledger (funding accrual per symbol) ------------------------------- #
    repos.ledger.add_many(
        [
            LedgerEntry(
                strategy=Strategy.TREND,
                ts_ms=day_start_ms(d),
                income_type=IncomeType.FUNDING_FEE,
                asset="USDT",
                amount=-0.25,
                symbol="BTCUSDT",
                tran_id=f"f{i}",
            )
            for i, d in enumerate(day_list)
        ]
    )

    # --- a rebalance with targets, slices and fills ------------------------ #
    started = day_start_ms(TODAY) + 5 * 60_000
    targets = Targets(
        targets=tuple(
            SymbolTarget(
                symbol=s,
                signal=0.5 - 0.1 * k,
                vol=0.6,
                raw=900.0,
                target_notional=1_000.0 - 100.0 * k,
                funding_ann=0.35 if k == 0 else 0.05,
                funding_haircut=0.5 if k == 0 else 1.0,
                caps_applied=("single",) if k == 0 else (),
                target_qty=0.02,
                current_qty=0.01,
                delta_notional=250.0,
                traded=True,
            )
            for k, s in enumerate(symbols)
        ),
        sigma_p=0.22,
        conv=0.4,
        sigma_eff=0.16,
        s=0.73,
        g=1.0,
        equity=equity,
    )
    repos.rebalances.create(
        REBALANCE_ID,
        TODAY,
        started,
        equity=equity,
        governor_g=1.0,
        order_plan=[{"symbol": "BTCUSDT", "side": "BUY", "delta_notional": 250.0, "sequence": 0}],
        decision_mids=dict.fromkeys(symbols, 100.0),
        planned_notional=750.0,
    )
    repos.targets.save(REBALANCE_ID, targets)
    repos.slices.create("SL-1", REBALANCE_ID, "BTCUSDT", 0, Side.BUY, 0.01, False, started + 1_000)
    repos.slices.finish("SL-1", "filled", 0.01, 61_000.0, False, started + 30_000)
    repos.fills.add_many(
        [
            Fill(
                trade_id="T1",
                order_id="O1",
                symbol="BTCUSDT",
                side=Side.BUY,
                qty=0.01,
                price=61_000.0,
                fee=0.12,
                fee_asset="USDT",
                is_maker=True,
                ts_ms=started + 30_000,
                rebalance_id=REBALANCE_ID,
                slice_id="SL-1",
                decision_mid=60_990.0,
                slippage_bps=1.6,
            )
        ]
    )
    repos.rebalances.finish(
        REBALANCE_ID,
        ended_ts=started + 600_000,
        status="complete",
        completion_pct=98.0,
        traded_notional=740.0,
        fees=0.12,
        avg_slippage_bps=1.6,
        maker_ratio=1.0,
        residuals=[{"symbol": "SOLUSDT", "residual_notional": 10.0}],
    )

    # --- governor, attribution, trades ------------------------------------- #
    repos.governor.record(day_start_ms(TODAY - timedelta(days=5)), 0.13, 1.0, 0.5, "down", True)
    repos.governor.record(day_start_ms(TODAY - timedelta(days=1)), 0.05, 0.5, 1.0, "up", True)
    for i, day in enumerate(day_list):
        repos.symbol_pnl.upsert_many(
            day,
            [
                {
                    "symbol": s,
                    "side": "long" if k % 2 == 0 else "short",
                    "avg_notional": 1_000.0,
                    "price_pnl": 3.0 - k,
                    "funding": -0.25,
                    "fees": -0.1,
                    "slippage": -0.05,
                    "net_pnl": 2.6 - k,
                    "traded_notional": 120.0,
                    "signal": 0.5 - 0.1 * k,
                }
                for k, s in enumerate(symbols)
            ],
        )
        if i == 0:
            continue
    repos.trades.upsert(
        {
            "trade_key": "BTCUSDT:1",
            "symbol": "BTCUSDT",
            "side": "long",
            "open_ts": day_start_ms(start),
            "close_ts": day_start_ms(TODAY),
            "days": float(days),
            "pnl": 120.0,
            "mae": -30.0,
            "max_notional": 1_200.0,
            "entry_signal": 0.6,
            "exit_signal": 0.1,
        }
    )

    # --- metrics (what every page reads instead of recomputing) ------------ #
    values: list[MetricValue] = []
    for period in ("7d", "30d", "90d", "mtd", "ytd", "since_inception"):
        extra = {
            "active_days": 25,
            "below_min_active": False,
            "min_active_days": 20,
            "period_days": 30,
            "start_day": start.isoformat(),
            "end_day": TODAY.isoformat(),
        }
        values += [
            MetricValue(Strategy.TREND, "sharpe", period, 1.1, now_ms, 25, 0.4, extra),
            MetricValue(Strategy.TREND, "max_drawdown", period, 0.08, now_ms, 25, None, extra),
            MetricValue(Strategy.TREND, "realised_vol", period, 0.19, now_ms, 25, None, extra),
            MetricValue(Strategy.TREND, "vol_ratio", period, 0.95, now_ms, 25, None, extra),
            MetricValue(Strategy.TREND, "concentration", period, 0.4, now_ms, 25, None, extra),
            MetricValue(Strategy.TREND, "cash_alternative", period, 40.0, now_ms, 25, None, extra),
            MetricValue(Strategy.TREND, "net_of_infra", period, 95.0, now_ms, 25, None, extra),
        ]
    values.append(
        MetricValue(
            Strategy.TREND,
            "regime_table",
            "since_inception",
            None,
            now_ms,
            3,
            None,
            {
                "buckets": {
                    "down": {
                        "label": "< -10%",
                        "months": 2,
                        "pnl": 40.0,
                        "hit_rate": 0.5,
                        "avg_exposure": 1.2,
                    },
                    "flat": {
                        "label": "-10%..+10%",
                        "months": 5,
                        "pnl": -5.0,
                        "hit_rate": 0.4,
                        "avg_exposure": 1.1,
                    },
                    "up": {
                        "label": "> +10%",
                        "months": 3,
                        "pnl": 90.0,
                        "hit_rate": 0.67,
                        "avg_exposure": 1.3,
                    },
                }
            },
        )
    )
    values.append(MetricValue(Strategy.TREND, "rss_mb", "7d", 420.0, now_ms, 7, None, {}))
    values.append(MetricValue(Strategy.TREND, "cpu_pct", "7d", 8.5, now_ms, 7, None, {}))
    repos.metrics.save_many(values)

    # --- ops ---------------------------------------------------------------- #
    repos.state.save(
        state="IDLE",
        phase="P1_PAPER",
        paused=False,
        stopped=False,
        safe_mode=False,
        halt_reason="",
        governor_g=1.0,
        blocks=[],
        context={},
        now_ms=now_ms,
    )
    repos.state.log_control("start", "operator", "phase P1", {}, now_ms - 3_600_000)
    for i in range(24):
        repos.heartbeats.add(now_ms - i * 3_600_000, ok=True, detail="tick")
    repos.reconciliations.add(now_ms - 600_000, "positions", True, "clean", [])
    repos.reconciliations.add(
        now_ms - 300_000, "balance", False, "1.2 USDT drift", [{"asset": "USDT", "delta": 1.2}]
    )
    repos.alerts.add(
        Alert(Strategy.TREND, now_ms - 900_000, Severity.INFO, "BACKUP_OK", "litestream replicated", {})
    )
    repos.alerts.add(
        Alert(
            Strategy.TREND,
            now_ms - 60_000,
            Severity.WARN,
            "DAILY_LOSS",
            "day loss 6.2% of equity",
            {"loss": 0.062},
        )
    )
    repos.reports.save("daily", TODAY.isoformat(), "TREND daily report", now_ms)

    # --- universe ----------------------------------------------------------- #
    repos.universe.save(
        UniverseResult(
            month="2026-08",
            entries=(
                UniverseEntry("BTCUSDT", 1, 9.0e8, 500, True),
                UniverseEntry("ETHUSDT", 2, 7.0e8, 500, True),
                UniverseEntry("XRPUSDT", 3, 4.0e8, 500, True),
            ),
        ),
        now_ms,
    )
    repos.universe.save(
        UniverseResult(
            month="2026-09",
            entries=(
                UniverseEntry("BTCUSDT", 1, 9.5e8, 520, True),
                UniverseEntry("ETHUSDT", 2, 7.5e8, 520, True),
                UniverseEntry("SOLUSDT", 3, 5.0e8, 430, True),
                UniverseEntry("DOGEUSDT", 4, 1.0e8, 120, False, "insufficient history"),
            ),
        ),
        now_ms,
    )
    repos.illiquid.flag("SOLUSDT", now_ms - 86_400_000, now_ms + 86_400_000, "3 failed rebalances")

    # --- backtest and tracking ---------------------------------------------- #
    repos.backtest.save_run(
        "RUN-1",
        created_ts=now_ms - 86_400_000,
        start_day="2021-01-01",
        end_day="2026-08-31",
        variant="default",
        params={"sizing": {"s_max": 3.0}},
        manifest={"git": "abc123", "rows": 2000},
        git_commit="abc123",
        metrics={"sharpe": 0.9, "max_drawdown": 0.22},
        equity=[
            {"day": d.isoformat(), "equity": 10_000.0 + i * 5, "twr_index": 1.0, "drawdown": 0.0}
            for i, d in enumerate(day_list)
        ],
        duration_s=42.0,
    )
    repos.backtest.save_robustness(
        "RUN-1",
        [
            {"variant": "default", "net_pnl": 1200.0, "sharpe": 0.9, "max_dd": 0.22, "sign_ok": True},
            {
                "variant": "fees_x2",
                "net_pnl": 800.0,
                "sharpe": 0.6,
                "max_dd": 0.25,
                "sign_ok": True,
                "detail": {"note": "costs doubled"},
            },
        ],
    )
    repos.backtest.save_walkforward(
        "RUN-1",
        [
            {
                "window": "2022H1",
                "param_set": "default",
                "test_sharpe": 0.8,
                "rank": 2,
                "n_params": 9,
                "default_in_top_half": True,
                "is_default": True,
            },
            {"window": "2022H1", "param_set": "fast", "test_sharpe": 1.0, "rank": 1, "n_params": 9},
        ],
    )
    repos.backtest.save_bootstrap("RUN-1", "3m", {5.0: -0.08, 50.0: 0.02, 95.0: 0.12})
    for i, day in enumerate(day_list):
        repos.tracking.upsert(
            day,
            live_pnl=2.0,
            ref_pnl=2.2,
            cum_live=2.0 * (i + 1),
            cum_ref=2.2 * (i + 1),
            corr_30d=0.85,
            cum_diff_frac=0.01,
            cost_ratio=1.2,
            turnover_ratio=1.1,
            in_bounds=True,
            breach_days=0,
        )


def build_client(config_paths: dict[str, Any]) -> TestClient:
    return TestClient(create_app(config_paths, clock=FakeClock(NOW)))


@pytest.fixture
def seeded_client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    seed(repos)
    repos.close()
    with build_client({"TREND": write_config(tmp_path, "TREND", db_path)}) as client:
        yield client


@pytest.fixture
def empty_client(tmp_path: Path) -> Iterator[TestClient]:
    """Day one: the schema exists, nothing has happened yet."""
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    repos.close()
    with build_client({"TREND": write_config(tmp_path, "TREND", db_path)}) as client:
        yield client


#: Every GET a dashboard page issues, as (path, minimal query).
GET_ROUTES: tuple[str, ...] = (
    "/api/health",
    "/api/strategies",
    "/api/overview",
    "/api/trend/overview",
    "/api/trend/signals",
    "/api/trend/signals/BTCUSDT/history?days=90",
    "/api/trend/positions",
    "/api/trend/rebalances",
    "/api/trend/attribution",
    "/api/trend/metrics",
    "/api/trend/operations",
    "/api/trend/backtest",
    "/api/trend/universe",
    "/api/trend/controls",
)
