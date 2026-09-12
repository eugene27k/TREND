"""Exception hierarchy. Every failure the engine can act on has a type."""

from __future__ import annotations


class AegisError(Exception):
    """Base for everything this codebase raises deliberately."""


class ConfigError(AegisError):
    """Invalid or unsafe configuration (also: missing LIVE_CONFIRM)."""


class GatewayError(AegisError):
    """Base for exchange-boundary failures."""


class ExchangeUnreachable(GatewayError):
    """Network/5xx/timeout — triggers safe mode (US-T13 AC 2)."""


class RateLimited(GatewayError):
    def __init__(self, message: str, retry_after_s: float = 1.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class OrderRejected(GatewayError):
    """The venue refused the order. ``code`` is the Binance error code."""

    def __init__(self, message: str, code: int = 0, *, client_order_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.client_order_id = client_order_id

    @property
    def is_reduce_only_violation(self) -> bool:
        # -2022 ReduceOnly Order is rejected; -4164/-1106 variants seen on testnet.
        return self.code in (-2022, -4164)

    @property
    def is_post_only_violation(self) -> bool:
        # -5022: Due to the order could not be executed as maker (GTX).
        return self.code == -5022


class InsufficientMargin(GatewayError):
    pass


class PermissionChanged(GatewayError):
    """API key lost futures permission or gained withdrawal permission."""


class ClockDrift(AegisError):
    def __init__(self, drift_ms: int) -> None:
        super().__init__(f"clock drift {drift_ms} ms exceeds tolerance")
        self.drift_ms = drift_ms


class ReconciliationBreak(AegisError):
    """Local state disagrees with the exchange (blocks risk-increasing orders)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class DataGap(AegisError):
    """A required daily bar or funding record is missing."""


class RiskHalt(AegisError):
    """A kill rule fired; the engine must flatten and stop trading."""


class PhaseGateFailed(AegisError):
    pass


__all__ = [
    "AegisError",
    "ClockDrift",
    "ConfigError",
    "DataGap",
    "ExchangeUnreachable",
    "GatewayError",
    "InsufficientMargin",
    "OrderRejected",
    "PermissionChanged",
    "PhaseGateFailed",
    "RateLimited",
    "ReconciliationBreak",
    "RiskHalt",
]
