"""``python -m engine --strategy trend --mode paper|demo|live`` (US-T01 AC 1)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from aegis.core.clock import SystemClock
from aegis.core.config import AppConfig, load_config
from aegis.core.context import Context
from aegis.core.errors import AegisError, ConfigError
from aegis.core.types import Mode, Strategy
from aegis.ops.alerts import AlertBus
from aegis.storage.db import json_dumps, open_db
from aegis.storage.repositories import Repositories
from engine.startup import assert_safe_to_start

LOG = logging.getLogger("aegis")
DEFAULT_CONFIG = {"trend": "config/trend.yaml", "carry": "config/carry.yaml"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engine", description="Aegis trading engine")
    p.add_argument("--strategy", required=True, choices=["trend", "carry"])
    p.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=None,
        help="overrides the config; live additionally requires LIVE_CONFIRM",
    )
    p.add_argument("--config", default=None, help="path to the YAML config")
    p.add_argument("--db", default=None, help="override storage.db_path")
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--health",
        action="store_true",
        help="report engine health and exit (used by the container healthcheck)",
    )
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    p.add_argument("--migrate-only", action="store_true", help="apply migrations and exit")
    p.add_argument(
        "--verify-restore",
        metavar="DB",
        default=None,
        help="compare a restored database against the live one and exit",
    )
    # Backtest mode
    p.add_argument("--start", default=None, help="backtest start date (YYYY-MM-DD)")
    p.add_argument("--end", default=None, help="backtest end date (YYYY-MM-DD)")
    p.add_argument(
        "--fetch-archive",
        action="store_true",
        help="download the public archive into the cache (the only networked step)",
    )
    p.add_argument(
        "--no-walkforward", action="store_true", help="skip the walk-forward grid (it dominates the runtime)"
    )
    return p


def resolve_config(args: argparse.Namespace) -> AppConfig:
    path = args.config or DEFAULT_CONFIG[args.strategy]
    if not Path(path).exists():
        raise ConfigError(f"config not found: {path} (pass --config)")
    overrides: dict[str, Any] = {"strategy": Strategy(args.strategy.upper())}
    if args.mode:
        overrides["mode"] = Mode(args.mode)
    if args.db:
        overrides["storage"] = {"db_path": args.db}
    return load_config(path, overrides)


def build_context(cfg: AppConfig) -> Context:
    from aegis.gateway.factory import build_gateway

    clock = SystemClock()
    db = open_db(cfg.storage.db_path, wal=cfg.storage.wal, busy_timeout_ms=cfg.storage.busy_timeout_ms)
    repos = Repositories(db, cfg.strategy)
    alerts = AlertBus(repos.alerts, clock, cfg.strategy, repeat_minutes=cfg.telegram.repeat_critical_minutes)
    _attach_sinks(cfg, alerts)
    return Context(cfg=cfg, clock=clock, gateway=build_gateway(cfg, clock), repos=repos, alerts=alerts)


def _attach_sinks(cfg: AppConfig, alerts: AlertBus) -> None:
    try:
        from aegis.ops.telegram import TelegramSink
    except ImportError:  # pragma: no cover - optional at build time
        return
    if cfg.telegram.enabled:
        alerts.add_sink(TelegramSink(cfg))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_health(cfg: AppConfig) -> int:
    """Liveness for the container healthcheck: the DB opens and the engine has ticked."""
    db = open_db(cfg.storage.db_path, wal=cfg.storage.wal, migrate=False)
    try:
        repos = Repositories(db, cfg.strategy)
        row = repos.state.load()
        if row is None:
            print(json_dumps({"ok": False, "reason": "engine has never started"}))
            return 1
        healthy = not row["stopped"]
        print(
            json_dumps(
                {
                    "ok": healthy,
                    "state": row["state"],
                    "phase": row["phase"],
                    "safe_mode": bool(row["safe_mode"]),
                    "blocks": row["blocks"],
                    "updated_ts": row["updated_ts"],
                }
            )
        )
        return 0 if healthy else 1
    finally:
        db.close()


def cmd_verify_restore(cfg: AppConfig, restored: str) -> int:
    """CARRY US-18 AC 2 / US-T19 AC 4 — a restore drill that actually compares."""
    live = open_db(cfg.storage.db_path, migrate=False)
    copy = open_db(restored, migrate=False)
    try:
        problems: list[str] = []
        live_tables, copy_tables = set(live.tables()), set(copy.tables())
        if live_tables - copy_tables:
            problems.append(f"missing tables: {sorted(live_tables - copy_tables)}")
        for table in sorted(live_tables & copy_tables):
            a = live.scalar(f"SELECT COUNT(*) FROM {table}") or 0
            b = copy.scalar(f"SELECT COUNT(*) FROM {table}") or 0
            if b < a:
                problems.append(f"{table}: {b} rows restored vs {a} live")
        print(
            json_dumps({"ok": not problems, "problems": problems, "tables": len(live_tables & copy_tables)})
        )
        return 0 if not problems else 1
    finally:
        live.close()
        copy.close()


def cmd_backtest(cfg: AppConfig, args: argparse.Namespace) -> int:
    from aegis.backtest_trend.archive import ArchiveLoader
    from aegis.backtest_trend.runner import BacktestRunner
    from aegis.core.clock import month_key

    start = date.fromisoformat(args.start or cfg.backtest.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    loader = ArchiveLoader(cfg.backtest.archive_cache_dir)

    inventory_start = month_key(date.fromisoformat(cfg.backtest.inventory_start))
    LOG.info("building the symbol inventory from the public archive")
    inventory = loader.inventory(inventory_start, month_key(end))
    if not inventory:
        LOG.error(
            "the archive returned no symbols. Populate the cache with --fetch-archive "
            "on a host that can reach data.binance.vision, then re-run offline."
        )
        return 2

    bars, funding = {}, {}
    for symbol, months in sorted(inventory.items()):
        bars[symbol] = loader.bars_range(symbol, months)
        rates = []
        for month in months:
            rates.extend(loader.funding(symbol, month))
        funding[symbol] = rates
    LOG.info("loaded %d symbols", len(bars))

    db = open_db(cfg.storage.db_path)
    repos = Repositories(db, cfg.strategy)
    try:
        runner = BacktestRunner(cfg, bars, funding, repo=repos.backtest)
        outcome = runner.run(start, end, with_walkforward=not args.no_walkforward)
    finally:
        db.close()

    print(
        json_dumps(
            {
                "run_id": outcome.result.run_id,
                "metrics": outcome.result.metrics,
                "p0_evidence": outcome.p0_evidence,
                "duration_s": outcome.result.duration_s,
            }
        )
    )
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    from aegis.ops.heartbeat import Heartbeat
    from aegis.ops.reports import Reporter
    from aegis.strategy_trend.runner import TrendRunner

    if cfg.strategy is not Strategy.TREND:
        # The shared layer, the schema and the dashboard are strategy-agnostic and
        # already carry CARRY's rows, but CARRY's own strategy modules belong to a
        # different PRD and are not in this build. Running the TREND engine against
        # a carry sub-account would trade the wrong strategy with real money, so
        # this refuses rather than improvises.
        raise ConfigError(
            f"the {cfg.strategy} sleeve's strategy modules are not part of this build; "
            "only --strategy trend can run. The shared layer and the dashboard do support "
            f"{cfg.strategy} once its engine exists."
        )

    ctx = build_context(cfg)
    try:
        for check in assert_safe_to_start(cfg, ctx.gateway, ctx.clock.now_ms()):
            LOG.info("startup %-16s %s", check.name, check.detail)
        runner = TrendRunner(ctx, heartbeat=Heartbeat(ctx), reporter=Reporter(ctx))
        runner.start()
        LOG.info("engine started: %s", json_dumps(runner.status(ctx.clock.now_ms())))
        if args.once:
            report = runner.tick(ctx.clock.now_ms())
            print(
                json_dumps(
                    {
                        "state": report.state,
                        "jobs": report.jobs,
                        "actions": report.actions,
                        "blocks": report.blocks,
                    }
                )
            )
            return 0
        runner.run()
        return 0
    finally:
        ctx.gateway.close()
        ctx.repos.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    try:
        cfg = resolve_config(args)
        if args.health:
            return cmd_health(cfg)
        if args.verify_restore:
            return cmd_verify_restore(cfg, args.verify_restore)
        if args.migrate_only:
            open_db(cfg.storage.db_path).close()
            LOG.info("migrations applied to %s", cfg.storage.db_path)
            return 0
        if args.fetch_archive:
            args.no_walkforward = True
        if cfg.mode is Mode.BACKTEST:
            return cmd_backtest(cfg, args)
        return cmd_run(cfg, args)
    except AegisError as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        LOG.info("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
