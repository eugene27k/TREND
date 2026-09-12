"""The metric engine — load rows, call the pure functions, store ``MetricValue``.

This is the only impure part of ``analytics/``. It exists so that the pure
functions never have to know where a return came from, and so that the
dashboard, the reports and the phase gates all read *one* stored set of numbers
rather than each recomputing its own.

Two rules from PRD Section 10.5 are implemented here rather than in the UI:

* **Nothing is silently dropped.** A metric with fewer than
  ``cfg.metrics.min_active_days`` active days is still written, with ``n_obs``
  and ``below_min_active`` set, so the dashboard can grey it. Dropping the row
  would make "no data" and "not enough data" indistinguishable.
* **Unknown is ``None``.** ``value = None`` reaches SQLite as NULL and JSON as
  ``null``; no metric is ever stored as a placeholder zero.

Daily returns are time-weighted (``twr_index`` ratios), so deposits and
withdrawals never show up as performance — the same curve the drawdown governor
reads (US-T08 AC 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from aegis.analytics import metrics as m
from aegis.analytics import trend_metrics as tm
from aegis.core.clock import DAY_MS, day_of, day_start_ms, month_key
from aegis.core.context import Context
from aegis.core.types import Fill, MetricValue, Strategy
from aegis.storage.repositories import EquityCurveRepo

#: Metric names promised by ``docs/SERVICES.md``. Stable — the dashboard keys on them.
DOCUMENTED_METRIC_NAMES: tuple[str, ...] = (
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "var_95",
    "skew",
    "kurtosis",
    "realised_vol",
    "vol_ratio",
    "gross_exposure",
    "net_exposure",
    "turnover",
    "cost_per_unit_bps",
    "execution_alpha",
    "maker_ratio",
    "rebalance_completion",
    "beta_btc",
    "corr_btc",
    "corr_carry",
    "long_pnl",
    "short_pnl",
    "hit_rate_long",
    "hit_rate_short",
    "governor_time_g1",
    "governor_time_g05",
    "governor_time_g025",
    "funding_share",
    "vol_target_adherence",
    "concentration",
    "trade_count",
    "avg_holding_days",
    "win_rate",
    "information_ratio",
    "net_of_infra",
    "cash_alternative",
)

#: The rest of the PRD Section 10 table, which the services doc folds into "additions".
EXTRA_METRIC_NAMES: tuple[str, ...] = (
    "cvar_95",
    "avg_long_count",
    "avg_short_count",
    "avg_long_notional",
    "avg_short_notional",
    "avg_win",
    "avg_loss",
    "signal_mean_abs",
    "signal_turnover",
    "signal_strong_frac",
    "regime_table",
)

METRIC_NAMES: tuple[str, ...] = DOCUMENTED_METRIC_NAMES + EXTRA_METRIC_NAMES

_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}


@dataclass(frozen=True, slots=True)
class Window:
    """A metric period resolved against a concrete ``now``."""

    period: str
    start_day: date
    end_day: date
    days: int

    @property
    def start_ms(self) -> int:
        return day_start_ms(self.start_day)

    @property
    def end_ms(self) -> int:
        """Exclusive — 00:00 of the day after ``end_day``."""
        return day_start_ms(self.end_day) + DAY_MS

    def contains(self, day: date) -> bool:
        return self.start_day <= day <= self.end_day


class MetricsEngine:
    """Computes every Section 10 metric for every configured period."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # ----------------------------------------------------------------- API --

    def compute_all(self, now_ms: int) -> list[MetricValue]:
        out: list[MetricValue] = []
        for period in self.ctx.cfg.metrics.periods:
            out.extend(self.compute_period(period, now_ms))
        return out

    def compute_period(self, period: str, now_ms: int) -> list[MetricValue]:
        window = self.window_for(period, now_ms)
        values = self._compute(window, now_ms)
        self.ctx.repos.metrics.save_many(values)
        return values

    # -------------------------------------------------------------- windows --

    def window_for(self, period: str, now_ms: int) -> Window:
        end_day = day_of(now_ms)
        if period in _PERIOD_DAYS:
            start_day = end_day - timedelta(days=_PERIOD_DAYS[period] - 1)
        elif period == "mtd":
            start_day = end_day.replace(day=1)
        elif period == "ytd":
            start_day = end_day.replace(month=1, day=1)
        else:  # since_inception (and any future alias) — everything we have
            first = self._first_day()
            start_day = min(first, end_day) if first else end_day
        return Window(
            period=period, start_day=start_day, end_day=end_day, days=(end_day - start_day).days + 1
        )

    def _first_day(self) -> date | None:
        rows = self.ctx.repos.equity.all()
        return date.fromisoformat(rows[0]["day"]) if rows else None

    # --------------------------------------------------------------- compute --

    def _compute(self, w: Window, now_ms: int) -> list[MetricValue]:
        repos = self.ctx.repos
        cfg = self.ctx.cfg

        equity_rows = repos.equity.all()
        returns_all = _returns_by_day(equity_rows)
        equity_by_day = {date.fromisoformat(r["day"]): float(r["equity"]) for r in equity_rows}
        index_by_day = {date.fromisoformat(r["day"]): float(r["twr_index"]) for r in equity_rows}

        days = sorted(d for d in returns_all if w.contains(d))
        returns = [returns_all[d] for d in days]
        active_days = sum(1 for r in returns if r != 0.0)
        window_equity = [equity_by_day[d] for d in sorted(equity_by_day) if w.contains(d)]
        window_index = [index_by_day[d] for d in sorted(index_by_day) if w.contains(d)]

        pnl_rows = repos.symbol_pnl.between(w.start_day, w.end_day)
        fills = repos.fills.between(w.start_ms, w.end_ms)
        rebalances = repos.rebalances.between(w.start_day, w.end_day)

        ctx_extra = {
            "period_days": w.days,
            "active_days": active_days,
            "min_active_days": cfg.metrics.min_active_days,
            "below_min_active": active_days < cfg.metrics.min_active_days,
            "start_day": w.start_day.isoformat(),
            "end_day": w.end_day.isoformat(),
        }

        def mv(
            name: str,
            value: float | None,
            *,
            n_obs: int = 0,
            std_error: float | None = None,
            extra: dict[str, Any] | None = None,
        ) -> MetricValue:
            return MetricValue(
                strategy=self.ctx.strategy,
                name=name,
                period=w.period,
                value=value,
                as_of_ts_ms=now_ms,
                n_obs=n_obs,
                std_error=std_error,
                extra={**ctx_extra, **(extra or {})},
            )

        out: list[MetricValue] = []
        n = len(returns)

        # --- shared risk/return set (US-T15 AC 3) ---------------------------
        sharpe_value, sharpe_se = m.sharpe(returns, cfg.bench.rf_annual)
        out.append(mv("sharpe", sharpe_value, n_obs=n, std_error=sharpe_se))
        out.append(mv("sortino", m.sortino(returns, cfg.bench.rf_annual), n_obs=n))
        max_dd = m.max_drawdown(window_index)
        out.append(mv("max_drawdown", max_dd, n_obs=len(window_index)))
        out.append(mv("calmar", m.calmar(returns, max_dd), n_obs=n))
        out.append(mv("var_95", m.var(returns), n_obs=n))
        out.append(mv("cvar_95", m.cvar(returns), n_obs=n))
        out.append(mv("skew", m.skew(returns), n_obs=n))
        out.append(mv("kurtosis", m.kurtosis(returns), n_obs=n))

        # --- vol targeting ---------------------------------------------------
        target = cfg.sizing.sigma_target_portfolio
        realised, ratio = tm.realised_vol_vs_target(returns, target)
        out.append(mv("realised_vol", realised, n_obs=active_days, extra={"target": target}))
        out.append(mv("vol_ratio", ratio, n_obs=active_days, extra={"target": target}))
        rolling = _rolling_vol(returns_all, days, cfg.metrics.vol_window_days)
        out.append(
            mv(
                "vol_target_adherence",
                tm.vol_target_adherence(rolling, target),
                n_obs=len(rolling),
                extra={"target": target},
            )
        )

        # --- exposure --------------------------------------------------------
        gross_s, net_s, eq_s, exposure_by_day = self._exposure_series(w, pnl_rows, equity_by_day)
        exposure = tm.exposure_stats(gross_s, net_s, eq_s)
        n_exp = int(exposure["n_obs"] or 0)
        out.append(
            mv(
                "gross_exposure",
                exposure["avg_gross"],
                n_obs=n_exp,
                extra={
                    "avg": exposure["avg_gross"],
                    "max": exposure["max_gross"],
                    "current": exposure["current_gross"],
                },
            )
        )
        out.append(
            mv(
                "net_exposure",
                exposure["avg_net"],
                n_obs=n_exp,
                extra={
                    "avg": exposure["avg_net"],
                    "max": exposure["max_net"],
                    "current": exposure["current_net"],
                },
            )
        )
        counts = _side_counts(pnl_rows)
        out.append(mv("avg_long_count", counts["avg_long_count"], n_obs=counts["n_days"]))
        out.append(mv("avg_short_count", counts["avg_short_count"], n_obs=counts["n_days"]))
        out.append(mv("avg_long_notional", counts["avg_long_notional"], n_obs=counts["n_days"]))
        out.append(mv("avg_short_notional", counts["avg_short_notional"], n_obs=counts["n_days"]))

        # --- turnover and execution quality ----------------------------------
        traded = sum(abs(f.notional) for f in fills)
        avg_equity = m.mean(window_equity) or 0.0
        period_turnover, annualised = tm.turnover(traded, avg_equity, w.days)
        out.append(
            mv(
                "turnover",
                period_turnover,
                n_obs=len(fills),
                extra={"annualised": annualised, "traded_notional": traded, "avg_equity": avg_equity},
            )
        )
        fees = sum(abs(f.fee) for f in fills)
        slippage = sum(abs(f.slippage_bps) / 10_000.0 * abs(f.notional) for f in fills)
        realised_bps = tm.cost_per_unit_bps(fees, slippage, traded)
        model_bps = self._model_cost_bps(fills)
        out.append(
            mv(
                "cost_per_unit_bps",
                realised_bps,
                n_obs=len(fills),
                extra={"fees": fees, "slippage": slippage, "traded_notional": traded},
            )
        )
        out.append(
            mv(
                "execution_alpha",
                None if realised_bps is None else tm.execution_alpha(model_bps, realised_bps, traded),
                n_obs=len(fills),
                extra={"model_bps": model_bps, "realised_bps": realised_bps},
            )
        )
        maker = sum(abs(f.notional) for f in fills if f.is_maker)
        out.append(mv("maker_ratio", tm.maker_ratio(maker, traded), n_obs=len(fills)))
        scheduled = [r for r in rebalances if r["kind"] == "scheduled"]
        out.append(
            mv(
                "rebalance_completion",
                tm.rebalance_completion([r["completion_pct"] for r in scheduled]),
                n_obs=len(scheduled),
            )
        )

        # --- market exposure --------------------------------------------------
        btc_returns = self._btc_returns()
        paired_days = [d for d in days if d in btc_returns]
        strat = [returns_all[d] for d in paired_days]
        btc = [btc_returns[d] for d in paired_days]
        window_beta, window_corr = tm.beta_to_btc(strat, btc, window=cfg.metrics.beta_window_days)
        full_beta, full_corr = tm.beta_to_btc(strat, btc, window=None)
        out.append(
            mv(
                "beta_btc",
                window_beta,
                n_obs=len(paired_days),
                extra={"window": cfg.metrics.beta_window_days, "full_period": full_beta},
            )
        )
        out.append(
            mv(
                "corr_btc",
                window_corr,
                n_obs=len(paired_days),
                extra={"window": cfg.metrics.beta_window_days, "full_period": full_corr},
            )
        )
        carry_returns = self._carry_returns()
        carry_days = [d for d in days if d in carry_returns]
        out.append(
            mv(
                "corr_carry",
                m.correlation([returns_all[d] for d in carry_days], [carry_returns[d] for d in carry_days]),
                n_obs=len(carry_days),
            )
        )

        # --- attribution -------------------------------------------------------
        sides = tm.side_attribution(pnl_rows)
        out.append(mv("long_pnl", sides["long_pnl"], n_obs=int(sides["n_long"] or 0)))
        out.append(mv("short_pnl", sides["short_pnl"], n_obs=int(sides["n_short"] or 0)))
        out.append(mv("hit_rate_long", sides["hit_rate_long"], n_obs=int(sides["n_long"] or 0)))
        out.append(mv("hit_rate_short", sides["hit_rate_short"], n_obs=int(sides["n_short"] or 0)))
        by_symbol = repos.symbol_pnl.by_symbol(w.start_day, w.end_day)
        shares, concentration = tm.per_symbol_contribution(by_symbol)
        out.append(mv("concentration", concentration, n_obs=len(by_symbol), extra={"shares": shares}))

        # --- trades and signals -------------------------------------------------
        trades = repos.trades.closed_between(w.start_ms, w.end_ms)
        stats = tm.trade_statistics(trades)
        count = int(stats["count"] or 0)
        out.append(
            mv(
                "trade_count",
                float(count),
                n_obs=count,
                extra={
                    "mae_median": stats["mae_median"],
                    "mae_p90": stats["mae_p90"],
                    "mae_max": stats["mae_max"],
                },
            )
        )
        out.append(mv("avg_holding_days", stats["avg_holding_days"], n_obs=count))
        out.append(mv("win_rate", stats["win_rate"], n_obs=count))
        out.append(mv("avg_win", stats["avg_win"], n_obs=count))
        out.append(mv("avg_loss", stats["avg_loss"], n_obs=count))
        signals = _signals_by_day(repos.signals.between(w.start_day, w.end_day))
        sig = tm.signal_statistics(signals)
        n_sig = int(sig["n_obs"] or 0)
        out.append(mv("signal_mean_abs", sig["mean_abs_signal"], n_obs=n_sig))
        out.append(mv("signal_turnover", sig["signal_turnover"], n_obs=n_sig))
        out.append(mv("signal_strong_frac", sig["strong_frac"], n_obs=n_sig))

        # --- governor and regime -------------------------------------------------
        gov_rows = self._governor_rows(w)
        time_in_state = tm.governor_time_in_state(gov_rows, w.start_ms, w.end_ms)
        out.append(mv("governor_time_g1", time_in_state["g1"], n_obs=len(gov_rows)))
        out.append(mv("governor_time_g05", time_in_state["g05"], n_obs=len(gov_rows)))
        out.append(
            mv(
                "governor_time_g025",
                time_in_state["g025"],
                n_obs=len(gov_rows),
                extra={"other": time_in_state["other"]},
            )
        )
        monthly_pnl = _monthly_pnl(pnl_rows)
        monthly_btc = _monthly_returns(self._btc_closes())
        monthly_exposure = _monthly_mean(exposure_by_day)
        table = tm.regime_table(monthly_pnl, monthly_btc, monthly_exposure)
        # n_obs counts the months that could be bucketed: the first month of a
        # series has no BTC month-on-month return and so cannot be classified.
        bucketed = sum(int(b["months"]) for b in table.values())
        out.append(mv("regime_table", None, n_obs=bucketed, extra={"buckets": table}))

        # --- P&L composition and opportunity cost ---------------------------------
        funding = sum(float(r["funding"]) for r in pnl_rows)
        gross_pnl = sum(float(r["price_pnl"]) for r in pnl_rows) + funding
        net_pnl = sum(float(r["net_pnl"]) for r in pnl_rows)
        out.append(
            mv(
                "funding_share",
                tm.funding_pnl_share(funding, gross_pnl),
                n_obs=len(pnl_rows),
                extra={"funding": funding, "gross_pnl": gross_pnl},
            )
        )
        out.append(
            mv(
                "net_of_infra",
                m.net_of_infra(net_pnl, cfg.infra.monthly_cost_eur, w.days),
                n_obs=len(pnl_rows),
                extra={"net_pnl": net_pnl, "monthly_cost_eur": cfg.infra.monthly_cost_eur},
            )
        )
        equity0 = window_equity[0] if window_equity else 0.0
        out.append(
            mv(
                "cash_alternative",
                m.cash_alternative(equity0, cfg.bench.rf_annual, w.days),
                n_obs=len(window_equity),
                extra={"rf_annual": cfg.bench.rf_annual, "equity0": equity0},
            )
        )
        bench = self._reference_returns(equity_by_day)
        ir_days = [d for d in days if d in bench]
        out.append(
            mv(
                "information_ratio",
                m.information_ratio([returns_all[d] for d in ir_days], [bench[d] for d in ir_days]),
                n_obs=len(ir_days),
            )
        )
        return out

    # ------------------------------------------------------------- loaders --

    def _exposure_series(
        self,
        w: Window,
        pnl_rows: Sequence[Mapping[str, Any]],
        equity_by_day: Mapping[date, float],
    ) -> tuple[list[float], list[float], list[float], dict[date, float]]:
        """Gross/net/equity series for the window, and gross-x by day for the regime table.

        Snapshots are preferred (they carry the exchange's own gross and net);
        when none exist for the window the series is rebuilt from the daily
        attribution rows, so exposure is reported for a backtest or a paper run
        that never took a snapshot.
        """
        snaps = self.ctx.repos.snapshots.between(w.start_ms, w.end_ms)
        if snaps:
            gross = [float(s["gross_notional"]) for s in snaps]
            net = [float(s["net_notional"]) for s in snaps]
            equity = [float(s["margin_balance"]) for s in snaps]
            by_day: dict[date, list[float]] = {}
            for s in snaps:
                e = float(s["margin_balance"])
                if e > 0:
                    by_day.setdefault(day_of(int(s["ts"])), []).append(float(s["gross_notional"]) / e)
            daily = {d: sum(v) / len(v) for d, v in by_day.items()}
            return gross, net, equity, daily

        per_day: dict[date, tuple[float, float]] = {}
        for row in pnl_rows:
            day = date.fromisoformat(row["day"])
            notional = abs(float(row["avg_notional"]))
            signed = -notional if str(row["side"]).lower() == "short" else notional
            g, nt = per_day.get(day, (0.0, 0.0))
            per_day[day] = (g + notional, nt + signed)
        gross, net, equity = [], [], []
        daily = {}
        for day in sorted(per_day):
            e = equity_by_day.get(day)
            if e is None or e <= 0:
                continue
            g, nt = per_day[day]
            gross.append(g)
            net.append(nt)
            equity.append(e)
            daily[day] = g / e
        return gross, net, equity, daily

    def _model_cost_bps(self, fills: Sequence[Fill]) -> float:
        """The backtest's conservative cost model in bps: taker fee + modelled slippage.

        Weighted by traded notional so that it is comparable, symbol for symbol,
        with the realised cost the execution algorithm actually paid.
        """
        cfg = self.ctx.cfg
        meta = self.ctx.repos.symbol_meta.all()
        total = 0.0
        weighted = 0.0
        for f in fills:
            notional = abs(f.notional)
            if notional <= 0:
                continue
            info = meta.get(f.symbol)
            taker_bps = (info.taker_fee if info else cfg.exec.taker_fee_fallback) * 10_000.0
            weighted += (taker_bps + cfg.exec.slippage_for(f.symbol)) * notional
            total += notional
        if total <= 0:
            return cfg.exec.taker_fee_fallback * 10_000.0 + cfg.exec.slippage_for("default")
        return weighted / total

    def _btc_closes(self) -> list[tuple[date, float]]:
        bars = self.ctx.repos.bars.series(self.ctx.cfg.bench.btc_symbol)
        return [(b.day, b.close) for b in bars if not b.filled and b.close > 0]

    def _btc_returns(self) -> dict[date, float]:
        closes = self._btc_closes()
        out: dict[date, float] = {}
        for (_, prev), (day, close) in pairwise(closes):
            if prev > 0:
                out[day] = close / prev - 1.0
        return out

    def _carry_returns(self) -> dict[date, float]:
        """CARRY daily returns from the same file, when the two sleeves share one.

        In production each sleeve has its own database and this is empty, which
        is why ``corr_carry`` is ``None`` rather than an error: the metric exists
        only when a combined view is available.
        """
        if self.ctx.strategy is Strategy.CARRY:
            return {}
        repo = EquityCurveRepo(self.ctx.repos.db, Strategy.CARRY)
        return _returns_by_day(repo.all())

    def _reference_returns(self, equity_by_day: Mapping[date, float]) -> dict[date, float]:
        """Backtest reference returns (US-T15 AC 3 information ratio "vs backtest").

        ``tracking.ref_pnl`` is a USDT amount; it becomes a return by dividing by
        the capital that earned it — the previous day's equity.
        """
        rows = self.ctx.repos.tracking.series(limit=10_000)
        days = sorted(equity_by_day)
        prev_equity = {day: equity_by_day[prev] for prev, day in pairwise(days)}
        out: dict[date, float] = {}
        for row in rows:
            day = date.fromisoformat(row["day"])
            base = prev_equity.get(day)
            if base and base > 0:
                out[day] = float(row["ref_pnl"] or 0.0) / base
        return out

    def _governor_rows(self, w: Window) -> list[dict[str, Any]]:
        rows = self.ctx.repos.governor.between(w.start_ms, w.end_ms)
        carried = self.ctx.repos.governor.last_before(w.start_ms)
        return [carried, *rows] if carried else rows


