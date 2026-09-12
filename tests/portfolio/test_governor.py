"""US-T08 — drawdown governor (PRD Section 5.7, Appendix C.5)."""

from __future__ import annotations

import math

import pytest

from aegis.core.config import GovernorConfig
from aegis.core.errors import ConfigError
from aegis.core.types import EquityPoint
from aegis.portfolio.governor import (
    drawdown_from_curve,
    governor,
    is_downward,
    time_weighted_points,
)
from tests.fixtures.appendix_c import C5_DD_SEQUENCE, C5_EXPECTED_G, C5_START_G

CFG = GovernorConfig()
DAY_MS = 86_400_000


def _curve(rows: list[tuple[float, float]]) -> list[EquityPoint]:
    """``[(equity, net_transfer), ...]`` on consecutive days."""
    return [
        EquityPoint(ts_ms=i * DAY_MS, equity=equity, net_transfer=transfer)
        for i, (equity, transfer) in enumerate(rows)
    ]


# --------------------------------------------------------------------------- #
# AC 1 — the Appendix C.5 walk
# --------------------------------------------------------------------------- #


def test_us_t08_ac1_appendix_c5_sequence_reproduces_exactly() -> None:
    g = C5_START_G
    walked: list[float] = []
    for dd in C5_DD_SEQUENCE:
        g = governor(dd, g, CFG)
        walked.append(g)
    assert walked == C5_EXPECTED_G


@pytest.mark.parametrize(
    ("step", "dd", "expected"),
    [(i, dd, g) for i, (dd, g) in enumerate(zip(C5_DD_SEQUENCE, C5_EXPECTED_G, strict=True))],
)
def test_us_t08_ac1_each_step_of_the_c5_walk(step: int, dd: float, expected: float) -> None:
    """Table-driven: step ``n`` is entered with the ``g`` step ``n-1`` produced."""
    before = C5_START_G if step == 0 else C5_EXPECTED_G[step - 1]
    assert governor(dd, before, CFG) == expected


@pytest.mark.parametrize(
    ("dd", "before", "after"),
    [
        (0.12, 1.0, 0.5),
        (0.20, 0.5, 0.25),
        (0.14, 0.25, 0.5),
        (0.07, 0.5, 1.0),
    ],
)
def test_us_t08_ac1_table_rows(dd: float, before: float, after: float) -> None:
    assert governor(dd, before, CFG) == after


@pytest.mark.parametrize(
    ("dd", "g"),
    [(0.119, 1.0), (0.199, 0.5), (0.15, 0.25), (0.08, 0.5), (0.16, 0.25)],
)
def test_us_t08_ac1_thresholds_are_inclusive_down_and_exclusive_up(dd: float, g: float) -> None:
    """``dd >= threshold`` cuts; ``dd < threshold`` restores. Just inside, nothing moves."""
    assert governor(dd, g, CFG) == g


# --------------------------------------------------------------------------- #
# State-machine properties
# --------------------------------------------------------------------------- #


def test_us_t08_one_transition_per_call_even_when_dd_gaps_through_both_cuts() -> None:
    """A 30 % drawdown from full risk steps 1.0 -> 0.5 -> 0.25, never straight to 0.25."""
    g1 = governor(0.30, 1.0, CFG)
    assert g1 == 0.5
    g2 = governor(0.30, g1, CFG)
    assert g2 == 0.25
    assert governor(0.30, g2, CFG) == 0.25


def test_us_t08_one_transition_per_call_on_the_way_up() -> None:
    g1 = governor(0.0, 0.25, CFG)
    assert g1 == 0.5
    assert governor(0.0, g1, CFG) == 1.0


def test_us_t08_thresholds_are_configuration_not_literals() -> None:
    """Guards against hard-coding: moving the threshold moves the transition."""
    loose = GovernorConfig(down={0.30: 0.5, 0.40: 0.25}, up={0.35: 0.5, 0.25: 1.0})
    assert governor(0.12, 1.0, loose) == 1.0
    assert governor(0.30, 1.0, loose) == 0.5
    assert governor(0.24, 0.5, loose) == 1.0


def test_us_t08_ac2_is_downward_flags_the_immediate_cuts() -> None:
    assert is_downward(1.0, 0.5)
    assert is_downward(0.5, 0.25)
    assert not is_downward(0.25, 0.5)
    assert not is_downward(0.5, 1.0)
    assert not is_downward(0.5, 0.5)


def test_us_t08_ac2_every_c5_downward_step_is_flagged() -> None:
    g = C5_START_G
    cuts = []
    for dd in C5_DD_SEQUENCE:
        new = governor(dd, g, CFG)
        if is_downward(g, new):
            cuts.append((dd, g, new))
        g = new
    assert cuts == [(0.12, 1.0, 0.5), (0.20, 0.5, 0.25)]


