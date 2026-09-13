"""US-T09 AC 2 — rebalance hysteresis (PRD Section 5.8, Appendix C.6)."""

from __future__ import annotations

import pytest

from aegis.core.config import RebalanceConfig
from aegis.portfolio.hysteresis import (
    REASON_EXCEEDS_BAND,
    REASON_FLAT,
    REASON_OUT_OF_UNIVERSE,
    REASON_WITHIN_BAND,
    hysteresis_threshold,
    should_trade,
    trade_reason,
)
from tests.fixtures.appendix_c import C6_CASES, C6_EQUITY

CFG = RebalanceConfig()

#: The reason code each Appendix C.6 row proves, in fixture order.
C6_REASONS = [REASON_WITHIN_BAND, REASON_EXCEEDS_BAND, REASON_WITHIN_BAND, REASON_OUT_OF_UNIVERSE]


@pytest.mark.parametrize(
    ("target", "current", "in_universe", "expected", "why", "reason"),
    [(*case, reason) for case, reason in zip(C6_CASES, C6_REASONS, strict=True)],
)
def test_us_t09_ac2_appendix_c6_cases(
    target: float,
    current: float,
    in_universe: bool,
    expected: bool,
    why: str,
    reason: str,
) -> None:
    assert should_trade(target, current, C6_EQUITY, CFG, in_universe) is expected, why
    assert trade_reason(target, current, C6_EQUITY, CFG, in_universe) == reason, why


def test_us_t09_ac2_proportional_band_governs_large_targets() -> None:
    """At target 1 000 the 10 % band (100) is larger than 0.25 % of E (25)."""
    assert hysteresis_threshold(1_000.0, C6_EQUITY, CFG) == pytest.approx(100.0)
    assert not should_trade(1_000.0, 901.0, C6_EQUITY, CFG)
    assert should_trade(1_000.0, 899.0, C6_EQUITY, CFG)


def test_us_t09_ac2_equity_floor_governs_small_targets() -> None:
    """At target 20 the 10 % band is 2, so the 0.25 %-of-equity floor (25) binds."""
    assert hysteresis_threshold(20.0, C6_EQUITY, CFG) == pytest.approx(25.0)
    assert not should_trade(20.0, 0.0, C6_EQUITY, CFG)
    assert should_trade(30.0, 0.0, C6_EQUITY, CFG)


def test_us_t09_ac2_band_is_strict_not_inclusive() -> None:
    """Section 5.8 trades on ``|delta| > band``; exactly on the band is no trade."""
    assert not should_trade(1_000.0, 900.0, C6_EQUITY, CFG)


def test_us_t09_ac2_symbol_out_of_universe_always_trades_to_zero() -> None:
    """Even a delta far inside the band: an unmanaged position has no signal behind it."""
    assert should_trade(0.0, 1.0, C6_EQUITY, CFG, in_universe=False)
    assert should_trade(0.0, -1.0, C6_EQUITY, CFG, in_universe=False)
    assert trade_reason(0.0, 1.0, C6_EQUITY, CFG, in_universe=False) == REASON_OUT_OF_UNIVERSE


def test_us_t09_ac2_out_of_universe_and_already_flat_does_nothing() -> None:
    assert not should_trade(0.0, 0.0, C6_EQUITY, CFG, in_universe=False)
    assert trade_reason(0.0, 0.0, C6_EQUITY, CFG, in_universe=False) == REASON_FLAT


def test_flat_target_and_flat_position_is_not_a_trade() -> None:
    assert not should_trade(0.0, 0.0, C6_EQUITY, CFG)
    assert trade_reason(0.0, 0.0, C6_EQUITY, CFG) == REASON_FLAT


def test_closing_a_position_to_zero_inside_the_universe_uses_the_equity_floor() -> None:
    """Target 0 kills the proportional band, so only the equity floor remains."""
    assert hysteresis_threshold(0.0, C6_EQUITY, CFG) == pytest.approx(25.0)
    assert not should_trade(0.0, 20.0, C6_EQUITY, CFG)
    assert should_trade(0.0, 30.0, C6_EQUITY, CFG)


def test_sign_flip_always_clears_the_band() -> None:
    assert should_trade(500.0, -500.0, C6_EQUITY, CFG)


def test_zero_equity_falls_back_to_the_proportional_band_alone() -> None:
    assert hysteresis_threshold(1_000.0, 0.0, CFG) == pytest.approx(100.0)
    assert should_trade(1_000.0, 800.0, 0.0, CFG)


def test_bands_are_configuration_not_literals() -> None:
    wide = RebalanceConfig(hysteresis_frac=0.50, hysteresis_equity_frac=0.10)
    assert should_trade(1_000.0, 850.0, C6_EQUITY, CFG)
    assert not should_trade(1_000.0, 850.0, C6_EQUITY, wide)
    assert hysteresis_threshold(1_000.0, C6_EQUITY, wide) == pytest.approx(1_000.0)


@pytest.mark.parametrize("target", [float("nan"), float("inf")])
def test_non_finite_target_never_forces_a_trade(target: float) -> None:
    """A broken target is not a reason to move the book."""
    assert not should_trade(target, 100.0, C6_EQUITY, CFG)
    assert trade_reason(target, 100.0, C6_EQUITY, CFG) == REASON_WITHIN_BAND
