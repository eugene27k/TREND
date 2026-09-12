"""US-T12 — exposure and margin supervision."""

from __future__ import annotations

import pytest

from aegis.core.types import RiskStatus, Targets
from aegis.strategy_trend.risk_supervisor import RiskSupervisor

E = 10_000.0


@pytest.fixture
def book(ctx):
    """A helper that puts a book on the venue and returns the supervisor."""

    def _build(notionals: dict[str, float], *, equity: float = E, maint: float = 0.0,
               adl: dict[str, int] | None = None):
        ctx.gateway.set_account(wallet_balance=equity, margin_balance=equity,
                                available_balance=equity, maint_margin=maint)
        for symbol, notional in notionals.items():
            ctx.gateway.set_symbol_info(symbol)
            ctx.gateway.set_mark(symbol, 100.0)
            ctx.gateway.set_position(symbol, qty=notional / 100.0, entry_price=100.0)
            if (adl or {}).get(symbol):
                ctx.gateway.set_adl_quantile(symbol, adl[symbol])
        return RiskSupervisor(ctx)

    return _build


def test_us_t12_ac1_green_when_every_cap_is_satisfied(book, ctx):
    # 0.20x and 0.15x singles, gross 0.35x, net 0.05x — inside all three caps.
    sup = book({"BTCUSDT": 2_000.0, "ETHUSDT": -1_500.0})
    s = sup.check(ctx.clock.now_ms())
    assert s.status is RiskStatus.GREEN
    assert s.breaches == ()
    assert s.gross == pytest.approx(3_500.0)
    assert s.net == pytest.approx(500.0)
    assert (s.n_long, s.n_short) == (1, 1)


def test_us_t12_ac1_amber_on_a_gross_cap_breach(book, ctx):
    # gross 26 000 = 2.6x equity, above the 2.5x cap
    sup = book({"BTCUSDT": 13_000.0, "ETHUSDT": -13_000.0})
    s = sup.check(ctx.clock.now_ms())
    assert s.status is RiskStatus.AMBER
    assert "gross" in s.breaches
    assert any(a["code"] == "CAP_BREACH" for a in ctx.repos.alerts.recent())


def test_us_t12_ac1_amber_on_a_net_cap_breach(book, ctx):
    sup = book({"BTCUSDT": 16_000.0})   # net 1.6x > 1.5x cap
    s = sup.check(ctx.clock.now_ms())
    assert "net" in s.breaches
    assert s.status is RiskStatus.AMBER


def test_us_t12_ac1_amber_on_a_single_cap_breach(book, ctx):
    sup = book({"BTCUSDT": 3_000.0})    # 0.30x > 0.25x cap
    s = sup.check(ctx.clock.now_ms())
    assert s.breaches == ("single:BTCUSDT",)


def test_us_t12_ac1_margin_bands(book, ctx):
    sup = book({"BTCUSDT": 1_000.0}, maint=2_100.0)     # ratio 21 %
    assert sup.check(ctx.clock.now_ms()).status is RiskStatus.AMBER

    sup = book({"BTCUSDT": 1_000.0}, maint=3_600.0)     # ratio 36 %
    s = sup.check(ctx.clock.now_ms())
    assert s.status is RiskStatus.RED
    assert any(a["code"] == "MARGIN_RED" for a in ctx.repos.alerts.recent())


def test_us_t12_ac2_red_reduces_every_position_by_25_pct(book, ctx):
    sup = book({"BTCUSDT": 1_000.0, "ETHUSDT": -800.0}, maint=4_000.0)
    s = sup.check(ctx.clock.now_ms())
    cuts = sup.reductions(s, ctx.gateway.positions())
    assert {c.symbol for c in cuts} == {"BTCUSDT", "ETHUSDT"}
    assert all(c.fraction == pytest.approx(0.25) for c in cuts)
    assert all(c.reason == "margin_red" for c in cuts)


def test_us_t12_ac2_single_cap_breach_trims_only_the_excess(book, ctx):
    # 4 000 on a 2 500 cap -> trim 37.5 % so the leg lands exactly on the cap.
    sup = book({"BTCUSDT": 4_000.0})
    s = sup.check(ctx.clock.now_ms())
    cuts = sup.reductions(s, ctx.gateway.positions())
    assert len(cuts) == 1
    assert cuts[0].symbol == "BTCUSDT"
    assert cuts[0].fraction == pytest.approx(1.0 - 2_500.0 / 4_000.0)
    remaining = 4_000.0 * (1.0 - cuts[0].fraction)
    assert remaining == pytest.approx(0.25 * E)


