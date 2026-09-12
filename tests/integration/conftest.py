"""A whole synthetic venue: 20 trending symbols, a book, an account, bars.

These fixtures build the smallest world in which the *entire* engine can run —
universe selection, bars, signals, risk model, sizing, planning, execution,
accounting and risk — so that integration defects show up here rather than in
production.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from aegis.core.clock import FakeClock, to_ms
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import DailyBar, Strategy
from aegis.gateway.fake import FakeGateway
from aegis.ops.alerts import AlertBus
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

TODAY = date(2026, 9, 8)
MIDNIGHT = "2026-09-08T00:00:30Z"
N_SYMBOLS = 20
HISTORY_DAYS = 520
EQUITY = 10_000.0


def build_world(
    clock: FakeClock, *, seed: int = 3, n_symbols: int = N_SYMBOLS, equity: float = EQUITY
) -> FakeGateway:
    gw = FakeGateway(clock)
    rng = random.Random(seed)
    for k in range(n_symbols):
        symbol = f"S{k:02d}USDT"
        gw.set_symbol_info(
            symbol,
            tick_size=0.001,
            step_size=0.001,
            min_qty=0.001,
            min_notional=5.0,
            price_precision=3,
            quantity_precision=3,
        )
        price = 100.0
        drift = 0.004 * (1 if k % 2 == 0 else -1)
        bars: list[DailyBar] = []
        for i in range(HISTORY_DAYS):
            day = TODAY - timedelta(days=HISTORY_DAYS - i)
            if i and i % 130 == 0:
                drift = -drift
            price *= math.exp(rng.gauss(drift, 0.02))
            open_ = price * math.exp(rng.gauss(0.0, 0.002))
            bars.append(
                DailyBar(
                    symbol,
                    day,
                    open_,
                    max(open_, price) * 1.01,
                    min(open_, price) * 0.99,
                    price,
                    1e5,
                    5e8 * (1 + 0.05 * k),
                    to_ms(day),
                    to_ms(day) + 86_399_999,
                )
            )
        gw.set_bars(symbol, bars)
        # Deliberately off the tick grid: a real venue quotes on it, and a taker
        # that only works on-grid is a bug waiting for the one venue that doesn't.
        gw.set_book(symbol, bid=price * 0.9995, ask=price * 1.0005)
        gw.set_mark(symbol, price)
        gw.set_predicted_funding(symbol, 0.0001, interval_hours=8.0)
    gw.set_account(wallet_balance=equity, margin_balance=equity, available_balance=equity)
    gw.set_fill_policy("immediate")
    return gw


@pytest.fixture
def world():
    clock = FakeClock(to_ms(MIDNIGHT))
    cfg = load_config("config/trend.yaml", use_env=False)
    gateway = build_world(clock)
    db = open_db(":memory:")
    repos = Repositories(db, Strategy.TREND)
    ctx = Context(
        cfg=cfg,
        clock=clock,
        gateway=gateway,
        repos=repos,
        alerts=AlertBus(repos.alerts, clock, Strategy.TREND),
    )
    yield ctx
    db.close()
