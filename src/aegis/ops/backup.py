"""Backup verification (CARRY US-18 AC 2, extended to both databases in US-T19 AC 4).

A backup nobody has ever restored is a rumour. This module therefore does two
things and reports on both: it checks that the Litestream replica exists and how
far behind it is, and it performs an actual restore into a temporary directory
and compares the restored schema and row counts against the live database.

The free host may not have ``litestream`` installed at all. That is a reportable
state — ``available=False`` — and never an exception: a missing backup tool must
not take down a running trading engine, and the operator needs to see the
difference between "backup is broken" and "backup is not installed here". For
the same reason the restore step is injectable, so the comparison logic is
tested without the binary (PRD Section 14: deterministic and offline).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from aegis.core.context import Context
from aegis.storage.db import Database

LITESTREAM = "litestream"
RESTORE_TIMEOUT_S = 300

Restorer = Callable[[Path, Path], bool]
"""``(replica_path, destination) -> restored``. Never raises."""


@dataclass(frozen=True, slots=True)
class ReplicaStatus:
    """What the replica looks like right now."""

    path: str
    exists: bool
    available: bool  # litestream present on this host
    lag_s: float | None  # age of the newest replica file; None when unknown
    detail: str

    @property
    def ok(self) -> bool:
        return self.exists and self.lag_s is not None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Outcome of an actual restore-and-compare."""

    ok: bool
    available: bool
    source: str
    detail: str
    tables: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
    mismatches: tuple[str, ...] = ()


class Backup:
    """Replica inspection and restore verification."""

    def __init__(self, ctx: Context, restorer: Restorer | None = None) -> None:
        self.ctx = ctx
        self.restorer = restorer or litestream_restore

    # -- replica --------------------------------------------------------- #

    @property
    def replica_path(self) -> Path:
        return Path(self.ctx.cfg.backup.litestream_replica_path)

    def verify_replica(self) -> ReplicaStatus:
        """Does the replica exist, and how stale is it?

        Lag is measured from the newest file under the replica path rather than
        by asking litestream, so the answer is the same whether or not the
        binary is installed — the file is the artefact that matters.
        """
        path = self.replica_path
        available = litestream_available()
        if not path.exists():
            return ReplicaStatus(str(path), False, available, None, "replica path does not exist")
        newest = _newest_mtime_ms(path)
        if newest is None:
            return ReplicaStatus(str(path), True, available, None, "replica path is empty")
        lag_s = max(0.0, (self.ctx.now_ms() - newest) / 1000.0)
        return ReplicaStatus(str(path), True, available, lag_s, f"replica {lag_s:.0f} s behind")

    # -- restore --------------------------------------------------------- #

    def restore_check(self, path: str | Path) -> RestoreResult:
        """Restore ``path``'s replica into a temp dir and compare it to the live file."""
        source = Path(path)
        if not source.exists():
            return RestoreResult(False, litestream_available(), str(source), "source database does not exist")
        if self.restorer is litestream_restore and not litestream_available():
            return RestoreResult(False, False, str(source), "litestream is not installed")

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / f"restored-{source.name}"
            if not self.restorer(self.replica_path, destination):
                return RestoreResult(False, True, str(source), "restore failed")
            if not destination.exists():
                return RestoreResult(False, True, str(source), "restore produced no file")
            return self._compare(source, destination)

    def _compare(self, source: Path, restored: Path) -> RestoreResult:
        live = Database(source, wal=False, read_only=True)
        copy = Database(restored, wal=False, read_only=True)
        try:
            live_tables = tuple(live.tables())
            copy_tables = tuple(copy.tables())
            mismatches: list[str] = []
            if live_tables != copy_tables:
                missing = set(live_tables) - set(copy_tables)
                extra = set(copy_tables) - set(live_tables)
                mismatches.append(f"schema: missing={sorted(missing)} extra={sorted(extra)}")
            counts: dict[str, int] = {}
            for table in live_tables:
                live_n = _count(live, table)
                counts[table] = live_n
                if table not in copy_tables:
                    continue
                copy_n = _count(copy, table)
                if live_n != copy_n:
                    mismatches.append(f"{table}: live={live_n} restored={copy_n}")
            ok = not mismatches
            detail = "restore matches the live database" if ok else "; ".join(mismatches)
            return RestoreResult(ok, True, str(source), detail, live_tables, counts, tuple(mismatches))
        finally:
            live.close()
            copy.close()

    def verify_all(self, paths: Sequence[str | Path]) -> dict[str, RestoreResult]:
        """US-T19 AC 4: the restore test covers every database, not just this one."""
        return {str(p): self.restore_check(p) for p in paths}


# --------------------------------------------------------------------------- #
# litestream boundary
# --------------------------------------------------------------------------- #


def litestream_available() -> bool:
    return shutil.which(LITESTREAM) is not None


def litestream_restore(replica: Path, destination: Path) -> bool:
    """Default restorer. Returns False rather than raising when anything goes wrong."""
    if not litestream_available():
        return False
    try:
        completed = subprocess.run(
            [LITESTREAM, "restore", "-o", str(destination), str(replica)],
            capture_output=True,
            timeout=RESTORE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and destination.exists()


def _newest_mtime_ms(path: Path) -> int | None:
    times = [int(p.stat().st_mtime * 1000) for p in path.rglob("*") if p.is_file()]
    if path.is_file():
        times.append(int(path.stat().st_mtime * 1000))
    return max(times) if times else None


def _count(db: Database, table: str) -> int:
    value: Any = db.scalar(f'SELECT COUNT(*) FROM "{table}"')
    return int(value or 0)


__all__ = [
    "LITESTREAM",
    "RESTORE_TIMEOUT_S",
    "Backup",
    "ReplicaStatus",
    "RestoreResult",
    "Restorer",
    "litestream_available",
    "litestream_restore",
]
