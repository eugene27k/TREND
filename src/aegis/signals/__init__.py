"""Section 5.3 trend signal engine — pure functions only (US-T04)."""

from aegis.signals.engine import (
    compute_signal,
    compute_signal_series,
    response,
    rolling_std,
    signal_snapshot_row,
)

__all__ = [
    "compute_signal",
    "compute_signal_series",
    "response",
    "rolling_std",
    "signal_snapshot_row",
]
