"""Reconciliation against the venue (CARRY US-10, reused for TREND; US-T11 AC 1).

Invariant 2 of the PRD — "every number reconciles" — is this file. The engine's
local tables are a *cache* of what the venue holds; the moment they disagree,
every downstream decision (deltas, exposure caps, margin headroom) is computed
against fiction. So three comparisons run on a schedule and after every
rebalance:

* ``positions`` — our position table against ``/fapi/v2/positionRisk``,
* ``balance``   — the equity implied by the ledger against the venue's margin
  balance,
* ``fills``     — our fill table against ``/fapi/v1/userTrades``, booking
  anything the engine missed (a fill lost to a restart is still cash).

Any break blocks risk-increasing orders (Section 12) and alerts ``WARN``,
escalating to ``CRITICAL`` once it has stayed open longer than
``cfg.risk.reconciliation_critical_minutes``. The escalation is measured from
the *oldest* unresolved break row rather than from this call, because the
operator cares how long the book has been untrustworthy, not how many times we
noticed.

The sharpest case is a position that moved with no order of ours behind it:
that is a liquidation or an ADL assignment (Section 12, US-T11 AC 1). It is not
a stale cache and it will not resolve itself, so it is reported as its own break
kind and raised as ``CRITICAL`` immediately rather than waiting out the
escalation window.

Tolerances are deliberately tight. A position matches if it is within one lot
step (the smallest quantity the venue can express); a balance matches within
0.01 USDT. The ledger is cash and the venue's margin balance marks open
positions to market, so the two are made comparable by *adding* the venue's own
unrealised P&L to the ledger side — never by treating it as slack, which would
hide a real cash break of up to twice its size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis.core.context import Context
from aegis.core.types import Fill

#: Alert codes owned by this module.
RECONCILIATION_BREAK = "RECONCILIATION_BREAK"
RECONCILIATION_RESOLVED = "RECONCILIATION_RESOLVED"
LIQUIDATION_SUSPECTED = "LIQUIDATION_SUSPECTED"

#: How far back "recent" reaches when looking for our own fills and trades.
DEFAULT_LOOKBACK_MS = 3_600_000

#: Cash tolerance before an equity difference counts as a break.
BALANCE_TOLERANCE_USDT = 0.01

#: Quantity tolerance when the symbol's lot step is unknown.
DEFAULT_QTY_TOLERANCE = 1e-9

KIND_POSITIONS = "positions"
KIND_BALANCE = "balance"
KIND_FILLS = "fills"
KIND_ALL = "all"

BREAK_POSITION_MISMATCH = "position_mismatch"
BREAK_UNEXPLAINED = "unexplained_position_change"
BREAK_BALANCE = "balance_mismatch"
BREAK_MISSING_FILL = "missing_fill"


@dataclass(frozen=True, slots=True)
class ReconResult:
    """One comparison's verdict. ``breaks`` is what the dashboard renders."""

    ok: bool
    kind: str
    breaks: tuple[dict, ...]
    detail: str


