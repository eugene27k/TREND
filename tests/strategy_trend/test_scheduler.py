"""Cadences (Locked Decision 6, Appendix A) — one table, tested."""

from __future__ import annotations

from aegis.core.clock import to_ms
from aegis.strategy_trend import scheduler as sch


def test_the_schedule_carries_every_prd_cadence(cfg):
    s = sch.build_schedule(cfg)
    assert s.daily[sch.REBALANCE].at == "00:05"
    assert s.daily[sch.BARS].at == "00:02"
    assert s.daily[sch.METRICS].at == "01:10", "staggered clear of CARRY's 00:05 job"
    assert s.daily[sch.DAILY_REPORT].at == "00:10"
    assert s.daily[sch.REBALANCE_REPORT].at == "01:05"
    assert s.interval[sch.SUPERVISOR].every_s == 60
    assert s.interval[sch.DRIFT].every_s == 3600
    assert s.interval[sch.STATUS_WATCH].every_s == 3600


def test_a_daily_job_fires_once_per_utc_day(cfg):
    s = sch.build_schedule(cfg)
    morning = to_ms("2026-09-08T00:06:00Z")
    assert sch.REBALANCE in s.due(morning)
    s.mark(sch.REBALANCE, morning)
    assert sch.REBALANCE not in s.due(morning)
    assert sch.REBALANCE not in s.due(to_ms("2026-09-08T23:59:00Z"))
    assert sch.REBALANCE in s.due(to_ms("2026-09-09T00:06:00Z"))


def test_a_daily_job_is_not_due_before_its_time(cfg):
    s = sch.build_schedule(cfg)
    assert sch.REBALANCE not in s.due(to_ms("2026-09-08T00:04:00Z"))
    assert sch.METRICS not in s.due(to_ms("2026-09-08T01:09:00Z"))


def test_a_restart_after_the_window_does_not_refire_the_days_job(cfg):
    """A restart at 00:30 must not re-run the 00:05 job it already ran."""
    s = sch.build_schedule(cfg)
    s.mark(sch.REBALANCE, to_ms("2026-09-08T00:06:00Z"))
    assert sch.REBALANCE not in s.due(to_ms("2026-09-08T00:30:00Z"))


def test_a_process_that_was_down_over_its_window_still_runs_that_day(cfg):
    """Late is better than never for the daily jobs: the bar is still today's."""
    s = sch.build_schedule(cfg)
    assert sch.REBALANCE in s.due(to_ms("2026-09-08T03:00:00Z"))


def test_interval_jobs_respect_their_period(cfg):
    s = sch.build_schedule(cfg)
    t0 = to_ms("2026-09-08T12:00:00Z")
    assert sch.SUPERVISOR in s.due(t0)
    s.mark(sch.SUPERVISOR, t0)
    assert sch.SUPERVISOR not in s.due(t0 + 59_000)
    assert sch.SUPERVISOR in s.due(t0 + 60_000)


def test_next_wake_is_bounded_so_the_loop_stays_responsive(cfg):
    s = sch.build_schedule(cfg)
    t0 = to_ms("2026-09-08T12:00:00Z")
    for name in list(s.interval):
        s.mark(name, t0)
    wait = s.next_wake_ms(t0)
    assert 1.0 <= wait <= 60.0
    assert s.next_wake_ms(t0 + 3_600_000) == 1.0, "overdue jobs wake immediately"


def test_universe_refresh_fires_on_first_start_and_on_the_first_of_the_month(cfg):
    assert sch.universe_refresh_due(cfg, to_ms("2026-09-08T00:06:00Z"), None)
    assert not sch.universe_refresh_due(cfg, to_ms("2026-09-08T00:06:00Z"), "2026-09")
    assert sch.universe_refresh_due(cfg, to_ms("2026-10-01T00:06:00Z"), "2026-09")


def test_universe_refresh_is_not_due_before_its_time_on_the_first(cfg):
    assert not sch.universe_refresh_due(cfg, to_ms("2026-10-01T00:01:00Z"), "2026-09")


def test_a_missed_universe_refresh_still_happens(cfg):
    """Coming back up on the 15th with last month's universe must refresh it."""
    assert sch.universe_refresh_due(cfg, to_ms("2026-10-15T09:00:00Z"), "2026-09")


def test_is_due_reads_without_marking(cfg):
    s = sch.build_schedule(cfg)
    now = to_ms("2026-09-08T00:06:00Z")
    assert s.is_due(sch.REBALANCE, now)
    assert s.is_due(sch.REBALANCE, now), "asking must not consume"
    assert not s.is_due("nonexistent", now)
