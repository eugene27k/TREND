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
    AccountState,
    DailyBar,
    EquityPoint,
    Fill,
    IncomeType,
    LedgerEntry,
    MetricValue,
    Position,
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
            # Funding settles three times a day per symbol: what the positions
            # page has to sum per open position (US-T18 AC 3).
            repos.ledger.add_many(
                LedgerEntry(
                    strategy=Strategy.TREND,
                    ts_ms=ts + h * 8 * 3_600_000,
                    income_type=IncomeType.FUNDING_FEE,
                    asset="USDT",
                    amount=-0.01,
                    symbol=s,
                    tran_id=f"{day.isoformat()}-{s}-{h}",
                )
                for s in SYMBOLS
                for h in range(3)
            )

        # A full book, so the positions page is measured with something in it.
        positions = [
            Position(
                symbol=s,
                qty=10.0 if k % 2 == 0 else -10.0,
                entry_price=100.0,
                mark_price=100.0,
                ts_ms=now_ms,
            )
            for k, s in enumerate(SYMBOLS)
        ]
        repos.positions.replace_all(positions, now_ms)
        repos.snapshots.add(
            AccountState(
                ts_ms=now_ms,
                wallet_balance=equity,
                margin_balance=equity,
                unrealized_pnl=0.0,
                available_balance=equity * 0.7,
                maint_margin=equity * 0.05,
                initial_margin=equity * 0.2,
            ),
            positions,
            gross=16_000.0,
            net=0.0,
        )
        for k, s in enumerate(SYMBOLS):
            repos.trades.upsert(
                {
                    "trade_key": f"{s}:1",
                    "symbol": s,
                    "side": "long" if k % 2 == 0 else "short",
                    "open_ts": day_start_ms(TODAY - timedelta(days=30)),
                    "days": 30.0,
                }
            )

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


def test_us_t18_ac8_the_positions_page_is_fast_with_a_full_book(large_client: TestClient) -> None:
    """16 open positions, each summing its own episode's funding out of a year of it."""
    large_client.get("/api/trend/positions")
    started = time.perf_counter()
    body = large_client.get("/api/trend/positions").json()
    elapsed = time.perf_counter() - started
    assert len(body["positions"]) == len(SYMBOLS)
    # 30 days x 3 settlements x -0.01, bounded by the open episode rather than
    # by the 400 days of funding the ledger holds.
    assert body["positions"][0]["funding_accrued"] == pytest.approx(-0.01 * 3 * 31, abs=0.03)
    assert elapsed < BUDGET_S, f"positions took {elapsed:.3f}s"
