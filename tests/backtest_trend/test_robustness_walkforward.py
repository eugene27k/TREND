"""PRD 11.5 and 11.6 — robustness variants and the walk-forward ranking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from aegis.backtest_trend import robustness as rb
from aegis.backtest_trend import walkforward as wf


@dataclass
class FakeResult:
    metrics: dict


def test_every_prd_11_5_variant_is_present():
    names = set(rb.VARIANTS)
    for expected in (
        "speeds_fast",
        "speeds_slow",
        "sigma_asset_down",
        "sigma_asset_up",
        "sigma_portfolio_down",
        "sigma_portfolio_up",
        "hysteresis_5",
        "hysteresis_20",
        "universe_12",
        "universe_20",
        "cost_x2",
        "governor_off",
        "rebalance_midday",
    ):
        assert expected in names, expected


def test_variants_produce_the_documented_parameter_changes(bt_cfg):
    by_name = {n: (c, o) for n, c, o in rb.variant_configs(bt_cfg)}
    assert by_name["speeds_fast"][0].signal.pairs == ((4, 12), (8, 24), (16, 48))
    assert by_name["speeds_slow"][0].signal.pairs == ((16, 48), (32, 96), (64, 192))
    assert by_name["sigma_asset_down"][0].sizing.sigma_target_asset == 0.175  # -30 %
    assert by_name["sigma_asset_up"][0].sizing.sigma_target_asset == 0.325  # +30 %
    assert by_name["sigma_portfolio_down"][0].sizing.sigma_target_portfolio == 0.14
    assert by_name["sigma_portfolio_up"][0].sizing.sigma_target_portfolio == 0.26
    assert by_name["hysteresis_5"][0].rebalance.hysteresis_frac == 0.05
    assert by_name["hysteresis_20"][0].rebalance.hysteresis_frac == 0.20
    assert by_name["universe_12"][0].universe.size == 12
    assert by_name["universe_20"][0].universe.size == 20
    assert by_name["cost_x2"][0].exec.slippage_for("BTCUSDT") == 4.0  # 2 x 2 bps
    assert by_name["cost_x2"][0].exec.slippage_for("SOLUSDT") == 12.0  # 2 x 6 bps
    assert by_name["governor_off"][0].governor.down == {}
    assert by_name["rebalance_midday"][1] == {"fill_at": "close"}


def test_applying_a_variant_does_not_mutate_the_original_config(bt_cfg):
    before = bt_cfg.signal.pairs
    rb.apply_overrides(bt_cfg, {"signal": {"pairs": ((1, 2),)}})
    assert bt_cfg.signal.pairs == before


def test_gate_passes_only_when_every_gated_variant_keeps_the_sign():
    results = {
        "default": FakeResult({"net_pnl": 1000.0, "sharpe": 1.0, "max_drawdown": 0.1}),
        "speeds_fast": FakeResult({"net_pnl": 500.0, "sharpe": 0.8, "max_drawdown": 0.2}),
    }
    assert rb.gate_passes(rb.evaluate(results))

    results["hysteresis_5"] = FakeResult({"net_pnl": -20.0, "sharpe": -0.1, "max_drawdown": 0.3})
    rows = rb.evaluate(results)
    assert not rb.gate_passes(rows)
    assert [r.variant for r in rows if not r.sign_ok] == ["hysteresis_5"]


def test_governor_off_is_reported_but_is_not_a_gate_condition():
    """PRD 11.5.6: 'to show the governor's contribution, not a gate condition'."""
    results = {
        "default": FakeResult({"net_pnl": 1000.0, "sharpe": 1.0, "max_drawdown": 0.1}),
        "governor_off": FakeResult({"net_pnl": -900.0, "sharpe": -0.5, "max_drawdown": 0.5}),
    }
    rows = rb.evaluate(results)
    assert rb.gate_passes(rows), "a governor_off flip must not fail P0"
    assert next(r for r in rows if r.variant == "governor_off").is_gate is False


def test_the_walkforward_grid_is_the_documented_3x3x3():
    grid = rb.parameter_grid()
    assert len(grid) == 27
    assert rb.DEFAULT_PARAM_SET in {n for n, _ in grid}
    overrides = dict(grid)[rb.DEFAULT_PARAM_SET]
    assert overrides["signal"]["pairs"] == ((8, 24), (16, 48), (32, 96))
    assert overrides["sizing"]["sigma_target_asset"] == 0.25
    assert overrides["rebalance"]["hysteresis_frac"] == 0.10


def test_windows_roll_12_month_train_and_6_month_test_by_6_months():
    ws = wf.windows(date(2021, 1, 1), date(2024, 1, 1))
    assert [w.name for w in ws][:3] == [
        "2022-01-01..2022-07-01",
        "2022-07-01..2023-01-01",
        "2023-01-01..2023-07-01",
    ]
    for w in ws:
        assert (w.test_start - w.train_start).days >= 364
        assert w.test_end > w.test_start


def test_windows_are_empty_when_there_is_not_a_year_of_history():
    assert wf.windows(date(2021, 1, 1), date(2021, 9, 1)) == []


def _rank_with(scores: dict[str, float]) -> wf.WindowRanking:
    grid = [(name, {}) for name in scores]
    window = wf.windows(date(2021, 1, 1), date(2022, 8, 1))[0]
    return wf.rank_window(window, lambda n, o, s, e: scores[n], grid)


def test_ranking_is_by_test_sharpe_descending_and_ties_break_by_name():
    ranking = _rank_with({"b": 1.0, "a": 1.0, rb.DEFAULT_PARAM_SET: 2.0})
    assert [r["param_set"] for r in ranking.rows] == [rb.DEFAULT_PARAM_SET, "a", "b"]
    assert ranking.default_rank == 1


def test_top_half_boundary_is_inclusive_for_an_odd_grid():
    # 5 params -> top half is ranks 1..3
    ranking = _rank_with({"a": 5.0, "b": 4.0, rb.DEFAULT_PARAM_SET: 3.0, "d": 2.0, "e": 1.0})
    assert ranking.default_rank == 3
    assert ranking.default_in_top_half

    ranking = _rank_with({"a": 5.0, "b": 4.0, "c": 3.5, rb.DEFAULT_PARAM_SET: 3.0, "e": 1.0})
    assert ranking.default_rank == 4
    assert not ranking.default_in_top_half


def test_us_t16_ac4_gate_needs_the_default_in_the_top_half_on_70_pct_of_windows():
    good = _rank_with({"a": 5.0, rb.DEFAULT_PARAM_SET: 4.0, "c": 1.0})
    bad = _rank_with({"a": 5.0, "b": 4.0, rb.DEFAULT_PARAM_SET: 1.0})
    assert wf.gate_passes([good] * 7 + [bad] * 3)
    assert not wf.gate_passes([good] * 6 + [bad] * 4)
    assert not wf.gate_passes([]), "no evidence is not a pass"


def test_storage_rows_carry_the_window_and_the_verdict():
    ranking = _rank_with({"a": 5.0, rb.DEFAULT_PARAM_SET: 4.0, "c": 1.0})
    rows = wf.rows_for_storage([ranking])
    assert len(rows) == 3
    assert all(r["window"] == ranking.window for r in rows)
    assert all(r["default_in_top_half"] is True for r in rows)
    assert sum(1 for r in rows if r["is_default"]) == 1
