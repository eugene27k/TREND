"""Storage primitives: migrations, transactions, deterministic JSON."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from aegis.storage.db import Database, json_dumps, json_loads, open_db


def test_migrate_is_idempotent(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    assert db.migrate() == [], "a second migrate must be a no-op"
    applied = {r["name"] for r in db.query("SELECT name FROM schema_migrations")}
    assert applied == {"001_shared.sql", "002_trend.sql"}


def test_migrate_creates_every_documented_table(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    tables = set(db.tables())
    for expected in (
        "universe_history", "daily_bars", "signal_snapshots", "risk_model_snapshots", "targets",
        "rebalances", "slices", "governor_state", "symbol_pnl_daily", "trades", "illiquid_flags",
        "robustness_reports", "walkforward", "ledger", "snapshots", "orders", "fills", "metrics",
        "approvals", "alerts", "reports", "engine_state", "heartbeats", "reconciliations",
    ):
        assert expected in tables, expected


def test_wal_mode_enabled_on_a_real_file(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    assert db.scalar("PRAGMA journal_mode") == "wal"


def test_transaction_rolls_back_on_error(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("INSERT INTO heartbeats (strategy, ts, ok) VALUES ('TREND', 1, 1)")
        raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM heartbeats") == 0


def test_transaction_commits_and_nests(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    with db.transaction():
        db.execute("INSERT INTO heartbeats (strategy, ts, ok) VALUES ('TREND', 1, 1)")
        with db.transaction():  # joins the outer transaction rather than failing
            db.execute("INSERT INTO heartbeats (strategy, ts, ok) VALUES ('TREND', 2, 1)")
    assert db.scalar("SELECT COUNT(*) FROM heartbeats") == 2


def test_executemany_with_no_rows_is_a_noop(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    db.executemany("INSERT INTO heartbeats (strategy, ts, ok) VALUES (?,?,?)", [])
    assert db.scalar("SELECT COUNT(*) FROM heartbeats") == 0


def test_query_one_and_scalar_return_none_when_empty(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    assert db.query_one("SELECT * FROM heartbeats") is None
    assert db.scalar("SELECT ts FROM heartbeats") is None


def test_json_dumps_is_deterministic_for_manifests() -> None:
    a = json_dumps({"b": 1, "a": [3, 2], "c": date(2026, 9, 8)})
    b = json_dumps({"c": date(2026, 9, 8), "a": [3, 2], "b": 1})
    assert a == b, "manifest hashing depends on key order being stable"
    assert a == '{"a":[3,2],"b":1,"c":"2026-09-08"}'


def test_json_loads_tolerates_garbage() -> None:
    assert json_loads(None, {}) == {}
    assert json_loads("", []) == []
    assert json_loads("{not json", {"fallback": True}) == {"fallback": True}


def test_foreign_keys_pragma_on(tmp_path) -> None:
    db = open_db(tmp_path / "t.db")
    assert db.scalar("PRAGMA foreign_keys") == 1


def test_database_context_manager_closes(tmp_path) -> None:
    with Database(tmp_path / "t.db") as db:
        db.migrate()
    with pytest.raises(sqlite3.ProgrammingError):
        db.query("SELECT 1")
