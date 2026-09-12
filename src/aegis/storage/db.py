"""SQLite access layer.

One file per strategy (Section 6: "one process per strategy, each with its own
SQLite file"), WAL mode so the read-only API process and Litestream can read
while the engine writes. Migrations are plain .sql files applied in name order
and recorded in ``schema_migrations``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Database:
    """Thin, thread-safe wrapper around a SQLite connection."""

    def __init__(self, path: str | Path, *, wal: bool = True, busy_timeout_ms: int = 10_000,
                 read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=busy_timeout_ms / 1000
        )
        self._conn.row_factory = _row_factory
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if wal and str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    # -- primitives --------------------------------------------------------- #

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any] | dict[str, Any]]) -> None:
        materialised = list(rows)
        if not materialised:
            return
        with self._lock:
            self._conn.executemany(sql, materialised)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        return list(self.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        rows = self.execute(sql, params).fetchmany(1)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        return next(iter(row.values()))

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """All-or-nothing write. Nested calls join the outer transaction."""
        with self._lock:
            if self._conn.in_transaction:
                yield self
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- migrations --------------------------------------------------------- #

    def migrate(self, directory: Path | None = None) -> list[str]:
        """Apply every unapplied .sql file in name order; returns what ran."""
        directory = directory or MIGRATIONS_DIR
        self.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_ts INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)*1000))"
        )
        applied = {r["name"] for r in self.query("SELECT name FROM schema_migrations")}
        ran: list[str] = []
        for sql_file in sorted(directory.glob("*.sql")):
            if sql_file.name in applied:
                continue
            with self._lock:
                self._conn.executescript(sql_file.read_text(encoding="utf-8"))
                self._conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (sql_file.name,))
            ran.append(sql_file.name)
        return ran

    def tables(self) -> list[str]:
        return [
            r["name"]
            for r in self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]

    def columns(self, table: str) -> list[str]:
        return [r["name"] for r in self.query(f"PRAGMA table_info({table})")]


def open_db(path: str | Path, *, wal: bool = True, busy_timeout_ms: int = 10_000,
            migrate: bool = True) -> Database:
    db = Database(path, wal=wal, busy_timeout_ms=busy_timeout_ms)
    if migrate:
        db.migrate()
    return db


def json_dumps(value: Any) -> str:
    """Deterministic JSON for stored blobs (sorted keys — manifests must hash equal)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "value"):
        return o.value
    if hasattr(o, "__dict__"):
        return o.__dict__
    raise TypeError(f"not JSON serialisable: {type(o)!r}")


def json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


__all__ = ["MIGRATIONS_DIR", "Database", "json_dumps", "json_loads", "open_db"]
