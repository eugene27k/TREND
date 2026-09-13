"""Wiring for the dashboard API: one read-only database per sleeve.

Three decisions are made here and nowhere else.

**The API never writes to the engine's tables.** Section 6 gives every strategy
its own SQLite file and one writing process; a second writer would be a
correctness bug, not a performance one. So each sleeve's connection is opened
through SQLite's own ``mode=ro`` URI: a stray ``INSERT`` anywhere in a route
fails at the driver, not at code review. The single documented exception —
``POST /api/{strategy}/controls`` — takes a separate, short-lived write
connection that appends to ``control_log`` and closes again. It never touches
``engine_state``: the engine reads the log on its next tick and decides for
itself, which is what keeps Invariant 1 the engine's property rather than the
dashboard's.

**A missing sleeve is absent, not fatal.** CARRY and TREND are deployed as two
containers (US-T19) and either may be absent on a given host — or present in
config but not yet created on disk. ``Registry.build`` skips what it cannot
open, so ``/api/strategies`` shrinks instead of the process failing.

**Pages read stored numbers.** US-T18 AC 8 gives every page a one-second budget
on a free VM, which is affordable only if nothing here recomputes what the
metric engine already wrote. The helpers below therefore resolve metrics by
``name:period`` out of the ``metrics`` table; the only arithmetic in this
package is shares, ratios and chart overlays over rows already selected.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from aegis.core.clock import Clock, SystemClock, day_of
from aegis.core.config import AppConfig, load_config
from aegis.core.errors import AegisError
from aegis.core.types import EngineState, Phase, Position, RiskStatus, Strategy
from aegis.storage.db import Database, json_loads
from aegis.storage.repositories import Repositories, StateRepo

#: Where each sleeve's config lives when the caller does not say.
DEFAULT_CONFIG_PATHS: dict[str, str] = {
    "CARRY": "config/carry.yaml",
    "TREND": "config/trend.yaml",
}

#: Lexicographic bounds for the ``YYYY-MM-DD`` day columns — "everything we have"
#: as a range query, so the repositories need no separate all-time variant.
DAY_MIN = "0000-01-01"
DAY_MAX = "9999-12-31"

_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}

#: How often a sleeve that is not deployed *yet* is looked for again.
RESOLVE_RETRY_MS = 5_000


# --------------------------------------------------------------------------- #
# Read-only database
# --------------------------------------------------------------------------- #


def _rows_as_dicts(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class ReadOnlyDatabase(Database):
    """A ``Database`` whose connection SQLite itself refuses to write through.

    Subclassing rather than adding a flag to ``Database`` keeps the read-only
    guarantee inside the API, where it is a hard rule, instead of making it an
    option the engine could pass by accident.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.read_only = True
        self._lock = threading.RLock()
        self._conn = self._connect(self.path, busy_timeout_ms)
        self._conn.row_factory = _rows_as_dicts
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")

    @staticmethod
    def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
        uri = f"file:{path}?mode=ro"
        try:
            return sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=busy_timeout_ms / 1000,
            )
        except sqlite3.OperationalError:
            # A WAL database whose -shm file is absent cannot always be opened
            # through mode=ro. query_only gives the same refusal from the same
            # engine, so the guarantee survives the fallback.
            conn = sqlite3.connect(
                str(path),
                check_same_thread=False,
                isolation_level=None,
                timeout=busy_timeout_ms / 1000,
            )
            conn.execute("PRAGMA query_only = ON")
            return conn

    def migrate(self, directory: Path | None = None) -> list[str]:  # noqa: ARG002 - signature match
        raise AegisError("the API never migrates a database")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StrategyDeps:
    """One sleeve: its config, its file and read-only repositories over it."""

    strategy: Strategy
    cfg: AppConfig
    db_path: Path
    db: ReadOnlyDatabase
    repos: Repositories

    def close(self) -> None:
        self.db.close()


