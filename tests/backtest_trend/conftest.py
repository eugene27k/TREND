"""Synthetic but realistic market data for the backtester tests.

Everything is generated from a fixed seed so the whole suite is deterministic
and offline: the Binance public archive is reached only through an injected
fetcher, and no test ever opens a socket.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from aegis.core.clock import month_key
from aegis.core.config import load_config
from aegis.core.types import DailyBar, FundingRate

START = date(2021, 1, 1)
DAYS = 900
N_SYMBOLS = 20


def make_market(
    seed: int = 7, days: int = DAYS, n_symbols: int = N_SYMBOLS, regime_days: int = 120
) -> tuple[dict, dict, dict]:
    """A market with genuine, flipping trends — the regime momentum is built for."""
    rng = random.Random(seed)
    bars: dict[str, list[DailyBar]] = {}
    funding: dict[str, list[FundingRate]] = {}
    inventory: dict[str, list[str]] = {}
    for k in range(n_symbols):
        symbol = f"S{k:02d}USDT"
        price = 100.0
        drift = 0.004 * (1 if k % 2 == 0 else -1)
        rows: list[DailyBar] = []
        rates: list[FundingRate] = []
        for i in range(days):
            day = START + timedelta(days=i)
            if i and i % regime_days == 0:
                drift = -drift
            price *= math.exp(rng.gauss(drift, 0.025))
            open_ = price * math.exp(rng.gauss(0.0, 0.002))
            rows.append(
                DailyBar(
                    symbol,
                    day,
                    open_,
                    max(open_, price) * 1.01,
                    min(open_, price) * 0.99,
                    price,
                    1e5,
                    5e8 * (1 + 0.05 * k),
                    0,
                    0,
                    source="synthetic",
                )
            )
            base_ms = int((day - date(1970, 1, 1)).days) * 86_400_000
            for hour in (0, 8, 16):
                rates.append(FundingRate(symbol, base_ms + hour * 3_600_000, 0.0001, 8.0))
        bars[symbol] = rows
        funding[symbol] = rates
        inventory[symbol] = sorted({month_key(r.day) for r in rows})
    return bars, funding, inventory


@pytest.fixture(scope="session")
def market():
    return make_market()


@pytest.fixture(scope="session")
def bt_cfg():
    return load_config("config/trend.yaml", use_env=False)
