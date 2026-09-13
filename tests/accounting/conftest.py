"""Shared offline fixtures for the accounting tests.

Everything is in memory and driven by ``FakeClock``: no network, no sleeping,
no wall-clock dependence (PRD Section 14).
"""

from __future__ import annotations

from datetime import date

import pytest

from aegis.core.clock import FakeClock, day_start_ms
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import Strategy
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

#: Every test day is this one, so timestamps in failures are readable.
DAY = date(2026, 1, 5)
T0 = day_start_ms(DAY)
HOUR_MS = 3_600_000


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def repos() -> Repositories:
    return Repositories(open_db(":memory:"), Strategy.TREND)


@pytest.fixture
def gateway(clock: FakeClock) -> FakeGateway:
    gw = FakeGateway(clock, wallet_balance=10_000.0, maker_fee=0.0002, taker_fee=0.0005)
    gw.set_symbol_info("BTCUSDT", step_size=0.001, tick_size=0.01, min_qty=0.001, min_notional=5.0)
    gw.set_symbol_info("ETHUSDT", step_size=0.01, tick_size=0.01, min_qty=0.01, min_notional=5.0)
    return gw


@pytest.fixture
def ctx(clock: FakeClock, repos: Repositories, gateway: FakeGateway) -> Context:
    alerts = AlertBus(repos.alerts, clock, Strategy.TREND)
    cfg = load_config(use_env=False)
    return Context(cfg=cfg, clock=clock, gateway=gateway, repos=repos, alerts=alerts)


def raw_income(
    gateway: FakeGateway,
    income_type: str,
    amount: float,
    ts_ms: int,
    *,
    symbol: str = "",
    tran_id: str | None = None,
) -> dict:
    """Append a raw venue income row — the shape ``LedgerService`` parses."""
    row = {
        "symbol": symbol,
        "incomeType": income_type,
        "income": f"{amount:.8f}",
        "asset": "USDT",
        "time": int(ts_ms),
        "info": "",
        "tranId": tran_id if tran_id is not None else str(900_000 + len(gateway.sim.income)),
        "tradeId": "",
    }
    gateway.sim.income.append(row)
    return row


def alert_codes(ctx: Context) -> list[str]:
    """Alert codes recorded so far, newest first."""
    return [row["code"] for row in ctx.repos.alerts.recent(limit=100)]
