"""Backup verification — CARRY US-18 AC 2, extended to both databases (US-T19 AC 4).

No test here requires ``litestream``: the restore step is injected, and the
"not installed" path is asserted explicitly, because that is the state the free
host is actually in.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aegis.core.clock import FakeClock, to_ms
from aegis.core.config import load_config
from aegis.core.context import Context
from aegis.core.types import Strategy
from aegis.ops import backup as backup_module
from aegis.ops.alerts import AlertBus
from aegis.ops.backup import Backup, litestream_restore
from aegis.storage.db import open_db
from aegis.storage.repositories import Repositories

NOW = to_ms("2026-09-08T00:05:00Z")


@pytest.fixture
def bctx(tmp_path: Path, gateway) -> Context:
    clock = FakeClock(NOW)
    cfg = load_config(
        "config/trend.yaml",
        {"backup": {"enabled": True, "litestream_replica_path": str(tmp_path / "replica")},
         "storage": {"db_path": str(tmp_path / "trend.db")}},
        use_env=False,
    )
    db = open_db(tmp_path / "trend.db")
    repos = Repositories(db, Strategy.TREND)
    repos.state.log_control("start", "alice", "seed a row", {}, NOW)
    yield Context(cfg=cfg, clock=clock, gateway=gateway, repos=repos,
                  alerts=AlertBus(repos.alerts, clock, Strategy.TREND))
    db.close()


def _copy_restorer(source: Path):
    """Stands in for litestream: a consistent copy of ``source`` (WAL included)."""

    def restore(replica: Path, destination: Path) -> bool:
        live = sqlite3.connect(source)
        copy = sqlite3.connect(destination)
        try:
            live.backup(copy)
        finally:
            live.close()
            copy.close()
        return True

    return restore


# --------------------------------------------------------------------------- #
# Replica
# --------------------------------------------------------------------------- #


def test_a_missing_replica_path_is_reported_not_raised(bctx: Context) -> None:
    status = Backup(bctx).verify_replica()

    assert status.exists is False
    assert status.ok is False
    assert status.lag_s is None
    assert "does not exist" in status.detail


def test_an_empty_replica_path_has_no_measurable_lag(bctx: Context, tmp_path: Path) -> None:
    (tmp_path / "replica").mkdir()

    status = Backup(bctx).verify_replica()

    assert status.exists is True
    assert status.lag_s is None
    assert status.ok is False


def test_replica_lag_is_measured_from_the_newest_file_against_the_engine_clock(
    bctx: Context, tmp_path: Path
) -> None:
    replica = tmp_path / "replica"
    replica.mkdir()
    snapshot = replica / "generations" / "abc" / "snapshots"
    snapshot.mkdir(parents=True)
    written = snapshot / "0000.snapshot.lz4"
    written.write_bytes(b"x")
    import os

    os.utime(written, ((NOW - 8_000) / 1000, (NOW - 8_000) / 1000))

    status = Backup(bctx).verify_replica()

    assert status.exists is True
    assert status.lag_s == pytest.approx(8.0, abs=0.01)
    assert status.ok is True


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #


def test_us18_ac2_a_restore_matching_the_live_database_passes(bctx: Context, tmp_path: Path) -> None:
    source = Path(bctx.cfg.storage.db_path)

    result = Backup(bctx, _copy_restorer(source)).restore_check(source)

    assert result.ok is True
    assert "control_log" in result.tables
    assert result.row_counts["control_log"] == 1
    assert result.mismatches == ()


def test_us18_ac2_a_restore_with_missing_rows_fails_and_names_the_table(
    bctx: Context, tmp_path: Path
) -> None:
    source = Path(bctx.cfg.storage.db_path)
    stale = tmp_path / "stale.db"
    open_db(stale).close()  # same schema, no rows

    result = Backup(bctx, _copy_restorer(stale)).restore_check(source)

    assert result.ok is False
    assert any("control_log" in m for m in result.mismatches)


def test_us18_ac2_a_restore_with_a_different_schema_fails(bctx: Context, tmp_path: Path) -> None:
    source = Path(bctx.cfg.storage.db_path)
    wrong = tmp_path / "wrong.db"
    db = open_db(wrong, migrate=False)
    db.execute("CREATE TABLE unrelated (x INTEGER)")
    db.close()

    result = Backup(bctx, _copy_restorer(wrong)).restore_check(source)

    assert result.ok is False
    assert any(m.startswith("schema:") for m in result.mismatches)


def test_a_failing_restore_is_reported_not_raised(bctx: Context) -> None:
    result = Backup(bctx, lambda replica, dest: False).restore_check(bctx.cfg.storage.db_path)

    assert result.ok is False
    assert result.detail == "restore failed"


def test_a_restorer_that_produces_no_file_is_reported(bctx: Context) -> None:
    result = Backup(bctx, lambda replica, dest: True).restore_check(bctx.cfg.storage.db_path)

    assert result.ok is False
    assert result.detail == "restore produced no file"


def test_a_missing_source_database_is_reported(bctx: Context, tmp_path: Path) -> None:
    result = Backup(bctx, _copy_restorer(tmp_path / "trend.db")).restore_check(tmp_path / "gone.db")

    assert result.ok is False
    assert "does not exist" in result.detail


def test_us_t19_ac4_a_host_without_litestream_reports_unavailable_rather_than_failing(
    bctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_module, "litestream_available", lambda: False)

    result = Backup(bctx, litestream_restore).restore_check(bctx.cfg.storage.db_path)

    assert result.available is False
    assert result.ok is False
    assert result.detail == "litestream is not installed"


def test_litestream_restore_returns_false_when_the_binary_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(backup_module, "litestream_available", lambda: False)

    assert litestream_restore(tmp_path / "replica", tmp_path / "out.db") is False


def test_us_t19_ac4_verify_all_covers_every_database(bctx: Context, tmp_path: Path) -> None:
    source = Path(bctx.cfg.storage.db_path)
    second = tmp_path / "carry.db"
    open_db(second).close()

    results = Backup(bctx, _copy_restorer(source)).verify_all([source, second])

    assert set(results) == {str(source), str(second)}
    assert results[str(source)].ok is True
    assert results[str(second)].ok is False  # the carry rows are not in the trend replica
