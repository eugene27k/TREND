"""The PRD Section 10 TREND metric additions — pure functions.

These are the numbers that answer "is the sleeve doing what it was designed to
do?" rather than "did it make money": realised vol against the target it was
sized for, how much it traded to get there, what that trading cost against the
backtest's conservative model, how much of the result is really BTC beta, and
how long the governor held risk down.

Conventions are the same as :mod:`aegis.analytics.metrics`: pure, ``None`` when
the input cannot support the number, 365-day annualisation, pairwise NaN
handling, and a raise (never a silent truncation) on mismatched lengths.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any

from aegis.analytics.metrics import (
    YEAR_DAYS,
    MetricInputError,
    beta,
    correlation,
    finite,
    paired,
    quantile,
    volatility,
)

STRONG_SIGNAL = 0.5
"""``|signal| > 0.5`` is the PRD's definition of a symbol carrying real conviction."""

_G_STATES: tuple[tuple[str, float], ...] = (("g1", 1.0), ("g05", 0.5), ("g025", 0.25))
_G_TOL = 1e-9

REGIME_DOWN = -0.10
REGIME_UP = 0.10


# --------------------------------------------------------------------------- #
# Vol targeting
# --------------------------------------------------------------------------- #


def realised_vol_vs_target(
    returns: Sequence[float], target: float, periods: int = YEAR_DAYS
) -> tuple[float | None, float | None]:
    """Annualised realised vol on active days, and its ratio to ``sigma_p,tgt``.

    "Active days only" (Section 10): a day the book sat flat produces a zero
    return that is not evidence about the vol the sizing achieved — including it
    would drag realised vol towards zero and make a correctly-sized book look
    under-risked.
    """
    active = [r for r in finite(returns) if r != 0.0]
    realised = volatility(active, periods=periods)
    if realised is None or target <= 0:
        return None, None
    return realised, realised / target


def vol_target_adherence(realised_vol_series: Sequence[float], target: float) -> float | None:
    """Fraction of days whose realised (rolling) vol sat inside 0.5x-1.5x target."""
    vols = finite(realised_vol_series)
    if not vols or target <= 0:
        return None
    lo, hi = 0.5 * target, 1.5 * target
    return sum(1 for v in vols if lo <= v <= hi) / len(vols)


# --------------------------------------------------------------------------- #
# Exposure and turnover
# --------------------------------------------------------------------------- #


def exposure_stats(
    gross_series: Sequence[float], net_series: Sequence[float], equity_series: Sequence[float]
) -> dict[str, float | None]:
    """Average / max / current gross and net exposure, as multiples of equity.

    ``max_net`` is the most extreme net reading, keeping its sign: a book that
    ran to -1.2x net is as interesting as one that ran to +1.2x.
    """
    n = len(gross_series)
    if len(net_series) != n or len(equity_series) != n:
        raise MetricInputError(
            f"exposure series lengths differ: gross={n} net={len(net_series)} equity={len(equity_series)}"
        )
    gross_x: list[float] = []
    net_x: list[float] = []
    for g, nt, e in zip(gross_series, net_series, equity_series, strict=True):
        if g is None or nt is None or e is None:
            continue
        fg, fn, fe = float(g), float(nt), float(e)
        if not (math.isfinite(fg) and math.isfinite(fn) and math.isfinite(fe)) or fe <= 0:
            continue
        gross_x.append(fg / fe)
        net_x.append(fn / fe)
    if not gross_x:
        return {
            "avg_gross": None,
            "max_gross": None,
            "current_gross": None,
            "avg_net": None,
            "max_net": None,
            "current_net": None,
            "n_obs": 0,
        }
    return {
        "avg_gross": sum(gross_x) / len(gross_x),
        "max_gross": max(gross_x),
        "current_gross": gross_x[-1],
        "avg_net": sum(net_x) / len(net_x),
        "max_net": max(net_x, key=abs),
        "current_net": net_x[-1],
        "n_obs": len(gross_x),
    }


def turnover(traded_notional: float, avg_equity: float, days: float) -> tuple[float | None, float | None]:
    """Period turnover and its annualisation (Appendix C.4: 1.2 -> 14.6)."""
    if avg_equity <= 0 or days <= 0:
        return None, None
    period = traded_notional / avg_equity
    return period, period * YEAR_DAYS / days


# --------------------------------------------------------------------------- #
# Execution quality
# --------------------------------------------------------------------------- #


def cost_per_unit_bps(fees: float, slippage: float, traded_notional: float) -> float | None:
    """``(fees + |slippage|) / traded notional`` in basis points.

    Both components are taken as magnitudes: fees are booked negative in the
    ledger and slippage can be signed, but a cost is a cost.
    """
    if traded_notional <= 0:
        return None
    return (abs(fees) + abs(slippage)) / traded_notional * 10_000.0


def execution_alpha(model_bps: float, realised_bps: float, traded_notional: float) -> float | None:
    """USDT saved against the backtest's conservative cost model.

    Positive means the live maker-first algorithm beat the model the P0 gate was
    judged on; negative means the backtest was optimistic and the tracking
    cost-ratio rule is about to complain.
    """
    if traded_notional <= 0:
        return None
    return (model_bps - realised_bps) / 10_000.0 * traded_notional