def test_us_t12_ac2_no_reductions_when_green(book, ctx):
    sup = book({"BTCUSDT": 1_000.0})
    s = sup.check(ctx.clock.now_ms())
    assert sup.reductions(s, ctx.gateway.positions()) == []


def test_us_t12_ac3_survivable_move_on_a_flat_book_is_total(book, ctx):
    sup = book({})
    assert sup.survivable_move({}, E) == 1.0


def test_us_t12_ac3_survivable_move_shrinks_as_the_net_book_grows(book, ctx):
    sup = book({})
    positions = ctx.gateway.positions()
    small = sup.survivable_move({}, E)
    ctx.gateway.set_symbol_info("BTCUSDT")
    ctx.gateway.set_mark("BTCUSDT", 100.0)
    ctx.gateway.set_position("BTCUSDT", qty=150.0, entry_price=100.0)   # net 1.5x
    positions = ctx.gateway.positions()
    big = sup.survivable_move(positions, E)
    assert big < small
    # equity 10 000, net 15 000, maint 0.005*15 000 = 75  ->  (10000-75)/15000
    assert big == pytest.approx((E - 0.005 * 15_000.0) / 15_000.0)


def test_us_t12_ac3_a_book_at_the_net_cap_survives_the_40_pct_shock(book, ctx):
    """PRD: 'tested with a book at net 1.5 E'.

    At the net cap the rule does NOT fire, and that is the point worth pinning:
    a 40 % adverse move on 1.5x net costs 0.6 E and leaves 0.4 E, so the caps
    themselves already guarantee survival. The rule earns its keep only once the
    book has drifted outside the caps on price moves — see the next test — which
    is exactly the situation the 60 s supervisor exists to catch.
    """
    sup = book({"BTCUSDT": 15_000.0})
    positions = ctx.gateway.positions()
    survivable = sup.survivable_move(positions, E)
    assert survivable == pytest.approx((E - 0.005 * 15_000.0) / 15_000.0)
    assert survivable > ctx.cfg.risk.shock_price
    assert sup.survives_downtime(positions, E)


def test_us_t12_ac3_a_book_far_outside_the_net_cap_fails_the_shock_rule(book, ctx):
    # net 2.6x equity: 40 % of it is 1.04 E — more than the account has.
    sup = book({"BTCUSDT": 26_000.0})
    positions = ctx.gateway.positions()
    assert sup.survivable_move(positions, E) < ctx.cfg.risk.shock_price
    assert not sup.survives_downtime(positions, E)


def test_us_t12_ac3_proposed_targets_that_breach_the_rule_are_refused(ctx):
    sup = RiskSupervisor(ctx)
    from aegis.core.types import SymbolTarget

    breaching = Targets((SymbolTarget("BTCUSDT", 1.0, 0.5, 26_000.0, 26_000.0),),
                        0.2, 1.0, 0.2, 1.0, 1.0, E)
    safe = Targets((SymbolTarget("BTCUSDT", 1.0, 0.5, 2_000.0, 2_000.0),),
                   0.2, 1.0, 0.2, 1.0, 1.0, E)
    assert sup.would_breach_downtime_rule(breaching, E)
    assert not sup.would_breach_downtime_rule(safe, E)


def test_us_t12_ac3_zero_equity_is_treated_as_a_breach(ctx):
    sup = RiskSupervisor(ctx)
    assert sup.survivable_move({}, 0.0) == 0.0
    assert sup.would_breach_downtime_rule(Targets((), 0.0, 0.0, 0.0, 0.0, 1.0, 0.0), 0.0)


def test_us_t12_ac4_short_at_adl_quantile_4_is_reduced_25_pct(book, ctx):
    sup = book({"BTCUSDT": -1_000.0}, adl={"BTCUSDT": 4})
    cuts = sup.adl_reductions(ctx.gateway.positions())
    assert len(cuts) == 1
    assert cuts[0].fraction == pytest.approx(0.25)
    assert any(a["code"] == "ADL_QUANTILE" for a in ctx.repos.alerts.recent())


def test_us_t12_ac4_longs_and_low_quantiles_are_left_alone(book, ctx):
    sup = book({"BTCUSDT": 1_000.0, "ETHUSDT": -1_000.0},
               adl={"BTCUSDT": 5, "ETHUSDT": 3})
    assert sup.adl_reductions(ctx.gateway.positions()) == []


