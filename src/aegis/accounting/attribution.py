"""Per-symbol and long/short attribution (US-T14).

Where did the money come from, and where did it go? A trend book makes its
return from price moves and loses some of it to funding, commission and
execution, and those four components have to be separable per symbol and per
side or the operator cannot tell a working signal from an expensive one.

Components (US-T14 AC 1), all in USDT and all signed as P&L:

``price_pnl``   realised P&L from the day's fills plus the change in unrealised
                P&L across the day — the mark-to-market of holding the position.
``funding``     the day's ``FUNDING_FEE`` ledger rows for the symbol.
``fees``        the day's ``COMMISSION`` ledger rows (negative — a cost).
``slippage``    the execution cost against the decision mid, ``sum(bps/1e4 x notional)``,
                negated so it reads as a cost like ``fees``.

``net_pnl = price_pnl + funding + fees``. Slippage is deliberately **not** in
that sum: it is already inside ``price_pnl`` (we paid it in the fill price), so
adding it would double count and break the identity below. It is carried as a
memo column because execution quality is a separate question from P&L.

The identity (AC 3) is the point of the module::

    sum(symbol net_pnl) + sum(non-position ledger items) == equity change - net transfers

to 0.01 USDT per day. The left side is built from what the engine believes it
did (its fills) plus the venue's income feed; the right side from the account
snapshots. They can only agree if the fills we booked match the realised P&L the
venue booked and the ledger is complete, which is exactly what needs testing —
a missed fill, a duplicated income row or a forgotten rebate all show up as a
non-zero residual.

Trade episodes (AC 4) are derived from the snapshot sequence rather than from
fills, because a position can also be moved by a liquidation or an ADL
assignment: the snapshots are what actually happened to the book.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from aegis.core.clock import DAY_MS, day_of, day_start_ms
from aegis.core.context import Context
from aegis.core.types import IncomeType, PositionSide
from aegis.storage.db import json_loads

#: Alert code owned by this module.
ATTRIBUTION_IDENTITY_BREAK = "ATTRIBUTION_IDENTITY_BREAK"

#: The identity tolerance from US-T14 AC 3.
IDENTITY_TOLERANCE_USDT = 0.01

#: Below this the average notional is treated as flat rather than a rounding sign.
FLAT_NOTIONAL = 1e-9

#: Ledger types attributed to a symbol; everything else is a "non-position item".
_ATTRIBUTED = (IncomeType.REALIZED_PNL, IncomeType.FUNDING_FEE, IncomeType.COMMISSION)


class Attribution:
    """Builds ``symbol_pnl_daily`` and the ``trades`` episode table."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ #
    # Daily per-symbol rows
    # ------------------------------------------------------------------ #

    def compute_day(self, day: date, now_ms: int) -> list[dict[str, Any]]:
        """Compute and persist one ``symbol_pnl_daily`` row per symbol.

        A day still in progress is attributed up to ``now_ms``; re-running after
        it closes replaces the rows, since the table is keyed on (day, symbol).
        """
        start_ms, end_ms = self._window(day, now_ms)
        repos = self.ctx.repos

        fills = repos.fills.between(start_ms, end_ms)
        ledger = repos.ledger.between(start_ms, end_ms)
        opening = self._positions(repos.snapshots.last_before(start_ms))
        closing = self._positions(repos.snapshots.last_before(end_ms - 1))
        observations = self._observations(start_ms, end_ms)
        signals = {r["symbol"]: float(r["signal"]) for r in repos.signals.day(day)}

        funding = self._ledger_by_symbol(ledger, IncomeType.FUNDING_FEE)
        fees = self._ledger_by_symbol(ledger, IncomeType.COMMISSION)

        symbols = set(opening) | set(closing) | {f.symbol for f in fills} | set(funding) | set(fees)
        for snapshot in observations:
            symbols |= set(snapshot)

        rows: list[dict[str, Any]] = []
        for symbol in sorted(s for s in symbols if s):
            symbol_fills = [f for f in fills if f.symbol == symbol]
            realised = sum(f.realized_pnl for f in symbol_fills)
            unrealised_change = _upnl(closing, symbol) - _upnl(opening, symbol)
            price_pnl = realised + unrealised_change
            slippage = -sum(f.slippage_bps / 10_000.0 * abs(f.notional) for f in symbol_fills)
            avg_notional = _mean(
                [_notional(snapshot, symbol) for snapshot in observations] or [_notional(closing, symbol)]
            )
            rows.append(
                {
                    "symbol": symbol,
                    "side": _side(avg_notional),
                    "avg_notional": avg_notional,
                    "price_pnl": price_pnl,
                    "funding": funding.get(symbol, 0.0),
                    "fees": fees.get(symbol, 0.0),
                    "slippage": slippage,
                    "net_pnl": price_pnl + funding.get(symbol, 0.0) + fees.get(symbol, 0.0),
                    "traded_notional": sum(abs(f.notional) for f in symbol_fills),
                    "signal": signals.get(symbol, 0.0),
                }
            )

        repos.symbol_pnl.upsert_many(day, rows)
        return rows

    # ------------------------------------------------------------------ #
    # The identity
    # ------------------------------------------------------------------ #

    def identity_check(self, day: date) -> tuple[bool, float]:
        """``(ok, residual)`` for US-T14 AC 3, over the stored rows of ``day``.

        Reads what was persisted rather than recomputing, so the check also
        covers the write path. A non-zero residual raises
        ``ATTRIBUTION_IDENTITY_BREAK``: the operator must never learn from a
        report that the books have not added up for a week.
        """
        start_ms = day_start_ms(day)
        end_ms = start_ms + DAY_MS
        repos = self.ctx.repos

        attributed_net = sum(float(r["net_pnl"]) for r in repos.symbol_pnl.between(day, day))

        sums = repos.ledger.sum_by_type(start_ms, end_ms)
        transfers = sum(v for k, v in sums.items() if IncomeType.parse(k).is_transfer)
        attributed_ledger = sum(sums.get(str(t), 0.0) for t in _ATTRIBUTED)
        non_position = sum(sums.values()) - transfers - attributed_ledger

        opening = repos.snapshots.last_before(start_ms)
        closing = repos.snapshots.last_before(end_ms - 1)
        start_equity = float(opening["margin_balance"]) if opening else 0.0
        end_equity = float(closing["margin_balance"]) if closing else start_equity

        residual = (attributed_net + non_position) - ((end_equity - start_equity) - transfers)
        ok = abs(residual) <= IDENTITY_TOLERANCE_USDT
        if not ok:
            self.ctx.alerts.warn(
                ATTRIBUTION_IDENTITY_BREAK,
                f"{day.isoformat()} does not reconcile: residual {residual:.4f} USDT",
                {
                    "day": day.isoformat(),
                    "residual": residual,
                    "attributed_net": attributed_net,
                    "non_position": non_position,
                    "equity_change": end_equity - start_equity,
                    "net_transfer": transfers,
                },
            )
        return ok, residual

    # ------------------------------------------------------------------ #
    # Trade episodes
    # ------------------------------------------------------------------ #

    def update_trades(self, day: date, now_ms: int) -> int:
        """Maintain open -> flat episodes from the day's snapshots (AC 4).

        Returns the number of ``trades`` rows written. Run after
        :meth:`compute_day`: a closing episode takes its P&L from the per-symbol
        daily rows, so those must already exist for the days it spans.
        """
        start_ms, end_ms = self._window(day, now_ms)
        repos = self.ctx.repos
        signals = {r["symbol"]: float(r["signal"]) for r in repos.signals.day(day)}

        state = {s: float(p.get("qty", 0.0)) for s, p in self._opening_positions(start_ms).items()}
        live: dict[str, dict[str, Any]] = {}
        written: set[str] = set()

        for snapshot in repos.snapshots.between(start_ms, end_ms):
            positions = self._positions(snapshot)
            ts = int(snapshot["ts"])
            for symbol in sorted(set(state) | set(positions)):
                before = state.get(symbol, 0.0)
                now_qty = float(positions.get(symbol, {}).get("qty", 0.0))
                state[symbol] = now_qty

                if before == 0.0 and now_qty != 0.0:
                    live[symbol] = _new_episode(symbol, now_qty, ts, signals.get(symbol, 0.0))
                trade = live.get(symbol) or self._resume(symbol)
                if trade is None:
                    continue
                live[symbol] = trade

                if now_qty != 0.0:
                    # Max adverse excursion: the worst unrealised loss seen while
                    # the episode is open, carried as a positive magnitude.
                    upnl = float(positions.get(symbol, {}).get("upnl", 0.0))
                    trade["mae"] = max(float(trade["mae"]), -upnl)
                    trade["max_notional"] = max(
                        float(trade["max_notional"]), abs(_notional(positions, symbol))
                    )
                    continue

                if before != 0.0:
                    open_ts = int(trade["open_ts"])
                    trade["close_ts"] = ts
                    trade["days"] = (ts - open_ts) / DAY_MS
                    trade["pnl"] = repos.symbol_pnl.by_symbol(day_of(open_ts), day).get(symbol, 0.0)
                    trade["exit_signal"] = signals.get(symbol, 0.0)
                    repos.trades.upsert(trade)
                    written.add(str(trade["trade_key"]))
                    live.pop(symbol, None)

        for trade in live.values():
            repos.trades.upsert(trade)
            written.add(str(trade["trade_key"]))
        return len(written)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _resume(self, symbol: str) -> dict[str, Any] | None:
        """The episode already open for ``symbol``, as a mutable dict."""
        row = self.ctx.repos.trades.open_trade(symbol)
        return dict(row) if row is not None else None

    def _opening_positions(self, start_ms: int) -> dict[str, dict[str, Any]]:
        return self._positions(self.ctx.repos.snapshots.last_before(start_ms))

    def _window(self, day: date, now_ms: int) -> tuple[int, int]:
        start_ms = day_start_ms(day)
        return start_ms, min(start_ms + DAY_MS, max(now_ms + 1, start_ms))

    def _observations(self, start_ms: int, end_ms: int) -> list[dict[str, dict[str, Any]]]:
        """Position readings that describe the day: its opening state and every
        snapshot inside it."""
        rows = self.ctx.repos.snapshots.between(start_ms, end_ms)
        opening = self.ctx.repos.snapshots.last_before(start_ms)
        out = [self._positions(r) for r in rows]
        if opening is not None and int(opening["ts"]) < start_ms:
            out.insert(0, self._positions(opening))
        return out

    @staticmethod
    def _positions(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if snapshot is None:
            return {}
        rows = json_loads(snapshot.get("positions_json"), []) or []
        return {str(r["symbol"]): r for r in rows if r.get("symbol")}

    @staticmethod
    def _ledger_by_symbol(rows: list[dict[str, Any]], income_type: IncomeType) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows:
            if IncomeType.parse(str(row["income_type"])) is not income_type:
                continue
            symbol = str(row["symbol"] or "")
            out[symbol] = out.get(symbol, 0.0) + float(row["amount"])
        return out


def _new_episode(symbol: str, qty: float, ts_ms: int, entry_signal: float) -> dict[str, Any]:
    """A fresh open -> flat episode, keyed on the instant the position opened."""
    return {
        "trade_key": f"{symbol}:{ts_ms}",
        "symbol": symbol,
        "side": str(PositionSide.LONG if qty > 0 else PositionSide.SHORT),
        "open_ts": ts_ms,
        "close_ts": None,
        "days": 0.0,
        "pnl": 0.0,
        "mae": 0.0,
        "max_notional": 0.0,
        "entry_signal": entry_signal,
        "exit_signal": 0.0,
    }


def _upnl(positions: dict[str, dict[str, Any]], symbol: str) -> float:
    return float(positions.get(symbol, {}).get("upnl", 0.0))


def _notional(positions: dict[str, dict[str, Any]], symbol: str) -> float:
    position = positions.get(symbol)
    if not position:
        return 0.0
    return float(position.get("qty", 0.0)) * float(position.get("mark", 0.0))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _side(avg_notional: float) -> str:
    if avg_notional > FLAT_NOTIONAL:
        return str(PositionSide.LONG)
    if avg_notional < -FLAT_NOTIONAL:
        return str(PositionSide.SHORT)
    return str(PositionSide.FLAT)


__all__ = ["ATTRIBUTION_IDENTITY_BREAK", "IDENTITY_TOLERANCE_USDT", "Attribution"]