def maker_ratio(maker_notional: float, total_notional: float) -> float | None:
    """Maker-filled notional as a fraction of all filled notional."""
    if total_notional <= 0:
        return None
    return maker_notional / total_notional


def rebalance_completion(completions: Sequence[float]) -> float | None:
    """Mean completion percentage of the rebalances in the window."""
    vals = finite(completions)
    if not vals:
        return None
    return sum(vals) / len(vals)


# --------------------------------------------------------------------------- #
# Market exposure
# --------------------------------------------------------------------------- #


def beta_to_btc(
    strategy_returns: Sequence[float], btc_returns: Sequence[float], window: int | None = 60
) -> tuple[float | None, float | None]:
    """OLS beta of the strategy on BTC and the Pearson correlation.

    ``window`` trims to the most recent N paired observations (Section 10 asks
    for a 60-day window and the full period); ``None`` uses everything.
    """
    s, b = paired(strategy_returns, btc_returns)
    if window is not None and window > 0 and len(s) > window:
        s, b = s[-window:], b[-window:]
    return beta(s, b), correlation(s, b)


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def per_symbol_contribution(symbol_pnl: Mapping[str, float]) -> tuple[dict[str, float], float | None]:
    """Each symbol's share of total net P&L, and the largest share.

    Shares are taken against the *signed* total, so a losing book produces
    shares whose sign says "this symbol helped" rather than "this symbol was
    big". Concentration is the P0 gate's "no single symbol > 50 %" input.
    """
    clean = {str(k): float(v) for k, v in symbol_pnl.items() if v is not None and math.isfinite(float(v))}
    total = sum(clean.values())
    if not clean or total == 0.0:
        return {}, None
    shares = {k: v / total for k, v in clean.items()}
    return shares, max(shares.values())


