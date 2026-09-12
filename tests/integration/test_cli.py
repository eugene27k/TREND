"""The CLI surface (US-T01 AC 1) and its refusals."""

from __future__ import annotations

import json

import pytest

from aegis.core.errors import ConfigError
from aegis.core.types import Mode, Strategy
from aegis.storage.db import open_db
from engine.cli import build_parser, cmd_health, cmd_verify_restore, main, resolve_config


def args(*argv):
    return build_parser().parse_args(list(argv))


def test_us_t01_ac1_the_documented_invocation_parses():
    parsed = args("--strategy", "trend", "--mode", "paper")
    cfg = resolve_config(parsed)
    assert cfg.strategy is Strategy.TREND
    assert cfg.mode is Mode.PAPER


@pytest.mark.parametrize("mode", ["backtest", "paper", "demo", "live"])
def test_every_mode_is_accepted_by_the_parser(mode):
    assert args("--strategy", "trend", "--mode", mode).mode == mode


def test_a_missing_config_is_a_clear_refusal():
    with pytest.raises(ConfigError, match="config not found"):
        resolve_config(args("--strategy", "trend", "--config", "nope.yaml"))


def test_the_db_override_reaches_the_config(tmp_path):
    cfg = resolve_config(args("--strategy", "trend", "--db", str(tmp_path / "x.db")))
    assert cfg.storage.db_path == str(tmp_path / "x.db")


def test_carry_is_refused_rather_than_run_with_the_trend_engine(tmp_path, capsys):
    """Running the TREND engine against a carry sub-account would trade the wrong strategy."""
    code = main(
        [
            "--strategy",
            "carry",
            "--mode",
            "paper",
            "--config",
            "config/trend.yaml",
            "--db",
            str(tmp_path / "carry.db"),
        ]
    )
    assert code == 1


def test_migrate_only_creates_the_schema_and_exits(tmp_path):
    db_path = tmp_path / "fresh.db"
    assert main(["--strategy", "trend", "--migrate-only", "--db", str(db_path)]) == 0
    db = open_db(db_path, migrate=False)
    assert "targets" in db.tables()
    db.close()


def test_health_reports_not_started_on_a_fresh_database(tmp_path, capsys):
    db_path = tmp_path / "fresh.db"
    open_db(db_path).close()
    cfg = resolve_config(args("--strategy", "trend", "--db", str(db_path)))
    assert cmd_health(cfg) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "never started" in payload["reason"]


def test_health_is_ok_once_the_engine_has_state(tmp_path, capsys):
    from aegis.storage.repositories import Repositories

    db_path = tmp_path / "live.db"
    db = open_db(db_path)
    Repositories(db, Strategy.TREND).state.save(
        state="IDLE",
        phase="P1_PAPER",
        paused=False,
        stopped=False,
        safe_mode=False,
        halt_reason="",
        governor_g=1.0,
        blocks=[],
        context={},
        now_ms=1_000,
    )
    db.close()
    cfg = resolve_config(args("--strategy", "trend", "--db", str(db_path)))
    assert cmd_health(cfg) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["state"] == "IDLE"


def test_health_fails_when_the_engine_is_stopped(tmp_path, capsys):
    from aegis.storage.repositories import Repositories

    db_path = tmp_path / "stopped.db"
    db = open_db(db_path)
    Repositories(db, Strategy.TREND).state.save(
        state="STOPPED",
        phase="P1_PAPER",
        paused=False,
        stopped=True,
        safe_mode=False,
        halt_reason="",
        governor_g=1.0,
        blocks=[],
        context={},
        now_ms=1_000,
    )
    db.close()
    cfg = resolve_config(args("--strategy", "trend", "--db", str(db_path)))
    assert cmd_health(cfg) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_us_t19_ac4_verify_restore_compares_row_counts(tmp_path, capsys):
    """CARRY US-18 AC 2 extended: a restore that is missing rows must fail loudly."""
    from aegis.core.types import Severity
    from aegis.storage.repositories import Repositories

    live_path, copy_path = tmp_path / "live.db", tmp_path / "copy.db"
    live = open_db(live_path)
    repos = Repositories(live, Strategy.TREND)
    for i in range(5):
        repos.alerts.add(
            __import__("aegis.core.types", fromlist=["Alert"]).Alert(
                Strategy.TREND, 1000 + i, Severity.INFO, "X", "m"
            )
        )
    live.close()
    open_db(copy_path).close()  # same schema, no rows

    cfg = resolve_config(args("--strategy", "trend", "--db", str(live_path)))
    assert cmd_verify_restore(cfg, str(copy_path)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any("alerts" in p for p in payload["problems"])


def test_verify_restore_passes_on_a_faithful_copy(tmp_path, capsys):
    import shutil

    live_path = tmp_path / "live.db"
    open_db(live_path).close()
    copy_path = tmp_path / "copy.db"
    shutil.copy(live_path, copy_path)
    cfg = resolve_config(args("--strategy", "trend", "--db", str(live_path)))
    assert cmd_verify_restore(cfg, str(copy_path)) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
