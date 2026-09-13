"""Process resource measurement (US-T19 AC 2).

The PRD asks for the combined footprint to be "measured and shown in the
Operations page": RSS under 1 GB and CPU under 20 % of one core outside the
rebalance window, on the free shape. A container memory limit is a *budget*, not
a measurement — it says what the process may not exceed, never what it actually
used — so the engine records its own.

Read from ``/proc`` rather than psutil: it is stdlib, it is exactly what the
Linux container the PRD targets provides, and it adds nothing to the free-first
dependency list. On a platform without ``/proc`` every reading is ``None``, and
the dashboard renders "n/a" rather than a fabricated number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROC_STATUS = Path("/proc/self/status")
PROC_STAT = Path("/proc/self/stat")
CLOCK_TICKS = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0


def rss_mb() -> float | None:
    """Resident set size of this process in MB, or None where /proc is absent."""
    try:
        for line in PROC_STATUS.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def cpu_seconds() -> float | None:
    """Total CPU seconds this process has consumed (user + system)."""
    try:
        fields = PROC_STAT.read_text().rsplit(")", 1)[-1].split()
        # After the comm field: state is [0], so utime is [11] and stime [12].
        return (float(fields[11]) + float(fields[12])) / CLOCK_TICKS
    except (OSError, ValueError, IndexError):
        return None


@dataclass(frozen=True, slots=True)
class Sample:
    ts_ms: int
    cpu_seconds: float | None
    rss_mb: float | None


class ResourceMonitor:
    """Turns two CPU-time readings into a percentage of one core.

    A single reading cannot express a rate, so the first call reports ``None``
    for CPU rather than the process's lifetime average, which on a long-running
    engine would understate a spike into invisibility.
    """

    def __init__(self) -> None:
        self._previous: Sample | None = None

    def sample(self, now_ms: int) -> Sample:
        return Sample(ts_ms=now_ms, cpu_seconds=cpu_seconds(), rss_mb=rss_mb())

    def measure(self, now_ms: int) -> tuple[float | None, float | None]:
        """``(rss_mb, cpu_pct_of_one_core)`` since the previous call."""
        current = self.sample(now_ms)
        previous, self._previous = self._previous, current
        if (
            previous is None
            or current.cpu_seconds is None
            or previous.cpu_seconds is None
            or current.ts_ms <= previous.ts_ms
        ):
            return current.rss_mb, None
        elapsed_s = (current.ts_ms - previous.ts_ms) / 1000.0
        used_s = max(0.0, current.cpu_seconds - previous.cpu_seconds)
        return current.rss_mb, 100.0 * used_s / elapsed_s


__all__ = ["CLOCK_TICKS", "ResourceMonitor", "Sample", "cpu_seconds", "rss_mb"]
