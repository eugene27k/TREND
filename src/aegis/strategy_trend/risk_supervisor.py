"""Exposure, margin and survivability supervision (US-T12, Section 12).

This runs every 60 s and is the backstop, not the primary control: exposure is
bounded inside ``size_targets`` before an order object exists (Invariant 8). The
supervisor exists because prices move between rebalances, so a book that was
inside the caps when it was planned can be outside them an hour later.

Everything here either observes or *reduces*. There is no path in this module
that can increase exposure (Invariant 1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aegis.core.context import Context
from aegis.core.types import (
    ExposureSnapshot,
    Position,
    RiskStatus,
    Severity,
    Targets,
)

#: Binance's lowest maintenance-margin tier for the majors. Used only for the
#: projected-liquidation estimate; the live margin ratio comes from the venue.
DEFAULT_MAINT_MARGIN_RATE = 0.005


@dataclass(frozen=True, slots=True)
class ReductionOrder:
    """A reduction the supervisor wants executed. Fractions are of |current|."""

    symbol: str
    fraction: float
    reason: str


class RiskSupervisor:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # -- the 60 s check ----------------------------------------------------- #

    def check(self, now_ms: int) -> ExposureSnapshot:
        """Read the book, classify it, alert on breaches. Never trades."""
        cfg = self.ctx.cfg
        account = self.ctx.gateway.account()
        positions = self.ctx.gateway.positions()
        equity = account.equity

        gross = sum(abs(p.notional) for p in positions.values())
        net = sum(p.notional for p in positions.values())
        largest_symbol, largest = "", 0.0
        for sym, p in positions.items():
            if abs(p.notional) > largest:
                largest_symbol, largest = sym, abs(p.notional)

        breaches: list[str] = []
        if equity > 0:
            if gross > cfg.caps.gross * equity:
                breaches.append("gross")
            if abs(net) > cfg.caps.net * equity:
                breaches.append("net")
            if largest > cfg.caps.single * equity:
                breaches.append(f"single:{largest_symbol}")

        margin_ratio = account.margin_ratio
        if margin_ratio >= cfg.risk.margin_red:
            status = RiskStatus.RED
        elif margin_ratio >= cfg.risk.margin_amber or breaches:
            status = RiskStatus.AMBER
        else:
            status = RiskStatus.GREEN

        snapshot = ExposureSnapshot(
            ts_ms=now_ms,
            equity=equity,
            gross=gross,
            net=net,
            largest_abs=largest,
            largest_symbol=largest_symbol,
            margin_ratio=margin_ratio,
            available_balance=account.available_balance,
            n_long=sum(1 for p in positions.values() if p.qty > 0),
            n_short=sum(1 for p in positions.values() if p.qty < 0),
            status=status,
            breaches=tuple(breaches),
            survivable_move=self.survivable_move(positions, equity),
        )

        self._alert(snapshot)
        self.ctx.repos.snapshots.add(account, list(positions.values()), gross=gross, net=net)
        return snapshot

    def _alert(self, s: ExposureSnapshot) -> None:
        if s.breaches:
            self.ctx.alerts.warn(
                "CAP_BREACH",
                f"caps exceeded: {', '.join(s.breaches)} (gross {s.gross_x:.2f}x, net {s.net_x:+.2f}x)",
                {"gross_x": s.gross_x, "net_x": s.net_x, "breaches": list(s.breaches)},
            )
        if s.status is RiskStatus.RED:
            self.ctx.alerts.emit(
                Severity.CRITICAL,
                "MARGIN_RED",
                f"margin ratio {s.margin_ratio:.1%} — reducing every position",
                {"margin_ratio": s.margin_ratio},
            )
        elif s.margin_ratio >= self.ctx.cfg.risk.margin_amber:
            self.ctx.alerts.warn(
                "MARGIN_AMBER",
                f"margin ratio {s.margin_ratio:.1%} — risk-increasing blocked",
                {"margin_ratio": s.margin_ratio},
            )

    # -- reductions the supervisor asks for --------------------------------- #

    def reductions(
        self, snapshot: ExposureSnapshot, positions: Mapping[str, Position]
    ) -> list[ReductionOrder]:
        """What must shrink, and by how much (US-T12 AC 2).

        ``red`` outranks a cap breach: reduce *everything* by 25 % rather than
        computing a precise trim, because at a 35 % margin ratio the priority is
        distance from liquidation, not elegance.
        """
        cfg = self.ctx.cfg
        if snapshot.status is RiskStatus.RED:
            frac = cfg.risk.red_reduce_frac
            return [ReductionOrder(s, frac, "margin_red") for s in positions]

        out: list[ReductionOrder] = []
        equity = snapshot.equity
        if equity <= 0 or not snapshot.breaches:
            return out

        # Single-asset breaches are trimmed symbol by symbol; gross and net are
        # scaled across the book. Applying single first means the proportional
        # scalings that follow are computed on an already-legal book.
        trimmed: dict[str, float] = {}
        limit = cfg.caps.single * equity
        for sym, p in positions.items():
            if abs(p.notional) > limit:
                trimmed[sym] = 1.0 - limit / abs(p.notional)

        remaining = {s: abs(p.notional) * (1.0 - trimmed.get(s, 0.0)) for s, p in positions.items()}
        signed = {s: p.notional * (1.0 - trimmed.get(s, 0.0)) for s, p in positions.items()}

        gross_after = sum(remaining.values())
        if gross_after > cfg.caps.gross * equity:
            scale = (cfg.caps.gross * equity) / gross_after
            for s in remaining:
                trimmed[s] = 1.0 - (1.0 - trimmed.get(s, 0.0)) * scale

        net_after = sum(signed.values())
        net_limit = cfg.caps.net * equity
        if abs(net_after) > net_limit:
            excess = abs(net_after) - net_limit
            side = 1.0 if net_after > 0 else -1.0
            side_gross = sum(abs(v) for v in signed.values() if v * side > 0)
            if side_gross > 0:
                scale = max(0.0, 1.0 - excess / side_gross)
                for s, v in signed.items():
                    if v * side > 0:
                        trimmed[s] = 1.0 - (1.0 - trimmed.get(s, 0.0)) * scale

        for sym, frac in trimmed.items():
            if frac > 1e-9:
                out.append(ReductionOrder(sym, min(1.0, frac), "cap_breach"))
        return out

    # -- the survivable-downtime rule (US-T12 AC 3, Aegis US-19 AC 5) -------- #

    def survivable_move(
        self,
        positions: Mapping[str, Position],
        equity: float,
        maint_margin_rate: float = DEFAULT_MAINT_MARGIN_RATE,
    ) -> float:
        """The adverse price move the net book survives before liquidation.

        Modelled as a single common shock against the *net* position, which is
        the honest worst case for a correlated crypto book: in a real crash the
        longs and shorts do not conveniently offset.

        Returns the fraction (0.40 = "survives a 40 % move"), or 1.0 when the
        book is flat — an empty book survives anything.
        """
        if equity <= 0:
            return 0.0
        net = abs(sum(p.notional for p in positions.values()))
        gross = sum(abs(p.notional) for p in positions.values())
        if net <= 0:
            return 1.0
        # Liquidation when equity - net*move <= maint_margin_rate * gross.
        return max(0.0, (equity - maint_margin_rate * gross) / net)

    def survives_downtime(self, positions: Mapping[str, Position], equity: float) -> bool:
        """True when a ``shock_price`` move over ``max_expected_downtime_h`` is survivable."""
        return self.survivable_move(positions, equity) > self.ctx.cfg.risk.shock_price

    def would_breach_downtime_rule(self, targets: Targets, equity: float) -> bool:
        """Would this *proposed* book fail the survivability rule? (blocks the rebalance)

        The supervisor is asked before the orders are planned, which is the only
        point at which refusing costs nothing.

        Worth knowing when reading the alerts: inside the caps this can never
        fire. A 40 % move against the 1.5x net cap costs 0.6 E and leaves 0.4 E,
        so the caps already guarantee survival. The rule binds only on a book
        that has drifted outside the caps on price moves — which is precisely
        what the 60 s supervisor exists to catch, and why the check is here
        rather than being folded into the sizing step.
        """
        if equity <= 0:
            return True
        net = abs(targets.net)
        gross = targets.gross
        if net <= 0:
            return False
        survivable = (equity - DEFAULT_MAINT_MARGIN_RATE * gross) / net
        return survivable <= self.ctx.cfg.risk.shock_price

    # -- ADL and fee balance ------------------------------------------------ #

    def adl_reductions(self, positions: Mapping[str, Position]) -> list[ReductionOrder]:
        """Short positions at ADL quantile >= 4 get trimmed (US-T12 AC 4).

        A high ADL quantile means the venue has us near the front of the queue to
        be auto-deleveraged if someone else blows up — a position we are about to
        lose control of is one to make smaller first.
        """
        threshold = self.ctx.cfg.risk.adl_reduce_quantile
        frac = self.ctx.cfg.risk.adl_reduce_frac
        out = []
        for sym, p in positions.items():
            if p.qty < 0 and p.adl_quantile >= threshold:
                out.append(ReductionOrder(sym, frac, f"adl_quantile_{p.adl_quantile}"))
                self.ctx.alerts.warn(
                    "ADL_QUANTILE",
                    f"{sym} ADL quantile {p.adl_quantile} >= {threshold} — reducing {frac:.0%}",
                    {"symbol": sym, "quantile": p.adl_quantile},
                )
        return out

    def check_bnb_balance(self, now_ms: int) -> float | None:
        """Days of fee cover left in BNB (US-T12 AC 5). None when unavailable."""
        try:
            balance = self.ctx.gateway.bnb_balance()
        except Exception:  # a missing balance must not stop the supervisor
            return None
        day_ms = 86_400_000
        fees = self.ctx.repos.ledger.sum_by_type(now_ms - 30 * day_ms, now_ms).get("COMMISSION", 0.0)
        daily = abs(fees) / 30.0
        if daily <= 0:
            return None
        days = balance / daily
        if days < self.ctx.cfg.risk.bnb_min_days:
            self.ctx.alerts.warn(
                "BNB_LOW",
                f"BNB fee balance covers {days:.1f} days",
                {"balance": balance, "days": days},
            )
        return days


__all__ = ["DEFAULT_MAINT_MARGIN_RATE", "ReductionOrder", "RiskSupervisor"]
