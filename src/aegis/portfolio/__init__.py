"""Portfolio construction (PRD Sections 5.5-5.8).

Pure functions only: no clock, no I/O, no positions. Invariant 8 — exposure is
bounded by construction — is enforced inside ``size_targets``, before any order
object exists.
"""

from aegis.portfolio.funding_overlay import (
    HOURS_PER_YEAR,
    NO_HAIRCUT,
    annualise_funding,
    apply_funding_overlay,
)
from aegis.portfolio.governor import (
    FULL_RISK,
    drawdown_from_curve,
    governor,
    is_downward,
    time_weighted_points,
)
from aegis.portfolio.hysteresis import (
    REASON_EXCEEDS_BAND,
    REASON_FLAT,
    REASON_OUT_OF_UNIVERSE,
    REASON_WITHIN_BAND,
    hysteresis_threshold,
    should_trade,
    trade_reason,
)
from aegis.portfolio.sizing import (
    CAP_GROSS,
    CAP_NET,
    CAP_ORDER,
    CAP_SINGLE,
    check_caps,
    size_targets,
)

__all__ = [
    "CAP_GROSS",
    "CAP_NET",
    "CAP_ORDER",
    "CAP_SINGLE",
    "FULL_RISK",
    "HOURS_PER_YEAR",
    "NO_HAIRCUT",
    "REASON_EXCEEDS_BAND",
    "REASON_FLAT",
    "REASON_OUT_OF_UNIVERSE",
    "REASON_WITHIN_BAND",
    "annualise_funding",
    "apply_funding_overlay",
    "check_caps",
    "drawdown_from_curve",
    "governor",
    "hysteresis_threshold",
    "is_downward",
    "should_trade",
    "size_targets",
    "time_weighted_points",
    "trade_reason",
]