def test_non_finite_drawdown_holds_the_current_multiplier() -> None:
    """A broken equity curve is not evidence that risk is safe — never restores."""
    assert governor(math.nan, 0.5, CFG) == 0.5


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_invalid_current_multiplier_raises_config_error(bad: float) -> None:
    with pytest.raises(ConfigError):
        governor(0.1, bad, CFG)


def test_a_cut_beats_a_restore_when_both_would_apply() -> None:
    """Invariant 1: the engine may only reduce risk on its own."""
    overlapping = GovernorConfig(down={0.10: 0.5}, up={0.20: 1.0})
    assert governor(0.15, 1.0, overlapping) == 0.5


# --------------------------------------------------------------------------- #
# AC 3 — time-weighted peak
# --------------------------------------------------------------------------- #


def test_us_t08_ac3_drawdown_without_transfers() -> None:
    dd = drawdown_from_curve(_curve([(100.0, 0.0), (120.0, 0.0), (90.0, 0.0)]))
    assert dd == pytest.approx(0.25)


def test_us_t08_ac3_mid_period_deposit_creates_no_fake_peak() -> None:
    """100 -> 90 is a 10 % drawdown; doubling the account with cash keeps it at 10 %."""
    flat = _curve([(100.0, 0.0), (90.0, 0.0)])
    deposited = _curve([(100.0, 0.0), (90.0, 0.0), (180.0, 90.0)])
    assert drawdown_from_curve(flat) == pytest.approx(0.10)
    assert drawdown_from_curve(deposited) == pytest.approx(0.10)
    # The naive equity-only reading (peak 180, equity 180) would erase it entirely.


def test_us_t08_ac3_mid_period_withdrawal_creates_no_fake_drawdown() -> None:
    withdrawn = _curve([(100.0, 0.0), (90.0, 0.0), (45.0, -45.0)])
    assert drawdown_from_curve(withdrawn) == pytest.approx(0.10)
    # Equity alone (peak 100, equity 45) would report 55 % and cut the book for nothing.


def test_us_t08_ac3_deposit_at_the_peak_does_not_lift_the_peak() -> None:
    curve = _curve([(100.0, 0.0), (1_000.0, 900.0), (900.0, 0.0)])
    points = time_weighted_points(curve)
    assert [p.twr_factor for p in points] == pytest.approx([1.0, 1.0, 0.9])
    assert drawdown_from_curve(curve) == pytest.approx(0.10)


def test_us_t08_ac3_twr_index_is_the_running_product_of_factors() -> None:
    points = time_weighted_points(_curve([(100.0, 0.0), (110.0, 0.0), (99.0, 0.0)]))
    assert [p.twr_factor for p in points] == pytest.approx([1.0, 1.1, 0.9])
    assert [p.twr_index for p in points] == pytest.approx([1.0, 1.1, 0.99])


def test_us_t08_ac3_time_weighting_is_idempotent() -> None:
    once = time_weighted_points(_curve([(100.0, 0.0), (90.0, 0.0), (180.0, 90.0)]))
    twice = time_weighted_points(once)
    assert [p.twr_index for p in twice] == pytest.approx([p.twr_index for p in once])


def test_us_t08_ac3_empty_and_single_point_curves() -> None:
    assert drawdown_from_curve([]) == 0.0
    assert drawdown_from_curve(_curve([(100.0, 0.0)])) == 0.0


def test_us_t08_ac3_zero_equity_start_does_not_divide_by_zero() -> None:
    points = time_weighted_points(_curve([(0.0, 0.0), (100.0, 100.0), (80.0, 0.0)]))
    assert all(math.isfinite(p.twr_factor) and math.isfinite(p.twr_index) for p in points)
    assert points[1].twr_factor == 1.0
    assert drawdown_from_curve(_curve([(0.0, 0.0), (100.0, 100.0), (80.0, 0.0)])) == pytest.approx(0.20)


def test_us_t08_ac3_total_loss_is_a_100_percent_drawdown_not_more() -> None:
    dd = drawdown_from_curve(_curve([(100.0, 0.0), (0.0, 0.0)]))
    assert dd == pytest.approx(1.0)


def test_us_t08_ac3_drawdown_feeds_the_governor() -> None:
    """The two halves of 5.7 compose: curve -> dd -> g."""
    curve = _curve([(100.0, 0.0), (85.0, 0.0)])
    assert governor(drawdown_from_curve(curve), 1.0, CFG) == 0.5


def test_us_t08_ac3_an_equity_path_that_goes_negative_clamps_at_a_total_loss() -> None:
    """A negative margin balance is a wipe-out, not a negative return to compound."""
    points = time_weighted_points(_curve([(100.0, 0.0), (-50.0, 0.0), (10.0, 60.0)]))
    assert points[1].twr_factor == 0.0
    assert all(p.twr_index >= 0.0 for p in points)
    assert drawdown_from_curve(_curve([(100.0, 0.0), (-50.0, 0.0)])) == pytest.approx(1.0)
