"""Orchestrates a full backtest run and stores its evidence (US-T16 AC 3).

One call produces everything the P0 gate reads: the default run, the PRD 11.5
robustness variants, the 11.6 walk-forward ranking, the bootstrap distribution
and the per-symbol contribution table — all persisted under one ``run_id`` with
a manifest, data checksums and the git commit, so that "which code and which
data produced this number" is always answerable.

Evaluating the gate is deliberately *not* done here: ``ops.phases`` reads the
stored evidence. Producing evidence and judging it are different jobs, and
keeping them apart means the gate cannot be quietly satisfied by the thing it is
supposed to be judging.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from aegis.backtest_trend import robustness as rb
from aegis.backtest_trend import walkforward as wf
from aegis.backtest_trend.bootstrap import block_bootstrap
from aegis.backtest_trend.simulator import BacktestResult, Simulator
from aegis.backtest_trend.universe_builder import PointInTimeUniverse, months_between
from aegis.core.clock import month_key
from aegis.core.config import AppConfig
from aegis.core.types import DailyBar, FundingRate, Strategy
from aegis.storage.repositories import BacktestRepo


def git_commit(cwd: str | None = None) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10, cwd=cwd, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass(frozen=True, slots=True)
class RunOutcome:
    result: BacktestResult
    robustness: tuple[rb.RobustnessRow, ...]
    walkforward: tuple[wf.WindowRanking, ...]
    bootstrap: dict[float, float]
    contribution: dict[str, float]
    p0_evidence: dict[str, Any]


class BacktestRunner:
    def __init__(
        self,
        cfg: AppConfig,
        bars: Mapping[str, Sequence[DailyBar]],
        funding: Mapping[str, Sequence[FundingRate]],
        *,
        initial_equity: float = 10_000.0,
        repo: BacktestRepo | None = None,
    ) -> None:
        self.cfg = cfg
        self.bars = dict(bars)
        self.funding = dict(funding)
        self.initial_equity = initial_equity
        self.repo = repo
        self.inventory = {
            s: sorted({month_key(b.day) for b in bl}) for s, bl in self.bars.items()
        }

    # -- pieces ------------------------------------------------------------- #

    def universes(self, cfg: AppConfig, start: date, end: date) -> dict[str, Any]:
        months = months_between(month_key(start), month_key(end))
        return PointInTimeUniverse(self.inventory, self.bars, cfg.universe).build(months)

    def simulate(self, cfg: AppConfig, start: date, end: date, *, variant: str = "default",
                 options: Mapping[str, Any] | None = None) -> BacktestResult:
        sim = Simulator(cfg, self.bars, self.funding, self.universes(cfg, start, end),
                        initial_equity=self.initial_equity,
                        fill_at=str((options or {}).get("fill_at", "open")))
        return sim.run(start, end, variant=variant)

    def run_robustness(self, start: date, end: date) -> tuple[dict[str, BacktestResult], list[rb.RobustnessRow]]:
        results: dict[str, BacktestResult] = {}
        for name, variant_cfg, options in rb.variant_configs(self.cfg):
            results[name] = self.simulate(variant_cfg, start, end, variant=name, options=options)
        return results, rb.evaluate(results)

    def run_walkforward(self, start: date, end: date,
                        grid: Sequence[tuple[str, dict[str, Any]]] | None = None,
                        ) -> list[wf.WindowRanking]:
        points = list(grid if grid is not None else rb.parameter_grid())

        def run(_name: str, overrides: dict[str, Any], test_start: date, test_end: date) -> float:
            cfg = rb.apply_overrides(self.cfg, overrides)
            result = self.simulate(cfg, test_start, test_end, variant="walkforward")
            return float(result.metrics.get("sharpe", 0.0))

        return [
            wf.rank_window(window, run, points)
            for window in wf.windows(start, end,
                                     train_months=self.cfg.backtest.walkforward_train_months,
                                     test_months=self.cfg.backtest.walkforward_test_months,
                                     step_months=self.cfg.backtest.walkforward_step_months)
        ]

    # -- the whole thing ---------------------------------------------------- #

    def run(self, start: date, end: date, *, with_robustness: bool = True,
            with_walkforward: bool = True,
            walkforward_grid: Sequence[tuple[str, dict[str, Any]]] | None = None) -> RunOutcome:
        base = self.simulate(self.cfg, start, end)

        rows: list[rb.RobustnessRow] = []
        if with_robustness:
            _, rows = self.run_robustness(start, end)

        rankings: list[wf.WindowRanking] = []
        if with_walkforward:
            rankings = self.run_walkforward(start, end, walkforward_grid)

        bt = self.cfg.backtest
        distribution = block_bootstrap(
            base.daily_returns, horizon_days=bt.bootstrap_block_days,
            block_days=bt.bootstrap_block_days, resamples=bt.bootstrap_resamples,
            seed=bt.bootstrap_seed, starting_equity=self.initial_equity,
        )
        contribution = self._contribution(base)
        evidence = self._p0_evidence(base, rows, rankings, contribution)

        if self.repo is not None:
            self._persist(base, rows, rankings, distribution, evidence)
        return RunOutcome(base, tuple(rows), tuple(rankings), distribution, contribution, evidence)

    # -- evidence ----------------------------------------------------------- #

    @staticmethod
    def _contribution(result: BacktestResult) -> dict[str, float]:
        """Net P&L share per symbol — the P0 'no single symbol > 50 %' input."""
        totals: dict[str, float] = {}
        for trade in result.trades:
            totals[str(trade["symbol"])] = totals.get(str(trade["symbol"]), 0.0) + float(trade["pnl"])
        gross = sum(abs(v) for v in totals.values())
        if gross <= 0:
            return {}
        return {s: v / gross for s, v in sorted(totals.items())}

    @staticmethod
    def _yearly_pnl(result: BacktestResult) -> dict[int, float]:
        by_year: dict[int, tuple[float, float]] = {}
        for point in result.equity:
            year = int(str(point["day"])[:4])
            equity = float(point["equity"])
            first, _ = by_year.get(year, (equity, equity))
            by_year[year] = (first, equity)
        return {y: last - first for y, (first, last) in by_year.items()}

    def _p0_evidence(self, base: BacktestResult, rows: Sequence[rb.RobustnessRow],
                     rankings: Sequence[wf.WindowRanking],
                     contribution: Mapping[str, float]) -> dict[str, Any]:
        """Every P0 criterion with its measured value (Section 7). Judging is ops.phases' job."""
        metrics = base.metrics
        yearly = self._yearly_pnl(base)
        positive_years = sum(1 for v in yearly.values() if v > 0)
        concentration = max((abs(v) for v in contribution.values()), default=0.0)
        return {
            "annualised_return": metrics.get("annualised_return"),
            "sharpe": metrics.get("sharpe"),
            "max_drawdown": metrics.get("max_drawdown"),
            "positive_year_fraction": (positive_years / len(yearly)) if yearly else None,
            "yearly_pnl": {str(k): v for k, v in sorted(yearly.items())},
            "max_symbol_contribution": concentration,
            "robustness_sign_ok": rb.gate_passes(list(rows)) if rows else None,
            "robustness_failures": [r.variant for r in rows if r.is_gate and not r.sign_ok],
            "turnover_annualised": metrics.get("turnover_annualised"),
            "walkforward": wf.summary(list(rankings)) if rankings else None,
            "walkforward_ok": wf.gate_passes(list(rankings),
                                             self.cfg.backtest.walkforward_top_half_frac)
            if rankings else None,
            "halted_on": base.halted_on.isoformat() if base.halted_on else None,
        }

    def _persist(self, base: BacktestResult, rows: Sequence[rb.RobustnessRow],
                 rankings: Sequence[wf.WindowRanking], distribution: dict[float, float],
                 evidence: dict[str, Any]) -> None:
        assert self.repo is not None
        metrics = dict(base.metrics)
        metrics["p0_evidence"] = evidence  # type: ignore[assignment]
        self.repo.save_run(
            base.run_id, created_ts=0, start_day=base.start_day.isoformat(),
            end_day=base.end_day.isoformat(), variant=base.variant,
            params=self.cfg.parameter_snapshot(), manifest=base.manifest,
            git_commit=git_commit(), metrics=metrics, equity=base.equity,
            duration_s=base.duration_s,
        )
        if rows:
            self.repo.save_robustness(base.run_id, [r.as_row() for r in rows])
        if rankings:
            self.repo.save_walkforward(base.run_id, wf.rows_for_storage(rankings))
        if distribution:
            self.repo.save_bootstrap(base.run_id, "3m", distribution)


def strategy_of(cfg: AppConfig) -> Strategy:
    return cfg.strategy


__all__ = ["BacktestRunner", "RunOutcome", "git_commit", "strategy_of"]
