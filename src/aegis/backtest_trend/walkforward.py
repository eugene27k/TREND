"""Walk-forward robustness (PRD 11.6, US-T16 AC 4).

This is explicitly **not** an optimiser. Invariant 7 forbids selecting parameters
from live or historical performance, so nothing here ever writes a config. What
it answers is narrower and more useful: *are the fixed defaults a reasonable
point in the parameter space, or did they only ever look good on the full
sample?* The test is that the defaults rank in the top half of the grid on at
least 70 % of out-of-sample test windows.

A strategy whose defaults sit in the bottom half most of the time was fitted,
whether or not anyone admits to fitting it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from aegis.backtest_trend.robustness import DEFAULT_PARAM_SET, parameter_grid
from aegis.core.clock import add_months, month_start

RunFn = Callable[[str, dict[str, Any], date, date], float]
"""``(param_set_name, overrides, test_start, test_end) -> test Sharpe``."""


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    train_start: date
    train_end: date
    test_start: date
    test_end: date


@dataclass(frozen=True, slots=True)
class WindowRanking:
    window: str
    rows: tuple[dict[str, Any], ...]
    default_rank: int
    n_params: int

    @property
    def default_in_top_half(self) -> bool:
        return self.n_params > 0 and self.default_rank <= (self.n_params + 1) // 2


def windows(
    start: date, end: date, *, train_months: int = 12, test_months: int = 6, step_months: int = 6
) -> list[Window]:
    """Rolling 12-month train / 6-month test windows stepping by 6 months.

    The train window carries no information — nothing is fitted — but it is kept
    because it defines how much history a *live* deployment would have had before
    the test period, which is what makes the test period genuinely out-of-sample.
    """
    out: list[Window] = []
    cursor = month_start(f"{start.year:04d}-{start.month:02d}")
    while True:
        train_start = cursor
        test_start = add_months(train_start, train_months)
        test_end = add_months(test_start, test_months)
        if test_start >= end:
            break
        out.append(
            Window(
                name=f"{test_start.isoformat()}..{min(test_end, end).isoformat()}",
                train_start=train_start,
                train_end=test_start,
                test_start=test_start,
                test_end=min(test_end, end),
            )
        )
        cursor = add_months(cursor, step_months)
    return out


def rank_window(
    window: Window,
    run: RunFn,
    grid: Sequence[tuple[str, dict[str, Any]]] | None = None,
    default_name: str = DEFAULT_PARAM_SET,
) -> WindowRanking:
    """Run every grid point on one test window and rank them by test Sharpe."""
    points = list(grid if grid is not None else parameter_grid())
    scored = [
        {
            "param_set": name,
            "test_sharpe": float(run(name, overrides, window.test_start, window.test_end)),
            "is_default": name == default_name,
        }
        for name, overrides in points
    ]
    # Ties rank by name so the result is deterministic.
    scored.sort(key=lambda r: (-r["test_sharpe"], r["param_set"]))
    for i, row in enumerate(scored, start=1):
        row["rank"] = i
        row["n_params"] = len(scored)
    default_rank = next((r["rank"] for r in scored if r["is_default"]), len(scored))
    ranking = WindowRanking(
        window=window.name, rows=tuple(scored), default_rank=int(default_rank), n_params=len(scored)
    )
    for row in scored:
        row["window"] = window.name
        row["default_in_top_half"] = ranking.default_in_top_half
    return ranking


def gate_passes(rankings: Sequence[WindowRanking], min_fraction: float = 0.70) -> bool:
    """US-T16 AC 4: top half on >= 70 % of test windows."""
    if not rankings:
        return False
    good = sum(1 for r in rankings if r.default_in_top_half)
    return good / len(rankings) >= min_fraction


def summary(rankings: Sequence[WindowRanking]) -> dict[str, Any]:
    return {
        "windows": len(rankings),
        "top_half": sum(1 for r in rankings if r.default_in_top_half),
        "fraction": (sum(1 for r in rankings if r.default_in_top_half) / len(rankings)) if rankings else 0.0,
        "ranks": [r.default_rank for r in rankings],
        "n_params": rankings[0].n_params if rankings else 0,
    }


def rows_for_storage(rankings: Sequence[WindowRanking]) -> list[dict[str, Any]]:
    return [dict(row) for ranking in rankings for row in ranking.rows]


__all__ = [
    "RunFn",
    "Window",
    "WindowRanking",
    "gate_passes",
    "rank_window",
    "rows_for_storage",
    "summary",
    "windows",
]
