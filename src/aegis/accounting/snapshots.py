"""Account snapshots and the time-weighted equity curve (CARRY US-11, US-T08 AC 3).

Two different questions need two different records.

``take`` answers "what did the venue hold at this instant" — the account state
and every open position, written verbatim. It is the raw material for
attribution (start/end unrealised P&L per symbol), for reconciliation (the local
position table is refreshed from the same read) and for the operator's audit
trail.

``update_equity_curve`` answers "how has the *capital* performed" — a different
question, because equity alone confuses performance with capital flow. A deposit
that lifts equity would print a new peak and erase a real drawdown; a withdrawal
would print a drawdown that never happened and cut the book for nothing. The
governor reads this curve (Section 5.7), so the distinction is not cosmetic: it
decides how much risk the engine is allowed to run.

The time-weighted arithmetic itself is *not* implemented here. It lives in
``aegis.portfolio.governor`` (``time_weighted_points`` / ``drawdown_from_curve``)
as a pure function with its own Appendix C tests; this module only supplies the
daily observations — closing equity and the transfers booked in the ledger for
that day — and persists what those functions return.

The whole curve is recomputed and re-upserted on every call. A day inserted out
of order (a backfill, a restored database) changes every ``twr_index`` after it,
and a curve that is only correct when written in order is a curve nobody can
trust.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from aegis.core.clock import DAY_MS, day_start_ms
from aegis.core.context import Context
from aegis.core.errors import DataGap
from aegis.core.types import AccountState, EquityPoint, IncomeType
from aegis.portfolio.governor import drawdown_from_curve, time_weighted_points


class SnapshotService:
    """Writes ``snapshots``, ``positions`` and ``equity_curve``."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #

    def take(self, now_ms: int) -> AccountState:
        """Persist one account + positions snapshot and refresh ``positions``.

        The snapshot is stamped with the engine clock rather than the venue's own
        timestamp: every other table is bucketed by engine time, and a snapshot
        that lands in a different day from the fills it accompanies would break
        the daily identity for a few milliseconds of clock skew.
        """
        account = self.ctx.gateway.account()
        positions = self.ctx.gateway.positions()
        rows = list(positions.values())
        if account.ts_ms != now_ms:
            account = replace(account, ts_ms=now_ms)

        gross = sum(abs(p.notional) for p in rows)
        net = sum(p.notional for p in rows)
        self.ctx.repos.snapshots.add(account, rows, gross=gross, net=net)
        self.ctx.repos.positions.replace_all(rows, now_ms)
        return account

    # ------------------------------------------------------------------ #
    # Equity curve
    # ------------------------------------------------------------------ #

    def update_equity_curve(self, day: date, now_ms: int) -> EquityPoint:
        """Compute and persist the time-weighted equity point for ``day``.

        The day's equity is the last snapshot at or before its close (or before
        ``now_ms`` for a day still in progress); its net transfer is the sum of
        the ledger rows in ``[00:00, next 00:00)`` whose income type
        :meth:`~aegis.core.types.IncomeType.is_transfer`.
        """
        start_ms = day_start_ms(day)
        end_ms = start_ms + DAY_MS
        cutoff = min(end_ms - 1, now_ms)
        snapshot = self.ctx.repos.snapshots.last_before(cutoff)
        if snapshot is None:
            raise DataGap(f"no account snapshot at or before {day.isoformat()} — cannot price the curve")

        observations = {
            row["day"]: (int(row["ts"]), float(row["equity"]), float(row["net_transfer"]))
            for row in self.ctx.repos.equity.all()
        }
        key = day.isoformat()
        observations[key] = (
            int(snapshot["ts"]),
            float(snapshot["margin_balance"]),
            self.net_transfer(start_ms, end_ms),
        )

        ordered = sorted(observations.items())
        weighted = time_weighted_points(
            [
                EquityPoint(ts_ms=ts, equity=equity, net_transfer=transfer)
                for _, (ts, equity, transfer) in ordered
            ]
        )

        result: EquityPoint | None = None
        peak = 1.0
        for (day_key, _), point in zip(ordered, weighted, strict=True):
            peak = max(peak, point.twr_index)
            self.ctx.repos.equity.upsert(
                day_key, point, peak_index=peak, drawdown=_drawdown(point.twr_index, peak)
            )
            if day_key == key:
                result = point
        if result is None:  # pragma: no cover - the day was inserted above
            raise DataGap(f"equity curve lost the point for {key}")
        return result

    def net_transfer(self, start_ms: int, end_ms: int) -> float:
        """Signed capital in/out booked in ``[start_ms, end_ms)`` — never P&L."""
        return sum(
            float(row["amount"])
            for row in self.ctx.repos.ledger.between(start_ms, end_ms)
            if IncomeType.parse(str(row["income_type"])).is_transfer
        )

    def drawdown(self, now_ms: int) -> float:
        """Current drawdown from the stored curve, ignoring points after ``now_ms``.

        Reads the persisted observations and lets the governor's own pure
        function derive the index, so the number the risk path acts on is
        produced by exactly the code its Appendix C vectors cover.
        """
        points = [p for p in self.ctx.repos.equity.points() if p.ts_ms <= now_ms]
        return drawdown_from_curve(points)


def _drawdown(index: float, peak: float) -> float:
    if peak <= 0.0:
        return 0.0
    return min(1.0, max(0.0, 1.0 - index / peak))


__all__ = ["SnapshotService"]
