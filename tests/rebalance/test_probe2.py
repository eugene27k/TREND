from __future__ import annotations

import json

from aegis.core.clock import FakeClock
from aegis.core.context import Context
from aegis.core.types import PlannedOrder, Side
from aegis.gateway.fake import FakeGateway
from aegis.rebalance.executor import RebalanceExecutor
from aegis.storage.db import json_dumps
from aegis.storage.repositories import Repositories
from tests.rebalance.conftest import BTC

HOUR_MS = 3_600_000


def test_probe_small_close_residual_is_dropped(ctx: Context, venue: FakeGateway, repos: Repositories, clock: FakeClock) -> None:
    venue.set_position(BTC, 0.2, 100.0)   # 20 USDT vs E = 10 000, band floor 25
    venue.set_fill_policy("none")
    plan = [PlannedOrder(symbol=BTC, side=Side.SELL, delta_notional=-20.0, delta_qty=-0.2,
                         current_qty=0.2, target_qty=0.0, reduce_only=True, risk_reducing=True,
                         sequence=0, n_slices=1)]
    outcome = RebalanceExecutor(ctx).execute("reb-1", plan, decision_mids={BTC: 100.0},
                                             end_ts_ms=clock.now_ms() + 60_000)
    print("STATUS", outcome.status, "RESIDUALS", outcome.residuals)
    print("POSITION STILL OPEN:", venue.positions())


def test_probe_infinity_json() -> None:
    s = json_dumps({"drift_frac": float("inf")})
    print("JSON:", s)
    try:
        json.loads(s, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as e:
        print("strict parser rejects:", e)
