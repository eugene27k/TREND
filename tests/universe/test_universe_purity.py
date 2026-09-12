"""US-T02 AC 1 — the selector sees exchangeInfo, bars and parameters, nothing else.

ARCHITECTURE.md, "Core rules every module follows": ``signals/``, ``riskmodel/``,
``portfolio/`` and ``universe/select.py`` contain pure functions only — no I/O, no
clock, no DB, no positions — "enforced by signature tests". The sibling modules
carry the same file; ``universe/`` was missing one.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from aegis.core.config import UniverseConfig
from aegis.universe import select as select_module
from aegis.universe.select import select_universe
from tests.universe.test_select import MONTH, MONTH_START, make_bars, make_symbol

FORBIDDEN = {"clock", "positions", "pnl", "db", "gateway", "now", "equity", "account"}


def test_us_t02_ac1_select_universe_takes_no_state_parameters() -> None:
    params = inspect.signature(select_universe).parameters
    assert not FORBIDDEN & {name.lower() for name in params}


def test_us_t02_ac1_signature_matches_the_architecture_contract() -> None:
    # docs/ARCHITECTURE.md, "Key signatures (do not change without updating every
    # caller)": select_universe(exchange_info, volume_history, params, month).
    assert list(inspect.signature(select_universe).parameters) == [
        "exchange_info",
        "volume_history",
        "params",
        "month",
    ]


def test_us_t02_ac1_module_never_calls_datetime_now_or_touches_io() -> None:
    source = Path(select_module.__file__).read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "time.time", "open(", "sqlite3", "httpx", "requests"):
        assert banned not in source


def test_us_t02_ac1_import_does_not_pull_gateway_or_storage() -> None:
    src = str(Path(select_module.__file__).parents[2])
    probe = "import sys;import aegis.universe.select;print([m for m in sys.modules if m.startswith('aegis.')])"
    out = subprocess.run(  # fixed argv, no shell, offline
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": src},
    )
    loaded = out.stdout.strip()
    assert "aegis.gateway" not in loaded
    assert "aegis.storage" not in loaded
    assert "aegis.universe.select" in loaded


def test_us_t02_ac1_selection_does_not_mutate_its_inputs() -> None:
    # The backtest reuses one bar store across 60 monthly calls: a selector that
    # sorted or trimmed the caller's lists in place would corrupt every later month.
    exchange_info = {s: make_symbol(s) for s in ("AAAUSDT", "BBBUSDT")}
    volume_history = {
        "AAAUSDT": make_bars("AAAUSDT", n=400, quote_volume=2e6),
        # Unsorted, with a duplicate day and a post-cut-off bar: the shapes the
        # selector normalises internally.
        "BBBUSDT": list(
            reversed(make_bars("BBBUSDT", n=401, quote_volume=1e6, last_day=MONTH_START))
        )
        + make_bars("BBBUSDT", n=1, quote_volume=1e6, last_day=MONTH_START - timedelta(days=1)),
    }
    info_snapshot = dict(exchange_info)
    history_snapshot = {s: list(bars) for s, bars in volume_history.items()}

    select_universe(exchange_info, volume_history, UniverseConfig(force_include=()), MONTH)

    assert exchange_info == info_snapshot
    assert volume_history == history_snapshot


def test_us_t02_ac1_repeated_calls_are_identical() -> None:
    exchange_info = {s: make_symbol(s) for s in ("AAAUSDT", "BBBUSDT")}
    volume_history = {
        "AAAUSDT": make_bars("AAAUSDT", n=400, quote_volume=2e6),
        "BBBUSDT": make_bars("BBBUSDT", n=400, quote_volume=1e6),
    }
    params = UniverseConfig(force_include=())
    first = select_universe(exchange_info, volume_history, params, MONTH)
    second = select_universe(exchange_info, volume_history, params, MONTH)
    assert first == second
