"""Startup safety checks (US-T01 AC 4, CARRY US-01 AC 4-5).

These run before the engine can place an order, and they all fail *closed*: an
answer the venue will not positively confirm is treated as the dangerous one.
The cost of refusing to start is a minute of the operator's time; the cost of
starting with a withdrawal-capable key on the wrong account is the account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from aegis.core.config import AppConfig
from aegis.core.errors import ConfigError, PermissionChanged
from aegis.core.types import Mode
from aegis.gateway.base import ExchangeGateway

LIVE_CONFIRM_ENV = "LIVE_CONFIRM"
LIVE_CONFIRM_TOKEN = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def check_live_confirm(cfg: AppConfig, environ: dict[str, str] | None = None) -> CheckResult:
    """Live mode needs an explicit, typed-out confirmation in the environment."""
    if cfg.mode is not Mode.LIVE:
        return CheckResult("live_confirm", True, f"not required in {cfg.mode} mode")
    env = environ if environ is not None else dict(os.environ)
    token = env.get(LIVE_CONFIRM_ENV, "") or cfg.phase.live_confirm
    if token != LIVE_CONFIRM_TOKEN:
        return CheckResult(
            "live_confirm",
            False,
            f"{LIVE_CONFIRM_ENV} must be set to {LIVE_CONFIRM_TOKEN!r} to start in live mode",
        )
    return CheckResult("live_confirm", True, "confirmed")


def check_key_permissions(cfg: AppConfig, gateway: ExchangeGateway) -> CheckResult:
    """The key must reach futures and must NOT be able to withdraw."""
    if not cfg.mode.sends_real_orders:
        return CheckResult("key_permissions", True, f"not checked in {cfg.mode} mode")
    perms = gateway.key_permissions()
    if not perms.get("futures", False):
        return CheckResult("key_permissions", False, "the API key has no futures permission")
    if perms.get("withdraw", True):
        # Unknown counts as True: the gateway reports withdraw=True when it could
        # not positively confirm the permission is absent.
        return CheckResult(
            "key_permissions",
            False,
            "the API key can withdraw (or the permission could not be confirmed absent)",
        )
    if cfg.mode is Mode.LIVE and not perms.get("ip_restricted", False):
        return CheckResult("key_permissions", False, "the API key is not IP-allowlisted")
    return CheckResult("key_permissions", True, "futures-only, no withdrawal, IP-allowlisted")


def check_sub_account(cfg: AppConfig, gateway: ExchangeGateway) -> CheckResult:
    """US-T01 AC 4: the key must belong to the configured sub-account.

    Binance does not expose a sub-account name on the futures endpoints, so the
    gateway returns None when it cannot positively confirm one. In live mode that
    is a refusal — trading the wrong sub-account silently mixes two sleeves'
    capital, which Locked Decision 9 exists to prevent.
    """
    if not cfg.mode.sends_real_orders or not cfg.account.assert_sub_account:
        return CheckResult("sub_account", True, f"not checked in {cfg.mode} mode")
    actual = gateway.sub_account_name()
    expected = cfg.account.sub_account_name
    if actual is None:
        if cfg.mode is Mode.LIVE:
            return CheckResult("sub_account", False, f"could not confirm the key belongs to {expected!r}")
        return CheckResult("sub_account", True, "unconfirmable on testnet — allowed in demo")
    if actual != expected:
        return CheckResult("sub_account", False, f"key belongs to {actual!r}, expected {expected!r}")
    return CheckResult("sub_account", True, f"confirmed {expected!r}")


def check_clock(cfg: AppConfig, gateway: ExchangeGateway, now_ms: int) -> CheckResult:
    """A clock that disagrees with the venue makes every signed request fail."""
    if cfg.mode is Mode.PAPER or cfg.mode is Mode.BACKTEST:
        return CheckResult("clock", True, f"not checked in {cfg.mode} mode")
    drift = abs(gateway.server_time_ms() - now_ms)
    if drift > cfg.risk.clock_drift_ms:
        return CheckResult("clock", False, f"clock drift {drift} ms exceeds {cfg.risk.clock_drift_ms} ms")
    return CheckResult("clock", True, f"drift {drift} ms")


def run_checks(
    cfg: AppConfig, gateway: ExchangeGateway, now_ms: int, environ: dict[str, str] | None = None
) -> list[CheckResult]:
    return [
        check_live_confirm(cfg, environ),
        check_key_permissions(cfg, gateway),
        check_sub_account(cfg, gateway),
        check_clock(cfg, gateway, now_ms),
    ]


def assert_safe_to_start(
    cfg: AppConfig, gateway: ExchangeGateway, now_ms: int, environ: dict[str, str] | None = None
) -> list[CheckResult]:
    """Raise unless every check passed. Returns the results for logging."""
    results = run_checks(cfg, gateway, now_ms, environ)
    failures = [r for r in results if not r.passed]
    if failures:
        detail = "; ".join(f"{r.name}: {r.detail}" for r in failures)
        if any(r.name == "key_permissions" for r in failures):
            raise PermissionChanged(f"refusing to start — {detail}")
        raise ConfigError(f"refusing to start — {detail}")
    return results


__all__ = [
    "LIVE_CONFIRM_ENV",
    "LIVE_CONFIRM_TOKEN",
    "CheckResult",
    "assert_safe_to_start",
    "check_clock",
    "check_key_permissions",
    "check_live_confirm",
    "check_sub_account",
    "run_checks",
]
