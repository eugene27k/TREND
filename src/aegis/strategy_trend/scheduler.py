"""What is due, and when.

The engine loop asks this module a single question each tick — "what should I do
now?" — so that every cadence in the PRD lives in one table instead of being
spread across the runner as scattered timestamp arithmetic.

Daily jobs fire at most once per UTC day, tracked by the day they last ran, so a
restart at 00:30 does not re-fire the 00:05 job and a process that was down over
its window does not fire it late the next morning. Interval jobs simply track
the last time they ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from aegis.core.clock import at_utc, day_of, month_key
from aegis.core.config import AppConfig

# Job names are stable — they appear in engine_state.context and the dashboard.
BARS = "bars"
REBALANCE = "rebalance"
REBALANCE_END = "rebalance_end"
UNIVERSE = "universe"
METRICS = "metrics"
DAILY_REPORT = "daily_report"
REBALANCE_REPORT = "rebalance_report"
SUPERVISOR = "supervisor"
DRIFT = "drift"
STATUS_WATCH = "status_watch"
LEDGER_SYNC = "ledger_sync"
HEARTBEAT = "heartbeat"


@dataclass(slots=True)
class DailyJob:
    name: str
    at: str  # "HH:MM" UTC
    last_run: date | None = None

    def due(self, now_ms: int) -> bool:
        today = day_of(now_ms)
        if self.last_run == today:
            return False
        return now_ms >= at_utc(today, self.at)

    def mark(self, now_ms: int) -> None:
        self.last_run = day_of(now_ms)


@dataclass(slots=True)
class IntervalJob:
    name: str
    every_s: float
    last_run_ms: int | None = None

    def due(self, now_ms: int) -> bool:
        return self.last_run_ms is None or now_ms - self.last_run_ms >= self.every_s * 1000

    def mark(self, now_ms: int) -> None:
        self.last_run_ms = now_ms


@dataclass(slots=True)
class Schedule:
    daily: dict[str, DailyJob] = field(default_factory=dict)
    interval: dict[str, IntervalJob] = field(default_factory=dict)

    def due(self, now_ms: int) -> list[str]:
        """Every job whose time has come, daily jobs first (they are the trading path)."""
        names = [j.name for j in self.daily.values() if j.due(now_ms)]
        names += [j.name for j in self.interval.values() if j.due(now_ms)]
        return names

    def mark(self, name: str, now_ms: int) -> None:
        if name in self.daily:
            self.daily[name].mark(now_ms)
        elif name in self.interval:
            self.interval[name].mark(now_ms)

    def is_due(self, name: str, now_ms: int) -> bool:
        job = self.daily.get(name) or self.interval.get(name)
        return bool(job and job.due(now_ms))

    def next_wake_ms(self, now_ms: int, *, floor_s: float = 1.0, ceiling_s: float = 60.0) -> float:
        """Seconds to sleep before the next check — bounded so the loop stays responsive."""
        waits = [
            (j.last_run_ms + j.every_s * 1000 - now_ms) / 1000
            for j in self.interval.values()
            if j.last_run_ms is not None
        ]
        soonest = min(waits, default=ceiling_s)
        return max(floor_s, min(ceiling_s, soonest))


def build_schedule(cfg: AppConfig) -> Schedule:
    """The PRD's cadences (Locked Decision 6, Section 13, Appendix A) in one place."""
    return Schedule(
        daily={
            BARS: DailyJob(BARS, cfg.rebalance.bar_fetch_time_utc),
            REBALANCE: DailyJob(REBALANCE, cfg.rebalance.time_utc),
            UNIVERSE: DailyJob(UNIVERSE, cfg.universe.refresh_time_utc),
            METRICS: DailyJob(METRICS, cfg.metrics.job_time_utc),
            DAILY_REPORT: DailyJob(DAILY_REPORT, cfg.telegram.daily_time_utc),
            REBALANCE_REPORT: DailyJob(REBALANCE_REPORT, cfg.telegram.rebalance_summary_time_utc),
        },
        interval={
            SUPERVISOR: IntervalJob(SUPERVISOR, cfg.risk.supervisor_interval_s),
            DRIFT: IntervalJob(DRIFT, cfg.risk.drift_interval_min * 60),
            STATUS_WATCH: IntervalJob(STATUS_WATCH, cfg.universe.status_watch_minutes * 60),
            LEDGER_SYNC: IntervalJob(LEDGER_SYNC, cfg.risk.supervisor_interval_s * 5),
            HEARTBEAT: IntervalJob(HEARTBEAT, cfg.heartbeat.interval_s),
        },
    )


def universe_refresh_due(cfg: AppConfig, now_ms: int, last_month: str | None) -> bool:
    """The 1st of the month at the configured time — or first start (5.1)."""
    today = day_of(now_ms)
    if last_month is None:
        return True
    if month_key(today) == last_month:
        return False
    return today.day >= cfg.universe.refresh_day_utc and now_ms >= at_utc(
        today, cfg.universe.refresh_time_utc
    )


__all__ = [
    "BARS",
    "DAILY_REPORT",
    "DRIFT",
    "HEARTBEAT",
    "LEDGER_SYNC",
    "METRICS",
    "REBALANCE",
    "REBALANCE_END",
    "REBALANCE_REPORT",
    "STATUS_WATCH",
    "SUPERVISOR",
    "UNIVERSE",
    "DailyJob",
    "IntervalJob",
    "Schedule",
    "build_schedule",
    "universe_refresh_due",
]
