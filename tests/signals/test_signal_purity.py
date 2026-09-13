"""US-T04 AC 5 — the signal engine sees prices and parameters, nothing else."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aegis.signals import engine

FORBIDDEN = {"clock", "positions", "pnl", "db", "gateway", "now", "equity", "account"}

PUBLIC = [
    engine.compute_signal,
    engine.compute_signal_series,
    engine.response,
    engine.rolling_std,
    engine.signal_snapshot_row,
]


@pytest.mark.parametrize("fn", PUBLIC, ids=lambda f: f.__name__)
def test_us_t04_ac5_public_functions_take_no_state_parameters(fn: object) -> None:
    params = inspect.signature(fn).parameters
    assert not FORBIDDEN & {name.lower() for name in params}


def test_us_t04_ac5_module_never_calls_datetime_now_or_touches_io() -> None:
    source = Path(engine.__file__).read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "time.time", "open(", "sqlite3", "httpx", "requests"):
        assert banned not in source


def test_us_t04_ac5_import_does_not_pull_gateway_or_storage() -> None:
    src = str(Path(engine.__file__).parents[2])
    probe = "import sys;import aegis.signals.engine;print([m for m in sys.modules if m.startswith('aegis.')])"
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
    assert "aegis.signals.engine" in loaded


def test_us_t04_ac5_results_are_frozen_and_carry_no_position_state() -> None:
    from aegis.core.types import SignalResult

    fields = set(SignalResult.__dataclass_fields__)
    assert not FORBIDDEN & fields
    with pytest.raises(AttributeError):
        engine.compute_signal([1.0, 2.0], engine.SignalConfig()).signal = 1.0  # type: ignore[misc]
