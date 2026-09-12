"""US-T07 — funding overlay (PRD Section 5.6)."""

from __future__ import annotations

import math

import pytest

from aegis.core.config import FundingConfig
from aegis.portfolio.funding_overlay import (
    HOURS_PER_YEAR,
    NO_HAIRCUT,
    annualise_funding,
    apply_funding_overlay,
)

CFG = FundingConfig()


def test_us_t07_ac1_long_in_expensive_funding_is_halved() -> None:
    notional, haircut = apply_funding_overlay(1_000.0, 0.35, CFG)
    assert notional == pytest.approx(500.0)
    assert haircut == 0.5


def test_us_t07_ac1_long_below_threshold_is_unchanged() -> None:
    notional, haircut = apply_funding_overlay(1_000.0, 0.25, CFG)
    assert notional == pytest.approx(1_000.0)
    assert haircut == NO_HAIRCUT


def test_us_t07_ac1_short_in_negative_funding_is_halved() -> None:
    notional, haircut = apply_funding_overlay(-1_000.0, -0.35, CFG)
    assert notional == pytest.approx(-500.0)
    assert haircut == 0.5


def test_us_t07_ac1_short_in_positive_funding_is_unchanged() -> None:
    """A short is *paid* by positive funding — only negative funding haircuts it."""
    notional, haircut = apply_funding_overlay(-1_000.0, 0.50, CFG)
    assert notional == pytest.approx(-1_000.0)
    assert haircut == NO_HAIRCUT


def test_us_t07_ac1_long_in_negative_funding_is_unchanged() -> None:
    notional, haircut = apply_funding_overlay(1_000.0, -0.50, CFG)
    assert notional == pytest.approx(1_000.0)
    assert haircut == NO_HAIRCUT


@pytest.mark.parametrize("target", [1_000.0, -1_000.0])
def test_us_t07_ac1_exact_threshold_does_not_haircut(target: float) -> None:
    """Section 5.6 says ``f > +30 %`` — strictly greater, so the boundary is safe."""
    funding = math.copysign(CFG.haircut_threshold, target)
    notional, haircut = apply_funding_overlay(target, funding, CFG)
    assert notional == pytest.approx(target)
    assert haircut == NO_HAIRCUT


def test_us_t07_ac1_just_past_threshold_haircuts() -> None:
    notional, haircut = apply_funding_overlay(1_000.0, CFG.haircut_threshold + 1e-9, CFG)
    assert notional == pytest.approx(500.0)
    assert haircut == 0.5


def test_us_t07_ac1_four_hour_interval_annualises_by_2190() -> None:
    assert HOURS_PER_YEAR / 4.0 == 2190.0
    assert annualise_funding(0.0001, 4.0) == pytest.approx(0.219)
    assert annualise_funding(0.0001, 8.0) == pytest.approx(0.0001 * 1095.0)


def test_us_t07_ac1_four_hour_interval_can_flip_the_overlay() -> None:
    """The same raw rate is harmless at 8 h and a haircut at 4 h."""
    rate = 0.00016  # 0.016 % per interval
    assert apply_funding_overlay(1_000.0, annualise_funding(rate, 8.0), CFG)[1] == NO_HAIRCUT
    assert apply_funding_overlay(1_000.0, annualise_funding(rate, 4.0), CFG)[1] == 0.5


@pytest.mark.parametrize("interval", [0.0, -8.0, math.inf, math.nan])
def test_annualise_rejects_unusable_interval(interval: float) -> None:
    """An unknown settlement interval must not be guessed into a haircut."""
    assert annualise_funding(0.01, interval) == 0.0


def test_annualise_rejects_non_finite_rate() -> None:
    assert annualise_funding(math.nan, 8.0) == 0.0


def test_zero_target_is_left_alone() -> None:
    assert apply_funding_overlay(0.0, 5.0, CFG) == (0.0, NO_HAIRCUT)


def test_non_finite_inputs_never_propagate() -> None:
    assert apply_funding_overlay(math.nan, 0.5, CFG) == (0.0, NO_HAIRCUT)
    assert apply_funding_overlay(1_000.0, math.nan, CFG) == (1_000.0, NO_HAIRCUT)


def test_haircut_is_configuration_not_a_literal() -> None:
    cfg = FundingConfig(haircut=0.25, haircut_threshold=0.10)
    notional, haircut = apply_funding_overlay(1_000.0, 0.20, cfg)
    assert notional == pytest.approx(250.0)
    assert haircut == 0.25
