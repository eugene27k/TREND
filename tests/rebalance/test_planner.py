"""US-T09 AC 2-3: hysteresis, rounding, flips and risk-first ordering."""

from __future__ import annotations

import inspect

import pytest

from aegis.core.config import AppConfig
from aegis.core.types import Side
from aegis.rebalance.planner import build_plan, clip_notional, minute_volume, slice_count
from tests.rebalance.conftest import BTC, ETH, SOL, info, position, targets

MARK = 100.0
EQUITY = 10_000.0
#: 0.005 * 2 880 000 / 1440 = 10 USDT per clip.
DAILY_VOLUME = 2_880_000.0


def plan_for(
    cfg: AppConfig,
    target_notionals: dict[str, float],
    positions: dict[str, float] | None = None,
    *,
    universe: tuple[str, ...] = (BTC,),
    equity: float = EQUITY,
    marks: dict[str, float] | None = None,
    infos: dict[str, object] | None = None,
    volumes: dict[str, float] | None = None,
):
    symbols = set(target_notionals) | set(positions or {}) | set(universe)
    marks = marks or dict.fromkeys(symbols, MARK)
    return build_plan(
        targets(target_notionals, equity),
        {s: position(s, q, marks[s]) for s, q in (positions or {}).items()},
        infos or {s: info(s) for s in symbols},
        marks,
        equity,
        cfg,
        volumes if volumes is not None else {s: minute_volume(DAILY_VOLUME) for s in symbols},
        universe,
    )


# --------------------------------------------------------------------------- #
# US-T09 AC 2 — hysteresis (the four PRD cases)
# --------------------------------------------------------------------------- #


def test_us_t09_ac2_target_1000_current_950_does_not_trade(cfg: AppConfig) -> None:
    assert plan_for(cfg, {BTC: 1_000.0}, {BTC: 9.5}) == []


def test_us_t09_ac2_target_1000_current_850_trades(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 1_000.0}, {BTC: 8.5})
    assert len(plan) == 1
    assert plan[0].side is Side.BUY
    assert plan[0].delta_notional == pytest.approx(150.0)
    assert plan[0].delta_qty == pytest.approx(1.5)


def test_us_t09_ac2_target_below_absolute_band_does_not_trade(cfg: AppConfig) -> None:
    # 20 USDT against E = 10 000: the band floor is 0.0025 * E = 25.
    assert plan_for(cfg, {BTC: 20.0}, {}) == []


def test_us_t09_ac2_symbol_out_of_universe_always_trades_to_zero(cfg: AppConfig) -> None:
    # 20 USDT is inside the band, but the symbol has no signal behind it anymore.
    plan = plan_for(cfg, {}, {ETH: 0.2}, universe=(BTC,))
    assert [(p.symbol, p.target_qty, p.reduce_only) for p in plan] == [(ETH, 0.0, True)]
    assert plan[0].delta_qty == pytest.approx(-0.2)


# --------------------------------------------------------------------------- #
# US-T09 AC 3 — ordering and sequencing
# --------------------------------------------------------------------------- #


def test_us_t09_ac3_risk_reducing_first_by_current_notional_descending(cfg: AppConfig) -> None:
    plan = plan_for(
        cfg,
        {BTC: 100.0, ETH: 0.0, SOL: 2_000.0},
        {BTC: 20.0, ETH: 5.0, SOL: 1.0},
        universe=(BTC, ETH, SOL),
    )
    # BTC 2 000 -> 100 and ETH 500 -> 0 reduce; SOL 100 -> 2 000 increases.
    assert [(p.symbol, p.risk_reducing, p.sequence) for p in plan] == [
        (BTC, True, 0),
        (ETH, True, 1),
        (SOL, False, 2),
    ]


def test_us_t09_ac3_sequence_numbers_are_dense_and_ordered(cfg: AppConfig) -> None:
    plan = plan_for(
        cfg,
        {BTC: 2_000.0, ETH: 1_500.0, SOL: 1_000.0},
        {},
        universe=(BTC, ETH, SOL),
    )
    assert [p.sequence for p in plan] == [0, 1, 2]
    # Risk-increasing orders go out largest first.
    assert [p.symbol for p in plan] == [BTC, ETH, SOL]


# --------------------------------------------------------------------------- #
# Flips: two legs, never one order through zero
# --------------------------------------------------------------------------- #


def test_us_t10_ac2_sign_flip_is_split_into_a_reducing_and_an_opening_leg(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: -1_000.0}, {BTC: 10.0})
    assert len(plan) == 2

    close, open_ = plan
    assert (close.symbol, close.sequence) == (BTC, 0)
    assert close.reduce_only is True
    assert close.risk_reducing is True
    assert close.target_qty == 0.0
    assert close.delta_qty == pytest.approx(-10.0)
    assert close.side is Side.SELL

    assert (open_.symbol, open_.sequence) == (BTC, 1)
    # A single order through zero would be rejected with reduce_only set, so the
    # opening leg must not carry it.
    assert open_.reduce_only is False
    assert open_.risk_reducing is False
    assert open_.current_qty == 0.0
    assert open_.target_qty == pytest.approx(-10.0)
    assert open_.side is Side.SELL


