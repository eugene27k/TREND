"""A database with known rows — the reports and gates are asserted against it."""

from __future__ import annotations

from datetime import date

import pytest

from aegis.core.clock import DAY_MS, FakeClock, day_start_ms, to_ms
from aegis.core.types import (
    AccountState,
    EquityPoint,
    MetricValue,
    Position,
    Strategy,
    UniverseEntry,
    UniverseResult,
)

DAY = date(2026, 9, 8)
EQUITY = 9874.10
REBALANCE_ID = "TREND-2026-09-08"


@pytest.fixture
def report_clock() -> FakeClock:
    """00:10 UTC on the morning after ``DAY`` — when the daily report is sent."""
    return FakeClock(to_ms("2026-09-09T00:10:00Z"))


def seed_daily(repos, *, day: date = DAY, equity: float = EQUITY) -> None:
    """Every row the Appendix D daily report reads, with Appendix D's own numbers."""
    end_ms = day_start_ms(day) + DAY_MS
    repos.equity.upsert(
        day,
        EquityPoint(ts_ms=day_start_ms(day), equity=equity, twr_factor=0.9938, twr_index=0.9874),
        peak_index=1.0,
        drawdown=0.021,
    )
    repos.governor.record(day_start_ms(day), 0.021, 1.0, 1.0, "daily")

    positions = _book(equity)
    account = AccountState(
        ts_ms=end_ms - 1,
        wallet_balance=equity,
        margin_balance=equity,
        unrealized_pnl=0.0,
        available_balance=equity * 0.6,
        maint_margin=equity * 0.09,
        initial_margin=equity * 0.2,
    )
    repos.snapshots.add(account, positions, gross=1.32 * equity, net=-0.41 * equity)

    repos.symbol_pnl.upsert_many(day, [
        {"symbol": "BTCUSDT", "side": "long", "avg_notional": 1000.0, "price_pnl": -30.0,
         "funding": 1.1, "fees": -3.3, "slippage": -1.1, "net_pnl": -33.3,
         "traded_notional": 1500.0, "signal": 0.4},
        {"symbol": "ETHUSDT", "side": "short", "avg_notional": -2073.561, "price_pnl": -25.9,
         "funding": 2.0, "fees": -3.0, "slippage": -1.0, "net_pnl": -27.9,
         "traded_notional": 1620.0, "signal": -0.6},
    ])

    repos.rebalances.create(REBALANCE_ID, day, day_start_ms(day) + 5 * 60_000, equity=equity)
    repos.rebalances.finish(
        REBALANCE_ID,
        ended_ts=day_start_ms(day) + 28 * 60_000,
        status="complete",
        completion_pct=100.0,
        traded_notional=3120.0,
        fees=4.90,
        avg_slippage_bps=1.4,
        maker_ratio=0.71,
        residuals=[],
    )

    repos.metrics.save_many([
        MetricValue(Strategy.TREND, "realised_vol", "30d", 0.178, end_ms, n_obs=30),
        MetricValue(Strategy.TREND, "sharpe", "since_inception", None, end_ms, n_obs=12),
    ])

    for minute in range(0, 1440, 60):
        repos.heartbeats.add(day_start_ms(day) + minute * 60_000, True, "tick")
    repos.reconciliations.add(end_ms - 1, "positions", True, "clean", [])

    repos.universe.save(
        UniverseResult(
            month="2026-09",
            entries=tuple(
                UniverseEntry(symbol=f"SYM{i}USDT", rank=i, median_quote_volume_30d=1e8,
                              history_days=500, included=True)
                for i in range(16)
            ),
        ),
        day_start_ms(day),
    )
    repos.state.save(
        state="IDLE", phase="P1_PAPER", paused=False, stopped=False, safe_mode=False,
        halt_reason="", governor_g=1.0, blocks=[],
        context={"backup_lag_s": 8.0, "bnb_days": 41.3}, now_ms=end_ms,
    )


def _book(equity: float) -> list[Position]:
    """6 long, 9 short, largest is a 0.21x ETH short (Appendix D)."""
    positions = [
        Position(symbol="ETHUSDT", qty=-1.0, entry_price=2100.0, mark_price=0.21 * equity)
    ]
    for i in range(6):
        positions.append(
            Position(symbol=f"L{i}USDT", qty=1.0, entry_price=100.0, mark_price=0.05 * equity)
        )
    for i in range(8):
        positions.append(
            Position(symbol=f"S{i}USDT", qty=-1.0, entry_price=100.0, mark_price=0.04 * equity)
        )
    return positions
