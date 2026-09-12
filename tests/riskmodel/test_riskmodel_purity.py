"""US-T05 — the estimators see returns and parameters, nothing else.

ARCHITECTURE.md, "Core rules every module follows": ``signals/``, ``riskmodel/``,
``portfolio/`` and ``universe/select.py`` contain pure functions only — no I/O,
no clock, no DB, no positions — "enforced by signature tests".
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aegis.riskmodel import estimators

FORBIDDEN = {"clock", "positions", "pnl", "db", "gateway", "now", "equity", "account"}

PUBLIC = [
    estimators.build_risk_model,
    estimators.decay,
    estimators.ewma_cov,
    estimators.ewma_vol,
    estimators.ewma_vol_series,
    estimators.nearest_psd,
]


@pytest.mark.parametrize("fn", PUBLIC, ids=lambda f: f.__name__)
def test_us_t05_public_functions_take_no_state_parameters(fn: object) -> None:
    params = inspect.signature(fn).parameters
    assert not FORBIDDEN & {name.lower() for name in params}


def test_us_t05_module_never_calls_datetime_now_or_touches_io() -> None:
    source = Path(estimators.__file__).read_text(encoding="utf-8")
    for banned in ("datetime.now", "utcnow", "time.time", "open(", "sqlite3", "httpx", "requests"):
        assert banned not in source


def test_us_t05_import_does_not_pull_gateway_or_storage() -> None:
    src = str(Path(estimators.__file__).parents[2])
    probe = "import sys;import aegis.riskmodel.estimators;print([m for m in sys.modules if m.startswith('aegis.')])"
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
    assert "aegis.riskmodel.estimators" in loaded


def test_us_t05_estimators_do_not_mutate_their_inputs() -> None:
    returns = {"AAAUSDT": [0.01, -0.02, 0.03], "BBBUSDT": [0.02, 0.0, -0.01]}
    snapshot = {s: list(v) for s, v in returns.items()}
    corr = [[1.0, 0.9, -0.9], [0.9, 1.0, 0.9], [-0.9, 0.9, 1.0]]
    corr_snapshot = [row[:] for row in corr]

    estimators.ewma_vol(returns["AAAUSDT"])
    estimators.ewma_vol_series(returns["AAAUSDT"])
    estimators.ewma_cov(returns, min_obs=3)
    estimators.nearest_psd(corr)

    assert returns == snapshot
    assert corr == corr_snapshot


def test_us_t05_signature_matches_the_architecture_contract() -> None:
    # docs/ARCHITECTURE.md, "Key signatures (do not change without updating
    # every caller)". An extra knob is not free: a `min_obs` on `ewma_vol` would
    # advertise an observation-count gate the estimator does not have.
    assert list(inspect.signature(estimators.ewma_vol).parameters) == [
        "returns",
        "half_life",
        "floor",
        "cap",
        "annualisation_days",
    ]
    assert list(inspect.signature(estimators.ewma_cov).parameters) == [
        "returns",
        "half_life",
        "min_obs",
        "annualisation_days",
    ]
