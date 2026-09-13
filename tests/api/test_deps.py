"""Unit cover for the wiring: sleeve resolution, windows and the risk tile."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from aegis.api.deps import (
    RESOLVE_RETRY_MS,
    ReadOnlyDatabase,
    Registry,
    cap_breaches,
    parse_strategy,
    risk_status,
    window_for,
)
from aegis.core.clock import FakeClock, to_ms
from aegis.core.config import load_config
from aegis.core.errors import AegisError
from aegis.core.types import EngineState, Position, RiskStatus, Strategy
from tests.api.conftest import NOW, build_client, make_db, write_config

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
    """A truncated or half-restored file is an absent sleeve, not a page of 500s."""
    db_path = tmp_path / "trend.db"
    db_path.write_bytes(b"this is not sqlite")
    config = write_config(tmp_path, "TREND", db_path)

    registry = Registry.build({"TREND": config}, FakeClock(NOW))
    try:
        assert registry.strategies == []
    finally:
        registry.close()

    with build_client({"TREND": config}) as client:
        assert client.get("/api/health").json()["strategies"] == []
        response = client.get("/api/trend/overview")
        assert response.status_code == 404
        assert "not configured" in response.json()["detail"]


def test_read_only_database_refuses_to_migrate(tmp_path: Path) -> None:
    db_path = tmp_path / "trend.db"
    make_db(db_path).close()
    db = ReadOnlyDatabase(db_path)
    try:
        with pytest.raises(AegisError):
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


def _pos(symbol: str, notional: float) -> Position:
    """A position worth ``notional`` at a mark of 100 (sign carried by the qty)."""
    return Position(symbol=symbol, qty=notional / 100.0, entry_price=100.0, mark_price=100.0, ts_ms=0)


def test_us_t12_ac1_cap_breaches_are_the_supervisors_own_comparison() -> None:
    cfg = load_config(None, use_env=False)  # caps 2.5 / 1.5 / 0.25 of equity
    equity = 10_000.0

    inside = {"BTCUSDT": _pos("BTCUSDT", 2_400.0), "ETHUSDT": _pos("ETHUSDT", -2_400.0)}
    assert cap_breaches(cfg, inside, equity) == []

    single = {"BTCUSDT": _pos("BTCUSDT", 2_600.0)}  # > 0.25 x 10 000
    assert cap_breaches(cfg, single, equity) == ["single"]

    net = {f"S{i}USDT": _pos(f"S{i}USDT", 2_000.0) for i in range(8)}  # net 16 000 > 1.5 x E
    assert cap_breaches(cfg, net, equity) == ["net"]

    gross = {f"L{i}USDT": _pos(f"L{i}USDT", 2_000.0) for i in range(7)}
    gross |= {f"S{i}USDT": _pos(f"S{i}USDT", -2_000.0) for i in range(7)}  # gross 28 000, net 0
    assert cap_breaches(cfg, gross, equity) == ["gross"]

    assert cap_breaches(cfg, single, 0.0) == []  # no equity recorded yet: nothing to divide by


def test_us_t12_ac1_a_cap_breach_is_amber_even_on_a_comfortable_margin() -> None:
    cfg = load_config(None, use_env=False)
    assert risk_status(cfg, 0.01, CLEAN, ["single"]) is RiskStatus.AMBER
    assert risk_status(cfg, 0.01, CLEAN, []) is RiskStatus.GREEN
    # Red still outranks a breach — margin distance comes first.
    assert risk_status(cfg, 0.40, CLEAN, ["gross"]) is RiskStatus.RED


def test_a_sleeve_whose_database_appears_after_start_up_is_picked_up(tmp_path: Path) -> None:
    """US-T19 AC 1 starts the dashboard beside the engines; the engine creates its
    file on its first migration, so the selector must fill in without a restart."""
    db_path = tmp_path / "trend.db"
    config = write_config(tmp_path, "TREND", db_path)
    clock = FakeClock(NOW)
    registry = Registry.build({"TREND": config}, clock)
    try:
        assert registry.strategies == []  # the engine has not run yet

        make_db(db_path).close()  # the engine migrates its database
        assert registry.strategies == []  # not before the retry interval

        clock.advance(seconds=RESOLVE_RETRY_MS / 1000)
        assert registry.strategies == [Strategy.TREND]
        assert registry.get("trend").db_path == db_path
    finally:
        registry.close()
