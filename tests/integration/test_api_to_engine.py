"""The dashboard -> engine round trip.

The API and the engine are separate processes that meet only in the database, so
each side's own tests can pass while the seam between them is broken. These
tests drive the real HTTP endpoint and then tick the real runner against the
same database — which is how the API's confirmation token turned out not to be
forwarded, so every Stop typed into the dashboard would have been accepted by
the API and then silently refused by the engine.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.core.clock import to_ms
from aegis.core.types import Strategy
from aegis.ops.controls import CONFIRM_TOKEN
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories
from aegis.strategy_trend.runner import TrendRunner
from tests.integration.conftest import MIDNIGHT, build_world

BARS_AT = "2026-09-08T00:02:00Z"
REBALANCE_AT = "2026-09-08T00:05:00Z"


def write_config(tmp_path, db_path) -> pathlib.Path:
    """A real config file pointing at a real database — what the API is given."""
    source = pathlib.Path("config/trend.yaml").read_text(encoding="utf-8")
    patched = source.replace("db_path: data/trend.db", f"db_path: {db_path}")
    assert patched != source, "config/trend.yaml no longer declares storage.db_path"
    path = tmp_path / "trend.yaml"
    path.write_text(patched, encoding="utf-8")
    return path


@pytest.fixture
def wired(tmp_path):
    """One database, two processes: a FastAPI client and a live runner."""
    from aegis.core.clock import FakeClock
    from aegis.core.config import load_config
    from aegis.core.context import Context
    from aegis.ops.alerts import AlertBus

    db_path = tmp_path / "trend.db"
    config_path = write_config(tmp_path, db_path)
    clock = FakeClock(to_ms(MIDNIGHT))
    cfg = load_config(config_path, use_env=False)
    gateway = build_world(clock)
    db = open_db(db_path)
    repos = Repositories(db, Strategy.TREND)
    ctx = Context(cfg=cfg, clock=clock, gateway=gateway, repos=repos,
                  alerts=AlertBus(repos.alerts, clock, Strategy.TREND))

    runner = TrendRunner(ctx)
    runner.start(clock.now_ms())
    clock.set(to_ms(BARS_AT))
    runner.tick(clock.now_ms())

    client = TestClient(create_app({"TREND": config_path}, clock=clock))
    yield client, runner, ctx
    db.close()


def post(client, action: str, **body):
    payload = {"action": action, "operator": "eugene", "reason": "integration test"}
    payload.update(body)
    return client.post("/api/TREND/controls", json=payload)


def test_a_pause_typed_in_the_dashboard_stops_the_next_rebalance(wired):
    client, runner, ctx = wired
    assert post(client, "pause").status_code == 202

    ctx.clock.set(to_ms(REBALANCE_AT))
    report = runner.tick(ctx.clock.now_ms())
    assert "control:pause" in report.actions
    assert ctx.repos.rebalances.recent(5) == []


def test_a_stop_with_the_token_actually_stops_the_engine(wired):
    """The seam that was broken: the API accepted it and the engine refused it."""
    client, runner, ctx = wired
    response = post(client, "stop", confirm=CONFIRM_TOKEN)
    assert response.status_code == 202

    ctx.clock.set(to_ms("2026-09-08T00:06:00Z"))
    report = runner.tick(ctx.clock.now_ms())
    assert "control:stop" in report.actions, "the API must forward the confirmation token"
    assert "control_refused:stop" not in report.actions
    assert runner.machine.state.stopped


def test_a_stop_without_the_token_is_refused_by_the_api(wired):
    client, runner, ctx = wired
    assert post(client, "stop", confirm="please").status_code == 400
    ctx.clock.set(to_ms("2026-09-08T00:06:00Z"))
    runner.tick(ctx.clock.now_ms())
    assert not runner.machine.state.stopped


def test_the_api_never_writes_engine_state(wired):
    """The engine is the single writer of its own state."""
    client, _runner, ctx = wired
    before = ctx.repos.state.load()
    post(client, "pause")
    assert ctx.repos.state.load() == before, "the API must only append to control_log"
    assert ctx.repos.state.controls_after(0)


def test_the_dashboard_reads_what_the_engine_wrote(wired):
    client, runner, ctx = wired
    ctx.clock.set(to_ms(REBALANCE_AT))
    runner.tick(ctx.clock.now_ms())

    overview = client.get("/api/TREND/overview")
    assert overview.status_code == 200

    positions = client.get("/api/TREND/positions").json()
    assert positions["positions"], "the book the engine just took must be visible"

    rebalances = client.get("/api/TREND/rebalances").json()
    assert rebalances["rows"]
    assert rebalances["rows"][0]["completion_pct"] >= 95.0

    signals = client.get("/api/TREND/signals").json()
    assert len(signals["rows"]) == ctx.cfg.universe.size


def test_every_page_answers_on_an_empty_database(tmp_path):
    """Day one: the dashboard must render before the engine has ever run."""
    db_path = tmp_path / "empty.db"
    open_db(db_path).close()
    client = TestClient(create_app({"TREND": write_config(tmp_path, db_path)}))
    for path in ("/api/strategies", "/api/overview", "/api/TREND/overview",
                 "/api/TREND/signals", "/api/TREND/positions", "/api/TREND/rebalances",
                 "/api/TREND/attribution", "/api/TREND/metrics", "/api/TREND/operations",
                 "/api/TREND/backtest", "/api/TREND/universe"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
