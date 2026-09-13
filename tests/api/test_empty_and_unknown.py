"""Day one and bad input: the dashboard renders before the engine has run.

An empty database is the normal state of a freshly deployed sleeve, so every
page must answer 200 with an empty payload rather than 500. An unknown or
undeployed strategy is a 404 with a message that says which sleeves exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aegis.core.clock import to_ms
from aegis.core.types import Position, Strategy
from aegis.storage.repositories import Repositories
from tests.api.conftest import GET_ROUTES, build_client, make_db, seed, write_config


def test_us_t18_every_page_returns_200_on_an_empty_database(empty_client: TestClient) -> None:
    for path in GET_ROUTES:
        response = empty_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text}"


def test_us_t18_empty_database_yields_empty_payloads_not_nulls(empty_client: TestClient) -> None:
    overview = empty_client.get("/api/trend/overview").json()
    assert overview["curve"] == []
    assert overview["kpis"]["equity"] == 0.0
    assert overview["kpis"]["sharpe"] is None
    assert overview["kpis"]["governor_g"] == 1.0
    assert overview["kpis"]["phase"] == "P0_BACKTEST"  # the config default, not a crash
    assert overview["cumulative_components"]["net_pnl"] == 0.0

    assert empty_client.get("/api/trend/signals").json()["rows"] == []
    assert empty_client.get("/api/trend/positions").json()["positions"] == []
    assert empty_client.get("/api/trend/rebalances").json()["rows"] == []
    assert empty_client.get("/api/trend/universe").json()["months"] == []
    assert empty_client.get("/api/trend/backtest").json()["run"] is None
    assert empty_client.get("/api/trend/operations").json()["heartbeat"]["uptime_24h_pct"] is None
    assert empty_client.get("/api/overview").json()["sleeves"][0]["equity"] == 0.0
    for block in empty_client.get("/api/trend/metrics").json()["periods"]:
        assert block["metrics"] == []


def test_us_t18_empty_positions_page_reports_full_cap_headroom(empty_client: TestClient) -> None:
    body = empty_client.get("/api/trend/positions").json()
    assert all(cap["used_usdt"] == 0.0 and cap["breached"] is False for cap in body["caps"])
    assert body["margin"]["survivable_move"] == 0.0  # no equity recorded yet
    assert body["risk_status"] == "green"


@pytest.mark.parametrize("path", ["/api/basket/overview", "/api/nonsense/positions"])
def test_us_t18_unknown_strategy_is_404_with_a_clear_message(seeded_client: TestClient, path: str) -> None:
    response = seeded_client.get(path)
    assert response.status_code == 404
    assert "strategy" in response.json()["detail"].lower()


def test_us_t18_ac7_a_sleeve_that_is_not_deployed_is_absent_not_a_500(tmp_path: Path) -> None:
    """CARRY configured but never created here: TREND still serves (US-T19)."""
    db_path = tmp_path / "trend.db"
    make_db(db_path).close()
    paths = {
        "TREND": write_config(tmp_path, "TREND", db_path),
        "CARRY": write_config(tmp_path, "CARRY", tmp_path / "carry.db"),  # file never created
        "GHOST": tmp_path / "missing.yaml",
    }
    with build_client(paths) as client:
        assert client.get("/api/health").json()["strategies"] == ["TREND"]
        assert client.get("/api/trend/overview").status_code == 200
        carry = client.get("/api/carry/overview")
        assert carry.status_code == 404
        assert "not configured" in carry.json()["detail"]
        assert [s["strategy"] for s in client.get("/api/overview").json()["sleeves"]] == ["TREND"]


def test_backtest_page_404s_on_a_run_id_that_does_not_exist(seeded_client: TestClient) -> None:
    assert seeded_client.get("/api/trend/backtest?run_id=RUN-404").status_code == 404


def test_us_t01_ac2_a_run_id_from_another_sleeve_is_not_served(tmp_path: Path) -> None:
    """``run_id`` arrives in the query string, so the strategy filter still applies."""
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    seed(repos)
    carry = Repositories(repos.db, Strategy.CARRY)
    carry.backtest.save_run(
        "CARRY-RUN",
        created_ts=to_ms("2026-09-11T00:00:00Z"),
        start_day="2021-01-01",
        end_day="2026-08-31",
        variant="default",
        params={},
        manifest={},
        git_commit="deadbee",
        metrics={"sharpe": 4.2},
        equity=[],
        duration_s=1.0,
    )
    repos.close()

    with build_client({"TREND": write_config(tmp_path, "TREND", db_path)}) as client:
        assert client.get("/api/trend/backtest?run_id=CARRY-RUN").status_code == 404
        body = client.get("/api/trend/backtest").json()
        assert [r["run_id"] for r in body["runs"]] == ["RUN-1"]
        assert body["run"]["run_id"] == "RUN-1"


def test_a_held_symbol_with_no_target_still_appears_on_the_signals_page(tmp_path: Path) -> None:
    """A position left over from a symbol that has left the universe must stay visible."""
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    ts = to_ms("2026-09-12T12:00:00Z")
    repos.positions.replace_all(
        [Position(symbol="OLDUSDT", qty=1.0, entry_price=10.0, mark_price=12.0, ts_ms=ts)], ts
    )
    repos.close()
    with build_client({"TREND": write_config(tmp_path, "TREND", db_path)}) as client:
        row = client.get("/api/trend/signals").json()["rows"][0]
    assert row["symbol"] == "OLDUSDT"
    assert row["target_notional"] == 0.0
    assert row["current_notional"] == 12.0
    assert row["delta_notional"] == -12.0
    assert row["in_universe"] is False
    assert row["vol"] is None
