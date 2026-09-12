"""Accounting — the layer that makes "every number reconciles" true.

Four services, in the order the engine runs them each day:

``LedgerService``   pulls the venue's income feed into ``ledger`` (cash truth).
``Reconciler``      compares local state against the venue and blocks the engine
                    when they disagree. It runs *before* the snapshot: the
                    snapshot rewrites the local position table from the venue,
                    and a table copied from the venue always agrees with it.
``SnapshotService`` records account + positions and maintains the time-weighted
                    equity curve the governor reads.
``Attribution``     splits the day's P&L per symbol and per side, and proves the
                    split adds up (US-T14 AC 3).

Everything here is I/O over ``ctx.gateway`` and ``ctx.repos``; the maths it needs
(time-weighted returns, drawdown) is imported from the pure modules rather than
re-implemented.
"""

from __future__ import annotations

from aegis.accounting.attribution import (
    ATTRIBUTION_IDENTITY_BREAK,
    IDENTITY_TOLERANCE_USDT,
    Attribution,
)
from aegis.accounting.ledger import LEDGER_SYNC_FAILED, LedgerService
from aegis.accounting.reconcile import (
    LIQUIDATION_SUSPECTED,
    RECONCILIATION_BREAK,
    RECONCILIATION_RESOLVED,
    Reconciler,
    ReconResult,
)
from aegis.accounting.snapshots import SnapshotService

__all__ = [
    "ATTRIBUTION_IDENTITY_BREAK",
    "IDENTITY_TOLERANCE_USDT",
    "LEDGER_SYNC_FAILED",
    "LIQUIDATION_SUSPECTED",
    "RECONCILIATION_BREAK",
    "RECONCILIATION_RESOLVED",
    "Attribution",
    "LedgerService",
    "ReconResult",
    "Reconciler",
    "SnapshotService",
]