def test_us_t12_ac5_bnb_balance_below_the_floor_alerts(book, ctx):
    from aegis.core.types import IncomeType, LedgerEntry, Strategy

    sup = book({"BTCUSDT": 1_000.0})
    now = ctx.clock.now_ms()
    # 30 USDT of commission over 30 days = 1/day; 2 BNB-equivalent covers 2 days.
    ctx.repos.ledger.add_many([
        LedgerEntry(Strategy.TREND, now - 86_400_000, IncomeType.COMMISSION, "USDT", -30.0,
                    "BTCUSDT", "t1")
    ])
    ctx.gateway.set_bnb_balance(2.0)
    days = sup.check_bnb_balance(now)
    assert days == pytest.approx(2.0)
    assert any(a["code"] == "BNB_LOW" for a in ctx.repos.alerts.recent())


def test_bnb_check_returns_none_without_fee_history(book, ctx):
    sup = book({"BTCUSDT": 1_000.0})
    assert sup.check_bnb_balance(ctx.clock.now_ms()) is None


def test_check_persists_a_snapshot(book, ctx):
    sup = book({"BTCUSDT": 1_000.0})
    sup.check(ctx.clock.now_ms())
    latest = ctx.repos.snapshots.latest()
    assert latest is not None
    assert latest["gross_notional"] == pytest.approx(1_000.0)


def test_supervisor_never_places_an_order(book, ctx):
    """Invariant 1: the supervisor observes and asks; it does not trade."""
    sup = book({"BTCUSDT": 4_000.0}, maint=4_000.0)
    s = sup.check(ctx.clock.now_ms())
    sup.reductions(s, ctx.gateway.positions())
    sup.adl_reductions(ctx.gateway.positions())
    assert ctx.gateway.open_orders() == []
    assert ctx.repos.orders.open_orders() == []


def test_us_t12_ac2_gross_breach_scales_the_whole_book_proportionally(book, ctx):
    # gross 30 000 = 3.0x on a 2.5x cap; singles are inside 0.25x so only gross binds.
    legs = {f"S{i}USDT": (2_500.0 if i % 2 == 0 else -2_500.0) for i in range(12)}
    sup = book(legs)
    s = sup.check(ctx.clock.now_ms())
    assert "gross" in s.breaches
    cuts = {c.symbol: c.fraction for c in sup.reductions(s, ctx.gateway.positions())}
    assert len(cuts) == 12
    # every leg is cut by the same proportion, landing gross exactly on the cap
    assert len({round(f, 9) for f in cuts.values()}) == 1
    remaining = sum(abs(n) * (1 - cuts[sym]) for sym, n in legs.items())
    assert remaining == pytest.approx(ctx.cfg.caps.gross * E)


def test_us_t12_ac2_net_breach_scales_only_the_dominant_side(book, ctx):
    # net +16 000 = 1.6x on a 1.5x cap; gross 18 000 = 1.8x is inside 2.5x.
    legs = {"AUSDT": 2_000.0, "BUSDT": 2_000.0, "CUSDT": 2_000.0, "DUSDT": 2_000.0,
            "EUSDT": 2_000.0, "FUSDT": 2_000.0, "GUSDT": 2_000.0, "HUSDT": 2_000.0,
            "IUSDT": 2_000.0, "JUSDT": -1_000.0, "KUSDT": -1_000.0}
    sup = book(legs)
    s = sup.check(ctx.clock.now_ms())
    assert "net" in s.breaches and "gross" not in s.breaches
    cuts = {c.symbol: c.fraction for c in sup.reductions(s, ctx.gateway.positions())}
    assert set(cuts) == {k for k, v in legs.items() if v > 0}, "only the long side is trimmed"
    remaining_net = sum(n * (1 - cuts.get(sym, 0.0)) for sym, n in legs.items())
    assert remaining_net == pytest.approx(ctx.cfg.caps.net * E)


def test_us_t12_ac2_zero_equity_reads_as_red_and_reduces_everything(book, ctx):
    """Zero equity gives a margin ratio of 1.0, which is an emergency, not a no-op."""
    sup = book({"BTCUSDT": 1_000.0}, equity=0.0)
    s = sup.check(ctx.clock.now_ms())
    assert s.status is RiskStatus.RED
    assert [c.reason for c in sup.reductions(s, ctx.gateway.positions())] == ["margin_red"]


def test_us_t12_ac2_a_flat_book_yields_no_reductions(book, ctx):
    sup = book({}, equity=0.0)
    s = sup.check(ctx.clock.now_ms())
    assert sup.reductions(s, ctx.gateway.positions()) == []


def test_bnb_check_survives_a_gateway_failure(book, ctx):
    from aegis.core.errors import ExchangeUnreachable

    sup = book({"BTCUSDT": 1_000.0})
    ctx.gateway.inject_error("bnb_balance", ExchangeUnreachable("down"))
    assert sup.check_bnb_balance(ctx.clock.now_ms()) is None
