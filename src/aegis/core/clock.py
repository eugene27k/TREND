"""Clock abstraction.

Nothing in the engine calls ``datetime.now()`` directly: every scheduled action,
timeout and timestamp goes through a ``Clock``. That is what makes the rebalance
window, the governor hysteresis and the kill rules testable without waiting.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable

MS = 1000
DAY_MS = 86_400_000


@runtime_checkable
class Clock(Protocol):
    def now_ms(self) -> int: ...
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall clock, UTC."""

    __slots__ = ()

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests and the backtester."""

    __slots__ = ("_ms", "slept")

    def __init__(self, start: datetime | int | str = 0) -> None:
        self._ms = to_ms(start) if not isinstance(start, int) else start
        self.slept: list[float] = []

    def now_ms(self) -> int:
        return self._ms

    def now(self) -> datetime:
        return from_ms(self._ms)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._ms += int(seconds * 1000)

    def advance(self, seconds: float = 0, *, minutes: float = 0, hours: float = 0, days: float = 0) -> None:
        self._ms += int((seconds + minutes * 60 + hours * 3600 + days * 86400) * 1000)

    def set(self, when: datetime | int | str) -> None:
        self._ms = to_ms(when) if not isinstance(when, int) else when


# --------------------------------------------------------------------------- #
# Conversions — all timestamps in the system are epoch milliseconds, UTC.
# --------------------------------------------------------------------------- #


def to_ms(when: datetime | date | str) -> int:
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if isinstance(when, datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return int(when.astimezone(UTC).timestamp() * 1000)
    return int(datetime(when.year, when.month, when.day, tzinfo=UTC).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def day_of(ms: int) -> date:
    return from_ms(ms).date()


def day_start_ms(d: date | datetime | int) -> int:
    """00:00:00.000 UTC of the day containing ``d``."""
    if isinstance(d, int):
        d = from_ms(d)
    if isinstance(d, datetime):
        d = d.date()
    return to_ms(d)


def parse_hhmm(value: str) -> tuple[int, int]:
    """``"00:05"`` -> ``(0, 5)``."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"out-of-range time {value!r}")
    return hh, mm


def at_utc(day: date, hhmm: str) -> int:
    """Epoch ms of ``hhmm`` UTC on ``day``."""
    hh, mm = parse_hhmm(hhmm)
    return to_ms(datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC))


def next_occurrence_ms(now_ms: int, hhmm: str) -> int:
    """Next epoch-ms at which the wall clock shows ``hhmm`` UTC (strictly future)."""
    now = from_ms(now_ms)
    hh, mm = parse_hhmm(hhmm)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if to_ms(candidate) <= now_ms:
        candidate += timedelta(days=1)
    return to_ms(candidate)


def month_key(ms: int | date | datetime) -> str:
    """``"2026-09"`` — the key used by ``universe_history``."""
    if isinstance(ms, int):
        ms = from_ms(ms)
    return f"{ms.year:04d}-{ms.month:02d}"


def month_start(key: str) -> date:
    y, m = key.split("-")
    return date(int(y), int(m), 1)


def add_months(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


__all__ = [
    "DAY_MS",
    "MS",
    "Clock",
    "FakeClock",
    "SystemClock",
    "add_months",
    "at_utc",
    "day_of",
    "day_start_ms",
    "from_ms",
    "month_key",
    "month_start",
    "next_occurrence_ms",
    "parse_hhmm",
    "to_ms",
]