# --------------------------------------------------------------------------- #
# Row -> series helpers (module-level so they stay testable and obviously pure)
# --------------------------------------------------------------------------- #


def _returns_by_day(equity_rows: Iterable[Mapping[str, Any]]) -> dict[date, float]:
    """Time-weighted daily returns from consecutive ``twr_index`` values."""
    out: dict[date, float] = {}
    prev: float | None = None
    for row in equity_rows:
        idx = float(row["twr_index"] or 0.0)
        day = date.fromisoformat(row["day"])
        if prev is not None and prev > 0:
            out[day] = idx / prev - 1.0
        prev = idx
    return out


def _rolling_vol(returns_by_day: Mapping[date, float], days: Sequence[date], window: int) -> list[float]:
    """Trailing annualised vol on each window day, using history from before it."""
    ordered = sorted(returns_by_day)
    position = {d: i for i, d in enumerate(ordered)}
    out: list[float] = []
    for day in days:
        i = position[day]
        chunk = [returns_by_day[d] for d in ordered[max(0, i - window + 1) : i + 1]]
        if len(chunk) < window:
            continue
        vol = m.volatility(chunk)
        if vol is not None:
            out.append(vol)
    return out


def _side_counts(pnl_rows: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Daily average count and notional of long and short positions."""
    per_day: dict[date, dict[str, float]] = {}
    for row in pnl_rows:
        day = date.fromisoformat(row["day"])
        side = str(row["side"]).lower()
        if side not in ("long", "short"):
            continue
        acc = per_day.setdefault(day, {"long": 0.0, "short": 0.0, "long_n": 0.0, "short_n": 0.0})
        acc[side] += 1.0
        acc[f"{side}_n"] += abs(float(row["avg_notional"]))
    n = len(per_day)
    if n == 0:
        return {
            "n_days": 0,
            "avg_long_count": None,
            "avg_short_count": None,
            "avg_long_notional": None,
            "avg_short_notional": None,
        }
    return {
        "n_days": n,
        "avg_long_count": sum(a["long"] for a in per_day.values()) / n,
        "avg_short_count": sum(a["short"] for a in per_day.values()) / n,
        "avg_long_notional": sum(a["long_n"] for a in per_day.values()) / n,
        "avg_short_notional": sum(a["short_n"] for a in per_day.values()) / n,
    }


def _signals_by_day(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        out.setdefault(str(row["day"]), {})[str(row["symbol"])] = float(row["signal"])
    return out


def _monthly_pnl(pnl_rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in pnl_rows:
        key = str(row["day"])[:7]
        out[key] = out.get(key, 0.0) + float(row["net_pnl"])
    return out


def _monthly_returns(closes: Sequence[tuple[date, float]]) -> dict[str, float]:
    """Month-end to month-end returns from a daily close series."""
    last: dict[str, float] = {}
    for day, close in closes:
        last[month_key(day)] = close
    months = sorted(last)
    out: dict[str, float] = {}
    for prev, cur in pairwise(months):
        if last[prev] > 0:
            out[cur] = last[cur] / last[prev] - 1.0
    return out


def _monthly_mean(by_day: Mapping[date, float]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for day, value in by_day.items():
        acc.setdefault(month_key(day), []).append(value)
    return {k: sum(v) / len(v) for k, v in acc.items()}


__all__ = ["DOCUMENTED_METRIC_NAMES", "EXTRA_METRIC_NAMES", "METRIC_NAMES", "MetricsEngine", "Window"]