def side_attribution(rows: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Long vs short net P&L and hit rate, from ``symbol_pnl_daily`` rows.

    A row is one symbol-day; the hit rate is the fraction of those symbol-days
    that made money on that side. Flat rows carry no side exposure and are
    excluded from both.
    """
    pnl = {"long": 0.0, "short": 0.0}
    n = {"long": 0, "short": 0}
    wins = {"long": 0, "short": 0}
    for row in rows:
        side = str(row.get("side", "flat")).lower()
        if side not in pnl:
            continue
        value = float(row.get("net_pnl", 0.0) or 0.0)
        if not math.isfinite(value):
            continue
        pnl[side] += value
        n[side] += 1
        if value > 0:
            wins[side] += 1
    return {
        "long_pnl": pnl["long"],
        "short_pnl": pnl["short"],
        "n_long": n["long"],
        "n_short": n["short"],
        "hit_rate_long": wins["long"] / n["long"] if n["long"] else None,
        "hit_rate_short": wins["short"] / n["short"] if n["short"] else None,
    }


def trade_statistics(trades: Iterable[Mapping[str, Any]]) -> dict[str, float | int | None]:
    """Count, holding period, win rate, average win/loss and the MAE distribution.

    A "trade" is an open -> flat episode per symbol (US-T14 AC 4), so these are
    the numbers a discretionary trader would recognise even though nothing here
    is discretionary.
    """
    days: list[float] = []
    pnls: list[float] = []
    maes: list[float] = []
    for t in trades:
        pnl = float(t.get("pnl", 0.0) or 0.0)
        if not math.isfinite(pnl):
            continue
        pnls.append(pnl)
        d = float(t.get("days", 0.0) or 0.0)
        if math.isfinite(d):
            days.append(d)
        mae = abs(float(t.get("mae", 0.0) or 0.0))
        if math.isfinite(mae):
            maes.append(mae)
    count = len(pnls)
    if count == 0:
        return {
            "count": 0,
            "avg_holding_days": None,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "mae_median": None,
            "mae_p90": None,
            "mae_max": None,
        }
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    return {
        "count": count,
        "avg_holding_days": sum(days) / len(days) if days else None,
        "win_rate": len(winners) / count,
        "avg_win": sum(winners) / len(winners) if winners else None,
        "avg_loss": sum(losers) / len(losers) if losers else None,
        "mae_median": quantile(maes, 0.5) if maes else None,
        "mae_p90": quantile(maes, 0.9) if maes else None,
        "mae_max": max(maes) if maes else None,
    }


def signal_statistics(signals_by_day: Mapping[Any, Mapping[str, float]]) -> dict[str, float | int | None]:
    """Mean ``|signal|``, signal turnover and the fraction of strong signals.

    Signal turnover is the mean per-day change: for each consecutive pair of
    days, the mean ``|delta signal|`` over the symbols present on both days,
    averaged over the pairs. A symbol that enters or leaves the universe
    therefore does not register as an infinite change.
    """
    days = sorted(signals_by_day)
    all_values: list[float] = []
    for day in days:
        all_values.extend(finite(list(signals_by_day[day].values())))
    deltas: list[float] = []
    for prev_day, day in pairwise(days):
        prev = signals_by_day[prev_day]
        cur = signals_by_day[day]
        shared = [s for s in cur if s in prev]
        changes = [
            abs(float(cur[s]) - float(prev[s]))
            for s in shared
            if math.isfinite(float(cur[s])) and math.isfinite(float(prev[s]))
        ]
        if changes:
            deltas.append(sum(changes) / len(changes))
    if not all_values:
        return {"mean_abs_signal": None, "signal_turnover": None, "strong_frac": None, "n_obs": 0}
    return {
        "mean_abs_signal": sum(abs(v) for v in all_values) / len(all_values),
        "signal_turnover": sum(deltas) / len(deltas) if deltas else None,
        "strong_frac": sum(1 for v in all_values if abs(v) > STRONG_SIGNAL) / len(all_values),
        "n_obs": len(all_values),
    }


# --------------------------------------------------------------------------- #
# Risk regime
# --------------------------------------------------------------------------- #


def governor_time_in_state(
    governor_rows: Iterable[Mapping[str, Any]], start: int, end: int
) -> dict[str, float | None]:
    """Fraction of ``[start, end)`` spent at g = 1 / 0.5 / 0.25.

    Time-weighted, not row-counted: the governor writes a row only when ``g``
    changes, so counting rows would say a book that spent 89 days at g = 1 and
    one day at g = 0.25 split its time evenly. The state in force at ``start``
    is carried in from the last row at or before it (g = 1 when there is none —
    full risk is the default).
    """
    span = end - start
    if span <= 0:
        return {"g1": None, "g05": None, "g025": None, "other": None}

    rows = sorted(
        ({"ts": int(r["ts"]), "g": float(r.get("g_after", 1.0))} for r in governor_rows),
        key=lambda r: r["ts"],
    )
    current = 1.0
    for row in rows:
        if row["ts"] <= start:
            current = row["g"]
    buckets = {"g1": 0.0, "g05": 0.0, "g025": 0.0, "other": 0.0}

    def bucket(g: float) -> str:
        for name, level in _G_STATES:
            if abs(g - level) <= _G_TOL:
                return name
        return "other"

    cursor = start
    for row in rows:
        if row["ts"] <= start or row["ts"] >= end:
            continue
        buckets[bucket(current)] += row["ts"] - cursor
        cursor = row["ts"]
        current = row["g"]
    buckets[bucket(current)] += end - cursor
    return {k: v / span for k, v in buckets.items()}


def regime_table(
    monthly_strategy_pnl: Mapping[str, float],
    monthly_btc_returns: Mapping[str, float],
    exposures: Mapping[str, float],
) -> dict[str, dict[str, Any]]:
    """Months bucketed by BTC monthly return: P&L, hit rate, average exposure.

    The buckets are the PRD's: BTC below -10 %, between -10 % and +10 %, above
    +10 %. This is the table that says whether the sleeve is a trend follower or
    a levered long.
    """
    order = (
        ("down", f"< {REGIME_DOWN:.0%}"),
        ("flat", f"{REGIME_DOWN:.0%}..{REGIME_UP:+.0%}"),
        ("up", f"> {REGIME_UP:+.0%}"),
    )
    acc: dict[str, dict[str, list[float]]] = {k: {"pnl": [], "exposure": []} for k, _ in order}
    for month, btc in sorted(monthly_btc_returns.items()):
        if month not in monthly_strategy_pnl:
            continue
        fb = float(btc)
        if not math.isfinite(fb):
            continue
        key = "down" if fb < REGIME_DOWN else ("up" if fb > REGIME_UP else "flat")
        pnl = float(monthly_strategy_pnl[month])
        if not math.isfinite(pnl):
            continue
        acc[key]["pnl"].append(pnl)
        exp = exposures.get(month)
        if exp is not None and math.isfinite(float(exp)):
            acc[key]["exposure"].append(float(exp))
    out: dict[str, dict[str, Any]] = {}
    for key, label in order:
        pnls = acc[key]["pnl"]
        exps = acc[key]["exposure"]
        out[key] = {
            "label": label,
            "months": len(pnls),
            "pnl": sum(pnls),
            "hit_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
            "avg_exposure": (sum(exps) / len(exps)) if exps else None,
        }
    return out


def funding_pnl_share(funding: float, gross_pnl: float) -> float | None:
    """Funding as a share of gross (pre-cost) P&L.

    TREND does not trade funding, so this is a diagnostic: a large share means
    the carry overlay, not the trend, paid for the month.
    """
    if gross_pnl == 0.0 or not math.isfinite(gross_pnl) or not math.isfinite(funding):
        return None
    return funding / gross_pnl


__all__ = [
    "REGIME_DOWN",
    "REGIME_UP",
    "STRONG_SIGNAL",
    "beta_to_btc",
    "cost_per_unit_bps",
    "execution_alpha",
    "exposure_stats",
    "funding_pnl_share",
    "governor_time_in_state",
    "maker_ratio",
    "per_symbol_contribution",
    "realised_vol_vs_target",
    "rebalance_completion",
    "regime_table",
    "side_attribution",
    "signal_statistics",
    "trade_statistics",
    "turnover",
    "vol_target_adherence",
]
