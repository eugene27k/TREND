"""US-T01 AC 4 / CARRY US-01 AC 4-5 — the checks that must fail closed."""

from __future__ import annotations

import pytest

from aegis.core.errors import ConfigError, PermissionChanged
from aegis.core.types import Mode
from engine.startup import (
    LIVE_CONFIRM_ENV,
    LIVE_CONFIRM_TOKEN,
    assert_safe_to_start,
    run_checks,
)

CONFIRMED = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_TOKEN}


def live(cfg):
    return cfg.model_copy(update={"mode": Mode.LIVE})


def result(cfg, gateway, clock, name, environ=None):
    return next(r for r in run_checks(cfg, gateway, clock.now_ms(), environ or {}) if r.name == name)


def test_paper_mode_checks_nothing_at_the_venue(cfg, gateway, clock):
    assert all(r.passed for r in run_checks(cfg, gateway, clock.now_ms(), {}))


def test_live_refuses_to_start_without_the_typed_confirmation(cfg, gateway, clock):
    assert not result(live(cfg), gateway, clock, "live_confirm").passed
    with pytest.raises(ConfigError, match="LIVE_CONFIRM"):
        assert_safe_to_start(live(cfg), gateway, clock.now_ms(), {})


def test_live_refuses_a_wrong_confirmation_token(cfg, gateway, clock):
    assert not result(live(cfg), gateway, clock, "live_confirm", {LIVE_CONFIRM_ENV: "yes"}).passed


def test_live_starts_once_confirmed(cfg, gateway, clock):
    assert assert_safe_to_start(live(cfg), gateway, clock.now_ms(), CONFIRMED)


def test_a_key_that_can_withdraw_is_refused(cfg, gateway, clock):
    gateway.set_permissions(futures=True, withdraw=True)
    with pytest.raises(PermissionChanged, match="withdraw"):
        assert_safe_to_start(live(cfg), gateway, clock.now_ms(), CONFIRMED)


def test_an_unconfirmable_withdrawal_permission_is_treated_as_present(cfg, gateway, clock):
    """Fail closed: 'unknown' and 'yes' must be the same answer here."""
    gateway.set_permissions(futures=True, withdraw=True, ip_restricted=True)
    assert not result(live(cfg), gateway, clock, "key_permissions", CONFIRMED).passed


def test_a_key_without_futures_permission_is_refused(cfg, gateway, clock):
    gateway.set_permissions(futures=False, withdraw=False)
    with pytest.raises(PermissionChanged, match="futures"):
        assert_safe_to_start(live(cfg), gateway, clock.now_ms(), CONFIRMED)


def test_live_requires_an_ip_allowlisted_key(cfg, gateway, clock):
    gateway.set_permissions(futures=True, withdraw=False, ip_restricted=False)
    assert not result(live(cfg), gateway, clock, "key_permissions", CONFIRMED).passed


def test_demo_does_not_require_ip_allowlisting(cfg, gateway, clock):
    demo = cfg.model_copy(update={"mode": Mode.DEMO})
    gateway.set_permissions(futures=True, withdraw=False, ip_restricted=False)
    assert result(demo, gateway, clock, "key_permissions").passed


def test_us_t01_ac4_the_key_must_belong_to_the_configured_sub_account(cfg, gateway, clock):
    gateway.set_sub_account_name("carry-01")
    check = result(live(cfg), gateway, clock, "sub_account", CONFIRMED)
    assert not check.passed
    assert "carry-01" in check.detail and "trend-01" in check.detail


def test_live_refuses_an_unconfirmable_sub_account(cfg, gateway, clock):
    """Trading the wrong sub-account silently mixes two sleeves' capital."""
    gateway.set_sub_account_name(None)
    assert not result(live(cfg), gateway, clock, "sub_account", CONFIRMED).passed


def test_demo_allows_an_unconfirmable_sub_account(cfg, gateway, clock):
    demo = cfg.model_copy(update={"mode": Mode.DEMO})
    gateway.set_sub_account_name(None)
    assert result(demo, gateway, clock, "sub_account").passed


def test_the_assertion_can_be_switched_off_deliberately(cfg, gateway, clock):
    gateway.set_sub_account_name("carry-01")
    relaxed = live(cfg).model_copy(
        update={"account": cfg.account.model_copy(update={"assert_sub_account": False})}
    )
    assert result(relaxed, gateway, clock, "sub_account", CONFIRMED).passed


def test_clock_drift_beyond_tolerance_refuses_to_start(cfg, gateway, clock):
    gateway.set_server_time_offset_ms(5_000)
    assert not result(live(cfg), gateway, clock, "clock", CONFIRMED).passed
    with pytest.raises(ConfigError, match="drift"):
        assert_safe_to_start(live(cfg), gateway, clock.now_ms(), CONFIRMED)


def test_small_clock_drift_is_tolerated(cfg, gateway, clock):
    gateway.set_server_time_offset_ms(200)
    assert result(live(cfg), gateway, clock, "clock", CONFIRMED).passed


def test_every_failure_is_named_in_the_refusal(cfg, gateway, clock):
    gateway.set_permissions(futures=True, withdraw=False, ip_restricted=False)
    gateway.set_sub_account_name("carry-01")
    with pytest.raises(PermissionChanged) as exc:
        assert_safe_to_start(live(cfg), gateway, clock.now_ms(), CONFIRMED)
    assert "key_permissions" in str(exc.value)
    assert "sub_account" in str(exc.value)
