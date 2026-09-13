"""Report bodies — PRD Appendix D, rendered from stored data only.

Three rules hold everywhere in this module.

1. **The reporter never calls the exchange.** Every number comes out of the
   database (equity curve, snapshots, symbol P&L, rebalances, governor state,
   metrics, heartbeats, reconciliations). A report is a view of what the engine
   recorded; if it could query the venue it could show a number the engine never
   acted on, which is the opposite of what a report is for.
2. **A missing value renders as ``n/a``.** Never a zero, never an exception. The
   00:10 report must still be sent on the morning a table is empty — that
   emptiness is itself the thing the operator needs to see.
3. **The formats are Appendix D, character for character**, down to the middle
   dot separator, the U+2212 minus and the space used as a thousands separator.
   The operator reads these at a glance on a phone; stable shape is a feature.

Scheduling belongs to the runner (Section 13 job table). This module provides
the bodies and ``due_reports``, which answers "what has come due and has not
been sent yet" from the ``reports`` table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from aegis.core.clock import DAY_MS, at_utc, day_of, day_start_ms, month_key, month_start
from aegis.core.context import Context
from aegis.storage.db import json_loads

NA = "n/a"
SEP = " · "
MINUS = "−"  # U+2212 MINUS SIGN — Appendix D uses it, not the hyphen
TIMES = "×"
ARROW = "→"

DAILY = "daily"
REBALANCE = "rebalance"
WEEKLY = "weekly"
MONTHLY = "monthly"

#: ``engine_state.context`` keys the runner stashes readings in. They are
#: measurements of the world outside the database (a filesystem replica, a BNB
#: balance); the reporter prints them but never takes them itself.
BACKUP_LAG_KEY = "backup_lag_s"
BNB_DAYS_KEY = "bnb_days"

#: How far back ``due_reports`` looks for something it never sent.
DAILY_LOOKBACK_DAYS = 7
WEEKLY_LOOKBACK = 4
MONTHLY_LOOKBACK = 2


# --------------------------------------------------------------------------- #
# Formatting primitives
# --------------------------------------------------------------------------- #


def num(value: float | None, dp: int = 2) -> str:
    """``9874.1 -> "9 874.10"``; the thousands separator is a space (Appendix D)."""
    if value is None:
        return NA
    text = f"{abs(value):,.{dp}f}".replace(",", " ")
    return f"{MINUS}{text}" if value < 0 else text


def signed(value: float | None, dp: int = 2) -> str:
    """Explicit sign — a P&L component without one is ambiguous at a glance."""
    if value is None:
        return NA
    text = f"{abs(value):,.{dp}f}".replace(",", " ")
    return f"{MINUS}{text}" if value < 0 else f"+{text}"


def pct(value: float | None, dp: int = 1, *, scale: float = 100.0) -> str:
    return NA if value is None else f"{num(value * scale, dp)} %"


def signed_pct(value: float | None, dp: int = 2, *, scale: float = 100.0) -> str:
    return NA if value is None else f"{signed(value * scale, dp)} %"


def times(value: float | None, dp: int = 2) -> str:
    return NA if value is None else f"{num(value, dp)}{TIMES}"


def signed_times(value: float | None, dp: int = 2) -> str:
    return NA if value is None else f"{signed(value, dp)}{TIMES}"


def unit(text: str, suffix: str) -> str:
    """``"1.4" -> "1.4 bps"``, but ``n/a`` stays ``n/a`` — a unitless absence."""
    return text if text == NA else f"{text} {suffix}"


def g_text(value: float | None) -> str:
    """``1.0``, ``0.5``, ``0.25`` — the governor's own vocabulary."""
    if value is None:
        return NA
    text = f"{value:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _base_asset(symbol: str, quote: str) -> str:
    return symbol[: -len(quote)] if quote and symbol.endswith(quote) else symbol


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #


