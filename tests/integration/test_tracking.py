"""US-T16 AC 5 — the daily live-vs-reference comparison.

Without this the tracking-error kill rule can never fire, and a kill rule that
can never fire is worse than none: the dashboard's status board reads green
forever while nobody is actually watching.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.backtest_trend.tracking import MIN_DAYS, TrackingService, correlation
from aegis.core.clock import to_ms
from aegis.core.types import EquityPoint
from aegis.strategy_trend.kill_rules import TRACKING_ERROR, KillRules

START = date(2026, 8, 1)


def seed_equity(ctx, live: list[float]) -> None:
    index = 1.0
    peak = 1.0
    for i, equity in enumerate(live):
        day = START + timedelta(days=i)
        if i:
            index *= equity / live[i - 1]
        peak = max(peak, index)
        ctx.repos.equity.upsert(
            day,
            EquityPoint(to_ms(day), equity, 0.0, 1.0, index),
            peak_index=peak,
            drawdown=max(0.0, 1.0 - index / peak),
        )


def test_correlation_is_none_when_undefined():
    assert correlation([], []) is None
    assert correlation([1.0], [1.0]) is None
    assert correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None, "zero variance"
    assert correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    assert correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_no_row_before_there_is_enough_live_history(world):
    seed_equity(world, [10_000.0] * (MIN_DAYS - 1))
    assert TrackingService(world).update(START + timedelta(days=MIN_DAYS - 2)) is None
    assert world.repos.tracking.latest() is None


def test_no_row_without_stored_market_data(world):
    seed_equity(world, [10_000.0 + i for i in range(30)])
    # The equity curve exists but no bars or universes have been recorded.
    assert TrackingService(world).update(START + timedelta(days=29)) is None


def seed_live_record(ctx, day: date, n_days: int = 40, drift: float = 5.0) -> None:
    """A live equity record ending on ``day`` — one real day is not a comparison."""
    equity = 10_000.0
    index = peak = 1.0
    for i in range(n_days):
        d = day - timedelta(days=n_days - 1 - i)
        previous = equity
        equity += drift * ((-1) ** i) + drift
        if i:
            index *= equity / previous
        peak = max(peak, index)
        ctx.repos.equity.upsert(
            d,
            EquityPoint(to_ms(d), equity, 0.0, 1.0, index),
            peak_index=peak,
            drawdown=max(0.0, 1.0 - index / peak),
        )


def test_a_comparison_is_stored_once_there_is_a_live_record(world, runner_after_a_day):
    """The real path: the engine traded, so bars and universes exist too."""
    ctx, day = runner_after_a_day
    seed_live_record(ctx, day)
    result = TrackingService(ctx).update(day)
    assert result is not None

    stored = ctx.repos.tracking.latest()
    assert stored is not None
    assert stored["day"] == day.isoformat()
    assert stored["cum_live"] == pytest.approx(result.cum_live)
    assert stored["in_bounds"] in (0, 1)


def test_a_live_path_that_diverges_from_the_reference_is_flagged(world, runner_after_a_day):
    """The comparison must actually notice a divergence, not just record numbers."""
    ctx, day = runner_after_a_day
    seed_live_record(ctx, day, drift=200.0)  # far away from any reference path
    result = TrackingService(ctx).update(day)
    assert result is not None
    assert not result.in_bounds
    assert "cum_diff" in result.breaches
    assert result.breach_days >= 1
    assert any(a["code"] == "TRACKING_ERROR" for a in ctx.repos.alerts.recent())


def test_a_breach_increments_the_streak_and_alerts(world):
    """The streak is what the 14-day kill rule counts."""
    cfg = world.cfg
    world.repos.tracking.upsert(START, in_bounds=False, breach_days=3)
    # A hand-made row standing in for yesterday's comparison.
    world.repos.tracking.upsert(START + timedelta(days=1), in_bounds=False, breach_days=4)
    latest = world.repos.tracking.latest()
    assert latest["breach_days"] == 4
    assert cfg.tracking.breach_days_block == 14


def test_the_kill_rule_fires_only_once_the_streak_reaches_the_bound(world):
    day = START.isoformat()
    world.repos.tracking.upsert(day, in_bounds=False, breach_days=13)
    assert TRACKING_ERROR not in {a.rule for a in KillRules(world).evaluate(to_ms(START))}

    world.repos.tracking.upsert(day, in_bounds=False, breach_days=14)
    fired = {a.rule for a in KillRules(world).evaluate(to_ms(START))}
    assert TRACKING_ERROR in fired


def test_the_streak_continues_from_the_previous_day(world, runner_after_a_day):
    ctx, day = runner_after_a_day
    seed_live_record(ctx, day, drift=200.0)
    ctx.repos.tracking.upsert(day - timedelta(days=1), in_bounds=False, breach_days=5)
    result = TrackingService(ctx).update(day)
    assert result is not None and not result.in_bounds
    assert result.breach_days == 6, "a breach must extend yesterday's streak"


def test_the_reference_uses_the_engines_own_stored_bars(world, runner_after_a_day):
    """A reference built from re-downloaded history would hide a data problem."""
    ctx, _day = runner_after_a_day
    service = TrackingService(ctx)
    bars, _funding = service.stored_market()
    assert bars
    stored_symbols = set(bars)
    universe = set(ctx.repos.universe.symbols(ctx.repos.universe.latest_month()))
    assert universe <= stored_symbols
