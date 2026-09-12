"""Daily point-in-time simulator (PRD 11.2, US-T16 AC 2).

The whole design rule here is that the backtester must not be a second
implementation of the strategy. It reuses ``signals/``, ``riskmodel/`` and
``portfolio/`` **unchanged** — the same functions, with the same config object —
so a backtest result is evidence about the code that will actually trade, not
about a sibling of it.

What the simulator itself owns is only the things that are not the strategy:

* the calendar (which bars exist on day *t*, and nothing after it);
* execution (fills at the **next** daily open, charged the taker fee plus the
  slippage table — Locked Decision 8's deliberately conservative model, so live
  maker execution shows up as upside rather than as a shortfall);
* cash and funding bookkeeping, which is kept as a literal cash account so that
  the equity path can be recomputed from the fills and reconciles by
  construction (Section 14 point 2).

Look-ahead is prevented structurally rather than by care: signals for day *t*
are read from a precomputed series at index *t*, fills happen at *t+1*'s open,
and the month's universe is built from bars strictly before the month started.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aegis.core.clock import month_key, to_ms
from aegis.core.config import AppConfig
from aegis.core.errors import ConfigError
from aegis.core.types import (
    DailyBar,
    EquityPoint,
    FundingRate,
    SignalResult,
    UniverseResult,
)
from aegis.portfolio.funding_overlay import annualise_funding
from aegis.portfolio.governor import governor
from aegis.portfolio.hysteresis import should_trade
from aegis.portfolio.sizing import size_targets
from aegis.riskmodel.estimators import build_risk_model
from aegis.signals.engine import compute_signal_series
from aegis.storage.db import json_dumps

#: EWMA weights decay so fast that a trailing window is exact to ~1e-6 while
#: keeping the daily covariance rebuild cheap. 500 days at a 20-day half-life
#: leaves the oldest observation a weight of 2**-25.
COV_WINDOW_DAYS = 500


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    start_day: date
    end_day: date
    variant: str
    equity: tuple[dict[str, Any], ...]
    daily_returns: tuple[float, ...]
    metrics: dict[str, float]
    symbol_pnl: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    duration_s: float = 0.0
    halted_on: date | None = None

    @property
    def final_equity(self) -> float:
        return float(self.equity[-1]["equity"]) if self.equity else 0.0

    @property
    def net_pnl(self) -> float:
        if not self.equity:
            return 0.0
        return float(self.equity[-1]["equity"]) - float(self.equity[0]["equity"])


@dataclass(slots=True)
class _Book:
    """Cash-and-positions accounting. Equity is always cash + marked positions."""

    cash: float
    qty: dict[str, float] = field(default_factory=dict)
    fees: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0
    traded_notional: float = 0.0

    def equity(self, marks: Mapping[str, float]) -> float:
        return self.cash + sum(q * marks.get(s, 0.0) for s, q in self.qty.items() if q)

    def notional(self, symbol: str, marks: Mapping[str, float]) -> float:
        return self.qty.get(symbol, 0.0) * marks.get(symbol, 0.0)


class Simulator:
    """Runs one backtest over pre-loaded bars, funding and monthly universes."""

    def __init__(
        self,
        cfg: AppConfig,
        bars: Mapping[str, Sequence[DailyBar]],
        funding: Mapping[str, Sequence[FundingRate]],
        universes: Mapping[str, UniverseResult],
        *,
        initial_equity: float = 10_000.0,
        min_notionals: Mapping[str, float] | None = None,
        fill_at: str = "open",
    ) -> None:
        self.cfg = cfg
        self.bars = {s: sorted(b, key=lambda x: x.day) for s, b in bars.items()}
        self.funding = {s: sorted(f, key=lambda x: x.funding_time_ms) for s, f in funding.items()}
        self.universes = dict(universes)
        self.initial_equity = initial_equity
        self.min_notionals = dict(min_notionals or {})
        if fill_at not in ("open", "close"):
            raise ConfigError(f"fill_at must be 'open' or 'close', got {fill_at!r}")
        self.fill_at = fill_at

        self._close: dict[str, dict[date, float]] = {
            s: {b.day: b.close for b in bl} for s, bl in self.bars.items()
        }
        # ``fill_at='close'`` is the 11.5 time-of-day variant: daily bars cannot
        # express a 12:00 UTC rebalance, so filling at the same day's close is the
        # nearest honest proxy. Recorded as a limitation rather than hidden.
        self._open: dict[str, dict[date, float]] = {
            s: {b.day: (b.close if fill_at == "close" else b.open) for b in bl}
            for s, bl in self.bars.items()
        }
        self._days: dict[str, list[date]] = {s: [b.day for b in bl] for s, bl in self.bars.items()}
        self._signals: dict[str, dict[date, SignalResult]] = {}
        self._returns: dict[str, dict[date, float]] = {}

    # -- precomputation ----------------------------------------------------- #

    def _prepare(self) -> None:
        """Signals and log returns for every symbol, once, over its full history.

        ``compute_signal_series`` is bar-for-bar identical to calling
        ``compute_signal`` on each prefix (its own test asserts that), so this is
        a speed optimisation with no effect on the result.
        """
        for symbol, bar_list in self.bars.items():
            closes = [b.close for b in bar_list]
            days = [b.day for b in bar_list]
            series = compute_signal_series(closes, self.cfg.signal, symbol, days)
            self._signals[symbol] = dict(zip(days, series, strict=True))
            rets: dict[date, float] = {}
            for i in range(1, len(closes)):
                prev, cur = closes[i - 1], closes[i]
                rets[days[i]] = math.log(cur / prev) if prev > 0 and cur > 0 else 0.0
            self._returns[symbol] = rets

    def _trailing_returns(self, symbols: Sequence[str], upto: date,
                          calendar: Sequence[date]) -> dict[str, list[float]]:
        window = [d for d in calendar if d <= upto][-COV_WINDOW_DAYS:]
        out: dict[str, list[float]] = {}
        for symbol in symbols:
            rets = self._returns.get(symbol, {})
            series = [rets[d] for d in window if d in rets]
            if series:
                out[symbol] = series
        return out

    # -- the run ------------------------------------------------------------ #

    def run(self, start: date, end: date, *, variant: str = "default") -> BacktestResult:
        import time

        began = time.perf_counter()
        self._prepare()

        calendar = sorted({d for days in self._days.values() for d in days if start <= d <= end})
        if not calendar:
            return self._empty(start, end, variant, time.perf_counter() - began)

        book = _Book(cash=self.initial_equity)
        g = 1.0
        halted_on: date | None = None
        equity_path: list[dict[str, Any]] = []
        daily_returns: list[float] = []
        symbol_pnl: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        open_trades: dict[str, dict[str, Any]] = {}
        peak_index = 1.0
        twr_index = 1.0
        prev_equity = self.initial_equity
        pending: dict[str, float] = {}        # symbol -> target qty, filled at the next open
        funding_cursor = dict.fromkeys(self.funding, 0)
        blocked_until: date | None = None

        for i, day in enumerate(calendar):
            marks_open = {s: self._open[s].get(day) for s in self._open}
            marks_open = {s: p for s, p in marks_open.items() if p}

            # 1. Execute yesterday's decisions at today's open (11.2).
            day_fees = day_slip = day_traded = 0.0
            if pending and not halted_on:
                day_fees, day_slip, day_traded = self._fill(book, pending, marks_open, day,
                                                            open_trades, trades)
            pending = {}

            marks_close = {s: self._close[s].get(day) for s in self._close}
            marks_close = {s: p for s, p in marks_close.items() if p}

            # 2. Funding for every settlement that fell on this day.
            day_funding = self._settle_funding(book, day, funding_cursor, marks_close)

            equity = book.equity(marks_close)
            if equity <= 0:
                halted_on = halted_on or day

            # 3. Mark the day and update the governor from the drawdown.
            ret = (equity / prev_equity - 1.0) if prev_equity > 0 else 0.0
            twr_index *= 1.0 + ret
            peak_index = max(peak_index, twr_index)
            drawdown = max(0.0, 1.0 - twr_index / peak_index) if peak_index > 0 else 0.0
            equity_path.append({"day": day.isoformat(), "equity": equity, "twr_index": twr_index,
                                "drawdown": drawdown, "g": g,
                                "gross": sum(abs(book.qty.get(s, 0.0) * marks_close.get(s, 0.0))
                                             for s in book.qty),
                                "net": sum(book.qty.get(s, 0.0) * marks_close.get(s, 0.0)
                                           for s in book.qty)})
            daily_returns.append(ret)
            symbol_pnl.extend(self._attribute(day, book, marks_close, day_fees, day_slip,
                                              day_funding, day_traded))
            prev_equity = equity

            g_next = governor(drawdown, g, self.cfg.governor)
            g = g_next

            # 4. Kill rules: a hard halt flattens and stops trading for good.
            if not halted_on and drawdown >= self._halt_threshold():
                halted_on = day
                if book.qty:
                    self._fill(book, dict.fromkeys(book.qty, 0.0), marks_close, day,
                               open_trades, trades)
            if halted_on:
                continue

            # 5. Decide tomorrow's book from today's closed bar.
            if i + 1 >= len(calendar):
                break
            risk_blocked = blocked_until is not None and day <= blocked_until
            if ret < -self.cfg.risk.daily_loss_block:
                blocked_until = calendar[min(i + 1, len(calendar) - 1)]
            pending = self._targets(day, book, marks_close, equity, g, calendar,
                                    risk_blocked=risk_blocked)

        duration = time.perf_counter() - began
        return BacktestResult(
            run_id=self._run_id(variant, start, end),
            start_day=start, end_day=end, variant=variant,
            equity=tuple(equity_path), daily_returns=tuple(daily_returns),
            metrics=self._metrics(equity_path, daily_returns, book),
            symbol_pnl=tuple(symbol_pnl), trades=tuple(trades),
            manifest=self.manifest(start, end, variant), duration_s=duration,
            halted_on=halted_on,
        )

    # -- steps -------------------------------------------------------------- #

    def _targets(self, day: date, book: _Book, marks: Mapping[str, float], equity: float,
                 g: float, calendar: Sequence[date], *, risk_blocked: bool) -> dict[str, float]:
        """Target quantities for tomorrow, in the live sizing pipeline's own code."""
        universe = self.universes.get(month_key(day))
        symbols = list(universe.symbols) if universe else []
        held = [s for s, q in book.qty.items() if q]

        signals: dict[str, float] = {}
        for symbol in symbols:
            result = self._signals.get(symbol, {}).get(day)
            if result is not None and result.warm:
                signals[symbol] = result.signal

        returns = self._trailing_returns(sorted(set(symbols) | set(held)), day, calendar)
        if not returns:
            return {}
        risk_model = build_risk_model(returns, self.cfg.vol, self.cfg.cov)
        vols = dict(risk_model.vols)

        funding_ann = {s: self._predicted_funding(s, day) for s in symbols}
        targets = size_targets(signals, vols, risk_model, equity, g, self.cfg,
                               funding_ann=funding_ann, min_notionals=self.min_notionals)

        wanted: dict[str, float] = {}
        by_symbol = targets.by_symbol()
        for symbol in sorted(set(symbols) | set(held)):
            price = marks.get(symbol)
            if not price:
                continue
            target_notional = by_symbol[symbol].target_notional if symbol in by_symbol else 0.0
            current_notional = book.notional(symbol, marks)
            in_universe = symbol in symbols
            if not in_universe:
                target_notional = 0.0          # 5.8: always traded to zero
            if risk_blocked and abs(target_notional) > abs(current_notional):
                continue                        # blocked: reductions only
            if not should_trade(target_notional, current_notional, equity,
                                self.cfg.rebalance, in_universe):
                continue
            wanted[symbol] = target_notional / price
        return wanted

    def _fill(self, book: _Book, wanted: Mapping[str, float], marks: Mapping[str, float],
              day: date, open_trades: dict[str, dict[str, Any]],
              trades: list[dict[str, Any]]) -> tuple[float, float, float]:
        """Fill at ``marks`` with the conservative cost model (Locked Decision 8)."""
        taker = self.cfg.exec.taker_fee_fallback
        fees = slip = traded = 0.0
        for symbol, target_qty in wanted.items():
            price = marks.get(symbol)
            if not price:
                continue
            current = book.qty.get(symbol, 0.0)
            dq = target_qty - current
            if dq == 0.0:
                continue
            notional = abs(dq) * price
            slip_bps = self.cfg.exec.slippage_for(symbol)
            slip_cost = notional * slip_bps / 10_000.0
            fee = notional * taker
            book.cash -= dq * price + fee + slip_cost
            book.qty[symbol] = target_qty
            book.fees += fee
            book.slippage += slip_cost
            book.traded_notional += notional
            fees += fee
            slip += slip_cost
            traded += notional
            self._track_trade(symbol, current, target_qty, price, day, open_trades, trades)
        return fees, slip, traded

    def _track_trade(self, symbol: str, before: float, after: float, price: float, day: date,
                     open_trades: dict[str, dict[str, Any]], trades: list[dict[str, Any]]) -> None:
        """Open -> flat episodes per symbol (US-T14 AC 4, mirrored in the backtest)."""
        if before == 0.0 and after != 0.0:
            signal = self._signals.get(symbol, {}).get(day)
            open_trades[symbol] = {
                "symbol": symbol, "side": "long" if after > 0 else "short",
                "open_day": day.isoformat(), "open_price": price, "qty": after,
                "entry_signal": signal.signal if signal else 0.0, "mae": 0.0,
            }
        elif before != 0.0 and after == 0.0 and symbol in open_trades:
            trade = open_trades.pop(symbol)
            signal = self._signals.get(symbol, {}).get(day)
            entry_price = float(trade["open_price"])
            qty = float(trade["qty"])
            trades.append({
                **trade, "close_day": day.isoformat(), "close_price": price,
                "days": (day - date.fromisoformat(str(trade["open_day"]))).days,
                "pnl": qty * (price - entry_price),
                "exit_signal": signal.signal if signal else 0.0,
            })

    def _settle_funding(self, book: _Book, day: date, cursor: dict[str, int],
                        marks: Mapping[str, float]) -> float:
        """Charge every settlement dated ``day`` against the position held."""
        start = to_ms(day)
        end = start + 86_400_000
        total = 0.0
        for symbol, rates in self.funding.items():
            idx = cursor.get(symbol, 0)
            while idx < len(rates) and rates[idx].funding_time_ms < start:
                idx += 1
            while idx < len(rates) and rates[idx].funding_time_ms < end:
                qty = book.qty.get(symbol, 0.0)
                if qty:
                    price = marks.get(symbol, 0.0)
                    # A long pays when the rate is positive; a short receives.
                    amount = -rates[idx].rate * qty * price
                    book.cash += amount
                    book.funding += amount
                    total += amount
                idx += 1
            cursor[symbol] = idx
        return total

    def _predicted_funding(self, symbol: str, day: date) -> float:
        """The last realised rate before ``day``, annualised — the documented proxy (11.1)."""
        rates = self.funding.get(symbol)
        if not rates:
            return 0.0
        cutoff = to_ms(day) + 86_400_000
        last: FundingRate | None = None
        for rate in rates:
            if rate.funding_time_ms >= cutoff:
                break
            last = rate
        if last is None:
            return 0.0
        return annualise_funding(last.rate, last.interval_hours or self.cfg.funding.default_interval_hours)

    def _attribute(self, day: date, book: _Book, marks: Mapping[str, float], fees: float,
                   slippage: float, funding: float, traded: float) -> list[dict[str, Any]]:
        rows = []
        gross = sum(abs(book.qty.get(s, 0.0) * marks.get(s, 0.0)) for s in book.qty) or 1.0
        for symbol, qty in book.qty.items():
            if not qty:
                continue
            notional = qty * marks.get(symbol, 0.0)
            share = abs(notional) / gross
            signal = self._signals.get(symbol, {}).get(day)
            rows.append({
                "day": day.isoformat(), "symbol": symbol,
                "side": "long" if qty > 0 else "short",
                "avg_notional": notional,
                "fees": -fees * share, "slippage": -slippage * share,
                "funding": funding * share, "traded_notional": traded * share,
                "signal": signal.signal if signal else 0.0,
            })
        return rows

    def _halt_threshold(self) -> float:
        return self.cfg.risk.hard_halt_dd

    # -- outputs ------------------------------------------------------------ #

    def _metrics(self, equity_path: Sequence[Mapping[str, Any]], returns: Sequence[float],
                 book: _Book) -> dict[str, float]:
        """A small, self-contained metric set. The full Section 10 table is the
        analytics engine's job; these are the numbers the P0 gate reads."""
        if not equity_path:
            return {}
        first = float(equity_path[0]["equity"])
        last = float(equity_path[-1]["equity"])
        days = len(equity_path)
        years = days / 365.0
        max_dd = max(float(p["drawdown"]) for p in equity_path)
        mean = sum(returns) / len(returns) if returns else 0.0
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
        vol = math.sqrt(var * 365.0)
        rf = self.cfg.bench.rf_annual
        ann_return = ((last / first) ** (1 / years) - 1.0) if years > 0 and first > 0 and last > 0 else 0.0
        sharpe = (ann_return - rf) / vol if vol > 0 else 0.0
        avg_equity = sum(float(p["equity"]) for p in equity_path) / days
        turnover = (book.traded_notional / avg_equity) * 365.0 / days if avg_equity > 0 and days else 0.0
        return {
            "net_pnl": last - first,
            "final_equity": last,
            "annualised_return": ann_return,
            "sharpe": sharpe,
            "volatility": vol,
            "max_drawdown": max_dd,
            "calmar": (ann_return / max_dd) if max_dd > 0 else 0.0,
            "turnover_annualised": turnover,
            "fees": book.fees,
            "slippage": book.slippage,
            "funding": book.funding,
            "traded_notional": book.traded_notional,
            "days": float(days),
        }

    def manifest(self, start: date, end: date, variant: str) -> dict[str, Any]:
        """Everything needed to reproduce this run bit-for-bit (11.4)."""
        checksums = {
            symbol: hashlib.sha256(
                json_dumps([[b.day.isoformat(), b.open, b.close, b.quote_volume] for b in bars])
                .encode()
            ).hexdigest()[:16]
            for symbol, bars in sorted(self.bars.items())
        }
        return {
            "start": start.isoformat(), "end": end.isoformat(), "variant": variant,
            "initial_equity": self.initial_equity,
            "parameters": self.cfg.parameter_snapshot(),
            "symbols": sorted(self.bars),
            "data_checksums": checksums,
            "cov_window_days": COV_WINDOW_DAYS,
            "fill_at": self.fill_at,
        }

    def _run_id(self, variant: str, start: date, end: date) -> str:
        payload = json_dumps(self.manifest(start, end, variant))
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _empty(self, start: date, end: date, variant: str, duration: float) -> BacktestResult:
        return BacktestResult(
            run_id=self._run_id(variant, start, end), start_day=start, end_day=end,
            variant=variant, equity=(), daily_returns=(), metrics={}, symbol_pnl=(), trades=(),
            manifest=self.manifest(start, end, variant), duration_s=duration,
        )


def equity_points(result: BacktestResult) -> list[EquityPoint]:
    """The result's path as ``EquityPoint``s, for the shared drawdown helpers."""
    return [
        EquityPoint(ts_ms=to_ms(date.fromisoformat(str(p["day"]))), equity=float(p["equity"]),
                    net_transfer=0.0, twr_factor=1.0, twr_index=float(p["twr_index"]))
        for p in result.equity
    ]


__all__ = ["COV_WINDOW_DAYS", "BacktestResult", "Simulator", "equity_points"]