class Reporter:
    """Builds and persists the Appendix D bodies."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    @property
    def prefix(self) -> str:
        return self.ctx.cfg.telegram.prefix

    # -- daily ------------------------------------------------------------- #

    def daily(self, day: date, now_ms: int) -> str:
        """The 00:10 UTC report for the calendar day that just closed (US-T17 AC 1)."""
        end_ms = day_start_ms(day) + DAY_MS
        equity_row = self._equity_row(day)
        snapshot = self.ctx.repos.snapshots.last_before(end_ms - 1)
        equity = _float(equity_row["equity"]) if equity_row else self._snapshot_equity(snapshot)

        lines = [
            f"{self.prefix}{SEP}{DAILY}{SEP}{day.isoformat()}",
            self._equity_line(equity, equity_row, end_ms),
            self._pnl_line(day),
            self._exposure_line(day, snapshot, equity),
            self._vol_line(),
            self._rebalance_line(day),
            self._risk_line(snapshot, equity),
            self._ops_line(day),
            self._housekeeping_line(day),
        ]
        body = "\n".join(lines)
        self.ctx.repos.reports.save(DAILY, day.isoformat(), body, now_ms)
        return body

    def _equity_line(self, equity: float | None, row: Mapping[str, Any] | None, end_ms: int) -> str:
        day_change = None if row is None else _float(row["twr_factor"])
        since = None if row is None else _float(row["twr_index"])
        dd = None if row is None else _float(row["drawdown"])
        gov = self.ctx.repos.governor.last_before(end_ms - 1)
        g = _float(gov["g_after"]) if gov else 1.0
        head = unit(num(equity), "USDT")
        change = (
            NA
            if day_change is None and since is None
            else f"({signed_pct(None if day_change is None else day_change - 1.0)} day,"
            f" {signed_pct(None if since is None else since - 1.0)} since start)"
        )
        return f"Equity {head} {change}{SEP}DD from peak {pct(dd)}{SEP}g = {g_text(g)}"

    def _pnl_line(self, day: date) -> str:
        rows = self.ctx.repos.symbol_pnl.between(day, day)
        if not rows:
            return f"Net P&L day {NA}"
        parts = self.ctx.repos.symbol_pnl.components(day, day)
        return (
            f"Net P&L day {signed(parts['net_pnl'])} = price {signed(parts['price_pnl'])}"
            f"{SEP}funding {signed(parts['funding'])}{SEP}fees {signed(parts['fees'])}"
            f"{SEP}slippage {signed(parts['slippage'])}"
        )

    def _exposure_line(self, day: date, snapshot: Mapping[str, Any] | None, equity: float | None) -> str:
        if snapshot is None or not equity:
            return f"Exposure gross {NA}{SEP}net {NA}{SEP}{NA}{SEP}largest {NA}"
        positions = json_loads(snapshot["positions_json"], []) or []
        gross = _float(snapshot["gross_notional"])
        net = _float(snapshot["net_notional"])
        longs = sum(1 for p in positions if _float(p.get("qty")) and float(p["qty"]) > 0)
        shorts = sum(1 for p in positions if _float(p.get("qty")) and float(p["qty"]) < 0)
        universe = self.ctx.repos.universe.symbols(month_key(day))
        total = len(universe) if universe else longs + shorts
        flat = max(0, total - longs - shorts)
        return (
            f"Exposure gross {times(None if gross is None else gross / equity)}"
            f"{SEP}net {signed_times(None if net is None else net / equity)}"
            f"{SEP}{longs} long / {shorts} short / {flat} flat"
            f"{SEP}largest {self._largest(positions, equity)}"
        )

    def _largest(self, positions: Sequence[Mapping[str, Any]], equity: float) -> str:
        best: tuple[float, str, float] | None = None
        for p in positions:
            qty = _float(p.get("qty")) or 0.0
            mark = _float(p.get("mark")) or 0.0
            notional = qty * mark
            if best is None or abs(notional) > best[0]:
                best = (abs(notional), str(p.get("symbol", "")), notional)
        if best is None or best[0] == 0.0:
            return NA
        _, symbol, notional = best
        side = "long" if notional > 0 else "short"
        base = _base_asset(symbol, self.ctx.cfg.universe.quote_asset)
        return f"{base} {side} {times(abs(notional) / equity)}"

    def _vol_line(self) -> str:
        realised, _ = self._metric("realised_vol", "30d")
        target = self.ctx.cfg.sizing.sigma_target_portfolio
        sharpe_row = self._metric_row("sharpe", "since_inception")
        sharpe = None if sharpe_row is None else _float(sharpe_row["value"])
        n_obs = 0 if sharpe_row is None else int(sharpe_row["n_obs"] or 0)
        sharpe_text = NA if sharpe is None else num(sharpe, 2)
        return (
            f"Realised vol 30d {pct(realised)} (target {pct(target, 0)})"
            f"{SEP}Sharpe since start {sharpe_text} ({n_obs} obs)"
        )

    def _rebalance_line(self, day: date) -> str:
        row = self._scheduled_rebalance(day)
        if row is None:
            return f"Rebalance {NA}"
        minutes = None
        if row["ended_ts"] and row["started_ts"]:
            minutes = (int(row["ended_ts"]) - int(row["started_ts"])) / 60_000.0
        duration = unit(num(minutes, 0), "min")
        return (
            f"Rebalance {pct(_float(row['completion_pct']), 0, scale=1.0)} in {duration}"
            f"{SEP}maker {pct(_float(row['maker_ratio']), 0)}"
            f"{SEP}slippage {unit(num(_float(row['avg_slippage_bps']), 1), 'bps')}"
            f"{SEP}traded {unit(num(_float(row['traded_notional']), 0), 'USDT')}"
        )

    def _risk_line(self, snapshot: Mapping[str, Any] | None, equity: float | None) -> str:
        if snapshot is None:
            return f"Risk {NA}{SEP}margin {NA}{SEP}ADL {NA}{SEP}caps {NA}"
        margin = _float(snapshot["margin_ratio"])
        positions = json_loads(snapshot["positions_json"], []) or []
        breached = self._cap_breaches(snapshot, equity, positions)
        status = self._risk_status(margin, breached)
        adl = max((int(p.get("adl") or 0) for p in positions), default=0)
        caps = "ok" if not breached else "breached " + "/".join(breached)
        return f"Risk {status}{SEP}margin {pct(margin, 0)}{SEP}ADL {NA if adl <= 0 else adl}{SEP}caps {caps}"

    def _cap_breaches(
        self, snapshot: Mapping[str, Any], equity: float | None, positions: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        if not equity:
            return []
        caps = self.ctx.cfg.caps
        out: list[str] = []
        gross = abs(_float(snapshot["gross_notional"]) or 0.0)
        net = abs(_float(snapshot["net_notional"]) or 0.0)
        largest = max(
            (abs((_float(p.get("qty")) or 0.0) * (_float(p.get("mark")) or 0.0)) for p in positions),
            default=0.0,
        )
        if gross / equity > caps.gross:
            out.append("gross")
        if net / equity > caps.net:
            out.append("net")
        if largest / equity > caps.single:
            out.append("single")
        return out

    def _risk_status(self, margin: float | None, breached: Sequence[str]) -> str:
        cfg = self.ctx.cfg.risk
        if margin is None:
            return NA
        if margin >= cfg.margin_red:
            return "RED"
        if margin >= cfg.margin_amber or breached:
            return "AMBER"
        return "GREEN"

    def _ops_line(self, day: date) -> str:
        start = day_start_ms(day)
        repos = self.ctx.repos
        beats = repos.heartbeats.count(start, start + DAY_MS)
        interval_s = self.ctx.cfg.heartbeat.interval_s
        uptime = repos.heartbeats.uptime_pct(start, start + DAY_MS, interval_s) if beats else None
        recon = repos.reconciliations.latest()
        recon_text = NA if recon is None else ("OK" if recon["ok"] else "BREAK")
        context = (repos.state.load() or {}).get("context") or {}
        lag = _float(context.get(BACKUP_LAG_KEY))
        bnb = _float(context.get(BNB_DAYS_KEY))
        if bnb is None:
            last = repos.alerts.last_of_code("BNB_LOW")
            if last is not None:
                bnb = _float(json_loads(last["context_json"], {}).get("days"))
        return (
            f"Heartbeat {pct(uptime, 0, scale=1.0)}{SEP}reconciliation {recon_text}"
            f"{SEP}backup lag {unit(num(lag, 0), 's')}"
            f"{SEP}BNB fees {unit(num(bnb, 0), 'd')}"
        )

    def _housekeeping_line(self, day: date) -> str:
        return (
            f"Next universe refresh {self.next_universe_refresh(day).isoformat()}"
            f"{SEP}infra cost month-to-date €{num(self._infra_mtd(day))}"
        )

    def next_universe_refresh(self, day: date) -> date:
        """The next monthly refresh on or after ``day`` + 1 (Section 5.1)."""
        cfg = self.ctx.cfg.universe
        candidate = date(day.year, day.month, min(cfg.refresh_day_utc, 28))
        if candidate <= day:
            nxt = month_start(month_key(day))
            nxt = date(nxt.year + (nxt.month == 12), (nxt.month % 12) + 1, 1)
            candidate = date(nxt.year, nxt.month, min(cfg.refresh_day_utc, 28))
        return candidate

    def _infra_mtd(self, day: date) -> float:
        """Monthly cost prorated to the day — the total the operator sees is €0."""
        monthly = self.ctx.cfg.infra.monthly_cost_eur
        first = date(day.year, day.month, 1)
        nxt = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
        days_in_month = (nxt - first).days
        return monthly * day.day / days_in_month

    # -- rebalance summary -------------------------------------------------- #

    def rebalance_summary(self, rebalance_id: str, now_ms: int) -> str:
        """The short 01:05 UTC follow-up (Appendix D, US-T17 AC 1)."""
        repos = self.ctx.repos
        row = repos.rebalances.get(rebalance_id)
        if row is None:
            body = f"{self.prefix}{SEP}{REBALANCE} {rebalance_id}{SEP}{NA}"
            repos.reports.save(REBALANCE, rebalance_id, body, now_ms)
            return body

        orders = len(repos.orders.for_rebalance(rebalance_id))
        slices = repos.slices.for_rebalance(rebalance_id)
        escalated = sum(1 for s in slices if s["outcome"] == "escalated")
        residuals = json_loads(row["residuals_json"], []) or []
        lines = [
            f"{self.prefix}{SEP}{REBALANCE} {row['day']}"
            f"{SEP}{pct(_float(row['completion_pct']), 0, scale=1.0)}"
            f"{SEP}{orders} orders{SEP}{escalated} escalated{SEP}residual {len(residuals)}",
            f"Largest deltas: {self._largest_deltas(rebalance_id)}",
            f"Fees {num(_float(row['fees']))}"
            f"{SEP}slippage {unit(num(_float(row['avg_slippage_bps']), 1), 'bps')}"
            f"{SEP}maker {pct(_float(row['maker_ratio']), 0)}",
        ]
        body = "\n".join(lines)
        repos.reports.save(REBALANCE, rebalance_id, body, now_ms)
        return body

    def _largest_deltas(self, rebalance_id: str, limit: int = 3) -> str:
        rows = [r for r in self.ctx.repos.targets.for_rebalance(rebalance_id) if r["delta_notional"]]
        if not rows:
            return NA
        rows.sort(key=lambda r: abs(float(r["delta_notional"])), reverse=True)
        quote = self.ctx.cfg.universe.quote_asset
        return ", ".join(
            f"{_base_asset(r['symbol'], quote)} {signed(float(r['delta_notional']), 0)}"
            f" ({_transition(float(r['current_qty']), float(r['target_qty']))})"
            for r in rows[:limit]
        )

    # -- periodic ------------------------------------------------------------ #

    def weekly(self, week_key: str, now_ms: int) -> str:
        start, end = week_bounds(week_key)
        body = self._period_body(WEEKLY, week_key, start, end, "30d")
        self.ctx.repos.reports.save(WEEKLY, week_key, body, now_ms)
        return body

    def monthly(self, month_key_str: str, now_ms: int) -> str:
        start, end = month_bounds(month_key_str)
        body = self._period_body(MONTHLY, month_key_str, start, end, "30d")
        self.ctx.repos.reports.save(MONTHLY, month_key_str, body, now_ms)
        return body

    def _period_body(self, kind: str, key: str, start: date, end: date, period: str) -> str:
        """US-T17 AC 2: the Section 10 metric tables, contribution, regime, tracking."""
        repos = self.ctx.repos
        rows = repos.symbol_pnl.between(start, end)
        parts = repos.symbol_pnl.components(start, end) if rows else None
        equity_row = self._equity_row(end) or repos.equity.latest()
        equity = None if equity_row is None else _float(equity_row["equity"])
        dd = None if equity_row is None else _float(equity_row["drawdown"])
        rebalances = [r for r in repos.rebalances.between(start, end) if r["kind"] == "scheduled"]

        header = f"{self.prefix}{SEP}{kind}{SEP}{key} ({start.isoformat()} {ARROW} {end.isoformat()})"
        equity_line = (
            f"Equity {unit(num(equity), 'USDT')}"
            f"{SEP}net {NA if parts is None else signed(parts['net_pnl'])}"
            f"{SEP}DD from peak {pct(dd)}{SEP}g = {g_text(repos.governor.current_g())}"
        )
        pnl_line = (
            f"P&L {NA}"
            if parts is None
            else f"P&L price {signed(parts['price_pnl'])}{SEP}funding {signed(parts['funding'])}"
            f"{SEP}fees {signed(parts['fees'])}{SEP}slippage {signed(parts['slippage'])}"
        )
        metrics_line = SEP.join(
            [
                f"Metrics sharpe {self._metric_text('sharpe', period, 2)}",
                f"sortino {self._metric_text('sortino', period, 2)}",
                f"max drawdown {pct(self._metric('max_drawdown', period)[0])}",
                f"realised vol {pct(self._metric('realised_vol', period)[0])}"
                f" (target {pct(self.ctx.cfg.sizing.sigma_target_portfolio, 0)})",
                f"turnover {times(self._metric('turnover', period)[0])}",
            ]
        )
        execution_line = SEP.join(
            [
                f"Execution completion {pct(self._metric('rebalance_completion', period)[0], 0, scale=1.0)}",
                f"maker {pct(self._metric('maker_ratio', period)[0], 0)}",
                f"cost {unit(num(self._metric('cost_per_unit_bps', period)[0], 1), 'bps')}",
                f"rebalances {len(rebalances)}",
            ]
        )
        lines = [
            header,
            equity_line,
            pnl_line,
            metrics_line,
            execution_line,
            f"Contribution: {self._contribution(start, end)}",
            f"Regime: {self._regime(period)}",
            f"Tracking: {self._tracking()}",
            f"Infra cost €{num(self._infra_mtd(end))}",
        ]
        return "\n".join(lines)

    def _contribution(self, start: date, end: date) -> str:
        by_symbol = self.ctx.repos.symbol_pnl.by_symbol(start, end)
        if not by_symbol:
            return NA
        total = sum(abs(v) for v in by_symbol.values())
        quote = self.ctx.cfg.universe.quote_asset
        ordered = sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return SEP.join(
            f"{_base_asset(symbol, quote)} {signed(value)} ({pct(abs(value) / total if total else None, 0)})"
            for symbol, value in ordered[:5]
        )

    def _regime(self, period: str) -> str:
        _, extra = self._metric("regime_table", period)
        buckets = extra.get("buckets") if isinstance(extra, dict) else None
        if not buckets:
            return NA
        return SEP.join(
            f"{name} {int(data.get('months') or 0)} m {signed(_float(data.get('pnl')))}"
            for name, data in sorted(buckets.items())
        )

    def _tracking(self) -> str:
        row = self.ctx.repos.tracking.latest()
        if row is None:
            return NA
        state = "in bounds" if row["in_bounds"] else "OUT OF BOUNDS"
        return (
            f"{state}{SEP}corr {num(_float(row['corr_30d']), 2)}"
            f"{SEP}cum diff {pct(_float(row['cum_diff_frac']))}"
            f"{SEP}cost ratio {num(_float(row['cost_ratio']), 2)}"
            f"{SEP}turnover ratio {num(_float(row['turnover_ratio']), 2)}"
        )

    # -- alert bodies --------------------------------------------------------- #

    def governor_alert(
        self,
        *,
        dd: float,
        peak_equity: float,
        g_before: float,
        g_after: float,
        gross_before: float,
        gross_after: float,
        n_orders: int,
        taker_allowed: bool = True,
        severity: str = "WARN",
    ) -> str:
        """Appendix D governor message. Not persisted — it is an alert, not a report."""
        restore_g, restore_dd = self.restore_step(g_after)
        permission = "taker allowed" if taker_allowed else "maker only"
        return "\n".join(
            [
                f"{self.prefix}{SEP}{severity}{SEP}GOVERNOR {g_text(g_before)} {ARROW} {g_text(g_after)}",
                f"Drawdown {pct(dd)} from peak {num(peak_equity)}. Immediate cut:"
                f" gross {times(gross_before)} {ARROW} {times(gross_after)}"
                f" ({n_orders} reduce-only orders, {permission}).",
                f"Restore to {g_text(restore_g)} when DD < {pct(restore_dd, 0)}.",
            ]
        )

    def restore_step(self, g_after: float) -> tuple[float | None, float | None]:
        """The next restore rung above ``g_after`` (Section 5.7 ``governor.up``)."""
        candidates = {dd: g for dd, g in self.ctx.cfg.governor.up.items() if g > g_after}
        if not candidates:
            return None, None
        dd = max(candidates)
        return candidates[dd], dd

    # -- scheduling helper ------------------------------------------------------ #

    def due_reports(self, now_ms: int) -> list[tuple[str, str]]:
        """``(kind, period_key)`` for every body whose time has come and that is unsent.

        The runner owns the clock; this only answers what is outstanding, which
        is why a process that was down over 00:10 still sends yesterday's report
        when it comes back.
        """
        cfg = self.ctx.cfg.telegram
        today = day_of(now_ms)
        due: list[tuple[str, str]] = []

        for back in range(1, DAILY_LOOKBACK_DAYS + 1):
            day = today - timedelta(days=back)
            if now_ms >= at_utc(day + timedelta(days=1), cfg.daily_time_utc):
                self._append_if_unsent(due, DAILY, day.isoformat())

        for back in range(DAILY_LOOKBACK_DAYS + 1):
            day = today - timedelta(days=back)
            if now_ms < at_utc(day, cfg.rebalance_summary_time_utc):
                continue
            for row in self.ctx.repos.rebalances.for_day(day):
                if row["kind"] == "scheduled":
                    self._append_if_unsent(due, REBALANCE, row["rebalance_id"])

        for back in range(1, WEEKLY_LOOKBACK + 1):
            key, send_day = _week_send(today - timedelta(weeks=back), cfg.weekly_dow)
            if now_ms >= at_utc(send_day, cfg.daily_time_utc):
                self._append_if_unsent(due, WEEKLY, key)

        for back in range(1, MONTHLY_LOOKBACK + 1):
            key, send_day = _month_send(today, back, cfg.monthly_day)
            if now_ms >= at_utc(send_day, cfg.daily_time_utc):
                self._append_if_unsent(due, MONTHLY, key)

        return due

    def _append_if_unsent(self, due: list[tuple[str, str]], kind: str, key: str) -> None:
        if self.ctx.repos.reports.get(kind, key) is None:
            due.append((kind, key))

    # -- loaders ----------------------------------------------------------------- #

    def _equity_row(self, day: date) -> dict[str, Any] | None:
        target = day.isoformat()
        return next((r for r in self.ctx.repos.equity.all() if r["day"] == target), None)

    def _snapshot_equity(self, snapshot: Mapping[str, Any] | None) -> float | None:
        return None if snapshot is None else _float(snapshot["margin_balance"])

    def _scheduled_rebalance(self, day: date) -> dict[str, Any] | None:
        rows = [r for r in self.ctx.repos.rebalances.for_day(day) if r["kind"] == "scheduled"]
        return rows[-1] if rows else None

    def _metric_row(self, name: str, period: str) -> dict[str, Any] | None:
        return self.ctx.repos.metrics.latest(period).get(f"{name}:{period}")

    def _metric(self, name: str, period: str) -> tuple[float | None, dict[str, Any]]:
        row = self._metric_row(name, period)
        if row is None:
            return None, {}
        return _float(row["value"]), json_loads(row["extra_json"], {})

    def _metric_text(self, name: str, period: str, dp: int) -> str:
        value, _ = self._metric(name, period)
        return num(value, dp)


# --------------------------------------------------------------------------- #
# Period keys
# --------------------------------------------------------------------------- #


def week_key_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year:04d}-W{week:02d}"


def week_bounds(key: str) -> tuple[date, date]:
    year, week = key.split("-W")
    start = date.fromisocalendar(int(year), int(week), 1)
    return start, start + timedelta(days=6)


def month_bounds(key: str) -> tuple[date, date]:
    start = month_start(key)
    nxt = date(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return start, nxt - timedelta(days=1)


def _week_send(day_in_week: date, weekly_dow: int) -> tuple[str, date]:
    """The week containing ``day_in_week`` and the day its report may be sent."""
    key = week_key_of(day_in_week)
    _, end = week_bounds(key)
    return key, end + timedelta(days=1 + (weekly_dow % 7))


def _month_send(today: date, months_back: int, monthly_day: int) -> tuple[str, date]:
    first = date(today.year, today.month, 1)
    for _ in range(months_back):
        first = date(first.year - (first.month == 1), 12 if first.month == 1 else first.month - 1, 1)
    key = f"{first.year:04d}-{first.month:02d}"
    nxt = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
    return key, date(nxt.year, nxt.month, min(max(monthly_day, 1), 28))


def _transition(current_qty: float, target_qty: float) -> str:
    """How a delta changes the book — the phrase Appendix D prints in brackets."""
    if current_qty == 0 and target_qty != 0:
        return "new long" if target_qty > 0 else "new short"
    if target_qty == 0 and current_qty != 0:
        return f"{_side_word(current_qty)} {ARROW} flat"
    if current_qty * target_qty < 0:
        return f"{_side_word(current_qty)} {ARROW} {_side_word(target_qty)}"
    if abs(target_qty) > abs(current_qty):
        return f"add {_side_word(target_qty)}"
    if abs(target_qty) < abs(current_qty):
        return f"trim {_side_word(target_qty)}"
    return "unchanged"


def _side_word(qty: float) -> str:
    return "long" if qty > 0 else "short" if qty < 0 else "flat"


__all__ = [
    "ARROW",
    "BACKUP_LAG_KEY",
    "BNB_DAYS_KEY",
    "DAILY",
    "MINUS",
    "MONTHLY",
    "NA",
    "REBALANCE",
    "SEP",
    "TIMES",
    "WEEKLY",
    "Reporter",
    "g_text",
    "month_bounds",
    "num",
    "pct",
    "signed",
    "signed_pct",
    "signed_times",
    "times",
    "week_bounds",
    "week_key_of",
]
