"""Pre-registered kill rules (US-T13, Section 12).

The point of writing these down in advance is that a drawdown is exactly the
moment at which judgement is worst. Every rule here was decided when nothing was
at stake, and each one either blocks risk-increasing orders, flattens the book,
or halts the engine — never anything else.

``evaluate`` is a pure read of stored state plus the clock. It decides nothing
about *how* to act; the runner executes the returned actions. That split is what
makes the whole table testable without an exchange.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from aegis.core.clock import day_of, day_start_ms, from_ms
from aegis.core.context import Context
from aegis.core.types import IncomeType, Severity

# Rule names are stable: they appear in engine_state.blocks_json, on the
# dashboard kill-rule board and in alert contexts.
HARD_HALT = "hard_halt_drawdown"
DAILY_LOSS = "daily_loss"
BOOTSTRAP_P05 = "backtest_p05"
TRACKING_ERROR = "tracking_error"
REBALANCE_FAILURE = "rebalance_failure"
RECONCILIATION = "reconciliation_break"

ALL_RULES = (HARD_HALT, DAILY_LOSS, BOOTSTRAP_P05, TRACKING_ERROR, REBALANCE_FAILURE, RECONCILIATION)


@dataclass(frozen=True, slots=True)
class KillAction:
    rule: str
    severity: Severity
    block_risk_increasing: bool
    flatten: bool
    halt: bool
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    #: Rules that clear themselves once the condition passes; the rest need an
    #: operator with a written reason (US-T13 AC 3).
    @property
    def auto_clears(self) -> bool:
        return self.rule in (DAILY_LOSS, REBALANCE_FAILURE, RECONCILIATION)


class KillRules:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def evaluate(self, now_ms: int) -> list[KillAction]:
        actions: list[KillAction] = []
        for check in (
            self._hard_halt,
            self._daily_loss,
            self._bootstrap_p05,
            self._tracking_error,
            self._rebalance_failure,
            self._reconciliation,
        ):
            action = check(now_ms)
            if action is not None:
                actions.append(action)
                self.ctx.alerts.emit(action.severity, action.rule.upper(), action.message, action.context)
        return actions

    # -- individual rules --------------------------------------------------- #

    def _hard_halt(self, _now_ms: int) -> KillAction | None:
        """DD >= 25 %, or >= 1.5x the backtest max DD if that is lower.

        The backtest-relative threshold matters: a strategy whose backtest max
        drawdown was 14 % has no business reaching 25 % before anyone looks at
        it, so the halt tightens automatically when the evidence says it should.
        """
        cfg = self.ctx.cfg.risk
        row = self.ctx.repos.equity.latest()
        if row is None:
            return None
        dd = float(row["drawdown"] or 0.0)

        threshold = cfg.hard_halt_dd
        run = self.ctx.repos.backtest.latest_run()
        if run:
            from aegis.storage.db import json_loads

            backtest_dd = json_loads(run["metrics_json"], {}).get("max_drawdown")
            if isinstance(backtest_dd, int | float) and backtest_dd > 0:
                threshold = min(threshold, cfg.hard_halt_backtest_multiple * float(backtest_dd))

        if dd < threshold:
            return None
        return KillAction(
            rule=HARD_HALT,
            severity=Severity.CRITICAL,
            block_risk_increasing=True,
            flatten=True,
            halt=True,
            message=f"drawdown {dd:.1%} >= hard halt {threshold:.1%} — flattening and halting",
            context={"drawdown": dd, "threshold": threshold},
        )

    def _daily_loss(self, now_ms: int) -> KillAction | None:
        """A day worse than -6 % of equity blocks new risk until the next rebalance."""
        cfg = self.ctx.cfg.risk
        day = day_of(now_ms)
        start = day_start_ms(day)
        opening = self.ctx.repos.snapshots.last_before(start)
        latest = self.ctx.repos.snapshots.latest()
        if opening is None or latest is None:
            return None
        equity_open = float(opening["margin_balance"] or 0.0)
        if equity_open <= 0:
            return None
        # ``between`` is half-open, so ask for now_ms + 1: a transfer stamped at
        # exactly this instant is part of today and must not read as a loss.
        transfers = sum(
            r["amount"]
            for r in self.ctx.repos.ledger.between(start, now_ms + 1)
            if IncomeType.parse(str(r["income_type"])).is_transfer
        )
        change = float(latest["margin_balance"] or 0.0) - equity_open - transfers
        loss_frac = change / equity_open
        if loss_frac > -cfg.daily_loss_block:
            return None
        return KillAction(
            rule=DAILY_LOSS,
            severity=Severity.WARN,
            block_risk_increasing=True,
            flatten=False,
            halt=False,
            message=f"day P&L {loss_frac:.1%} of equity — risk-increasing orders blocked until the next rebalance",
            context={"loss_frac": loss_frac, "day": day.isoformat()},
        )

    def _bootstrap_p05(self, now_ms: int) -> KillAction | None:
        """Rolling 3-month live P&L below the backtest bootstrap 5th percentile.

        This is the rule that distinguishes "losing" from "broken": the bootstrap
        says what a bad-but-normal quarter looks like for this strategy, and only
        a result outside that is evidence of a changed world.
        """
        run = self.ctx.repos.backtest.latest_run()
        if run is None:
            return None
        p05 = self.ctx.repos.backtest.bootstrap(run["run_id"], "3m").get(5.0)
        if p05 is None:
            return None
        start = now_ms - 91 * 86_400_000
        rows = self.ctx.repos.symbol_pnl.between(day_of(start), day_of(now_ms))
        if len({r["day"] for r in rows}) < 60:
            return None  # not yet three months of live evidence
        live = sum(float(r["net_pnl"] or 0.0) for r in rows)
        if live >= p05:
            return None
        return KillAction(
            rule=BOOTSTRAP_P05,
            severity=Severity.WARN,
            block_risk_increasing=True,
            flatten=False,
            halt=False,
            message=(
                f"rolling 3-month P&L {live:.2f} below the backtest bootstrap p05 {p05:.2f} — "
                "risk-increasing blocked, post-mortem required"
            ),
            context={"live_3m": live, "bootstrap_p05": p05},
        )

    def _tracking_error(self, _now_ms: int) -> KillAction | None:
        """Out of tracking bounds for 14 consecutive days."""
        cfg = self.ctx.cfg.tracking
        latest = self.ctx.repos.tracking.latest()
        if latest is None:
            return None
        breach_days = int(latest["breach_days"] or 0)
        if breach_days < cfg.breach_days_block:
            return None
        return KillAction(
            rule=TRACKING_ERROR,
            severity=Severity.WARN,
            block_risk_increasing=True,
            flatten=False,
            halt=False,
            message=f"tracking bounds breached {breach_days} days — risk-increasing blocked",
            context={"breach_days": breach_days, "day": latest["day"]},
        )

    def _rebalance_failure(self, now_ms: int) -> KillAction | None:
        """Three consecutive days below 50 % completion."""
        cfg = self.ctx.cfg.rebalance
        streak = self.ctx.repos.rebalances.consecutive_failures(
            day_of(now_ms), cfg.failure_completion_pct, cfg.failure_days
        )
        if streak < cfg.failure_days:
            return None
        return KillAction(
            rule=REBALANCE_FAILURE,
            severity=Severity.CRITICAL,
            block_risk_increasing=True,
            flatten=False,
            halt=False,
            message=(
                f"{streak} consecutive rebalances below {cfg.failure_completion_pct:.0f} % "
                "completion — blocked until acknowledged"
            ),
            context={"streak": streak},
        )

    def _reconciliation(self, now_ms: int) -> KillAction | None:
        """Any open reconciliation break blocks new risk; 60 minutes makes it CRITICAL."""
        breaks = self.ctx.repos.reconciliations.open_breaks()
        if not breaks:
            return None
        oldest = min(int(b["ts"]) for b in breaks)
        age_min = (now_ms - oldest) / 60_000
        critical = age_min >= self.ctx.cfg.risk.reconciliation_critical_minutes
        return KillAction(
            rule=RECONCILIATION,
            severity=Severity.CRITICAL if critical else Severity.WARN,
            block_risk_increasing=True,
            flatten=False,
            halt=False,
            message=(
                f"{len(breaks)} open reconciliation break(s), oldest {age_min:.0f} min "
                f"({from_ms(oldest).isoformat()})"
            ),
            context={"count": len(breaks), "age_minutes": age_min},
        )

    # -- status board for the dashboard ------------------------------------- #

    def status_board(self, now_ms: int) -> list[dict[str, Any]]:
        """Every rule with its current state — the US-T18 AC 3 kill-rule board."""
        fired = {a.rule: a for a in self.evaluate(now_ms)}
        return [
            {"rule": rule, "active": rule in fired, "detail": fired[rule].message if rule in fired else ""}
            for rule in ALL_RULES
        ]

    @staticmethod
    def next_rebalance_clear_ms(now_ms: int) -> int:
        """When a daily-loss block lapses: the start of the next UTC day."""
        return day_start_ms(from_ms(now_ms) + timedelta(days=1))


__all__ = [
    "ALL_RULES",
    "BOOTSTRAP_P05",
    "DAILY_LOSS",
    "HARD_HALT",
    "REBALANCE_FAILURE",
    "RECONCILIATION",
    "TRACKING_ERROR",
    "KillAction",
    "KillRules",
]