def test_flip_drops_the_opening_leg_below_min_notional_but_still_closes(cfg: AppConfig) -> None:
    # Target -4 USDT against a minNotional of 5: close the long, open nothing.
    infos = {BTC: info(BTC, min_notional=5.0)}
    plan = plan_for(cfg, {BTC: -4.0}, {BTC: 10.0}, infos=infos)
    assert [(p.reduce_only, p.target_qty) for p in plan] == [(True, 0.0)]


# --------------------------------------------------------------------------- #
# Rounding: never up, never below the venue floor, always close in full
# --------------------------------------------------------------------------- #


def test_reducing_order_below_min_notional_is_dropped_not_bumped(cfg: AppConfig) -> None:
    # Delta of 4 USDT with minNotional 5 — bumping it would trade more than asked.
    infos = {BTC: info(BTC, min_notional=5.0)}
    assert plan_for(cfg, {BTC: 996.0}, {BTC: 10.0}, infos=infos) == []


def test_closing_order_below_min_notional_still_closes_the_whole_position(cfg: AppConfig) -> None:
    infos = {BTC: info(BTC, min_notional=50.0)}
    plan = plan_for(cfg, {}, {BTC: 0.2}, universe=(), infos=infos)  # 20 USDT position
    assert len(plan) == 1
    assert plan[0].delta_qty == pytest.approx(-0.2)
    assert plan[0].reduce_only is True


def test_delta_that_rounds_to_zero_on_the_lot_grid_is_dropped(cfg: AppConfig) -> None:
    infos = {BTC: info(BTC, step_size=1.0, min_qty=1.0, min_notional=0.0)}
    # 50 USDT at 100 is half a lot: floor to 0 and drop.
    assert plan_for(cfg, {BTC: 50.0}, {}, infos=infos) == []


def test_delta_qty_is_floored_onto_the_lot_grid(cfg: AppConfig) -> None:
    infos = {BTC: info(BTC, step_size=0.1, min_qty=0.1, min_notional=0.0)}
    plan = plan_for(cfg, {BTC: 1_234.0}, {}, infos=infos)
    assert plan[0].delta_qty == pytest.approx(12.3)  # not 12.34, not 12.4


def test_symbol_without_instrument_metadata_is_skipped(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 1_000.0, ETH: 1_000.0}, {}, universe=(BTC, ETH), infos={BTC: info(BTC)})
    assert [p.symbol for p in plan] == [BTC]


def test_symbol_without_a_mark_is_skipped(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 1_000.0}, {}, marks={BTC: 0.0})
    assert plan == []


# --------------------------------------------------------------------------- #
# 5.9 step 2 — the liquidity clip
# --------------------------------------------------------------------------- #


def test_clip_is_half_a_percent_of_a_typical_minute(cfg: AppConfig) -> None:
    assert minute_volume(DAILY_VOLUME) == pytest.approx(2_000.0)
    assert clip_notional(minute_volume(DAILY_VOLUME), cfg) == pytest.approx(10.0)


def test_us_t10_ac3_order_of_25_clips_produces_25_slices(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 250.0}, {})  # clip = 10 USDT
    assert plan[0].n_slices == 25
    assert plan[0].clip_qty == pytest.approx(0.1)


def test_us_t10_ac3_slice_count_is_capped_at_max_slices(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 1_000.0}, {})  # 100 clips
    assert plan[0].n_slices == cfg.exec.max_slices == 30


def test_slice_count_without_a_volume_estimate_is_one(cfg: AppConfig) -> None:
    plan = plan_for(cfg, {BTC: 1_000.0}, {}, volumes={})
    assert plan[0].n_slices == 1
    assert slice_count(1_000.0, 0.0, cfg) == 1


# --------------------------------------------------------------------------- #
# Purity (Architecture: planner takes no collaborators)
# --------------------------------------------------------------------------- #


def test_build_plan_is_pure_no_context_clock_or_gateway(cfg: AppConfig) -> None:
    params = set(inspect.signature(build_plan).parameters)
    assert not params & {"ctx", "clock", "gateway", "repos", "alerts"}
    positions = {BTC: position(BTC, 8.5, MARK)}
    first = build_plan(
        targets({BTC: 1_000.0}),
        positions,
        {BTC: info(BTC)},
        {BTC: MARK},
        EQUITY,
        cfg,
        {BTC: minute_volume(DAILY_VOLUME)},
        (BTC,),
    )
    second = build_plan(
        targets({BTC: 1_000.0}),
        positions,
        {BTC: info(BTC)},
        {BTC: MARK},
        EQUITY,
        cfg,
        {BTC: minute_volume(DAILY_VOLUME)},
        (BTC,),
    )
    assert first == second
