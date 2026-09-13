"""US-T14 — per-symbol and long/short attribution, the daily identity, trade episodes."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from aegis.accounting.attribution import ATTRIBUTION_IDENTITY_BREAK, Attribution
from aegis.accounting.ledger import LedgerService
from aegis.accounting.reconcile import Reconciler
from aegis.accounting.snapshots import SnapshotService
from aegis.core.clock import DAY_MS, FakeClock, day_start_ms
from aegis.core.context import Context
from aegis.core.types import PositionSide, Side, SignalResult
from aegis.gateway.fake import FakeGateway
from tests.accounting.conftest import DAY, HOUR_MS, T0, raw_income

NEXT_DAY = DAY + timedelta(days=1)


def _snapshot(ctx: Context, clock: FakeClock, ts_ms: int) -> None:
    clock.set(ts_ms)
    SnapshotService(ctx).take(ts_ms)


def _save_signal(ctx: Context, day: date, symbol: str, signal: float) -> None:
    ctx.repos.signals.save_many(
        day, [SignalResult(symbol=symbol, x=(), y=(), z=(), u=(), signal=signal)], day_start_ms(day)
    )


def _book_everything(ctx: Context, now_ms: int) -> None:
    """What the engine does at the end of a day: pull income, book any missed fill."""
    Reconciler(ctx, lookback_ms=2 * DAY_MS).fills(now_ms)
    LedgerService(ctx).sync(now_ms)


def _synthetic_day(ctx: Context, gateway: FakeGateway, clock: FakeClock) -> int:
    """One realistic day: a long, a short, funding, a partial close, a deposit.

    Returns the timestamp the day is attributed at.
        00:00  flat, equity 10 000
        01:00  BUY  10 BTC @ 100 (taker)   SELL 20 ETH @ 50 (taker)
        08:00  funding settles on both
        12:00  marks move: BTC 110, ETH 48
        15:00  operator deposits 500, venue pays a 1.00 referral kickback
        20:00  SELL 4 BTC @ 110 — realises 40
        23:00  closing snapshot
    """
    gateway.set_book("BTCUSDT", 99.99, 100.01, mark=100.0)
    gateway.set_book("ETHUSDT", 49.99, 50.01, mark=50.0)
    _snapshot(ctx, clock, T0)

    gateway.sim.apply_fill(
        symbol="BTCUSDT", side=Side.BUY, qty=10.0, price=100.0, ts_ms=T0 + HOUR_MS, is_maker=False
    )
    gateway.sim.apply_fill(
        symbol="ETHUSDT", side=Side.SELL, qty=20.0, price=50.0, ts_ms=T0 + HOUR_MS, is_maker=False
    )
    _snapshot(ctx, clock, T0 + 2 * HOUR_MS)

    gateway.settle_funding("BTCUSDT", 0.0001, T0 + 8 * HOUR_MS)  # long pays 0.10
    gateway.settle_funding("ETHUSDT", 0.0002, T0 + 8 * HOUR_MS)  # short receives 0.20

    gateway.set_mark("BTCUSDT", 110.0)
    gateway.set_mark("ETHUSDT", 48.0)
    _snapshot(ctx, clock, T0 + 12 * HOUR_MS)

    gateway.sim.transfer(500.0, T0 + 15 * HOUR_MS)
    # A credit the venue pays outside any position: income row plus the cash.
    raw_income(gateway, "REFERRAL_KICKBACK", 1.0, T0 + 15 * HOUR_MS)
    gateway.sim.wallet_balance += 1.0

    gateway.sim.apply_fill(
        symbol="BTCUSDT", side=Side.SELL, qty=4.0, price=110.0, ts_ms=T0 + 20 * HOUR_MS, is_maker=False
    )
    now = T0 + 23 * HOUR_MS
    _snapshot(ctx, clock, now)

    _save_signal(ctx, DAY, "BTCUSDT", 0.9)
    _save_signal(ctx, DAY, "ETHUSDT", -0.6)
    _book_everything(ctx, now)
    return now


# --------------------------------------------------------------------------- #
# AC 3 — the identity. This test is the point of the module.
# --------------------------------------------------------------------------- #


def test_us_t14_ac3_the_daily_identity_holds_to_a_cent_on_a_synthetic_day(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)
    Attribution(ctx).compute_day(DAY, now)

    ok, residual = Attribution(ctx).identity_check(DAY)

    assert ok, f"residual {residual}"
    assert abs(residual) <= 0.01
    assert ATTRIBUTION_IDENTITY_BREAK not in [a["code"] for a in ctx.repos.alerts.recent()]


def test_us_t14_ac3_the_identity_survives_a_deposit_and_a_non_position_credit(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """The deposit must be netted out and the kickback counted, or the sums lie."""
    now = _synthetic_day(ctx, gateway, clock)
    Attribution(ctx).compute_day(DAY, now)

    start = ctx.repos.snapshots.last_before(T0)
    end = ctx.repos.snapshots.last_before(T0 + DAY_MS - 1)
    equity_change = end["margin_balance"] - start["margin_balance"]
    attributed = sum(r["net_pnl"] for r in ctx.repos.symbol_pnl.between(DAY, DAY))

    assert equity_change == pytest.approx(501.0 + attributed, abs=0.01)
    assert attributed == pytest.approx(138.88, abs=0.01)
    assert Attribution(ctx).identity_check(DAY)[0]


def test_us_t14_ac3_income_missing_from_the_ledger_breaks_the_identity_and_alerts(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """The wallet was charged funding but the income rows never arrived."""
    now = _synthetic_day(ctx, gateway, clock)
    ctx.repos.ledger.db.execute("DELETE FROM ledger WHERE income_type = 'FUNDING_FEE'")
    Attribution(ctx).compute_day(DAY, now)

    ok, residual = Attribution(ctx).identity_check(DAY)

    assert not ok
    assert residual == pytest.approx(-0.1, abs=1e-6)  # the net funding nobody booked
    assert ATTRIBUTION_IDENTITY_BREAK in [a["code"] for a in ctx.repos.alerts.recent()]


def test_us_t14_ac3_a_fill_the_engine_never_booked_shows_up_as_a_residual(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """The venue realised 40 USDT on a trade our fill table never saw."""
    now = _synthetic_day(ctx, gateway, clock)
    ctx.repos.fills.db.execute("DELETE FROM fills WHERE realized_pnl != 0")
    Attribution(ctx).compute_day(DAY, now)

    ok, residual = Attribution(ctx).identity_check(DAY)

    assert not ok
    assert residual == pytest.approx(-40.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# AC 1 — the components
# --------------------------------------------------------------------------- #


def test_us_t14_ac1_one_row_per_symbol_with_every_component(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)

    rows = {r["symbol"]: r for r in Attribution(ctx).compute_day(DAY, now)}

    btc = rows["BTCUSDT"]
    assert btc["price_pnl"] == pytest.approx(100.0)  # 40 realised + 60 still unrealised
    assert btc["funding"] == pytest.approx(-0.10)
    assert btc["fees"] == pytest.approx(-(0.5 + 0.22))
    assert btc["net_pnl"] == pytest.approx(99.18)
    assert btc["traded_notional"] == pytest.approx(10 * 100 + 4 * 110)
    assert btc["signal"] == pytest.approx(0.9)

    eth = rows["ETHUSDT"]
    assert eth["price_pnl"] == pytest.approx(40.0)  # short into a falling mark
    assert eth["funding"] == pytest.approx(0.20)  # a short is paid when the rate is positive
    assert eth["fees"] == pytest.approx(-0.5)
    assert eth["net_pnl"] == pytest.approx(39.70)
    assert eth["signal"] == pytest.approx(-0.6)


def test_us_t14_ac1_side_comes_from_the_average_signed_notional(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)

    rows = {r["symbol"]: r for r in Attribution(ctx).compute_day(DAY, now)}

    assert rows["BTCUSDT"]["side"] == str(PositionSide.LONG)
    assert rows["BTCUSDT"]["avg_notional"] > 0
    assert rows["ETHUSDT"]["side"] == str(PositionSide.SHORT)
    assert rows["ETHUSDT"]["avg_notional"] < 0


def test_us_t14_ac1_slippage_against_the_decision_mid_is_a_signed_cost(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    gateway.set_book("BTCUSDT", 99.99, 100.01, mark=100.0)
    _snapshot(ctx, clock, T0)
    # Bought 5 bps above the decision mid of 100.00.
    gateway.sim.apply_fill(
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=10.0,
        price=100.05,
        ts_ms=T0 + HOUR_MS,
        is_maker=False,
        decision_mid=100.0,
    )
    _snapshot(ctx, clock, T0 + 2 * HOUR_MS)
    _book_everything(ctx, T0 + 2 * HOUR_MS)

    row = {r["symbol"]: r for r in Attribution(ctx).compute_day(DAY, T0 + 2 * HOUR_MS)}["BTCUSDT"]

    assert row["slippage"] == pytest.approx(-(5.0 / 10_000.0) * (10.0 * 100.05))
    # Slippage is a memo: it is already inside the fill price and must not be
    # added again, or the identity would double count it.
    assert row["net_pnl"] == pytest.approx(row["price_pnl"] + row["funding"] + row["fees"])
    assert Attribution(ctx).identity_check(DAY)[0]


def test_us_t14_ac3_a_day_with_no_opening_snapshot_is_not_evaluable(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """The engine's first day: the capital was on the venue before we ever looked.

    With no snapshot before 00:00 there is no opening equity, and assuming zero
    would report the whole account as an unexplained gain — a guaranteed false
    ATTRIBUTION_IDENTITY_BREAK on day one.
    """
    _snapshot(ctx, clock, T0 + 12 * HOUR_MS)  # the first snapshot ever, equity 10 000
    attribution = Attribution(ctx)
    attribution.compute_day(DAY, T0 + 23 * HOUR_MS)

    assert attribution.identity_check(DAY) == (True, 0.0)
    assert ATTRIBUTION_IDENTITY_BREAK not in [a["code"] for a in ctx.repos.alerts.recent()]


def test_us_t14_ac1_a_day_with_no_activity_produces_no_rows(ctx: Context, clock: FakeClock) -> None:
    _snapshot(ctx, clock, T0)

    assert Attribution(ctx).compute_day(DAY, T0 + HOUR_MS) == []
    assert Attribution(ctx).identity_check(DAY) == (True, 0.0)


# --------------------------------------------------------------------------- #
# AC 2 — roll-ups
# --------------------------------------------------------------------------- #


def test_us_t14_ac2_rollups_by_symbol_side_and_month(ctx: Context) -> None:
    ctx.repos.symbol_pnl.upsert_many(
        DAY,
        [
            {"symbol": "BTCUSDT", "side": str(PositionSide.LONG), "net_pnl": 100.0},
            {"symbol": "ETHUSDT", "side": str(PositionSide.SHORT), "net_pnl": -40.0},
        ],
    )
    ctx.repos.symbol_pnl.upsert_many(
        date(2026, 2, 3),
        [{"symbol": "BTCUSDT", "side": str(PositionSide.SHORT), "net_pnl": 25.0}],
    )
    start, end = DAY, date(2026, 2, 28)

    assert ctx.repos.symbol_pnl.by_symbol(start, end) == {"BTCUSDT": 125.0, "ETHUSDT": -40.0}
    assert ctx.repos.symbol_pnl.by_side(start, end) == {"long": 100.0, "short": -15.0}
    assert ctx.repos.symbol_pnl.by_month(start, end) == {"2026-01": 60.0, "2026-02": 25.0}
    assert ctx.repos.symbol_pnl.by_month(start, end, symbol="BTCUSDT") == {
        "2026-01": 100.0,
        "2026-02": 25.0,
    }


def test_us_t14_ac2_contribution_share_identifies_concentration(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)
    Attribution(ctx).compute_day(DAY, now)

    by_symbol = ctx.repos.symbol_pnl.by_symbol(DAY, DAY)
    total = sum(by_symbol.values())
    shares = {s: v / total for s, v in by_symbol.items()}

    assert sum(shares.values()) == pytest.approx(1.0)
    assert max(shares.values()) == pytest.approx(99.18 / 138.88, abs=1e-6)


# --------------------------------------------------------------------------- #
# AC 4 — trade episodes
# --------------------------------------------------------------------------- #


def test_us_t14_ac4_an_open_to_flat_episode_records_days_pnl_mae_and_signals(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """Long BTC at noon on day 0, flat at noon on day 1, with a dip in between."""
    attribution = Attribution(ctx)
    gateway.set_book("BTCUSDT", 99.99, 100.01, mark=100.0)
    _save_signal(ctx, DAY, "BTCUSDT", 0.8)
    _save_signal(ctx, NEXT_DAY, "BTCUSDT", -0.4)

    _snapshot(ctx, clock, T0)
    gateway.sim.apply_fill(
        symbol="BTCUSDT", side=Side.BUY, qty=10.0, price=100.0, ts_ms=T0 + 11 * HOUR_MS, is_maker=False
    )
    _snapshot(ctx, clock, T0 + 12 * HOUR_MS)
    gateway.set_mark("BTCUSDT", 92.0)  # -80 unrealised: the worst point of the trade
    _snapshot(ctx, clock, T0 + 18 * HOUR_MS)
    _book_everything(ctx, T0 + 23 * HOUR_MS)
    attribution.compute_day(DAY, T0 + DAY_MS - 1)
    assert attribution.update_trades(DAY, T0 + DAY_MS - 1) == 1

    day1 = T0 + DAY_MS
    gateway.set_mark("BTCUSDT", 105.0)
    _snapshot(ctx, clock, day1)
    gateway.sim.apply_fill(
        symbol="BTCUSDT",
        side=Side.SELL,
        qty=10.0,
        price=105.0,
        ts_ms=day1 + 11 * HOUR_MS,
        is_maker=False,
    )
    _snapshot(ctx, clock, day1 + 12 * HOUR_MS)
    _book_everything(ctx, day1 + 12 * HOUR_MS)
    attribution.compute_day(NEXT_DAY, day1 + 12 * HOUR_MS)

    assert attribution.update_trades(NEXT_DAY, day1 + 12 * HOUR_MS) == 1

    closed = ctx.repos.trades.closed()
    assert len(closed) == 1
    trade = closed[0]
    assert trade["symbol"] == "BTCUSDT"
    assert trade["side"] == str(PositionSide.LONG)
    assert trade["open_ts"] == T0 + 12 * HOUR_MS
    assert trade["close_ts"] == day1 + 12 * HOUR_MS
    assert trade["days"] == pytest.approx(1.0)
    assert trade["mae"] == pytest.approx(80.0)
    assert trade["entry_signal"] == pytest.approx(0.8)
    assert trade["exit_signal"] == pytest.approx(-0.4)
    # The episode's P&L is the attributed net over the days it spanned.
    expected = sum(
        r["net_pnl"] for r in ctx.repos.symbol_pnl.between(DAY, NEXT_DAY) if r["symbol"] == "BTCUSDT"
    )
    assert trade["pnl"] == pytest.approx(expected)
    assert ctx.repos.trades.open_trade("BTCUSDT") is None


def test_us_t14_ac4_a_position_still_open_stays_open(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)
    attribution = Attribution(ctx)
    attribution.compute_day(DAY, now)

    assert attribution.update_trades(DAY, now) == 2

    assert ctx.repos.trades.closed() == []
    btc = ctx.repos.trades.open_trade("BTCUSDT")
    eth = ctx.repos.trades.open_trade("ETHUSDT")
    assert btc is not None and btc["side"] == str(PositionSide.LONG)
    assert eth is not None and eth["side"] == str(PositionSide.SHORT)
    assert btc["max_notional"] == pytest.approx(1100.0)  # 10 x 110 at the day's high mark


def test_us_t14_ac4_rerunning_a_day_does_not_duplicate_episodes(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    now = _synthetic_day(ctx, gateway, clock)
    attribution = Attribution(ctx)
    attribution.compute_day(DAY, now)
    attribution.update_trades(DAY, now)

    attribution.update_trades(DAY, now)

    rows = ctx.repos.trades.db.query("SELECT trade_key FROM trades")
    assert len(rows) == 2


def test_us_t14_ac4_a_position_flattening_with_no_recorded_episode_is_ignored(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """The engine started mid-position: there is no episode to close, and no crash."""
    gateway.set_book("BTCUSDT", 99.99, 100.01, mark=100.0)
    gateway.set_position("BTCUSDT", 5.0, entry_price=100.0, mark_price=100.0)
    _snapshot(ctx, clock, T0)
    ctx.repos.trades.db.execute("DELETE FROM trades")
    gateway.sim.set_position("BTCUSDT", 0.0, 0.0, mark_price=100.0)
    _snapshot(ctx, clock, T0 + HOUR_MS)

    assert Attribution(ctx).update_trades(DAY, T0 + 2 * HOUR_MS) == 0
    assert ctx.repos.trades.closed() == []


def test_us_t14_ac1_a_position_carried_over_with_no_snapshot_inside_the_day(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """Yesterday's closing snapshot still describes today's book and its side."""
    gateway.set_book("BTCUSDT", 99.99, 100.01, mark=100.0)
    gateway.set_position("BTCUSDT", 4.0, entry_price=90.0, mark_price=100.0)
    _snapshot(ctx, clock, T0 + 23 * HOUR_MS)

    rows = Attribution(ctx).compute_day(NEXT_DAY, T0 + DAY_MS + HOUR_MS)

    assert [r["symbol"] for r in rows] == ["BTCUSDT"]
    assert rows[0]["side"] == str(PositionSide.LONG)
    assert rows[0]["avg_notional"] == pytest.approx(400.0)
    assert rows[0]["price_pnl"] == pytest.approx(0.0)  # nothing moved overnight