class Reconciler:
    """Compares local state against the venue and records every verdict."""

    def __init__(self, ctx: Context, *, lookback_ms: int = DEFAULT_LOOKBACK_MS) -> None:
        self.ctx = ctx
        self.lookback_ms = max(0, int(lookback_ms))

    # ------------------------------------------------------------------ #
    # Positions
    # ------------------------------------------------------------------ #

    def positions(self, now_ms: int) -> ReconResult:
        """Compare the local position table against the venue, symbol by symbol.

        Call this *before* the day's snapshot: ``SnapshotService.take`` rewrites
        the local position table from the same venue read, and a table copied
        from the venue always agrees with it. With no snapshot ever recorded
        there is no local state to compare either — "every position appeared out
        of nowhere" is the absence of history, not a liquidation — so the check
        abstains exactly as :meth:`balance` does.
        """
        if self.ctx.repos.snapshots.first() is None:
            result = ReconResult(
                ok=True,
                kind=KIND_POSITIONS,
                breaks=(),
                detail="no baseline snapshot — positions not evaluable",
            )
            self._record(now_ms, result)
            return result

        local = self.ctx.repos.positions.all()
        venue = self.ctx.gateway.positions()
        traded = self._symbols_traded_since(now_ms - self.lookback_ms, now_ms)

        breaks: list[dict[str, Any]] = []
        for symbol in sorted(set(local) | set(venue)):
            local_qty = local[symbol].qty if symbol in local else 0.0
            venue_qty = venue[symbol].qty if symbol in venue else 0.0
            diff = venue_qty - local_qty
            if abs(diff) <= self._qty_tolerance(symbol):
                continue
            # No order of ours moved this symbol in the window -> the venue moved
            # it for us: liquidation or ADL (US-T11 AC 1).
            kind = BREAK_POSITION_MISMATCH if symbol in traded else BREAK_UNEXPLAINED
            breaks.append(
                {
                    "kind": kind,
                    "symbol": symbol,
                    "local_qty": local_qty,
                    "exchange_qty": venue_qty,
                    "diff": diff,
                }
            )

        unexplained = [b for b in breaks if b["kind"] == BREAK_UNEXPLAINED]
        detail = (
            f"{len(breaks)} position break(s) across {len(set(local) | set(venue))} symbol(s)"
            if breaks
            else f"{len(venue)} position(s) match"
        )
        result = ReconResult(ok=not breaks, kind=KIND_POSITIONS, breaks=tuple(breaks), detail=detail)
        self._record(now_ms, result)
        if unexplained:
            symbols = ", ".join(str(b["symbol"]) for b in unexplained)
            self.ctx.alerts.critical(
                LIQUIDATION_SUSPECTED,
                f"position changed with no order of ours: {symbols}",
                {"breaks": unexplained},
            )
        return result

    # ------------------------------------------------------------------ #
    # Balance
    # ------------------------------------------------------------------ #

    def balance(self, now_ms: int) -> ReconResult:
        """Compare ledger-derived equity against the venue's margin balance.

        The ledger holds changes, not levels, so it is anchored on the oldest
        stored snapshot's wallet balance and summed forward. That gives cash;
        the venue's unrealised P&L is added to it to reach the same quantity the
        venue calls the margin balance. Without an anchor there is nothing to
        compare and the check abstains rather than inventing a starting capital
        of zero.
        """
        anchor = self.ctx.repos.snapshots.first()
        if anchor is None:
            result = ReconResult(
                ok=True, kind=KIND_BALANCE, breaks=(), detail="no baseline snapshot — balance not evaluable"
            )
            self._record(now_ms, result)
            return result

        anchor_ts = int(anchor["ts"])
        sums = self.ctx.repos.ledger.sum_by_type(anchor_ts + 1, now_ms + 1)
        cash = float(anchor["wallet_balance"]) + sum(sums.values())

        account = self.ctx.gateway.account()
        # Cash + the venue's own mark-to-market is what the margin balance is.
        expected = cash + account.unrealized_pnl
        diff = expected - account.margin_balance
        tolerance = BALANCE_TOLERANCE_USDT
        ok = abs(diff) <= tolerance

        breaks: tuple[dict, ...] = ()
        if not ok:
            breaks = (
                {
                    "kind": BREAK_BALANCE,
                    "expected_equity": expected,
                    "exchange_equity": account.margin_balance,
                    "unrealized_pnl": account.unrealized_pnl,
                    "diff": diff,
                    "tolerance": tolerance,
                },
            )
        detail = (
            f"ledger {expected:.4f} (cash {cash:.4f} + unrealised {account.unrealized_pnl:.4f})"
            f" vs venue {account.margin_balance:.4f} (tol {tolerance:.4f})"
        )
        result = ReconResult(ok=ok, kind=KIND_BALANCE, breaks=breaks, detail=detail)
        self._record(now_ms, result)
        return result

    # ------------------------------------------------------------------ #
    # Fills
    # ------------------------------------------------------------------ #

    def fills(self, now_ms: int) -> ReconResult:
        """Book any recent venue trade the engine never recorded.

        A missed fill is both a break (our state was wrong) and something we can
        repair on the spot, so the fills are inserted before the result is
        returned. ``fills`` is keyed on the venue's trade id, so booking a trade
        that a slow order poll also books later costs nothing.
        """
        start_ms = max(0, now_ms - self.lookback_ms)
        known = {f.trade_id for f in self.ctx.repos.fills.between(start_ms, now_ms + 1)}

        missing: list[Fill] = []
        for symbol in sorted(self._symbols_of_interest(start_ms, now_ms)):
            for fill in self.ctx.gateway.user_trades(symbol, start_ms=start_ms):
                if fill.ts_ms > now_ms or fill.trade_id in known:
                    continue
                known.add(fill.trade_id)
                missing.append(fill)

        if missing:
            self.ctx.repos.fills.add_many(missing)
        breaks = tuple(
            {
                "kind": BREAK_MISSING_FILL,
                "symbol": f.symbol,
                "trade_id": f.trade_id,
                "qty": f.qty,
                "price": f.price,
                "ts": f.ts_ms,
            }
            for f in missing
        )
        detail = f"booked {len(missing)} missing fill(s)" if missing else f"{len(known)} recent fill(s) match"
        result = ReconResult(ok=not missing, kind=KIND_FILLS, breaks=breaks, detail=detail)
        self._record(now_ms, result)
        return result

    # ------------------------------------------------------------------ #
    # All three
    # ------------------------------------------------------------------ #

    def run_all(self, now_ms: int) -> ReconResult:
        """Run every comparison, then alert or resolve on the combined verdict."""
        results = [self.positions(now_ms), self.balance(now_ms), self.fills(now_ms)]
        breaks = tuple(b for r in results for b in r.breaks)
        ok = all(r.ok for r in results)
        detail = "; ".join(f"{r.kind}: {r.detail}" for r in results)

        if ok:
            self._resolve_open(now_ms)
        else:
            self._alert_break(now_ms, breaks, detail)
        return ReconResult(ok=ok, kind=KIND_ALL, breaks=breaks, detail=detail)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _record(self, now_ms: int, result: ReconResult) -> int:
        return self.ctx.repos.reconciliations.add(
            now_ms, result.kind, result.ok, result.detail, list(result.breaks)
        )

    def _resolve_open(self, now_ms: int) -> None:
        open_rows = self.ctx.repos.reconciliations.open_breaks()
        if not open_rows:
            return
        for row in open_rows:
            self.ctx.repos.reconciliations.resolve(int(row["id"]), now_ms)
        self.ctx.alerts.info(
            RECONCILIATION_RESOLVED,
            f"{len(open_rows)} reconciliation break(s) resolved",
            {"resolved": len(open_rows)},
        )

    def _alert_break(self, now_ms: int, breaks: tuple[dict, ...], detail: str) -> None:
        open_rows = self.ctx.repos.reconciliations.open_breaks()
        oldest = min((int(r["ts"]) for r in open_rows), default=now_ms)
        open_minutes = max(0.0, (now_ms - oldest) / 60_000.0)
        context = {"breaks": list(breaks), "open_minutes": round(open_minutes, 2)}
        if open_minutes >= self.ctx.cfg.risk.reconciliation_critical_minutes:
            self.ctx.alerts.critical(
                RECONCILIATION_BREAK, f"unresolved for {open_minutes:.0f} min — {detail}", context
            )
        else:
            self.ctx.alerts.warn(RECONCILIATION_BREAK, detail, context)

    def _qty_tolerance(self, symbol: str) -> float:
        info = self.ctx.repos.symbol_meta.get(symbol)
        if info is not None and info.step_size > 0:
            return info.step_size
        return DEFAULT_QTY_TOLERANCE

    def _symbols_traded_since(self, start_ms: int, now_ms: int) -> set[str]:
        return {f.symbol for f in self.ctx.repos.fills.between(start_ms, now_ms + 1)}

    def _symbols_of_interest(self, start_ms: int, now_ms: int) -> set[str]:
        """Symbols worth asking the venue about: held either side, traded lately,
        or moved by the income feed.

        The income feed matters on its own: a liquidation closes the position and
        books our fills nowhere, so by the time the local table has been
        refreshed the symbol is in neither book and its trade print would never
        be pulled — leaving the realised P&L out of ``fills`` and the day's
        identity broken.
        """
        return (
            set(self.ctx.repos.positions.all())
            | set(self.ctx.gateway.positions())
            | self._symbols_traded_since(start_ms, now_ms)
            | self._symbols_with_income(start_ms, now_ms)
        )

    def _symbols_with_income(self, start_ms: int, now_ms: int) -> set[str]:
        return {
            str(row["symbol"]) for row in self.ctx.repos.ledger.between(start_ms, now_ms + 1) if row["symbol"]
        }


__all__ = [
    "BALANCE_TOLERANCE_USDT",
    "BREAK_BALANCE",
    "BREAK_MISSING_FILL",
    "BREAK_POSITION_MISMATCH",
    "BREAK_UNEXPLAINED",
    "DEFAULT_LOOKBACK_MS",
    "LIQUIDATION_SUSPECTED",
    "RECONCILIATION_BREAK",
    "RECONCILIATION_RESOLVED",
    "ReconResult",
    "Reconciler",
]
