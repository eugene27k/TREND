"""US-10 (CARRY, reused for TREND) — reconciliation; US-T11 AC 1 — liquidation/ADL."""

from __future__ import annotations

import pytest

from aegis.accounting.reconcile import (
    BREAK_BALANCE,
    BREAK_MISSING_FILL,
    BREAK_POSITION_MISMATCH,
    BREAK_UNEXPLAINED,
    LIQUIDATION_SUSPECTED,
    RECONCILIATION_BREAK,
    RECONCILIATION_RESOLVED,
    Reconciler,
)
from aegis.accounting.snapshots import SnapshotService
from aegis.core.clock import DAY_MS, FakeClock
from aegis.core.context import Context
from aegis.core.types import Severity, Side
from aegis.gateway.fake import FakeGateway
from aegis.storage.db import json_loads
from tests.accounting.conftest import HOUR_MS, T0


def _meta(ctx: Context, gateway: FakeGateway) -> None:
    """Publish the venue's lot steps so the position tolerance is the real one."""
    ctx.repos.symbol_meta.upsert_many(gateway.exchange_info().values(), T0)


def _reconciler(ctx: Context) -> Reconciler:
    return Reconciler(ctx, lookback_ms=DAY_MS)


def _alerts_of(ctx: Context, code: str) -> list[dict]:
    return [r for r in ctx.repos.alerts.recent(limit=100) if r["code"] == code]


# --------------------------------------------------------------------------- #
# positions()
# --------------------------------------------------------------------------- #


def test_us_10_ac1_matching_positions_reconcile(ctx: Context, gateway: FakeGateway) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 1.5, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)

    result = _reconciler(ctx).positions(T0 + 1000)

    assert result.ok
    assert result.breaks == ()
    assert ctx.repos.reconciliations.open_breaks() == []


def test_us_10_ac1_a_difference_within_one_lot_step_is_not_a_break(
    ctx: Context, gateway: FakeGateway
) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 1.5, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    # Step size is 0.001; the venue reports dust below one lot.
    gateway.sim.set_position("BTCUSDT", 1.5008, 100.0, mark_price=100.0)

    assert _reconciler(ctx).positions(T0 + 1000).ok


def test_us_10_ac1_a_position_mismatch_is_recorded_as_an_open_break(
    ctx: Context, gateway: FakeGateway
) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 1.5, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    # Our own fill moved the book and was recorded; the position table is stale.
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=0.5, price=100.0, ts_ms=T0 + 60_000)
    ctx.repos.fills.add_many(gateway.user_trades("BTCUSDT"))

    result = _reconciler(ctx).positions(T0 + HOUR_MS)

    assert not result.ok
    assert [b["kind"] for b in result.breaks] == [BREAK_POSITION_MISMATCH]
    assert result.breaks[0]["diff"] == pytest.approx(0.5)
    open_rows = ctx.repos.reconciliations.open_breaks()
    assert len(open_rows) == 1
    assert json_loads(open_rows[0]["breaks_json"], [])[0]["symbol"] == "BTCUSDT"


def test_us_t11_ac1_position_change_without_an_own_order_is_liquidation_suspected(
    ctx: Context, gateway: FakeGateway
) -> None:
    """A book that moves with no fill of ours behind it is a liquidation or ADL."""
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    gateway.sim.set_position("BTCUSDT", 0.0, 0.0, mark_price=100.0)  # liquidated

    result = _reconciler(ctx).positions(T0 + HOUR_MS)

    assert not result.ok
    assert [b["kind"] for b in result.breaks] == [BREAK_UNEXPLAINED]
    alerts = _alerts_of(ctx, LIQUIDATION_SUSPECTED)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == str(Severity.CRITICAL)


def test_us_t11_ac1_a_mismatch_explained_by_our_own_fills_is_not_a_liquidation(
    ctx: Context, gateway: FakeGateway
) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.SELL, qty=2.0, price=100.0, ts_ms=T0 + 60_000)
    _reconciler(ctx).fills(T0 + HOUR_MS)  # the engine books its own trade

    result = _reconciler(ctx).positions(T0 + HOUR_MS)

    assert [b["kind"] for b in result.breaks] == [BREAK_POSITION_MISMATCH]
    assert _alerts_of(ctx, LIQUIDATION_SUSPECTED) == []


# --------------------------------------------------------------------------- #
# balance()
# --------------------------------------------------------------------------- #


def test_us_10_ac2_ledger_derived_equity_matches_the_venue(ctx: Context, gateway: FakeGateway) -> None:
    from aegis.accounting.ledger import LedgerService

    SnapshotService(ctx).take(T0)  # anchor at 10 000 flat
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=5.0, price=100.0, ts_ms=T0 + HOUR_MS)
    gateway.settle_funding("BTCUSDT", 0.0001, T0 + 2 * HOUR_MS)
    LedgerService(ctx).sync(T0 + 3 * HOUR_MS)

    result = _reconciler(ctx).balance(T0 + 3 * HOUR_MS)

    assert result.ok, result.detail


