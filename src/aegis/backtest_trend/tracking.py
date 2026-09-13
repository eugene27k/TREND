"""Live-vs-reference tracking error (US-T16 AC 5, CARRY US-14 AC 7).

Every day the engine re-runs the strategy over the *live* period using the bars
and universes it actually saw, and compares that reference path to what really
happened. The question it answers is the one that matters in a drawdown: is the
bot doing what the backtest said it would, or has something changed — a cost
model that no longer holds, an execution path that is losing more than modelled,
a universe that drifted?

Four bounds, from Appendix A:

* daily P&L correlation >= 0.7 over at least 30 days — the shapes must match;
* cumulative difference within +/- 3 % of equity — the levels must match;
* cost ratio <= 2 — live costs no more than twice the conservative model;
* turnover ratio <= 1.5 — live trading no more than 1.5x the reference's.

Breaching any of them for 14 consecutive days blocks risk-increasing orders
(``kill_rules.TRACKING_ERROR``). Without this module that rule could never fire,
which is worse than not having it: the status board would show it green forever.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from aegis.backtest_trend.simulator import BacktestResult, Simulator
from aegis.core.context import Context
from aegis.core.types import DailyBar, FundingRate, UniverseResult

#: A reference run needs a warm signal, so the comparison starts once the live
#: record itself is long enough to be worth comparing.
MIN_DAYS = 10


@dataclass(frozen=True, slots=True)
class TrackingResult:
    day: date
    live_pnl: float
    ref_pnl: float
    cum_live: float
    cum_ref: float
    corr_30d: float | None
    cum_diff_frac: float
    cost_ratio: float | None
    turnover_ratio: float | None
    in_bounds: bool
    breaches: tuple[str, ...]
    breach_days: int

    def as_row(self) -> dict[str, object]:
        return {
            "live_pnl": self.live_pnl,
            "ref_pnl": self.ref_pnl,
            "cum_live": self.cum_live,
            "cum_ref": self.cum_ref,
            "corr_30d": self.corr_30d,
            "cum_diff_frac": self.cum_diff_frac,
            "cost_ratio": self.cost_ratio,
            "turnover_ratio": self.turnover_ratio,
            "in_bounds": self.in_bounds,
            "breach_days": self.breach_days,
        }


class TrackingService:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- the reference path ------------------------------------------------- #

    def stored_market(self) -> tuple[dict[str, list[DailyBar]], dict[str, list[FundingRate]]]:
        """Bars and funding as the engine recorded them — not re-fetched.

        Using the engine's own stored data is the point: a reference built from
        freshly downloaded history would silently paper over a data problem,
        which is one of the things the comparison is meant to catch.
        """
        repos = self.ctx.repos
        symbols = repos.bars.symbols()
        bars = {s: repos.bars.series(s) for s in symbols}
        funding = {s: repos.funding.history(s) for s in symbols}
        return {s: b for s, b in bars.items() if b}, funding

    def stored_universes(self) -> dict[str, UniverseResult]:
        repos = self.ctx.repos
        out: dict[str, UniverseResult] = {}
        for month in repos.universe.months():
            result = repos.universe.month(month)
            if result is not None:
                out[month] = result
        return out

    def reference_run(self, start: date, end: date, initial_equity: float) -> BacktestResult | None:
        bars, funding = self.stored_market()
        universes = self.stored_universes()
        if not bars or not universes:
            return None
        simulator = Simulator(self.ctx.cfg, bars, funding, universes, initial_equity=initial_equity)
        return simulator.run(start, end, variant="reference")

    # -- the comparison ----------------------------------------------------- #

    def update(self, day: date, _now_ms: int = 0) -> TrackingResult | None:
        """Recompute the reference over the live period and store today's row."""
        curve = self.ctx.repos.equity.all()
        curve = [row for row in curve if row["day"] <= day.isoformat()]
        if len(curve) < MIN_DAYS:
            return None

        start = date.fromisoformat(str(curve[0]["day"]))
        initial = float(curve[0]["equity"])
        reference = self.reference_run(start, day, initial)
        if reference is None or not reference.equity:
            return None

        live_by_day = {str(r["day"]): float(r["equity"]) for r in curve}
        ref_by_day = {str(p["day"]): float(p["equity"]) for p in reference.equity}
        days = sorted(set(live_by_day) & set(ref_by_day))
        if len(days) < 2:
            return None

        live_returns, ref_returns = [], []
        for previous, current in pairwise(days):
            live_returns.append(live_by_day[current] - live_by_day[previous])
            ref_returns.append(ref_by_day[current] - ref_by_day[previous])

        cum_live = live_by_day[days[-1]] - live_by_day[days[0]]
        cum_ref = ref_by_day[days[-1]] - ref_by_day[days[0]]
        equity = live_by_day[days[-1]] or initial
        cum_diff_frac = (cum_live - cum_ref) / equity if equity else 0.0

        cfg = self.ctx.cfg.tracking
        corr = correlation(live_returns[-30:], ref_returns[-30:]) if len(live_returns) >= 2 else None
        window_start = date.fromisoformat(days[0])
        cost_ratio = self._cost_ratio(reference, window_start, day)
        turnover_ratio = self._turnover_ratio(reference, window_start, day)

        breaches: list[str] = []
        # Below the minimum sample the correlation is noise, so it is reported
        # but never counted as a breach.
        if len(live_returns) >= cfg.min_days and (corr is None or corr < cfg.min_corr):
            breaches.append("corr")
        if abs(cum_diff_frac) > cfg.max_cum_diff:
            breaches.append("cum_diff")
        if cost_ratio is not None and cost_ratio > cfg.max_cost_ratio:
            breaches.append("cost_ratio")
        if turnover_ratio is not None and turnover_ratio > cfg.max_turnover_ratio:
            breaches.append("turnover_ratio")

        previous = self.ctx.repos.tracking.latest()
        prior_streak = int(previous["breach_days"] or 0) if previous else 0
        breach_days = prior_streak + 1 if breaches else 0

        result = TrackingResult(
            day=day,
            live_pnl=live_returns[-1],
            ref_pnl=ref_returns[-1],
            cum_live=cum_live,
            cum_ref=cum_ref,
            corr_30d=corr,
            cum_diff_frac=cum_diff_frac,
            cost_ratio=cost_ratio,
            turnover_ratio=turnover_ratio,
            in_bounds=not breaches,
            breaches=tuple(breaches),
            breach_days=breach_days,
        )
        self.ctx.repos.tracking.upsert(day, **result.as_row())
        if breaches:
            self.ctx.alerts.warn(
                "TRACKING_ERROR",
                f"tracking out of bounds ({', '.join(breaches)}) for {breach_days} day(s)",
                {
                    "breaches": breaches,
                    "corr_30d": corr,
                    "cum_diff_frac": cum_diff_frac,
                    "cost_ratio": cost_ratio,
                    "turnover_ratio": turnover_ratio,
                },
            )
        return result

    # -- ratios ------------------------------------------------------------- #

    def _cost_ratio(self, reference: BacktestResult, start: date, end: date) -> float | None:
        """Live cost per unit traded, over the reference's (Section 10 definition)."""
        ref_traded = float(reference.metrics.get("traded_notional", 0.0))
        ref_cost = abs(float(reference.metrics.get("fees", 0.0))) + abs(
            float(reference.metrics.get("slippage", 0.0))
        )
        live_traded, live_cost = self._live_costs(start, end)
        if ref_traded <= 0 or live_traded <= 0 or ref_cost <= 0:
            return None
        return (live_cost / live_traded) / (ref_cost / ref_traded)

    def _turnover_ratio(self, reference: BacktestResult, start: date, end: date) -> float | None:
        ref_traded = float(reference.metrics.get("traded_notional", 0.0))
        live_traded, _ = self._live_costs(start, end)
        if ref_traded <= 0:
            return None
        return live_traded / ref_traded

    def _live_costs(self, start: date, end: date) -> tuple[float, float]:
        """(traded notional, fees + |slippage|) from the rebalance ledger."""
        summary = self.ctx.repos.rebalances.cost_summary(start, end)
        return summary["traded"], abs(summary["fees"]) + abs(summary["slippage"])


def correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson correlation, or None when it is not defined."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    xs, ys = list(a[-n:]), list(b[-n:])
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


__all__ = ["MIN_DAYS", "TrackingResult", "TrackingService", "correlation"]