class Registry:
    """Every sleeve the API can answer for.

    Resolved at start-up and re-probed while something is missing: the dashboard
    and the engines come up together (US-T19 AC 1) and an engine creates its
    SQLite file on its first migration, so a registry that only looked once
    would serve an empty strategy selector until someone restarted the
    container. A missing sleeve therefore costs one stat every
    ``RESOLVE_RETRY_MS`` and nothing else.
    """

    def __init__(
        self,
        sleeves: Mapping[Strategy, StrategyDeps],
        clock: Clock,
        started_ms: int,
        pending: Mapping[str, str | Path] | None = None,
    ) -> None:
        self._sleeves = dict(sleeves)
        self._pending = dict(pending or {})
        self._lock = threading.Lock()
        self._last_probe_ms = started_ms
        self.clock = clock
        self.started_ms = started_ms

    @classmethod
    def build(
        cls, config_paths: Mapping[str, str | Path] | None = None, clock: Clock | None = None
    ) -> Registry:
        clock = clock or SystemClock()
        paths = dict(config_paths) if config_paths is not None else dict(DEFAULT_CONFIG_PATHS)
        sleeves: dict[Strategy, StrategyDeps] = {}
        pending: dict[str, str | Path] = {}
        for name, config_path in paths.items():
            sleeve = _try_open(name, config_path)
            if sleeve is not None:
                sleeves[sleeve.strategy] = sleeve
            else:
                pending[name] = config_path
        return cls(sleeves, clock, clock.now_ms(), pending)

    @property
    def strategies(self) -> list[Strategy]:
        self._resolve_pending()
        return sorted(self._sleeves, key=str)

    def _resolve_pending(self) -> None:
        """Try again to open the sleeves that were not there at start-up."""
        if not self._pending:
            return
        now = self.clock.now_ms()
        with self._lock:
            if now - self._last_probe_ms < RESOLVE_RETRY_MS:
                return
            self._last_probe_ms = now
            for name, config_path in list(self._pending.items()):
                sleeve = _try_open(name, config_path)
                if sleeve is not None:
                    self._sleeves[sleeve.strategy] = sleeve
                    del self._pending[name]

    def get(self, name: str) -> StrategyDeps:
        parsed = parse_strategy(name)
        if parsed is None:
            raise HTTPException(404, f"unknown strategy {name!r}; known: {[str(s) for s in Strategy]}")
        sleeve = self._sleeves.get(parsed)
        if sleeve is None:
            self._resolve_pending()
            sleeve = self._sleeves.get(parsed)
        if sleeve is None:
            available = [str(s) for s in self.strategies]
            raise HTTPException(
                404, f"strategy {parsed} is not configured on this host; available: {available}"
            )
        return sleeve

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def close(self) -> None:
        for sleeve in self._sleeves.values():
            sleeve.close()
        self._sleeves.clear()
        self._pending.clear()

    # -- the one write path ------------------------------------------------- #

    def append_control(
        self,
        sleeve: StrategyDeps,
        action: str,
        operator: str,
        reason: str,
        payload: dict[str, Any],
        now_ms: int,
    ) -> None:
        """Append one ``control_log`` row on a connection that lives for one call.

        The engine picks the row up on its next tick. Nothing else in the API
        may open a writable connection, and this one writes no other table.
        """
        db = Database(
            sleeve.db_path, wal=sleeve.cfg.storage.wal, busy_timeout_ms=sleeve.cfg.storage.busy_timeout_ms
        )
        try:
            StateRepo(db, sleeve.strategy).log_control(action, operator, reason, payload, now_ms)
        finally:
            db.close()


def _try_open(name: str, config_path: str | Path) -> StrategyDeps | None:
    """Open one sleeve, or return None when it is simply not deployed here."""
    strategy = parse_strategy(name)
    if strategy is None:
        return None
    try:
        cfg = load_config(config_path, use_env=False)
    except (FileNotFoundError, ValueError):
        return None
    db_path = Path(cfg.storage.db_path)
    if not db_path.exists():
        return None
    db: ReadOnlyDatabase | None = None
    try:
        db = ReadOnlyDatabase(db_path, busy_timeout_ms=cfg.storage.busy_timeout_ms)
        # SQLite opens lazily, so a truncated or half-restored file connects
        # happily and only fails on the first real read. Probe it here: a sleeve
        # we cannot read is absent (404 with a message that says so) rather than
        # a live sleeve whose every page is a 500.
        db.query("SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 1")
    except sqlite3.Error:
        if db is not None:
            db.close()
        return None
    return StrategyDeps(strategy=strategy, cfg=cfg, db_path=db_path, db=db, repos=Repositories(db, strategy))


def parse_strategy(name: str) -> Strategy | None:
    try:
        return Strategy(str(name).upper())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #


def get_registry(request: Request) -> Registry:
    return request.app.state.registry  # type: ignore[no-any-return]


def get_sleeve(strategy: str, request: Request) -> StrategyDeps:
    return get_registry(request).get(strategy)


# --------------------------------------------------------------------------- #
# Shared read helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Window:
    """A metric period resolved against ``now`` — the same shape the engine used."""

    period: str
    start_day: date
    end_day: date

    @property
    def days(self) -> int:
        return (self.end_day - self.start_day).days + 1


def window_for(period: str, now_ms: int, first_day: date | None) -> Window:
    """Mirror of ``MetricsEngine.window_for`` for reading — it stores no windows."""
    end_day = day_of(now_ms)
    if period in _PERIOD_DAYS:
        start_day = end_day - timedelta(days=_PERIOD_DAYS[period] - 1)
    elif period == "mtd":
        start_day = end_day.replace(day=1)
    elif period == "ytd":
        start_day = end_day.replace(month=1, day=1)
    else:
        start_day = min(first_day, end_day) if first_day else end_day
    return Window(period=period, start_day=start_day, end_day=end_day)


