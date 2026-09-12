"""The write rules: reads never write, and the one write is one control row.

The engine is the only writer of its database. These tests hold the API to
that: hashing every table before and after a full page sweep, checking that the
sleeve's own connection refuses a write at the driver, and checking that a
control request appends exactly one ``control_log`` row and leaves
``engine_state`` — the engine's own state machine — untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.deps import ReadOnlyDatabase
from aegis.ops.controls import CONFIRM_TOKEN
from aegis.storage.db import Database
from tests.api.conftest import GET_ROUTES, build_client, make_db, seed, write_config


def dump(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Every row of every table — the fingerprint a read must not change."""
    db = Database(db_path, wal=False)
    try:
        return {t: db.query(f"SELECT * FROM {t}") for t in db.tables()}
    finally:
        db.close()


@pytest.fixture
def seeded_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "trend.db"
    repos = make_db(db_path)
    seed(repos)
    repos.close()
    return db_path


def test_us_t18_no_get_route_changes_a_single_engine_row(tmp_path: Path, seeded_path: Path) -> None:
    before = dump(seeded_path)
    with build_client({"TREND": write_config(tmp_path, "TREND", seeded_path)}) as client:
        for path in GET_ROUTES:
            assert client.get(path).status_code == 200, path
        assert client.get("/api/trend/rebalances/TREND-2026-09-12").status_code == 200
    assert dump(seeded_path) == before


def test_the_sleeve_connection_is_opened_read_only(seeded_path: Path) -> None:
    db = ReadOnlyDatabase(seeded_path)
    try:
        assert db.query("SELECT * FROM engine_state")  # reading is the point
        with pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only|query_only"):
            db.execute("UPDATE engine_state SET paused = 1 WHERE strategy = 'TREND'")
    finally:
        db.close()


def test_us_t18_controls_appends_exactly_one_row_and_leaves_engine_state_alone(
    tmp_path: Path, seeded_path: Path
) -> None:
    before = dump(seeded_path)
    with build_client({"TREND": write_config(tmp_path, "TREND", seeded_path)}) as client:
        response = client.post(
            "/api/trend/controls",
            json={"action": "pause", "operator": "alice", "reason": "manual check"},
        )
        assert response.status_code == 202
        assert response.json()["accepted"] is True
    after = dump(seeded_path)

    assert after["engine_state"] == before["engine_state"]  # the engine owns its state
    new_rows = [r for r in after["control_log"] if r not in before["control_log"]]
    assert len(new_rows) == 1
    assert new_rows[0]["action"] == "pause"
    assert new_rows[0]["operator"] == "alice"
    assert new_rows[0]["reason"] == "manual check"
    # sqlite_sequence moves with control_log's own autoincrement, nothing else may.
    for table, rows in after.items():
        if table not in ("control_log", "sqlite_sequence"):
            assert rows == before[table], table


@pytest.mark.parametrize("action", ["stop", "flatten_all"])
def test_us_t18_destructive_controls_need_the_typed_confirmation(
    tmp_path: Path, seeded_path: Path, action: str
) -> None:
    before = dump(seeded_path)
    with build_client({"TREND": write_config(tmp_path, "TREND", seeded_path)}) as client:
        bad = client.post(
            "/api/trend/controls",
            json={"action": action, "operator": "alice", "reason": "drill", "confirm": "yes"},
        )
        assert bad.status_code == 400
        good = client.post(
            "/api/trend/controls",
            json={"action": action, "operator": "alice", "reason": "drill", "confirm": CONFIRM_TOKEN},
        )
        assert good.status_code == 202
    after = dump(seeded_path)
    assert len(after["control_log"]) == len(before["control_log"]) + 1  # the refused one wrote nothing


def test_us_t13_ac3_clear_halt_requires_a_written_reason(tmp_path: Path, seeded_path: Path) -> None:
    with build_client({"TREND": write_config(tmp_path, "TREND", seeded_path)}) as client:
        response = client.post(
            "/api/trend/controls", json={"action": "clear_halt", "operator": "alice", "reason": "  "}
        )
        assert response.status_code == 400
        assert "reason" in response.json()["detail"]


def test_controls_rejects_an_unknown_action_and_an_anonymous_operator(
    tmp_path: Path, seeded_path: Path
) -> None:
    with build_client({"TREND": write_config(tmp_path, "TREND", seeded_path)}) as client:
        assert (
            client.post(
                "/api/trend/controls", json={"action": "launch_rocket", "operator": "alice"}
            ).status_code
            == 422
        )
        assert client.post("/api/trend/controls", json={"action": "pause", "operator": ""}).status_code == 422


def test_controls_get_shows_the_state_and_the_audit_log(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/trend/controls").json()
    assert body["state"]["state"] == "IDLE"
    assert "flatten_all" in body["confirm_required"]
    assert body["log"][0]["action"] == "start"
