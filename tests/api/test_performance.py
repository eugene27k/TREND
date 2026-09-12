"""US-T18 AC 8 — every page loads from stored data in well under a second.

The database here is the shape the free VM will actually hold after a year and
a bit: 400 days x 16 symbols of bars, signals and attribution rows, 400 daily
rebalances with their targets, slices and fills, and the full metric set. The
budget asserted is 0.5 s per page — half the PRD's second, so the test fails
before the operator notices, and with headroom for a slower host than this one.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from aegis.core.clock import day_start_ms
from aegis.core.types import (
    DailyBar,
    EquityPoint,
    Fill,
    MetricValue,
    Side,
    SignalResult,
    Strategy,
    SymbolTarget,
    Targets,
)
from tests.api.conftest import GET_ROUTES, TODAY, build_client, make_db, write_config

DAYS = 400
SYMBOLS = tuple(f"SYM{i:02d}USDT" for i in range(16))
BUDGET_S = 0.5


@pytest.fixture(scope="module")
def large_client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    tmp_path = tmp_path_factory.mktemp("large")
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    _seed_large(repos)
    repos.close()
    with build_client({"TREND": write_config(tmp_path, "TREND", db_path)}) as client:
        yield client


def _seed_large(repos) -> None:
    start = TODAY - timedelta(days=DAYS - 1)
    days = [start + timedelta(days=i) for i in range(DAYS)]
    now_ms = day_start_ms(TODAY) + 12 * 3_600_000
    equity = 10_000.0

    with repos.db.transaction():
        for i, day in enumerate(days):
            ts = day_start_ms(day)
            repos.bars.upsert_many(
                DailyBar(
                    symbol=s,
                    day=day,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0 + i,
                    volume=1_000.0,
                    quote_volume=5.0e7,
                    open_time_ms=ts,
                    close_time_ms=ts + 86_399_999,
                )
                for s in SYMBOLS
            )
            repos.signals.save_many(
                day,
                [
                    SignalResult(
                        symbol=s,
                        x=(0.1,) * 3,
                        y=(0.2,) * 3,
                        z=(0.3,) * 3,
                        u=(0.4,) * 3,
                        signal=0.3,
                        bar_day=day,
                        bar_ts_ms=ts,
                    )
                    for s in SYMBOLS
                ],
                now_ms,
            )
            repos.symbol_pnl.upsert_many(
                day,
                [
                    {
                        "symbol": s,
                        "side": "long",
                        "avg_notional": 1_000.0,
                        "price_pnl": 1.0,
                        "funding": -0.1,
                        "fees": -0.05,
                        "slippage": -0.02,
                        "net_pnl": 0.83,
                        "traded_notional": 100.0,
                        "signal": 0.3,
                    }
                    for s in SYMBOLS
                ],
            )
            equity *= 1.0005
            repos.equity.upsert(
                day,
                EquityPoint(ts_ms=ts, equity=equity, twr_index=equity / 10_000.0),
                peak_index=equity / 10_000.0,
                drawdown=0.0,
            )

            rid = f"R-{day.isoformat()}"
            repos.rebalances.create(
                rid, day, ts + 300_000, equity=equity, decision_mids=dict.fromkeys(SYMBOLS, 100.0)
            )
            repos.targets.save(
                rid,
                Targets(
                    targets=tuple(
                        SymbolTarget(
                            symbol=s,
                            signal=0.3,
                            vol=0.6,
                            raw=900.0,
                            target_notional=1_000.0,
                            target_qty=10.0,
                            current_qty=9.0,
                            delta_notional=100.0,
                        )
                        for s in SYMBOLS
                    ),
                    sigma_p=0.2,
                    conv=0.4,
                    sigma_eff=0.16,
                    s=0.8,
                    g=1.0,
                    equity=equity,
                ),
            )
            for k, s in enumerate(SYMBOLS):
                repos.slices.create(f"{rid}-{k}", rid, s, k, Side.BUY, 1.0, False, ts + 360_000)
            repos.fills.add_many(
                Fill(
                    trade_id=f"{rid}-{k}",
                    order_id=f"{rid}-{k}",
                    symbol=s,
                    side=Side.BUY,
                    qty=1.0,
                    price=100.0,
                    fee=0.05,
                    fee_asset="USDT",
                    is_maker=True,
                    ts_ms=ts + 400_000,
                    rebalance_id=rid,
                    slice_id=f"{rid}-{k}",
                )
                for k, s in enumerate(SYMBOLS)
            )
            repos.rebalances.finish(
                rid,
                ended_ts=ts + 900_000,
                status="complete",
                completion_pct=99.0,
                traded_notional=1_600.0,
                fees=0.8,
                avg_slippage_bps=1.5,
                maker_ratio=0.95,
                residuals=[],
            )
            repos.heartbeats.add(ts, ok=True)

        repos.metrics.save_many(
            [
                MetricValue(Strategy.TREND, name, period, 1.0, now_ms, 300, 0.3, {"active_days": 300})
                for period in ("7d", "30d", "90d", "mtd", "ytd", "since_inception")
                for name in (
                    "sharpe",
                    "max_drawdown",
                    "realised_vol",
                    "vol_ratio",
                    "concentration",
                    "cash_alternative",
                    "net_of_infra",
                    "turnover",
                    "maker_ratio",
                )
            ]
        )


@pytest.mark.parametrize("path", GET_ROUTES)
def test_us_t18_ac8_every_page_answers_well_inside_one_second(large_client: TestClient, path: str) -> None:
    large_client.get(path)  # warm the connection; the assertion is about the query cost
    started = time.perf_counter()
    response = large_client.get(path)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < BUDGET_S, f"{path} took {elapsed:.3f}s"


def test_us_t18_ac8_rebalance_drilldown_is_fast_on_a_year_of_history(large_client: TestClient) -> None:
    rid = large_client.get("/api/trend/rebalances").json()["rows"][0]["rebalance_id"]
    started = time.perf_counter()
    response = large_client.get(f"/api/trend/rebalances/{rid}")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert len(response.json()["targets"]) == len(SYMBOLS)
    assert elapsed < BUDGET_S, f"drill-down took {elapsed:.3f}s"