def test_us_10_ac2_unrealised_pnl_alone_is_never_a_balance_break(ctx: Context, gateway: FakeGateway) -> None:
    SnapshotService(ctx).take(T0)
    gateway.set_position("BTCUSDT", 10.0, entry_price=100.0, mark_price=130.0)  # +300 unrealised

    result = _reconciler(ctx).balance(T0 + HOUR_MS)

    assert result.ok
    assert gateway.account().unrealized_pnl == pytest.approx(300.0)


def test_us_10_ac2_cash_missing_from_the_ledger_is_a_break(ctx: Context, gateway: FakeGateway) -> None:
    SnapshotService(ctx).take(T0)
    # The wallet moved without any income row to explain it.
    gateway.sim.wallet_balance -= 25.0

    result = _reconciler(ctx).balance(T0 + HOUR_MS)

    assert not result.ok
    assert result.breaks[0]["kind"] == BREAK_BALANCE
    assert result.breaks[0]["diff"] == pytest.approx(25.0)
    assert result.breaks[0]["tolerance"] == pytest.approx(0.01)


def test_us_10_ac2_an_open_position_does_not_hide_a_cash_break(
    ctx: Context, gateway: FakeGateway
) -> None:
    """Unrealised P&L is added to the ledger side, never used as slack.

    Anchor 10 000 cash, no income rows, a position worth +300 unrealised and
    25 USDT gone from the wallet: the venue's margin balance is 10 275 while the
    books say 10 300. Counting |unrealised| as tolerance would call a 25 USDT
    hole agreement (Invariant 2, Section 12 "any break").
    """
    SnapshotService(ctx).take(T0)
    gateway.set_position("BTCUSDT", 10.0, entry_price=100.0, mark_price=130.0)
    gateway.sim.wallet_balance -= 25.0

    result = _reconciler(ctx).balance(T0 + HOUR_MS)

    assert gateway.account().margin_balance == pytest.approx(10_275.0)
    assert not result.ok, result.detail
    assert result.breaks[0]["expected_equity"] == pytest.approx(10_300.0)
    assert result.breaks[0]["diff"] == pytest.approx(25.0)
    assert result.breaks[0]["tolerance"] == pytest.approx(0.01)


def test_us_10_ac1_positions_abstain_without_a_baseline_snapshot(
    ctx: Context, gateway: FakeGateway
) -> None:
    """A fresh database is not evidence that the venue liquidated us."""
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)

    result = _reconciler(ctx).positions(T0)

    assert result.ok
    assert "no baseline snapshot" in result.detail
    assert _alerts_of(ctx, LIQUIDATION_SUSPECTED) == []


def test_us_10_ac2_balance_abstains_without_a_baseline_snapshot(ctx: Context) -> None:
    result = _reconciler(ctx).balance(T0)

    assert result.ok
    assert "no baseline snapshot" in result.detail


# --------------------------------------------------------------------------- #
# fills()
# --------------------------------------------------------------------------- #


def test_us_10_ac3_fills_the_engine_missed_are_booked(ctx: Context, gateway: FakeGateway) -> None:
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.set_position("BTCUSDT", 1.0, entry_price=100.0, mark_price=100.0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=100.0, ts_ms=T0 + HOUR_MS)

    result = _reconciler(ctx).fills(T0 + 2 * HOUR_MS)

    assert not result.ok
    assert [b["kind"] for b in result.breaks] == [BREAK_MISSING_FILL]
    assert ctx.repos.fills.count() == 1
    assert ctx.repos.fills.between(T0, T0 + DAY_MS)[0].symbol == "BTCUSDT"


def test_us_10_ac3_rebooking_the_same_window_adds_nothing(ctx: Context, gateway: FakeGateway) -> None:
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.set_position("BTCUSDT", 1.0, entry_price=100.0, mark_price=100.0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=1.0, price=100.0, ts_ms=T0 + HOUR_MS)
    reconciler = _reconciler(ctx)
    reconciler.fills(T0 + 2 * HOUR_MS)

    second = reconciler.fills(T0 + 3 * HOUR_MS)

    assert second.ok
    assert ctx.repos.fills.count() == 1


def test_us_10_ac3_a_liquidating_trade_is_booked_although_the_position_is_gone(
    ctx: Context, gateway: FakeGateway
) -> None:
    """The venue closed the position itself, so the symbol is in no position table.

    Only the income feed still names it; without that, its realised P&L never
    reaches ``fills`` and the day's identity (US-T14 AC 3) cannot close.
    """
    from aegis.accounting.ledger import LedgerService

    _meta(ctx, gateway)
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.SELL, qty=2.0, price=90.0, ts_ms=T0 + 60_000)
    SnapshotService(ctx).take(T0 + 2 * 60_000)  # local state now agrees: flat
    LedgerService(ctx).sync(T0 + HOUR_MS)

    result = _reconciler(ctx).fills(T0 + HOUR_MS)

    assert [b["kind"] for b in result.breaks] == [BREAK_MISSING_FILL]
    booked = ctx.repos.fills.between(T0, T0 + DAY_MS)
    assert [f.symbol for f in booked] == ["BTCUSDT"]
    assert booked[0].realized_pnl == pytest.approx(-20.0)  # 2 x (90 - 100)


