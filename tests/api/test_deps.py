"""Unit cover for the wiring: sleeve resolution, windows and the risk tile."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from aegis.api.deps import ReadOnlyDatabase, Registry, parse_strategy, risk_status, window_for
from aegis.core.clock import FakeClock, to_ms
from aegis.core.config import load_config
from aegis.core.types import EngineState, RiskStatus, Strategy
from tests.api.conftest import NOW, make_db, write_config

CLEAN = {"state": str(EngineState.IDLE), "blocks": [], "safe_mode": False}


def test_parse_strategy_is_case_insensitive_and_rejects_the_unknown() -> None:
    assert parse_strategy("trend") is Strategy.TREND
    assert parse_strategy("CARRY") is Strategy.CARRY
    assert parse_strategy("basket") is None


def test_registry_skips_a_config_that_does_not_parse(tmp_path: Path) -> None:
    broken = tmp_path / "trend.yaml"
    broken.write_text("- not: a mapping\n", encoding="utf-8")
    registry = Registry.build({"TREND": broken}, FakeClock(NOW))
    assert registry.strategies == []


def test_registry_skips_a_database_file_that_is_not_a_database(tmp_path: Path) -> None:
    db_path = tmp_path / "trend.db"
    db_path.write_bytes(b"this is not sqlite")
    registry = Registry.build({"TREND": write_config(tmp_path, "TREND", db_path)}, FakeClock(NOW))
    # SQLite only notices on first read, so the sleeve resolves but its pages 404
    # rather than the process failing at import time.
    assert registry.strategies in ([], [Strategy.TREND])
    registry.close()


def test_read_only_database_refuses_to_migrate(tmp_path: Path) -> None:
    db_path = tmp_path / "trend.db"
    make_db(db_path).close()
    db = ReadOnlyDatabase(db_path)
    try:
        with pytest.raises(RuntimeError):
            db.migrate()
    finally:
        db.close()


def test_read_only_database_falls_back_when_the_ro_uri_cannot_open(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "trend.db"
    make_db(db_path).close()
    real_connect = sqlite3.connect

    def refuse_uri(*args, **kwargs):
        if kwargs.pop("uri", False):
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", refuse_uri)
    db = ReadOnlyDatabase(db_path)
    try:
        assert db.tables()
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO control_log (strategy, ts, action) VALUES ('TREND', 1, 'x')")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("7d", date(2026, 9, 6), date(2026, 9, 12)),
        ("30d", date(2026, 8, 14), date(2026, 9, 12)),
        ("mtd", date(2026, 9, 1), date(2026, 9, 12)),
        ("ytd", date(2026, 1, 1), date(2026, 9, 12)),
    ],
)
def test_window_for_matches_the_metric_engine_periods(period: str, start: date, end: date) -> None:
    w = window_for(period, to_ms(NOW), date(2025, 1, 1))
    assert (w.start_day, w.end_day) == (start, end)
    assert w.days == (end - start).days + 1


def test_window_for_since_inception_starts_at_the_first_recorded_day() -> None:
    assert window_for("since_inception", to_ms(NOW), date(2025, 3, 4)).start_day == date(2025, 3, 4)
    # No equity yet: the window collapses to today rather than to the epoch.
    assert window_for("since_inception", to_ms(NOW), None).start_day == date(2026, 9, 12)


def test_risk_status_reflects_margin_then_holds_then_green() -> None:
    cfg = load_config(None, use_env=False)
    assert risk_status(cfg, 0.40, CLEAN) is RiskStatus.RED
    assert risk_status(cfg, 0.25, CLEAN) is RiskStatus.AMBER
    assert risk_status(cfg, 0.01, {**CLEAN, "blocks": ["daily_loss"]}) is RiskStatus.AMBER
    assert risk_status(cfg, 0.01, {**CLEAN, "safe_mode": True}) is RiskStatus.AMBER
    assert risk_status(cfg, 0.01, {**CLEAN, "state": str(EngineState.HALTED_RISK)}) is RiskStatus.RED
    assert risk_status(cfg, 0.01, CLEAN) is RiskStatus.GREEN
