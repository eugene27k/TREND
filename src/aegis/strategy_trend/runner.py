"""The engine loop (PRD 5.11, Section 12, Locked Decision 6).

One pass of :meth:`TrendRunner.tick` is the whole bot. The ordering inside it is
the design, not an accident:

1. **The risk path runs first and runs always** — even paused, even halted, even
   mid-rebalance. A supervisor reading, an ADL trim, a delisting close and the
   kill rules do not wait for a scheduled activity to finish, because the only
   thing they can do is reduce (Invariant 1).
2. **Accounting next**, so the governor and the kill rules judge today's equity
   rather than yesterday's.
3. **The trading path last**, and only when nothing above blocked it.

``tick`` takes the time as an argument and never sleeps, so the entire daily
cycle — including the 00:05 rebalance, a mid-rebalance restart and a governor
cut — is exercised in tests by advancing a ``FakeClock``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.accounting.attribution import Attribution
from aegis.accounting.ledger import LedgerService
from aegis.accounting.reconcile import Reconciler
from aegis.accounting.snapshots import SnapshotService
from aegis.analytics.engine import MetricsEngine
from aegis.backtest_trend.tracking import TrackingService
from aegis.bars.service import BarService
from aegis.core.clock import DAY_MS, at_utc, day_of, month_key
from aegis.core.context import Context
from aegis.core.errors import AegisError, ExchangeUnreachable, GatewayError, RateLimited
from aegis.core.types import EngineState, MetricValue, Phase, RiskStatus, Severity
from aegis.ops import reports as rp
from aegis.ops.controls import FLATTEN_REQUEST, Controls
from aegis.ops.resources import ResourceMonitor
from aegis.portfolio.governor import governor, is_downward
from aegis.portfolio.sizing import size_targets
from aegis.rebalance.drift import DriftMonitor
from aegis.rebalance.executor import RebalanceExecutor
from aegis.rebalance.planner import build_plan, minute_volume
from aegis.riskmodel.estimators import build_risk_model
from aegis.signals.engine import compute_signal
from aegis.storage.db import json_dumps, json_loads
from aegis.strategy_trend import scheduler as sch
from aegis.strategy_trend.kill_rules import KillRules
from aegis.strategy_trend.machine import StateMachine
from aegis.strategy_trend.risk_supervisor import RiskSupervisor
from aegis.universe.service import UniverseService
from aegis.universe.status_watch import StatusWatch


@dataclass(slots=True)
class TickReport:
    """What one pass did — returned for tests and written to the engine context."""

    now_ms: int
    jobs: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    state: str = ""

    def note(self, action: str) -> None:
        self.actions.append(action)


class TrendRunner:
    def __init__(
        self,
        ctx: Context,
        *,
        heartbeat: Any = None,
        reporter: Any = None,
        report_sender: Any = None,
    ) -> None:
        self.ctx = ctx
        self.machine = StateMachine(ctx.repos, ctx.clock, phase=Phase(ctx.cfg.phase.current))
        self.schedule = sch.build_schedule(ctx.cfg)
        self.bars = BarService(ctx)
        self.universe = UniverseService(ctx)
        self.status_watch = StatusWatch(ctx)
        self.supervisor = RiskSupervisor(ctx)
        self.kill_rules = KillRules(ctx)
        self.executor = RebalanceExecutor(ctx)
        self.drift = DriftMonitor(ctx)
        self.ledger = LedgerService(ctx)
        self.snapshots = SnapshotService(ctx)
        self.reconciler = Reconciler(ctx)
        self.attribution = Attribution(ctx)
        self.metrics = MetricsEngine(ctx)
        self.controls = Controls(ctx)
        self.tracking = TrackingService(ctx)
        self.resources = ResourceMonitor()
        self.heartbeat = heartbeat
        self.reporter = reporter
        #: ``(body) -> delivered``. A report is *stored* by the reporter and
        #: *sent* here; ``reports.delivered`` must mean the operator has it.
        self.report_sender = report_sender
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self, now_ms: int | None = None) -> TickReport:
        """Load persisted state and resume anything that was in flight."""
        now = now_ms if now_ms is not None else self.ctx.clock.now_ms()
        report = TickReport(now_ms=now)
        self.machine.load()
        self._started = True

        self.apply_account_settings(now)

        unfinished = self.ctx.repos.rebalances.unfinished()
        if unfinished is not None:
            rebalance_id = str(unfinished["rebalance_id"])
            self.ctx.alerts.info(
                "REBALANCE_RESUME",
                f"resuming {rebalance_id} from cursor {unfinished['cursor']}",
                {"rebalance_id": rebalance_id, "cursor": unfinished["cursor"]},
            )
            if self.machine.state.state is not EngineState.REBALANCING:
                self.machine.state.state = EngineState.REBALANCING
                self.machine.save()
            end = self._window_end_ms(now)
            if now < end:
                # The stored plan carries risk-increasing legs. A block, a halt
                # or safe mode raised while the process was down forbids those,
                # so they are left behind and only the reductions are replayed
                # (Invariant 1) — exactly as a freshly planned rebalance is
                # filtered below.
                state = self.machine.state
                reducing_only = bool(state.halted or state.stopped or state.paused or state.blocks)
                # Safe mode is deliberately not in that list: the flag we just
                # loaded records the fault that killed the process, not a live
                # reading. If the venue is still unreachable the resume fails and
                # the next tick re-enters safe mode; treating the stale flag as
                # binding would disable US-T09 AC 4 for the one crash that causes
                # it most often.
                self.executor.resume(rebalance_id, end, reducing_only=reducing_only)
                report.note(f"resumed:{rebalance_id}")
            else:
                # The window has closed; abandon rather than trade against stale targets.
                self.ctx.repos.rebalances.set_status(rebalance_id, "window_end", ended_ts=now)
                report.note(f"abandoned:{rebalance_id}")
            self.machine.to(EngineState.IDLE)
        report.state = str(self.machine.state.state)
        return report

    def run(self, *, max_ticks: int | None = None) -> None:  # pragma: no cover - the loop
        if not self._started:
            self.start()
        ticks = 0
        while not self.machine.state.stopped:
            now = self.ctx.clock.now_ms()
            self.tick(now)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            self.ctx.clock.sleep(self.schedule.next_wake_ms(self.ctx.clock.now_ms()))

    # ------------------------------------------------------------------ #
    # One pass
    # ------------------------------------------------------------------ #

    def tick(self, now_ms: int) -> TickReport:
        if not self._started:
            self.start(now_ms)
        report = TickReport(now_ms=now_ms, jobs=self.schedule.due(now_ms))

        # The API process appends operator actions to control_log and never
        # touches engine_state, so the engine stays the single writer of its own
        # state. Applying the queue first — and reloading afterwards — means a
        # pause typed a second ago is respected by this very tick.
        self._drain_controls(now_ms, report)
        state = self.machine.load()

        if state.stopped:
            report.state = str(state.state)
            return report

        try:
            self._risk_path(now_ms, report)
            self._accounting(now_ms, report)
            self._trading_path(now_ms, report)
            self._periodic(now_ms, report)
            self.machine.leave_safe_mode()
        except (ExchangeUnreachable, RateLimited) as exc:
            # Safe mode: risk-reducing orders only, alert every 15 minutes.
            self.machine.enter_safe_mode(str(exc))
            self.ctx.alerts.warn("SAFE_MODE", f"exchange unreachable: {exc}", {"error": str(exc)})
            report.note("safe_mode")
        except GatewayError as exc:
            self.ctx.alerts.emit(Severity.CRITICAL, "GATEWAY_ERROR", str(exc), {"error": str(exc)})
            report.note("gateway_error")

        self._beat(now_ms, report)
        report.blocks = sorted(self.machine.state.blocks)
        report.state = str(self.machine.state.state)
        self.machine.state.context["last_tick"] = now_ms
        return report

    def apply_account_settings(self, now_ms: int) -> list[str]:
        """Locked Decision 1: 5x leverage, cross margin, on every universe symbol.

        Leverage here is a *margin allocation* setting, not a risk setting —
        actual exposure is governed by the Section 5.5 caps — but leaving it at
        the venue's default changes the maintenance-margin schedule the risk
        supervisor's survivability estimate is computed against, so the two
        would silently disagree.

        Only meaningful where orders reach a venue; paper and backtest have
        nothing to configure. Failures are alerted, never fatal: a symbol whose
        leverage could not be set is still tradeable, just on the wrong schedule.

        The account-wide settings (one-way position mode, multi-assets OFF, BNB
        fee discount ON) are not exposed by the futures API in a form we can set
        safely, and are operator prerequisites — see docs/RUNBOOK.md.
        """
        if not self.ctx.cfg.mode.sends_real_orders:
            return []
        applied: list[str] = []
        cfg = self.ctx.cfg
        for symbol in self.universe.all_symbols(now_ms):
            try:
                self.ctx.gateway.set_margin_type(symbol, cfg.account.margin_type)
                self.ctx.gateway.set_leverage(symbol, cfg.account.leverage)
            except GatewayError as exc:
                self.ctx.alerts.warn(
                    "ACCOUNT_SETTINGS",
                    f"could not set {symbol} to {cfg.account.leverage}x {cfg.account.margin_type}: {exc}",
                    {"symbol": symbol},
                )
                continue
            applied.append(symbol)
        return applied

    # ------------------------------------------------------------------ #
    # 0. The operator's inbox
    # ------------------------------------------------------------------ #

    def _drain_controls(self, now_ms: int, report: TickReport) -> None:
        """Apply anything the dashboard queued since the last tick."""
        context = self.machine.state.context
        last_id = int(context.get("last_control_id", 0) or 0)
        rows = self.ctx.repos.state.controls_after(last_id)
        if not rows:
            return
        applied = last_id
        for row in rows:
            applied = max(applied, int(row["id"]))
            payload = json_loads(row["payload_json"], {}) or {}
            if payload.get("source") != "api":
                continue  # the engine's own audit rows; applying them would loop
            action = str(row["action"])
            operator = str(row["operator"] or "api")
            reason = str(row["reason"] or "")
            confirm = str(payload.get("confirm", ""))
            try:
                self._apply_control(action, operator, reason, confirm, now_ms, report)
            except AegisError as exc:
                self.ctx.alerts.warn(
                    "CONTROL_REFUSED", f"{action}: {exc}", {"action": action, "operator": operator}
                )
                report.note(f"control_refused:{action}")
        self.machine.load()
        self.machine.state.context["last_control_id"] = applied
        self.machine.save()

    def _apply_control(
        self, action: str, operator: str, reason: str, confirm: str, now_ms: int, report: TickReport
    ) -> None:
        if action == "start":
            self.controls.start(operator, reason)
        elif action == "pause":
            self.controls.pause(operator, reason)
        elif action == "resume":
            self.controls.resume(operator, reason)
        elif action == "stop":
            self.controls.stop(operator, reason, confirm)
        elif action == "clear_halt":
            self.controls.clear_halt(operator, reason)
        elif action == "flatten_all":
            # The control records the request; flattening is an execution action
            # and goes through the executor under the usual slicing rules.
            self.controls.flatten_all(operator, reason, confirm)
            if self.ctx.gateway.positions():
                self.executor.flatten_all(f"operator:{operator}", now_ms)
            # The request is served, so it must not survive into the next tick as
            # a standing instruction to flatten again.
            self.machine.load()
            self.machine.state.context.pop(FLATTEN_REQUEST, None)
            self.machine.save()
        else:
            raise AegisError(f"unknown control action {action!r}")
        report.note(f"control:{action}")

    # ------------------------------------------------------------------ #
    # 1. Risk — always, whatever else is happening
    # ------------------------------------------------------------------ #

    def _risk_path(self, now_ms: int, report: TickReport) -> None:
        if self.schedule.is_due(sch.SUPERVISOR, now_ms):
            self.schedule.mark(sch.SUPERVISOR, now_ms)
            snapshot = self.supervisor.check(now_ms)
            positions = self.ctx.gateway.positions()
            # A cap breach is trimmed as soon as it is seen (Section 12: "within
            # 15 min"). The red-margin ladder is rate-limited to its configured
            # cadence instead: "reduce every position by 25 % per 5 minutes" means
            # per five minutes, and running it on the 60 s tick would cut three
            # quarters of the book in three and pay the spread four times over.
            cuts = self.supervisor.reductions(snapshot, positions)
            if snapshot.status is RiskStatus.RED and not self._interval_elapsed(
                "red_reduce", now_ms, self.ctx.cfg.risk.red_reduce_interval_s
            ):
                cuts = []
            if self._interval_elapsed("adl", now_ms, self.ctx.cfg.risk.adl_check_interval_s):
                cuts += self.supervisor.adl_reductions(positions)
            if cuts:
                self._risk_cut(
                    {c.symbol: c.fraction for c in cuts}, reason=cuts[0].reason, now_ms=now_ms, report=report
                )
            # The reading, not just the alert: Appendix D's daily line prints
            # "BNB fees 41 d" while cover is healthy, and only this path knows it.
            days = self.supervisor.check_bnb_balance(now_ms)
            if days is not None:
                self.machine.state.context[rp.BNB_DAYS_KEY] = days
                self.machine.save()
            self._record_resources(now_ms)

        if self.schedule.is_due(sch.STATUS_WATCH, now_ms):
            self.schedule.mark(sch.STATUS_WATCH, now_ms)
            for symbol in self.status_watch.check(now_ms):
                if self.ctx.gateway.positions().get(symbol):
                    self._risk_cut({symbol: 1.0}, reason="delisting", now_ms=now_ms, report=report)
            self.status_watch.check_funding_intervals(now_ms)

        if self.schedule.is_due(sch.DRIFT, now_ms):
            self.schedule.mark(sch.DRIFT, now_ms)
            drifts = self.drift.check(now_ms, in_rebalance_window=self._in_window(now_ms))
            if any(d.get("severity") == "CRITICAL" for d in drifts):
                self.reconciler.run_all(now_ms)
                report.note("drift_reconcile")

        self._apply_kill_rules(now_ms, report)

    def _apply_kill_rules(self, now_ms: int, report: TickReport) -> None:
        actions = self.kill_rules.evaluate(now_ms)
        self.machine.set_blocks(a.rule for a in actions if a.block_risk_increasing)
        for action in actions:
            if action.flatten and self.ctx.gateway.positions():
                self.executor.flatten_all(action.rule, now_ms)
                report.note(f"flatten:{action.rule}")
            if action.halt and not self.machine.state.halted:
                self.machine.halt(action.message)
                report.note(f"halt:{action.rule}")

    def _risk_cut(self, fractions: dict[str, float], *, reason: str, now_ms: int, report: TickReport) -> None:
        """Reductions go through RISK_ACTION whatever the engine was doing."""
        previous = self.machine.state.state
        if self.machine.can(EngineState.RISK_ACTION):
            self.machine.to(EngineState.RISK_ACTION, risk_reason=reason)
        self.executor.reduce_by(fractions, reason, now_ms)
        self.snapshots.take(self.ctx.clock.now_ms())
        report.note(f"risk_cut:{reason}:{len(fractions)}")
        if self.machine.state.state is EngineState.RISK_ACTION:
            self.machine.to(EngineState.IDLE if previous is EngineState.REBALANCING else previous)

    # ------------------------------------------------------------------ #
    # 2. Accounting — so the governor judges today's equity
    # ------------------------------------------------------------------ #

    def _accounting(self, now_ms: int, report: TickReport) -> None:
        if not self.schedule.is_due(sch.LEDGER_SYNC, now_ms):
            return
        self.schedule.mark(sch.LEDGER_SYNC, now_ms)
        self.ledger.sync(now_ms)
        # Reconcile *before* the snapshot: `take` rewrites the local position
        # table from the venue, and a table copied from the venue can never
        # disagree with it — a liquidation would become invisible (US-T11 AC 1).
        self.reconciler.run_all(now_ms)
        self.snapshots.take(now_ms)
        self.snapshots.update_equity_curve(day_of(now_ms), now_ms)
        self._update_governor(now_ms, report)

    def _update_governor(self, now_ms: int, report: TickReport) -> None:
        """5.7: downward transitions cut immediately; upward ones wait for the rebalance."""
        drawdown = self.snapshots.drawdown(now_ms)
        before = self.ctx.repos.governor.current_g()
        after = governor(drawdown, before, self.ctx.cfg.governor)
        if after == before:
            return

        down = is_downward(before, after)
        self.ctx.repos.governor.record(
            now_ms, drawdown, before, after, trigger=f"dd={drawdown:.4f}", applied=down
        )
        self.machine.set_governor(after)
        # The cut's own numbers are known before it is sent — Appendix D's body
        # announces what is about to happen — so the alert can carry them. A
        # venue that cannot be read must still not swallow the alert, so the
        # failure is held and re-raised (into safe mode) after it has gone out.
        positions: dict[str, Any] = {}
        unreachable: GatewayError | None = None
        if down:
            try:
                positions = dict(self.ctx.gateway.positions())
            except GatewayError as exc:
                unreachable = exc
        self.ctx.alerts.warn(
            "GOVERNOR",
            self._governor_message(drawdown, before, after, len(positions), down=down),
            {"dd": drawdown, "g_before": before, "g_after": after},
        )
        if not down:
            report.note(f"governor_up:{after:g}")
            return
        if unreachable is not None:
            raise unreachable

        # An outright proportional cut, taker allowed, paired with nothing.
        if positions:
            fraction = 1.0 - (after / before) if before > 0 else 1.0
            self._risk_cut(
                dict.fromkeys(positions, fraction), reason="governor", now_ms=now_ms, report=report
            )
        report.note(f"governor_down:{after:g}")

    def _governor_message(self, dd: float, before: float, after: float, n_orders: int, *, down: bool) -> str:
        """The Appendix D governor body (US-T17 AC 4) when a reporter is wired.

        Only for a cut: Appendix D's wording is "Immediate cut", which an upward
        transition — applied at the next rebalance, never immediately — would
        misdescribe. That one keeps the plain line.
        """
        plain = f"{before:g} -> {after:g} at drawdown {dd:.1%}"
        if self.reporter is None or not down:
            return plain
        snapshot = self.ctx.repos.snapshots.latest()
        equity = float(snapshot["margin_balance"] or 0.0) if snapshot else 0.0
        gross = abs(float(snapshot["gross_notional"] or 0.0)) if snapshot else 0.0
        if not equity or before <= 0:
            return plain
        gross_x = gross / equity
        try:
            return str(
                self.reporter.governor_alert(
                    dd=dd,
                    peak_equity=equity / (1.0 - dd) if dd < 1.0 else equity,
                    g_before=before,
                    g_after=after,
                    gross_before=gross_x,
                    gross_after=gross_x * (after / before),
                    n_orders=n_orders,
                )
            )
        except Exception:  # a report must never stop a risk action
            return plain

    # ------------------------------------------------------------------ #
    # 3. Trading — only when nothing above blocked it
    # ------------------------------------------------------------------ #

    def _trading_path(self, now_ms: int, report: TickReport) -> None:
        if self.schedule.is_due(sch.UNIVERSE, now_ms) or sch.universe_refresh_due(
            self.ctx.cfg, now_ms, self.ctx.repos.universe.latest_month()
        ):
            self.schedule.mark(sch.UNIVERSE, now_ms)
            if self.universe.refresh_if_due(now_ms) is not None:
                report.note("universe_refresh")
                # Entrants have never been configured at the venue.
                self.apply_account_settings(now_ms)

        symbols = self.universe.current_symbols(now_ms)
        if self.schedule.is_due(sch.BARS, now_ms) and symbols:
            self.schedule.mark(sch.BARS, now_ms)
            deadline = at_utc(day_of(now_ms), self.ctx.cfg.rebalance.bar_deadline_utc)
            _, missing = self.bars.ensure_day(symbols, self.bars.last_closed_day(now_ms), deadline)
            self.bars.sync_funding(symbols, now_ms)
            if missing:
                self.ctx.alerts.warn(
                    "BAR_MISSING",
                    f"{len(missing)} bar(s) missing at the deadline — deferring the rebalance",
                    {"symbols": sorted(missing)},
                )
                report.note(f"bars_missing:{len(missing)}")
            else:
                report.note("bars_ready")

        if not self.schedule.is_due(sch.REBALANCE, now_ms):
            return
        if not self.machine.state.may_rebalance():
            report.note("rebalance_skipped:not_tradeable")
            self.schedule.mark(sch.REBALANCE, now_ms)
            return
        self.schedule.mark(sch.REBALANCE, now_ms)
        self._rebalance(now_ms, report)

    def _rebalance(self, now_ms: int, report: TickReport) -> None:
        cfg = self.ctx.cfg
        symbols = self.universe.current_symbols(now_ms)
        if not symbols:
            report.note("rebalance_skipped:no_universe")
            return

        self.machine.to(EngineState.COMPUTING)
        day = self.bars.last_closed_day(now_ms)
        self.bars.forward_fill(symbols, day)

        signals, results = {}, []
        for symbol in symbols:
            closes = self.bars.closes(symbol, end=day)
            result = compute_signal(closes, cfg.signal, symbol, day)
            results.append(result)
            if result.warm:
                signals[symbol] = result.signal
        self.ctx.repos.signals.save_many(day, results, now_ms)

        held = [s for s, p in self.ctx.gateway.positions().items() if p.qty]
        returns = {s: self.bars.log_returns(s, end=day) for s in sorted(set(symbols) | set(held))}
        returns = {s: r for s, r in returns.items() if r}
        if not returns:
            report.note("rebalance_skipped:no_returns")
            self.machine.to(EngineState.IDLE)
            return
        risk_model = build_risk_model(returns, cfg.vol, cfg.cov)
        self.ctx.repos.risk_model.save(day, risk_model, now_ms)

        account = self.ctx.gateway.account()
        g = self.ctx.repos.governor.current_g()
        funding_ann = self.bars.predicted_funding(symbols)
        info = self.ctx.gateway.exchange_info()
        targets = size_targets(
            signals,
            dict(risk_model.vols),
            risk_model,
            account.equity,
            g,
            cfg,
            funding_ann=funding_ann,
            min_notionals={s: i.min_notional for s, i in info.items()},
        )

        # The survivable-downtime rule is asked *before* the orders are planned —
        # the only point at which refusing costs nothing (US-T12 AC 3).
        if self.supervisor.would_breach_downtime_rule(targets, account.equity):
            self.ctx.alerts.warn(
                "DOWNTIME_RULE",
                "the proposed book would not survive the configured shock — rebalance blocked",
                {"gross": targets.gross, "net": targets.net, "equity": account.equity},
            )
            report.note("rebalance_blocked:downtime_rule")
            self.machine.to(EngineState.IDLE)
            return

        positions = self.ctx.gateway.positions()
        marks = {s: self.ctx.gateway.mark_price(s) for s in sorted(set(symbols) | set(positions))}
        plan = build_plan(
            targets,
            positions,
            info,
            marks,
            account.equity,
            cfg,
            {s: minute_volume(self.bars.avg_daily_quote_volume(s)) for s in marks},
            symbols,
        )
        if not self.machine.state.may_increase_risk():
            before = len(plan)
            plan = [p for p in plan if p.risk_reducing]
            report.note(f"plan_reduced_only:{before}->{len(plan)}")

        # Keyed by the TRADING day, not the bar day. The 00:05 rebalance of the
        # 8th works the bar that closed at 00:00 on the 7th, and the operator —
        # and Appendix D's "rebalance 2026-09-08" line, and the 01:05 summary
        # that looks the row up by today's date — all mean the 8th.
        rebalance_id = f"rb-{day_of(now_ms).isoformat()}"
        self.ctx.repos.targets.save(rebalance_id, targets)
        if not plan:
            report.note("rebalance_noop")
            self.machine.to(EngineState.IDLE)
            return

        self.machine.to(EngineState.REBALANCING, rebalance_id=rebalance_id)
        outcome = self.executor.execute(
            rebalance_id,
            plan,
            decision_mids={s: marks[s] for s in marks},
            end_ts_ms=self._window_end_ms(now_ms),
        )
        report.note(f"rebalance:{outcome.status}:{outcome.completion_pct:.0f}%")
        # Record what we now hold rather than waiting for the accounting cycle.
        # The dashboard reads stored data only, so without this it shows the
        # pre-rebalance book for minutes after the trades — and the drift monitor
        # would compare the new targets against a stale position.
        self.snapshots.take(self.ctx.clock.now_ms())
        self.machine.to(EngineState.IDLE)

    # ------------------------------------------------------------------ #
    # 4. Periodic jobs
    # ------------------------------------------------------------------ #

    def _periodic(self, now_ms: int, report: TickReport) -> None:
        if self.schedule.is_due(sch.METRICS, now_ms):
            self.schedule.mark(sch.METRICS, now_ms)
            yesterday = day_of(now_ms - 86_400_000)
            self.attribution.compute_day(yesterday, now_ms)
            self.attribution.update_trades(yesterday, now_ms)
            ok, residual = self.attribution.identity_check(yesterday)
            if not ok:
                self.ctx.alerts.warn(
                    "ATTRIBUTION_IDENTITY_BREAK",
                    f"attribution residual {residual:.4f} USDT on {yesterday}",
                    {"day": yesterday.isoformat(), "residual": residual},
                )
            self.metrics.compute_all(now_ms)
            report.note("metrics")
            # US-T16 AC 5: re-run the reference over the live period and compare.
            # Without this the tracking-error kill rule can never fire, and the
            # dashboard's status board reads green forever.
            try:
                tracked = self.tracking.update(yesterday, now_ms)
            except Exception as exc:  # a comparison must never stop the engine
                self.ctx.alerts.warn("TRACKING_FAILED", str(exc), {"day": yesterday.isoformat()})
            else:
                if tracked is not None:
                    report.note(f"tracking:{'ok' if tracked.in_bounds else 'breach'}")

        if self.reporter is None:
            return
        # The 00:10 report covers the calendar day that has just *closed* — the
        # one whose rebalance summary is already final (US-T17 AC 1). Reporting
        # today at 00:10 would describe ten minutes and a rebalance still
        # running. The 01:05 summary is today's, whose window ended at 01:00.
        closed_day = day_of(now_ms - DAY_MS)
        rebalance_id = f"rb-{day_of(now_ms).isoformat()}"
        if self.schedule.is_due(sch.DAILY_REPORT, now_ms):
            # US-T17 AC 1 wants "day P&L by component" in the 00:10 body, but the
            # metrics job that attributes day D runs at 01:10 on D+1 — an hour
            # later. Attribute the closed day here if nothing has yet, or the
            # headline of every daily report would read n/a. The 01:10 job
            # recomputes the same day idempotently.
            with contextlib.suppress(Exception):
                if not self.ctx.repos.symbol_pnl.between(closed_day, closed_day):
                    self.attribution.compute_day(closed_day, now_ms)
        for job, method, kind, key in (
            (sch.DAILY_REPORT, "daily", rp.DAILY, closed_day),
            (sch.REBALANCE_REPORT, "rebalance_summary", rp.REBALANCE, rebalance_id),
        ):
            if self.schedule.is_due(job, now_ms):
                self.schedule.mark(job, now_ms)
                try:
                    body = getattr(self.reporter, method)(key, now_ms)
                    report.note(job)
                except Exception as exc:  # a report must never stop the engine
                    self.ctx.alerts.warn("REPORT_FAILED", f"{job}: {exc}", {"job": job})
                    continue
                period_key = key if isinstance(key, str) else key.isoformat()
                if self._send_report(kind, period_key, str(body)):
                    report.note(f"{job}:sent")

    def _send_report(self, kind: str, period_key: str, body: str) -> bool:
        """Deliver a stored body (US-T17 AC 1) and record that it got through.

        Storing is not sending: the reporter writes the body so the day can be
        reconstructed from the database, but the operator only has it once a sink
        took it, which is what ``reports.delivered`` — and the Operations page —
        mean. A dead channel leaves the row undelivered instead of raising.
        """
        if self.report_sender is None:
            return False
        try:
            delivered = bool(self.report_sender(body))
        except Exception as exc:  # a chat outage is not an engine fault
            self.ctx.alerts.warn("REPORT_FAILED", f"{kind}: {exc}", {"kind": kind})
            return False
        if delivered:
            self.ctx.repos.reports.mark_delivered(kind, period_key)
        return delivered

    def _beat(self, now_ms: int, report: TickReport) -> None:
        if self.heartbeat is None or not self.schedule.is_due(sch.HEARTBEAT, now_ms):
            return
        self.schedule.mark(sch.HEARTBEAT, now_ms)
        healthy = not self.machine.state.halted and not self.machine.state.safe_mode
        # The heartbeat is the last thing allowed to break the engine.
        with contextlib.suppress(Exception):
            self.heartbeat.beat(now_ms, healthy, json_dumps(report.actions))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _record_resources(self, now_ms: int) -> None:
        """US-T19 AC 2: the footprint must be *measured*, not budgeted.

        Sampled on the supervisor's 60 s tick so the CPU figure is a rate over a
        minute rather than the process's lifetime average, which on a long-running
        engine would flatten a rebalance spike into invisibility. A platform
        without /proc records nothing, and the dashboard shows "n/a" rather than
        a fabricated number.
        """
        rss, cpu = self.resources.measure(now_ms)
        values = []
        if rss is not None:
            values.append(MetricValue(self.ctx.strategy, "rss_mb", "7d", rss, now_ms, n_obs=1))
        if cpu is not None:
            values.append(MetricValue(self.ctx.strategy, "cpu_pct", "7d", cpu, now_ms, n_obs=1))
        if values:
            self.ctx.repos.metrics.save_many(values)

    def _interval_elapsed(self, name: str, now_ms: int, seconds: float) -> bool:
        """True the first time, then only once ``seconds`` have passed.

        Marking is a side effect, so a caller that asks has consumed the slot.
        The stamp lives in the engine state so a restart does not reset a rate
        limit into firing immediately.
        """
        key = f"last_{name}_ms"
        last = self.machine.state.context.get(key)
        if last is not None and now_ms - int(last) < seconds * 1000:
            return False
        self.machine.state.context[key] = now_ms
        self.machine.save()
        return True

    def _window_end_ms(self, now_ms: int) -> int:
        return at_utc(day_of(now_ms), self.ctx.cfg.rebalance.end_utc)

    def _in_window(self, now_ms: int) -> bool:
        day = day_of(now_ms)
        return at_utc(day, self.ctx.cfg.rebalance.time_utc) <= now_ms <= self._window_end_ms(now_ms)

    def status(self, now_ms: int) -> dict[str, Any]:
        """A compact snapshot for the dashboard and the CLI health check."""
        state = self.machine.state
        return {
            "state": str(state.state),
            "phase": str(state.phase),
            "paused": state.paused,
            "halted": state.halted,
            "safe_mode": state.safe_mode,
            "blocks": sorted(state.blocks),
            "governor_g": state.governor_g,
            "month": month_key(now_ms),
            "universe": self.universe.current_symbols(now_ms),
        }


def missing_symbols(fetched: set[str], wanted: Sequence[str]) -> list[str]:
    return sorted(set(wanted) - set(fetched))


def today(now_ms: int) -> date:
    return day_of(now_ms)


__all__ = ["TickReport", "TrendRunner", "missing_symbols", "today"]
