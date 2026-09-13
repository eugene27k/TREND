from __future__ import annotations

import pytest

from aegis.core.clock import FakeClock
from aegis.core.context import Context
from aegis.gateway.fake import FakeGateway
from aegis.rebalance.drift import DriftMonitor
from aegis.rebalance.executor import RebalanceExecutor
from aegis.storage.repositories import Repositories
from tests.rebalance.conftest import BTC, ETH, fill_hook, targets

HOUR_MS = 3_600_000


def test_probe_risk_cut_poisons_latest_targets(ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock) -> None:
    repos.rebalances.create("rb-1", "2026-09-08", clock.now_ms())
    repos.targets.save("rb-1", targets({BTC: 1000.0}))
    venue.set_position(BTC, 10.0, 100.0)
    assert repos.targets.latest_by_symbol() == {BTC: 1000.0}

    venue.set_fill_policy("callable", fill_hook())
    clock.advance(minutes=5)
    RebalanceExecutor(ctx).reduce_by({BTC: 0.25}, "margin_red", clock.now_ms())
    print("AFTER RISK CUT latest_by_symbol =", repos.targets.latest_by_symbol())

    clock.advance(minutes=70)
    found = DriftMonitor(ctx).check(clock.now_ms(), in_rebalance_window=False)
    print("DRIFT FINDINGS:", found)


def test_probe_flip_slice_id_collision(ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock) -> None:
    from aegis.core.config import AppConfig
    from aegis.rebalance.planner import build_plan, minute_volume
    from tests.rebalance.conftest import info, position

    venue.set_position(BTC, 10.0, 100.0)
    venue.set_fill_policy("callable", fill_hook())
    plan = build_plan(
        targets({BTC: -1000.0}),
        {BTC: position(BTC, 10.0, 100.0)},
        {BTC: info(BTC)},
        {BTC: 100.0},
        10_000.0,
        ctx.cfg,
        {BTC: minute_volume(2_880_000.0)},
        (BTC,),
    )
    print("PLAN:", [(p.symbol, p.sequence, p.target_qty, p.reduce_only) for p in plan])
    RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids={BTC: 100.0}, end_ts_ms=clock.now_ms() + HOUR_MS)
    rows = repos.slices.for_rebalance("reb-1")
    print("SLICE ROWS:", [(r["slice_id"], r["side"], r["qty"], r["outcome"], r["fill_qty"]) for r in rows])
    print("POSITION:", venue.positions())
