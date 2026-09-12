"""Income ledger sync (CARRY US-11, reused for TREND).

The exchange's ``/fapi/v1/income`` feed — not our own fill bookkeeping — is the
source of truth for cash. Realised P&L, commission, funding, rebates and
transfers all arrive there with the venue's own transaction id, which is what
makes the daily identity in ``attribution.py`` a real test rather than a
tautology: one side of it comes from the venue, the other from what the engine
believes it did.

Two properties matter more than anything else here.

**Idempotence.** ``ledger`` is unique on ``(strategy, tran_id, income_type, ts,
amount)`` and the repository inserts with ``OR IGNORE``, so re-syncing a window
books nothing new. Every restart re-reads the recent past; if that double
counted, every number downstream would drift a little every crash.

**Overlap.** A settlement can be written to the venue's income history *after*
we have already read past its timestamp — funding in particular is stamped with
the settlement time but materialises a little later. Advancing the cursor to the
last booked timestamp would step straight over such a row and lose it for good.
So the cursor is rewound by :data:`DEFAULT_OVERLAP_MS` after every successful
pull: the next sync re-reads the last hour, and idempotence makes that free.

An empty response never advances the cursor. "The venue returned no rows" is not
proof that there was no income — it is equally the shape of a silently degraded
endpoint — and the cost of being wrong is a permanently missing settlement.
"""

from __future__ import annotations

from typing import Any

from aegis.core.context import Context
from aegis.core.errors import GatewayError
from aegis.core.types import IncomeType, LedgerEntry

#: Alert code owned by this module.
LEDGER_SYNC_FAILED = "LEDGER_SYNC_FAILED"

#: How far the cursor is rewound after a pull, so a late settlement is re-read.
DEFAULT_OVERLAP_MS = 3_600_000

#: Rows per venue request. Binance caps ``/fapi/v1/income`` at 1000.
DEFAULT_PAGE_LIMIT = 1000

#: Hard stop on the paging loop — a venue that never stops paging is a bug,
#: not a reason to spin forever inside one tick of the engine.
MAX_PAGES = 500


class LedgerService:
    """Pulls the venue income feed into the ``ledger`` table."""

    def __init__(
        self,
        ctx: Context,
        *,
        overlap_ms: int = DEFAULT_OVERLAP_MS,
        page_limit: int = DEFAULT_PAGE_LIMIT,
    ) -> None:
        self.ctx = ctx
        self.overlap_ms = max(0, int(overlap_ms))
        self.page_limit = max(1, int(page_limit))

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def sync(self, now_ms: int) -> int:
        """Book everything from the stored cursor to ``now_ms``. Returns new rows.

        With no cursor the pull starts at the epoch: a fresh database wants the
        whole history, and the venue answers an empty range with a single empty
        page, so the "expensive" case costs one request.
        """
        cursor = self.ctx.repos.ledger.sync_cursor()
        return self._pull(cursor if cursor is not None else 0, now_ms)

    def backfill(self, since_ms: int) -> int:
        """Re-read from ``since_ms`` regardless of the cursor. Returns new rows.

        Used after a restore or when an operator suspects a gap. The cursor only
        ever moves forward, so a backfill of old history cannot rewind the live
        sync into re-reading months of rows on every tick.
        """
        return self._pull(max(0, int(since_ms)), self.ctx.clock.now_ms())

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _pull(self, start_ms: int, end_ms: int) -> int:
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        failure: GatewayError | None = None
        try:
            for _ in range(MAX_PAGES):
                batch = self.ctx.gateway.income(cursor, end_ms, self.page_limit)
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < self.page_limit:
                    break
                last = max(_int(r.get("time")) for r in batch)
                # A full page that does not advance time would loop forever; step
                # one millisecond past it. Re-reading a boundary row is harmless.
                cursor = last if last > cursor else last + 1
        except GatewayError as exc:
            failure = exc

        booked = self._book(rows, end_ms)
        if failure is not None:
            # Book what did arrive first, then let the caller decide (an
            # unreachable exchange is a safe-mode trigger, US-T13 AC 2).
            self.ctx.alerts.warn(
                LEDGER_SYNC_FAILED,
                f"income sync failed after {booked} rows: {failure}",
                {"start_ms": start_ms, "end_ms": end_ms, "booked": booked},
            )
            raise failure
        return booked

    def _book(self, rows: list[dict[str, Any]], end_ms: int) -> int:
        if not rows:
            return 0
        entries = [self._to_entry(r) for r in rows]
        booked = self.ctx.repos.ledger.add_many(entries)
        last_ts = max(e.ts_ms for e in entries)
        current = self.ctx.repos.ledger.sync_cursor() or 0
        new_cursor = max(current, last_ts - self.overlap_ms)
        if new_cursor != current:
            self.ctx.repos.ledger.set_sync_cursor(new_cursor, end_ms)
        return booked

    def _to_entry(self, row: dict[str, Any]) -> LedgerEntry:
        symbol = str(row.get("symbol") or "") or None
        trade_id = str(row.get("tradeId") or "") or None
        return LedgerEntry(
            strategy=self.ctx.strategy,
            ts_ms=_int(row.get("time")),
            income_type=IncomeType.parse(str(row.get("incomeType") or "")),
            asset=str(row.get("asset") or "USDT"),
            amount=_float(row.get("income")),
            symbol=symbol,
            tran_id=str(row.get("tranId") or ""),
            trade_id=trade_id,
            info=str(row.get("info") or ""),
        )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["DEFAULT_OVERLAP_MS", "DEFAULT_PAGE_LIMIT", "LEDGER_SYNC_FAILED", "LedgerService"]