# --------------------------------------------------------------------------- #
# run_all() — alerting and escalation
# --------------------------------------------------------------------------- #


def test_us_10_ac4_a_fresh_break_warns_and_escalates_to_critical_after_the_window(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    gateway.sim.set_position("BTCUSDT", 1.0, 100.0, mark_price=100.0)
    reconciler = _reconciler(ctx)

    first = reconciler.run_all(T0 + 60_000)
    assert not first.ok
    warns = _alerts_of(ctx, RECONCILIATION_BREAK)
    assert [a["severity"] for a in warns] == [str(Severity.WARN)]

    critical_after = ctx.cfg.risk.reconciliation_critical_minutes * 60_000
    clock.advance(seconds=critical_after / 1000 + 60)
    reconciler.run_all(T0 + 60_000 + critical_after + 60_000)

    severities = [a["severity"] for a in _alerts_of(ctx, RECONCILIATION_BREAK)]
    assert str(Severity.CRITICAL) in severities


def test_us_10_ac4_resolving_closes_the_open_breaks_and_alerts(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    SnapshotService(ctx).take(T0)
    gateway.sim.set_position("BTCUSDT", 1.0, 100.0, mark_price=100.0)
    reconciler = _reconciler(ctx)
    reconciler.run_all(T0 + 60_000)
    assert ctx.repos.reconciliations.open_breaks()

    SnapshotService(ctx).take(T0 + 120_000)  # operator/engine refreshes local state
    clock.advance(minutes=2)
    result = reconciler.run_all(T0 + 120_000)

    assert result.ok
    assert ctx.repos.reconciliations.open_breaks() == []
    assert _alerts_of(ctx, RECONCILIATION_RESOLVED)


def test_us_t11_ac1_reconciling_before_the_snapshot_is_what_makes_a_liquidation_visible(
    ctx: Context, gateway: FakeGateway
) -> None:
    """The engine's daily order: sync, reconcile, *then* snapshot.

    ``SnapshotService.take`` rewrites the local position table from the very read
    ``positions()`` compares against, so a snapshot taken first makes every
    liquidation look like agreement — US-T11 AC 1 would never fire.
    """
    _meta(ctx, gateway)
    gateway.set_position("BTCUSDT", 2.0, entry_price=100.0, mark_price=100.0)
    snapshots = SnapshotService(ctx)
    snapshots.take(T0)
    gateway.sim.set_position("BTCUSDT", 0.0, 0.0, mark_price=100.0)  # liquidated

    result = _reconciler(ctx).run_all(T0 + HOUR_MS)
    snapshots.take(T0 + HOUR_MS)

    assert not result.ok
    assert [b["kind"] for b in result.breaks if b["kind"] == BREAK_UNEXPLAINED]
    assert [a["severity"] for a in _alerts_of(ctx, LIQUIDATION_SUSPECTED)] == [str(Severity.CRITICAL)]


def test_us_10_ac4_run_all_records_one_row_per_comparison(ctx: Context, gateway: FakeGateway) -> None:
    SnapshotService(ctx).take(T0)

    result = _reconciler(ctx).run_all(T0 + 1000)

    kinds = [r["kind"] for r in ctx.repos.reconciliations.db.query("SELECT kind FROM reconciliations")]
    assert kinds == ["positions", "balance", "fills"]
    assert result.ok and result.kind == "all"


def test_us_10_ac4_the_whole_day_reconciles_after_a_clean_pass(
    ctx: Context, gateway: FakeGateway, clock: FakeClock
) -> None:
    """A day of ordinary trading reconciles when the engine keeps its state fresh."""
    from aegis.accounting.ledger import LedgerService

    service = SnapshotService(ctx)
    service.take(T0)
    gateway.set_mark("BTCUSDT", 100.0)
    gateway.sim.apply_fill(symbol="BTCUSDT", side=Side.BUY, qty=3.0, price=100.0, ts_ms=T0 + HOUR_MS)
    clock.set(T0 + 2 * HOUR_MS)
    service.take(T0 + 2 * HOUR_MS)
    LedgerService(ctx).sync(T0 + 2 * HOUR_MS)
    reconciler = _reconciler(ctx)
    reconciler.fills(T0 + 2 * HOUR_MS)

    result = reconciler.run_all(T0 + 2 * HOUR_MS + 1000)

    assert result.ok, result.detail
    assert _alerts_of(ctx, RECONCILIATION_BREAK) == []
