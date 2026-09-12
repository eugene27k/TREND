"""US-T06 — sizing, scaling and caps (PRD Section 5.5, Appendix C.3).

Invariant 8 lives in ``size_targets``: the caps bound exposure before any order
object exists. The hypothesis property at the bottom of this file is the
statement of that invariant, and it must never be weakened.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from aegis.core.config import AppConfig
from aegis.core.errors import ConfigError
from aegis.core.types import RiskModel, SymbolTarget, Targets
from aegis.portfolio.sizing import (
    CAP_GROSS,
    CAP_NET,
    CAP_ORDER,
    CAP_SINGLE,
    check_caps,
    size_targets,
)
from tests.fixtures.appendix_c import (
    C3_CONV,
    C3_CORR,
    C3_EQUITY,
    C3_G,
    C3_GROSS,
    C3_N,
    C3_NET,
    C3_RAW,
    C3_S,
    C3_S_UNCLIPPED,
    C3_SIGMA_EFF,
    C3_SIGMA_P,
    C3_SIGMA_TARGET_ASSET,
    C3_SIGMA_TARGET_PORTFOLIO,
    C3_SIGNALS,
    C3_SYMBOLS,
    C3_TARGETS,
    C3_VOLS,
    TOL,
)

# --------------------------------------------------------------------------- #
# Rigs
# --------------------------------------------------------------------------- #


def c3_config() -> AppConfig:
    """Appendix C.3 parameters, stated from the fixture rather than the defaults."""
    return AppConfig.model_validate(
        {
            "universe": {"size": C3_N},
            "sizing": {
                "sigma_target_asset": C3_SIGMA_TARGET_ASSET,
                "sigma_target_portfolio": C3_SIGMA_TARGET_PORTFOLIO,
                "conviction_full": 0.5,
                "s_max": 3.0,
                "min_target_frac": 0.001,
            },
        }
    )


def c3_risk_model() -> RiskModel:
    return RiskModel(symbols=C3_SYMBOLS, vols=dict(C3_VOLS), corr=C3_CORR, avg_corr=0.7)


#: A rig in which ``target_i == signal_i * equity`` before any cap.
#:
#: ``N = 1``, ``sigma_tgt = 1``, every vol 1.0 and an identity correlation, with
#: ``sigma_p,tgt`` large enough that the ``s_max`` clip always binds at 1.0. Cap
#: arithmetic is then readable straight off the signals, which is the only way to
#: assert *exact* post-cap notionals and an observable ordering.
_RIG_SIZING = {
    "sigma_target_asset": 1.0,
    "sigma_target_portfolio": 100.0,
    "conviction_full": 0.01,
    "s_max": 1.0,
    "min_target_frac": 0.0,
}


def rig_config(**caps: float) -> AppConfig:
    return AppConfig.model_validate(
        {"universe": {"size": 1}, "sizing": dict(_RIG_SIZING), "caps": caps or {}}
    )


def rig_risk_model(symbols: tuple[str, ...]) -> RiskModel:
    n = len(symbols)
    corr = tuple(tuple(1.0 if i == j else 0.0 for j in range(n)) for i in range(n))
    return RiskModel(symbols=symbols, vols=dict.fromkeys(symbols, 1.0), corr=corr, avg_corr=0.0)


def rig_book(desired: dict[str, float], equity: float, cfg: AppConfig) -> Targets:
    """Size a book whose *pre-cap* targets are exactly ``desired``."""
    signals = {s: v / equity for s, v in desired.items()}
    vols = dict.fromkeys(desired, 1.0)
    return size_targets(signals, vols, rig_risk_model(tuple(sorted(desired))), equity, 1.0, cfg)


def notionals(targets: Targets) -> dict[str, float]:
    return {t.symbol: t.target_notional for t in targets.targets}


def caps_of(targets: Targets) -> dict[str, tuple[str, ...]]:
    return {t.symbol: t.caps_applied for t in targets.targets}


# --------------------------------------------------------------------------- #
# AC 1 — Appendix C.3
# --------------------------------------------------------------------------- #


def test_us_t06_ac1_appendix_c3_intermediates() -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    assert out.sigma_p == pytest.approx(C3_SIGMA_P, abs=TOL)
    assert out.conv == pytest.approx(C3_CONV, abs=TOL)
    assert out.sigma_eff == pytest.approx(C3_SIGMA_EFF, abs=TOL)
    assert out.s == pytest.approx(C3_S, abs=TOL)
    assert out.g == C3_G
    assert out.equity == C3_EQUITY


def test_us_t06_ac1_appendix_c3_raw_notionals() -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    for symbol, expected in C3_RAW.items():
        assert out.by_symbol()[symbol].raw == pytest.approx(expected, abs=TOL)


def test_us_t06_ac1_appendix_c3_targets_gross_and_net() -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    for symbol, expected in C3_TARGETS.items():
        assert out.by_symbol()[symbol].target_notional == pytest.approx(expected, abs=TOL)
    assert out.gross == pytest.approx(C3_GROSS, abs=TOL)
    assert out.net == pytest.approx(C3_NET, abs=TOL)


def test_us_t06_ac1_appendix_c3_s_max_clip_binds() -> None:
    """The clip is the whole point of C.3 — assert the unclipped ratio too."""
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    assert out.sigma_eff / out.sigma_p == pytest.approx(C3_S_UNCLIPPED, abs=TOL)
    assert out.s == pytest.approx(c3_config().sizing.s_max)
    assert out.s < C3_S_UNCLIPPED


def test_us_t06_ac1_appendix_c3_no_cap_binds() -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    assert all(t.caps_applied == () for t in out.targets)
    assert check_caps(out, c3_config()) == ()


def test_us_t06_ac1_governor_scales_the_whole_book_linearly() -> None:
    cfg = c3_config()
    full = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 1.0, cfg)
    half = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 0.5, cfg)
    for symbol, target in C3_TARGETS.items():
        assert half.by_symbol()[symbol].target_notional == pytest.approx(target * 0.5, abs=TOL)
    assert half.gross == pytest.approx(full.gross * 0.5, abs=TOL)
    assert half.g == 0.5


def test_us_t06_ac1_divisor_is_the_fixed_universe_size_not_the_signal_count() -> None:
    """Three live signals out of sixteen must not be levered up to a full book."""
    cfg = c3_config()
    assert cfg.sizing_divisor == C3_N
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, cfg)
    assert out.by_symbol()["AAAUSDT"].raw == pytest.approx(
        C3_SIGNALS["AAAUSDT"] * (C3_SIGMA_TARGET_ASSET / C3_VOLS["AAAUSDT"]) / C3_N * C3_EQUITY,
        abs=TOL,
    )


# --------------------------------------------------------------------------- #
# The rig itself — the cap tests are only meaningful if this holds
# --------------------------------------------------------------------------- #


def test_rig_produces_exactly_the_requested_pre_cap_book() -> None:
    cfg = rig_config(single=10.0, net=10.0, gross=10.0)
    out = rig_book({"A": 1_800.0, "B": -400.0}, 1_000.0, cfg)
    assert out.s == pytest.approx(1.0)
    assert notionals(out) == pytest.approx({"A": 1_800.0, "B": -400.0})
    assert all(t.caps_applied == () for t in out.targets)


# --------------------------------------------------------------------------- #
# AC 2 — the three caps, in order
# --------------------------------------------------------------------------- #


def test_us_t06_ac2_single_cap_only_clips_each_symbol_independently() -> None:
    cfg = rig_config(single=0.5, net=10.0, gross=10.0)  # limits: 500 / 10 000 / 10 000
    out = rig_book({"A": 1_800.0, "B": -400.0}, 1_000.0, cfg)
    assert notionals(out) == pytest.approx({"A": 500.0, "B": -400.0})
    assert caps_of(out) == {"A": (CAP_SINGLE,), "B": ()}
    assert check_caps(out, cfg) == ()


def test_us_t06_ac2_single_cap_clips_a_short_symmetrically() -> None:
    cfg = rig_config(single=0.5, net=10.0, gross=10.0)
    out = rig_book({"A": -1_800.0, "B": 400.0}, 1_000.0, cfg)
    assert notionals(out) == pytest.approx({"A": -500.0, "B": 400.0})


def test_us_t06_ac2_net_cap_only_scales_the_dominant_side() -> None:
    """The short leg is untouched: shrinking it would *raise* net exposure."""
    cfg = rig_config(single=10.0, net=1.0, gross=10.0)  # limits: 10 000 / 1 000 / 10 000
    out = rig_book({"A": 1_800.0, "B": -400.0}, 1_000.0, cfg)
    assert notionals(out) == pytest.approx({"A": 1_400.0, "B": -400.0})
    assert out.net == pytest.approx(1_000.0)
    assert caps_of(out) == {"A": (CAP_NET,), "B": ()}


def test_us_t06_ac2_net_cap_scales_the_short_side_when_net_is_negative() -> None:
    cfg = rig_config(single=10.0, net=1.0, gross=10.0)
    out = rig_book({"A": -1_800.0, "B": 400.0}, 1_000.0, cfg)
    assert notionals(out) == pytest.approx({"A": -1_400.0, "B": 400.0})
    assert out.net == pytest.approx(-1_000.0)
    assert caps_of(out) == {"A": (CAP_NET,), "B": ()}


def test_us_t06_ac2_gross_cap_only_scales_everything_proportionally() -> None:
    cfg = rig_config(single=10.0, net=10.0, gross=1.0)  # limits: 10 000 / 10 000 / 1 000
    out = rig_book({"A": 1_800.0, "B": -400.0}, 1_000.0, cfg)
    scale = 1_000.0 / 2_200.0
    assert notionals(out) == pytest.approx({"A": 1_800.0 * scale, "B": -400.0 * scale})
    assert out.gross == pytest.approx(1_000.0)
    assert caps_of(out) == {"A": (CAP_GROSS,), "B": (CAP_GROSS,)}


def test_us_t06_ac2_all_three_caps_bind_in_order() -> None:
    """single clips A and B to 1 000; net then scales the long side by 0.7;
    gross then scales the whole book by 1 500 / 1 800."""
    cfg = rig_config(single=0.1, net=0.1, gross=0.15)  # limits: 1 000 / 1 000 / 1 500
    out = rig_book({"A": 18_000.0, "B": 16_000.0, "C": -400.0}, 10_000.0, cfg)
    gross_scale = 1_500.0 / 1_800.0
    assert notionals(out) == pytest.approx(
        {
            "A": 700.0 * gross_scale,
            "B": 700.0 * gross_scale,
            "C": -400.0 * gross_scale,
        }
    )
    assert out.gross == pytest.approx(1_500.0)
    assert abs(out.net) <= 1_000.0 + TOL
    assert caps_of(out) == {
        "A": (CAP_SINGLE, CAP_NET, CAP_GROSS),
        "B": (CAP_SINGLE, CAP_NET, CAP_GROSS),
        "C": (CAP_GROSS,),
    }
    assert check_caps(out, cfg) == ()


def test_us_t06_ac2_cap_order_is_observable_and_follows_the_prd() -> None:
    """Net-then-gross and gross-then-net give different books; the PRD order wins.

    Pre-cap ``[+1 800, -400]`` with limits net 1 000 and gross 2 000:
      * PRD (net first):   long side -> 1 400, gross 1 800 <= 2 000, gross never bites.
      * Reversed (gross first): everything -> x 0.909091, then net -> [1 363.64, -363.64].
    """
    cfg = rig_config(single=10.0, net=1.0, gross=2.0)
    out = rig_book({"A": 1_800.0, "B": -400.0}, 1_000.0, cfg)

    assert notionals(out) == pytest.approx({"A": 1_400.0, "B": -400.0})
    assert caps_of(out)["B"] == ()  # gross never ran, so the short is untouched

    gross_first = {"A": 1_800.0 * (2_000.0 / 2_200.0), "B": -400.0 * (2_000.0 / 2_200.0)}
    net_scale = (1_000.0 - gross_first["B"]) / gross_first["A"]
    reversed_answer = {"A": gross_first["A"] * net_scale, "B": gross_first["B"]}
    assert reversed_answer["A"] == pytest.approx(1_363.636364, abs=1e-6)
    assert notionals(out)["A"] != pytest.approx(reversed_answer["A"])


def test_us_t06_ac2_gross_scaling_cannot_re_break_single_or_net() -> None:
    """The ordering proof: the last cap only ever shrinks every leg."""
    cfg = rig_config(single=0.1, net=0.1, gross=0.15)
    out = rig_book({"A": 18_000.0, "B": 16_000.0, "C": -400.0}, 10_000.0, cfg)
    assert max(abs(v) for v in notionals(out).values()) <= 0.1 * 10_000.0 + TOL
    assert abs(out.net) <= 0.1 * 10_000.0 + TOL


def test_us_t06_ac2_cap_order_constant_matches_the_application_order() -> None:
    assert CAP_ORDER == (CAP_SINGLE, CAP_NET, CAP_GROSS)


def _directional_book(equity: float, cfg: AppConfig, **kwargs: object) -> Targets:
    """Eight longs and one short — a book the net cap binds on, not the gross cap."""
    desired = {f"L{i}": 2_500.0 for i in range(8)}
    desired["S0"] = -2_000.0
    signals = {s: v / equity for s, v in desired.items()}
    return size_targets(
        signals,
        dict.fromkeys(desired, 1.0),
        rig_risk_model(tuple(sorted(desired))),
        equity,
        1.0,
        cfg,
        **kwargs,  # type: ignore[arg-type]
    )


def test_us_t06_ac2_net_cap_holds_after_the_funding_haircut() -> None:
    """Halving the minority side *raises* net, so the cap is re-checked last.

    Pre-cap ``8 x +2 500`` and one ``-2 000`` on E = 10 000: net 18 000 > 1.5 E,
    so the net cap scales the longs by ``(15 000 + 2 000) / 20 000 = 0.85`` to
    2 125 each (net 15 000, gross 19 000 — the gross cap never bites). The short
    then pays -40 % funding and is halved to -1 000, which lifts net to 16 000 =
    1.6 E unless the cap runs again: re-scaling the longs by 16 000 / 17 000 puts
    each at 2 000 and net back on 15 000. Invariant 8 is a post-condition on the
    book that leaves ``size_targets``, not on an intermediate one.
    """
    cfg = rig_config()  # production caps: single 0.25, net 1.5, gross 2.5
    out = _directional_book(10_000.0, cfg, funding_ann={"S0": -0.40})

    assert notionals(out) == pytest.approx({**{f"L{i}": 2_000.0 for i in range(8)}, "S0": -1_000.0})
    assert out.net == pytest.approx(1.5 * 10_000.0)
    assert out.by_symbol()["S0"].funding_haircut == 0.5
    assert check_caps(out, cfg) == ()


def test_us_t06_ac2_net_cap_holds_after_the_zeroing_floor() -> None:
    """Zeroing the minority leg raises net exactly as the haircut does.

    Pre-cap ``8 x +2 500`` and one ``-50`` short with a 100 USDT ``min_notional``:
    the net cap scales the longs by ``(15 000 + 50) / 20 000 = 0.7525`` to
    1 881.25 each, then the short is zeroed as dust and net would read 15 050
    (1.505 E). Re-scaling the longs by 15 000 / 15 050 puts each on 1 875.
    """
    cfg = rig_config()
    desired = {f"L{i}": 2_500.0 for i in range(8)}
    desired["S0"] = -50.0
    equity = 10_000.0
    out = size_targets(
        {s: v / equity for s, v in desired.items()},
        dict.fromkeys(desired, 1.0),
        rig_risk_model(tuple(sorted(desired))),
        equity,
        1.0,
        cfg,
        min_notionals={"S0": 100.0},
    )

    assert notionals(out)["S0"] == 0.0
    assert notionals(out) == pytest.approx({**{f"L{i}": 1_875.0 for i in range(8)}, "S0": 0.0})
    assert out.net == pytest.approx(1.5 * equity)
    assert check_caps(out, cfg) == ()


def test_us_t06_ac2_caps_applied_stays_in_cap_order_when_net_runs_twice() -> None:
    cfg = rig_config()
    out = _directional_book(10_000.0, cfg, funding_ann={"S0": -0.40})
    for leg in out.targets:
        assert list(leg.caps_applied) == [c for c in CAP_ORDER if c in leg.caps_applied]
        assert len(set(leg.caps_applied)) == len(leg.caps_applied)


def _book(equity: float, **notional: float) -> Targets:
    """A hand-built ``Targets`` — what the risk supervisor sees after fills."""
    return Targets(
        targets=tuple(
            SymbolTarget(symbol=s, signal=0.0, vol=1.0, raw=v, target_notional=v)
            for s, v in sorted(notional.items())
        ),
        sigma_p=0.0,
        conv=0.0,
        sigma_eff=0.0,
        s=0.0,
        g=1.0,
        equity=equity,
    )


@pytest.mark.parametrize(
    ("book", "breached"),
    [
        ({"A": 2_000.0, "B": -1_000.0}, ()),
        ({"A": 3_000.0, "B": -1_000.0}, (CAP_SINGLE,)),
        ({"A": 2_000.0, "B": 2_000.0}, (CAP_NET,)),
        ({"A": 2_500.0, "B": -2_500.0}, (CAP_GROSS,)),
        ({"A": 9_000.0, "B": 9_000.0}, (CAP_SINGLE, CAP_NET, CAP_GROSS)),
    ],
)
def test_us_t06_ac2_check_caps_names_every_breach(book: dict[str, float], breached: tuple[str, ...]) -> None:
    """``check_caps`` is re-used by the risk supervisor on *filled* positions."""
    # Limits on E = 10 000: single 2 500, net 3 000, gross 4 500.
    cfg = AppConfig.model_validate({"caps": {"single": 0.25, "net": 0.3, "gross": 0.45}})
    assert check_caps(_book(10_000.0, **book), cfg) == breached


# --------------------------------------------------------------------------- #
# AC 3 — zeroing
# --------------------------------------------------------------------------- #


def test_us_t06_ac3_target_below_min_target_frac_of_equity_becomes_exactly_zero() -> None:
    cfg = AppConfig.model_validate(
        {"universe": {"size": 1}, "sizing": {**_RIG_SIZING, "min_target_frac": 0.001}}
    )
    out = rig_book({"A": 5.0, "B": 500.0}, 10_000.0, cfg)  # floor = 10
    assert notionals(out)["A"] == 0.0
    assert isinstance(notionals(out)["A"], float)
    assert notionals(out)["B"] == pytest.approx(500.0)


def test_us_t06_ac3_target_below_symbol_min_notional_becomes_exactly_zero() -> None:
    cfg = AppConfig.model_validate(
        {"universe": {"size": 1}, "sizing": {**_RIG_SIZING, "min_target_frac": 0.0}}
    )
    equity = 10_000.0
    desired = {"A": 30.0, "B": 60.0}
    signals = {s: v / equity for s, v in desired.items()}
    out = size_targets(
        signals,
        dict.fromkeys(desired, 1.0),
        rig_risk_model(("A", "B")),
        equity,
        1.0,
        cfg,
        min_notionals={"A": 50.0, "B": 50.0},
    )
    assert notionals(out)["A"] == 0.0
    assert notionals(out)["B"] == pytest.approx(60.0)


def test_us_t06_ac3_the_larger_of_the_two_floors_wins() -> None:
    cfg = AppConfig.model_validate(
        {"universe": {"size": 1}, "sizing": {**_RIG_SIZING, "min_target_frac": 0.001}}
    )
    equity = 100_000.0  # frac floor = 100, symbol floor = 5
    desired = {"A": 40.0}
    out = size_targets(
        {s: v / equity for s, v in desired.items()},
        dict.fromkeys(desired, 1.0),
        rig_risk_model(("A",)),
        equity,
        1.0,
        cfg,
        min_notionals={"A": 5.0},
    )
    assert notionals(out)["A"] == 0.0


def test_us_t06_ac3_zeroing_applies_after_the_funding_haircut() -> None:
    """A haircut can push a target under the floor; the floor is checked last."""
    cfg = AppConfig.model_validate(
        {"universe": {"size": 1}, "sizing": {**_RIG_SIZING, "min_target_frac": 0.0}}
    )
    equity = 10_000.0
    desired = {"A": 80.0}
    out = size_targets(
        {s: v / equity for s, v in desired.items()},
        dict.fromkeys(desired, 1.0),
        rig_risk_model(("A",)),
        equity,
        1.0,
        cfg,
        funding_ann={"A": 0.50},
        min_notionals={"A": 50.0},
    )
    assert out.by_symbol()["A"].funding_haircut == 0.5
    assert notionals(out)["A"] == 0.0


# --------------------------------------------------------------------------- #
# AC 4 — every intermediate survives to the caller
# --------------------------------------------------------------------------- #


def test_us_t06_ac4_every_intermediate_is_persisted_on_the_result() -> None:
    cfg = c3_config()
    out = size_targets(
        C3_SIGNALS,
        C3_VOLS,
        c3_risk_model(),
        C3_EQUITY,
        C3_G,
        cfg,
        funding_ann={"AAAUSDT": 0.40},
        prices={"AAAUSDT": 50.0},
    )
    leg = out.by_symbol()["AAAUSDT"]
    assert leg.signal == C3_SIGNALS["AAAUSDT"]
    assert leg.vol == C3_VOLS["AAAUSDT"]
    assert leg.raw == pytest.approx(C3_RAW["AAAUSDT"], abs=TOL)
    assert leg.funding_ann == pytest.approx(0.40)
    assert leg.funding_haircut == 0.5
    assert leg.target_notional == pytest.approx(C3_TARGETS["AAAUSDT"] * 0.5, abs=TOL)
    assert leg.target_qty == pytest.approx(leg.target_notional / 50.0, abs=TOL)
    # The book-level intermediates are the Appendix C.3 ones: the haircut runs
    # after them and must not disturb what is persisted for the audit trail.
    assert out.sigma_p == pytest.approx(C3_SIGMA_P, abs=TOL)
    assert out.conv == pytest.approx(C3_CONV, abs=TOL)
    assert out.sigma_eff == pytest.approx(C3_SIGMA_EFF, abs=TOL)
    assert out.s == pytest.approx(C3_S, abs=TOL)
    assert (out.g, out.equity) == (C3_G, C3_EQUITY)


def test_us_t06_ac4_target_qty_is_zero_without_prices() -> None:
    """Lot rounding belongs to the planner; quantities only appear on request."""
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, C3_G, c3_config())
    assert all(t.target_qty == 0.0 for t in out.targets)


def test_us_t06_ac4_symbols_come_back_in_sorted_order() -> None:
    out = size_targets(
        {"ZZZUSDT": 0.5, "AAAUSDT": 0.5},
        {"ZZZUSDT": 1.0, "AAAUSDT": 1.0},
        rig_risk_model(("AAAUSDT", "ZZZUSDT")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert [t.symbol for t in out.targets] == ["AAAUSDT", "ZZZUSDT"]


# --------------------------------------------------------------------------- #
# Edge cases — every one of these must produce a flat book, not an exception
# --------------------------------------------------------------------------- #


def test_no_signals_gives_an_empty_flat_book() -> None:
    out = size_targets({}, {}, rig_risk_model(()), 10_000.0, 1.0, rig_config())
    assert out.targets == ()
    assert out.sigma_p == 0.0
    assert out.s == 0.0
    assert out.gross == 0.0


def test_zero_signals_give_sigma_p_zero_and_a_flat_book() -> None:
    """Idle is valid (Invariant 3): no signal is no position, never a forced trade."""
    out = size_targets(
        {"A": 0.0, "B": 0.0},
        {"A": 0.5, "B": 0.5},
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.sigma_p == 0.0
    assert out.conv == 0.0
    assert out.sigma_eff == 0.0
    assert out.s == 0.0
    assert notionals(out) == {"A": 0.0, "B": 0.0}


def test_degenerate_covariance_gives_s_zero_without_dividing_by_zero() -> None:
    """A covariance that says the book carries no risk must not lever it to infinity."""
    zero_risk = RiskModel(
        symbols=("A", "B"),
        vols={"A": 0.0, "B": 0.0},
        corr=((1.0, 0.0), (0.0, 1.0)),
        avg_corr=0.0,
    )
    out = size_targets({"A": 0.8, "B": -0.6}, {"A": 0.5, "B": 0.5}, zero_risk, 10_000.0, 1.0, rig_config())
    assert out.sigma_p == 0.0
    assert out.s == 0.0
    assert all(t.target_notional == 0.0 for t in out.targets)
    assert all(math.isfinite(t.target_notional) for t in out.targets)


def test_zero_conviction_gives_sigma_eff_zero() -> None:
    cfg = c3_config()
    out = size_targets(
        {"AAAUSDT": 0.0, "BBBUSDT": 0.0, "CCCUSDT": 0.0},
        C3_VOLS,
        c3_risk_model(),
        C3_EQUITY,
        1.0,
        cfg,
    )
    assert out.conv == 0.0
    assert out.sigma_eff == 0.0
    assert out.gross == 0.0


def test_partial_conviction_tapers_sigma_eff_linearly() -> None:
    cfg = c3_config()
    out = size_targets(
        {"AAAUSDT": 0.25, "BBBUSDT": -0.25, "CCCUSDT": 0.25},
        C3_VOLS,
        c3_risk_model(),
        C3_EQUITY,
        1.0,
        cfg,
    )
    assert out.conv == pytest.approx(0.25)
    assert out.sigma_eff == pytest.approx(C3_SIGMA_TARGET_PORTFOLIO * 0.5)


def test_conviction_is_the_mean_over_the_symbols_given_a_signal() -> None:
    """Not over the fixed universe: three strong signals are three strong signals."""
    out = size_targets(
        {"A": 1.0, "B": -1.0},
        {"A": 1.0, "B": 1.0},
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.conv == pytest.approx(1.0)


@pytest.mark.parametrize("vol", [0.0, -0.5, math.nan, math.inf])
def test_symbol_with_a_signal_but_no_usable_vol_is_reported_flat(vol: float) -> None:
    out = size_targets(
        {"A": 0.8, "B": 0.8},
        {"A": vol, "B": 1.0},
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    leg = out.by_symbol()["A"]
    assert leg.vol == 0.0
    assert leg.raw == 0.0
    assert leg.target_notional == 0.0
    assert leg.signal == pytest.approx(0.8)  # reported, not dropped
    assert out.by_symbol()["B"].target_notional != 0.0


def test_symbol_missing_from_vols_entirely_is_reported_flat() -> None:
    out = size_targets(
        {"A": 0.8, "B": 0.8},
        {"B": 1.0},
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.by_symbol()["A"].target_notional == 0.0


def test_non_finite_signal_is_treated_as_no_position() -> None:
    out = size_targets(
        {"A": math.nan, "B": 0.5},
        {"A": 1.0, "B": 1.0},
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.by_symbol()["A"].target_notional == 0.0
    assert all(math.isfinite(t.target_notional) for t in out.targets)


@pytest.mark.parametrize("equity", [0.0, -1_000.0, math.nan])
def test_zero_or_negative_equity_gives_a_flat_book_without_raising(equity: float) -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), equity, 1.0, c3_config())
    assert all(t.target_notional == 0.0 for t in out.targets)
    assert all(t.raw == 0.0 for t in out.targets)
    assert out.s == 0.0
    assert out.sigma_p == 0.0
    assert out.gross == 0.0


def test_zero_governor_multiplier_flattens_the_book() -> None:
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 0.0, c3_config())
    assert out.gross == 0.0
    assert out.g == 0.0


def test_symbol_outside_the_risk_model_falls_back_to_average_correlation() -> None:
    """Too little history for the covariance window is not a licence for zero risk."""
    partial = RiskModel(symbols=("A",), vols={"A": 1.0}, corr=((1.0,),), avg_corr=0.9)
    out = size_targets(
        {"A": 0.5, "B": 0.5},
        {"A": 1.0, "B": 1.0},
        partial,
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.sigma_p > 0.0
    assert out.by_symbol()["B"].raw != 0.0


def test_empty_risk_model_still_sizes_from_the_vol_vector() -> None:
    empty = RiskModel(symbols=(), vols={}, corr=(), avg_corr=0.0)
    out = size_targets({"A": 0.5}, {"A": 1.0}, empty, 10_000.0, 1.0, rig_config())
    assert out.sigma_p == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Configuration that could *raise* risk is rejected loudly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("g", [-0.5, math.nan, math.inf])
def test_invalid_governor_multiplier_raises_config_error(g: float) -> None:
    with pytest.raises(ConfigError):
        size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, g, c3_config())


def test_non_positive_divisor_raises_config_error() -> None:
    cfg = AppConfig.model_validate({"sizing": {"universe_size_divisor": -1}})
    with pytest.raises(ConfigError):
        size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 1.0, cfg)


def test_negative_cap_raises_config_error() -> None:
    cfg = AppConfig.model_validate({"caps": {"single": -0.1}})
    with pytest.raises(ConfigError):
        size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 1.0, cfg)


def test_negative_s_max_raises_config_error() -> None:
    cfg = AppConfig.model_validate({"sizing": {"s_max": -1.0}})
    with pytest.raises(ConfigError):
        size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 1.0, cfg)


def test_zero_conviction_full_does_not_divide_by_zero() -> None:
    cfg = AppConfig.model_validate({"sizing": {"conviction_full": 0.0}})
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), C3_EQUITY, 1.0, cfg)
    assert out.sigma_eff == pytest.approx(cfg.sizing.sigma_target_portfolio)


def test_check_caps_on_a_zero_equity_book_is_satisfied_by_a_flat_book() -> None:
    cfg = c3_config()
    out = size_targets(C3_SIGNALS, C3_VOLS, c3_risk_model(), 0.0, 1.0, cfg)
    assert check_caps(out, cfg) == ()


# --------------------------------------------------------------------------- #
# Invariant 8 — the post-condition, as a property
# --------------------------------------------------------------------------- #

_SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    signals=st.lists(
        st.floats(min_value=-1.5, max_value=1.5, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=len(_SYMBOLS),
    ),
    vols=st.lists(
        st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
        min_size=len(_SYMBOLS),
        max_size=len(_SYMBOLS),
    ),
    equity=st.floats(min_value=0.0, max_value=1e8, allow_nan=False, allow_infinity=False),
    g=st.sampled_from([0.25, 0.5, 1.0]),
    rho=st.floats(min_value=0.0, max_value=0.95),
    s_max=st.floats(min_value=0.0, max_value=10.0),
    funding=st.lists(
        st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False),
        min_size=len(_SYMBOLS),
        max_size=len(_SYMBOLS),
    ),
    min_notional=st.floats(min_value=0.0, max_value=500.0),
)
def test_us_t06_invariant_8_all_three_caps_hold_on_every_returned_book(
    signals: list[float],
    vols: list[float],
    equity: float,
    g: float,
    rho: float,
    s_max: float,
    funding: list[float],
    min_notional: float,
) -> None:
    """Exposure is bounded by construction — for every book, not just the tested ones.

    The funding haircut and the min-notional floor are drawn too: both run *after*
    the caps and both can lift net exposure, so a property that leaves them out
    cannot see Invariant 8 break.
    """
    symbols = _SYMBOLS[: len(signals)]
    n = len(symbols)
    corr = tuple(tuple(1.0 if i == j else rho for j in range(n)) for i in range(n))
    risk_model = RiskModel(
        symbols=symbols,
        vols={s: vols[i] for i, s in enumerate(symbols)},
        corr=corr,
        avg_corr=rho,
    )
    cfg = AppConfig.model_validate({"sizing": {"s_max": s_max}})
    out = size_targets(
        {s: signals[i] for i, s in enumerate(symbols)},
        {s: vols[i] for i, s in enumerate(symbols)},
        risk_model,
        equity,
        g,
        cfg,
        funding_ann={s: funding[i] for i, s in enumerate(symbols)},
        min_notionals=dict.fromkeys(symbols, min_notional),
    )

    assert check_caps(out, cfg) == ()
    assert all(math.isfinite(t.target_notional) for t in out.targets)
    assert math.isfinite(out.sigma_p) and out.sigma_p >= 0.0
    assert 0.0 <= out.s <= cfg.sizing.s_max
    if equity > 0.0:
        tol = 1e-6 * max(1.0, equity)
        assert (
            max((abs(t.target_notional) for t in out.targets), default=0.0) <= cfg.caps.single * equity + tol
        )
        assert abs(out.net) <= cfg.caps.net * equity + tol
        assert out.gross <= cfg.caps.gross * equity + tol
    else:
        assert out.gross == 0.0


def test_unparseable_signal_or_vol_is_treated_as_no_position() -> None:
    """Inputs can arrive from JSON/SQLite; a ``None`` must not crash a rebalance."""
    signals: dict[str, object] = {"A": None, "B": 0.5}
    vols: dict[str, object] = {"A": 1.0, "B": "1.0"}
    out = size_targets(
        signals,  # type: ignore[arg-type]
        vols,  # type: ignore[arg-type]
        rig_risk_model(("A", "B")),
        10_000.0,
        1.0,
        rig_config(),
    )
    assert out.by_symbol()["A"].target_notional == 0.0
    assert out.by_symbol()["B"].target_notional != 0.0
