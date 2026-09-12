"""Position drift monitoring between rebalances (PRD 5.9 / 12, US-T11 AC 1).

The daily rebalance is the only scheduled trading event (Invariant 9), which
means that for 23 hours a day nothing is supposed to move the book except price.
This monitor is how we find out when something did.

It distinguishes two failures that look the same in a position table:

* **Drift** — the position is still ours, but price has carried it away from the
  target we set. That is expected in small amounts and is only worth a ``WARN``
  past ``risk.drift_frac``. Inside the rebalance window it is not even that: the
  book is *supposed* to be moving then, so the check is suppressed.
* **An unexplained position change** — the quantity changed with no order of ours
  behind it. That is a liquidation, an ADL, a manual trade or a bug, and it is
  ``CRITICAL``: the engine's model of the book is wrong until a reconciliation
  says otherwise.

The monitor never trades and never reconciles: it returns what it found and the
runner decides. Keeping the decision out of here is what lets the risk path stay
the only thing that acts (Invariant 1).
"""

from __future__ import annotations

import math
from typing import Any

from aegis.core.context import Context
from aegis.core.types import Severity

#: Alert codes — stable, the dashboard filters on them.
ALERT_DRIFT = "DRIFT"
ALERT_UNEXPLAINED = "UNEXPLAINED_POSITION_CHANGE"

#: ``kind`` values in the returned records.
KIND_DRIFT = "drift"
KIND_UNEXPLAINED = "unexplained_position_change"

_QTY_TOL = 1e-9


class DriftMonitor:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._last_check_ms: int | None = None
        # Seeded from the persisted snapshot on the first check and maintained
        # here afterwards: the ``positions`` table belongs to the snapshot
        # service, and a monitor that wrote to it would race with it.
        self._last_qty: dict[str, float] | None = None

    @property
    def last_check_ms(self) -> int | None:
        return self._last_check_ms

    def check(self, now_ms: int, *, in_rebalance_window: bool) -> list[dict[str, Any]]:
        """One drift pass, at most every ``risk.drift_interval_min`` minutes.

        Returns one record per finding. A record with ``reconcile`` set is the
        monitor asking the runner for an immediate reconciliation (US-T11 AC 1);
        the monitor deliberately does not call the reconciler itself.
        """
        cfg = self.ctx.cfg
        interval_ms = cfg.risk.drift_interval_min * 60_000
        if self._last_check_ms is not None and now_ms - self._last_check_ms < interval_ms:
            return []
        since_ms = self._last_check_ms if self._last_check_ms is not None else now_ms - interval_ms
        self._last_check_ms = now_ms

        positions = self.ctx.gateway.positions()
        targets = self.ctx.repos.targets.latest_by_symbol()
        previous, baseline_known = self._previous_qty()

        findings: list[dict[str, Any]] = []
        for symbol in sorted(set(positions) | set(targets) | set(previous)):
            position = positions.get(symbol)
            current_qty = position.qty if position is not None else 0.0
            mark = position.mark_price if position is not None else 0.0
            current_notional = current_qty * mark
            target_notional = float(targets.get(symbol, 0.0))
            prior_qty = previous.get(symbol, 0.0)

            if (
                baseline_known
                and abs(current_qty - prior_qty) > _QTY_TOL
                and not self._own_activity(symbol, since_ms, now_ms)
            ):
                findings.append(
                    self._unexplained(symbol, prior_qty, current_qty, current_notional, target_notional)
                )
                continue

            if in_rebalance_window:
                continue
            fraction = _drift_fraction(current_notional, target_notional)
            if fraction > cfg.risk.drift_frac:
                findings.append(
                    self._drift(symbol, current_qty, current_notional, target_notional, fraction)
                )

        self._last_qty = {s: p.qty for s, p in positions.items() if p.qty != 0.0}
        return findings

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _previous_qty(self) -> tuple[dict[str, float], bool]:
        """Last known quantities, and whether they are a usable baseline.

        On the very first pass after a restart the persisted snapshot may be
        empty, and "every position appeared out of nowhere" is not a finding —
        it is the absence of history. That pass establishes the baseline instead.
        """
        if self._last_qty is None:
            persisted = {s: p.qty for s, p in self.ctx.repos.positions.all().items() if p.qty != 0.0}
            self._last_qty = persisted
            return persisted, bool(persisted)
        return self._last_qty, True

    def _own_activity(self, symbol: str, since_ms: int, now_ms: int) -> bool:
        """Did one of *our* orders touch this symbol in the window?"""
        for fill in self.ctx.repos.fills.between(since_ms, now_ms + 1):
            if fill.symbol == symbol:
                return True
        return any(o.symbol == symbol for o in self.ctx.repos.orders.open_orders())

    def _drift(
        self,
        symbol: str,
        current_qty: float,
        current_notional: float,
        target_notional: float,
        fraction: float,
    ) -> dict[str, Any]:
        record = {
            "kind": KIND_DRIFT,
            "symbol": symbol,
            "severity": str(Severity.WARN),
            "code": ALERT_DRIFT,
            "current_qty": current_qty,
            "current_notional": current_notional,
            "target_notional": target_notional,
            "drift_frac": fraction,
            "reconcile": False,
        }
        self.ctx.alerts.warn(
            ALERT_DRIFT,
            f"{symbol} drifted {fraction * 100:.1f}% from its last target "
            f"({current_notional:.2f} vs {target_notional:.2f} USDT)",
            record,
        )
        return record

    def _unexplained(
        self,
        symbol: str,
        prior_qty: float,
        current_qty: float,
        current_notional: float,
        target_notional: float,
    ) -> dict[str, Any]:
        record = {
            "kind": KIND_UNEXPLAINED,
            "symbol": symbol,
            "severity": str(Severity.CRITICAL),
            "code": ALERT_UNEXPLAINED,
            "previous_qty": prior_qty,
            "current_qty": current_qty,
            "current_notional": current_notional,
            "target_notional": target_notional,
            "reconcile": True,
        }
        self.ctx.alerts.critical(
            ALERT_UNEXPLAINED,
            f"{symbol} position moved {prior_qty} -> {current_qty} with no order of ours "
            f"(liquidation, ADL or manual trade); reconciliation requested",
            record,
        )
        return record


def _drift_fraction(current_notional: float, target_notional: float) -> float:
    """``|current - target| / |target|``; a position against a zero target is total drift."""
    if abs(target_notional) <= _QTY_TOL:
        return 0.0 if abs(current_notional) <= _QTY_TOL else math.inf
    return abs(current_notional - target_notional) / abs(target_notional)


__all__ = ["ALERT_DRIFT", "ALERT_UNEXPLAINED", "KIND_DRIFT", "KIND_UNEXPLAINED", "DriftMonitor"]