def first_equity_day(repos: Repositories) -> date | None:
    rows = repos.equity.all()
    return date.fromisoformat(rows[0]["day"]) if rows else None


class MetricIndex:
    """The stored metric set, addressed as ``name`` + ``period``."""

    __slots__ = ("_rows",)

    def __init__(self, repos: Repositories, period: str | None = None) -> None:
        self._rows = repos.metrics.latest(period)

    def row(self, name: str, period: str) -> dict[str, Any] | None:
        return self._rows.get(f"{name}:{period}")

    def value(self, name: str, period: str) -> float | None:
        row = self.row(name, period)
        if row is None or row["value"] is None:
            return None
        return float(row["value"])

    def std_error(self, name: str, period: str) -> float | None:
        row = self.row(name, period)
        if row is None or row["std_error"] is None:
            return None
        return float(row["std_error"])

    def n_obs(self, name: str, period: str) -> int:
        row = self.row(name, period)
        return int(row["n_obs"]) if row else 0

    def extra(self, name: str, period: str) -> dict[str, Any]:
        row = self.row(name, period)
        return json_loads(row["extra_json"], {}) if row else {}

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows.values())


def engine_state(repos: Repositories, cfg: AppConfig) -> dict[str, Any]:
    """The persisted engine state, with day-one defaults for an empty database."""
    row = repos.state.load()
    if row is None:
        return {
            "state": str(EngineState.IDLE),
            "phase": str(Phase(cfg.phase.current)),
            "paused": False,
            "stopped": False,
            "safe_mode": False,
            "halt_reason": "",
            "governor_g": 1.0,
            "blocks": [],
            "context": {},
            "updated_ts": None,
        }
    return {
        "state": str(row["state"]),
        "phase": str(row["phase"]),
        "paused": bool(row["paused"]),
        "stopped": bool(row["stopped"]),
        "safe_mode": bool(row["safe_mode"]),
        "halt_reason": str(row["halt_reason"] or ""),
        "governor_g": float(row["governor_g"]),
        "blocks": list(row.get("blocks") or []),
        "context": dict(row.get("context") or {}),
        "updated_ts": int(row["updated_ts"]),
    }


def latest_equity(repos: Repositories) -> float:
    """Equity as last recorded: the daily curve, else the last snapshot, else zero."""
    row = repos.equity.latest()
    if row is not None:
        return float(row["equity"])
    snap = repos.snapshots.latest()
    return float(snap["margin_balance"]) if snap else 0.0


def cap_breaches(cfg: AppConfig, book: Mapping[str, Position], equity: float) -> list[str]:
    """Which of the three caps the stored book exceeds — the supervisor's own test.

    ``RiskSupervisor.check`` classifies a book ``amber`` when *any* cap is
    exceeded by fills or price moves (US-T12 AC 1), and it does not persist that
    verdict, so the dashboard has to re-apply the same comparison to the last
    recorded book rather than colour by margin alone.
    """
    if equity <= 0 or not book:
        return []
    gross = sum(abs(p.notional) for p in book.values())
    net = sum(p.notional for p in book.values())
    largest = max((abs(p.notional) for p in book.values()), default=0.0)
    out: list[str] = []
    if gross > cfg.caps.gross * equity:
        out.append("gross")
    if abs(net) > cfg.caps.net * equity:
        out.append("net")
    if largest > cfg.caps.single * equity:
        out.append("single")
    return out


def risk_status(
    cfg: AppConfig,
    margin_ratio: float,
    state: Mapping[str, Any],
    breaches: Sequence[str] = (),
) -> RiskStatus:
    """The tile colour, derived from stored state only.

    ``ExposureSnapshot.status`` is a live reading the supervisor never persists,
    so the dashboard recolours from what *is* stored: the margin ratio of the
    last snapshot, the caps against the last recorded book and the engine's own
    holds. US-T12 AC 1: green is "all caps satisfied *and* margin < 20 %", so a
    cap breach is amber even on a comfortable margin.
    """
    if state["state"] in (str(EngineState.HALTED_RISK), str(EngineState.STOPPED)):
        return RiskStatus.RED
    if margin_ratio >= cfg.risk.margin_red:
        return RiskStatus.RED
    if margin_ratio >= cfg.risk.margin_amber or breaches or state["blocks"] or state["safe_mode"]:
        return RiskStatus.AMBER
    return RiskStatus.GREEN


def share(value: float, total: float) -> float:
    return value / total if total else 0.0


__all__ = [
    "DAY_MAX",
    "DAY_MIN",
    "DEFAULT_CONFIG_PATHS",
    "RESOLVE_RETRY_MS",
    "MetricIndex",
    "ReadOnlyDatabase",
    "Registry",
    "StrategyDeps",
    "Window",
    "cap_breaches",
    "engine_state",
    "first_equity_day",
    "get_registry",
    "get_sleeve",
    "latest_equity",
    "parse_strategy",
    "risk_status",
    "share",
    "window_for",
]
