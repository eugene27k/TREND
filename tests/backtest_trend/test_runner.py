"""US-T16 AC 3 — the run orchestrator and the evidence it stores for P0."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aegis.backtest_trend.runner import BacktestRunner
from aegis.core.types import Strategy
from aegis.storage.db import json_loads, open_db
from aegis.storage.repositories import Repositories
from tests.backtest_trend.conftest import DAYS, START

WARM = START + timedelta(days=430)
END = START + timedelta(days=DAYS - 1)
SHORT_END = WARM + timedelta(days=200)

#: A 3-point grid keeps the walk-forward test fast; the real grid has 27 points.
TINY_GRID = [
    ("default|0.25|0.10", {}),
    (
        "fast|0.20|0.05",
        {
            "signal": {"pairs": ((4, 12), (8, 24), (16, 48))},
            "sizing": {"sigma_target_asset": 0.20},
            "rebalance": {"hysteresis_frac": 0.05},
        },
    ),
    (
        "slow|0.30|0.20",
        {
            "signal": {"pairs": ((16, 48), (32, 96), (64, 192))},
            "sizing": {"sigma_target_asset": 0.30},
            "rebalance": {"hysteresis_frac": 0.20},
        },
    ),
]


@pytest.fixture
def repos():
    db = open_db(":memory:")
    yield Repositories(db, Strategy.TREND)
    db.close()


@pytest.fixture(scope="module")
def outcome(bt_cfg, market):
    bars, funding, _ = market
    cfg = bt_cfg.model_copy(
        update={"backtest": bt_cfg.backtest.model_copy(update={"bootstrap_resamples": 2_000})}
    )
    runner = BacktestRunner(cfg, bars, funding, initial_equity=10_000.0)
    return runner.run(WARM, SHORT_END, with_walkforward=False)


def test_the_run_produces_every_piece_of_p0_evidence(outcome):
    e = outcome.p0_evidence
    for key in (
        "annualised_return",
        "sharpe",
        "max_drawdown",
        "positive_year_fraction",
        "max_symbol_contribution",
        "robustness_sign_ok",
        "turnover_annualised",
    ):
        assert key in e, key
        assert e[key] is not None, key


def test_every_robustness_variant_is_run(outcome):
    from aegis.backtest_trend.robustness import VARIANTS

    assert {r.variant for r in outcome.robustness} == set(VARIANTS)


def test_the_bootstrap_distribution_is_produced(outcome):
    assert 5.0 in outcome.bootstrap
    assert outcome.bootstrap[5.0] < outcome.bootstrap[95.0]


def test_contribution_shares_sum_to_one(outcome):
    if outcome.contribution:
        assert sum(abs(v) for v in outcome.contribution.values()) == pytest.approx(1.0)
        assert outcome.p0_evidence["max_symbol_contribution"] <= 1.0


def test_the_run_persists_everything_under_one_run_id(bt_cfg, market, repos):
    bars, funding, _ = market
    cfg = bt_cfg.model_copy(
        update={"backtest": bt_cfg.backtest.model_copy(update={"bootstrap_resamples": 500})}
    )
    runner = BacktestRunner(cfg, bars, funding, repo=repos.backtest)
    out = runner.run(WARM, WARM + timedelta(days=120), with_walkforward=False)

    stored = repos.backtest.get_run(out.result.run_id)
    assert stored is not None
    assert stored["variant"] == "default"
    assert json_loads(stored["manifest_json"], {})["parameters"]["signal"]["price_std_window"] == 63
    assert len(repos.backtest.robustness(out.result.run_id)) == len(out.robustness)
    assert repos.backtest.bootstrap(out.result.run_id, "3m")
    assert repos.backtest.latest_run()["run_id"] == out.result.run_id


def test_walkforward_rows_are_stored_and_rank_the_default(bt_cfg, market, repos):
    bars, funding, _ = market
    cfg = bt_cfg.model_copy(
        update={"backtest": bt_cfg.backtest.model_copy(update={"bootstrap_resamples": 200})}
    )
    runner = BacktestRunner(cfg, bars, funding, repo=repos.backtest)
    out = runner.run(WARM, END, with_robustness=False, walkforward_grid=TINY_GRID)

    assert out.walkforward, "the window range must produce at least one test window"
    rows = repos.backtest.walkforward(out.result.run_id)
    assert rows
    assert {r["param_set"] for r in rows} == {n for n, _ in TINY_GRID}
    for ranking in out.walkforward:
        assert 1 <= ranking.default_rank <= ranking.n_params
    assert out.p0_evidence["walkforward"]["n_params"] == len(TINY_GRID)
    assert isinstance(out.p0_evidence["walkforward_ok"], bool)


def test_yearly_pnl_is_broken_out_for_the_60_pct_criterion(outcome):
    yearly = outcome.p0_evidence["yearly_pnl"]
    assert yearly
    assert all(len(year) == 4 and year.isdigit() for year in yearly)
    fraction = outcome.p0_evidence["positive_year_fraction"]
    assert 0.0 <= fraction <= 1.0


def test_a_robustness_failure_is_named_not_just_counted(outcome):
    assert isinstance(outcome.p0_evidence["robustness_failures"], list)
    for name in outcome.p0_evidence["robustness_failures"]:
        assert name in {r.variant for r in outcome.robustness}
