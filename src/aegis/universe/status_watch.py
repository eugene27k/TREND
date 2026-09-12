"""Symbol status and delisting watch — Section 5.10 (US-T11 AC 2/3).

A perpetual that stops trading is the one event that can strand a position: the
book disappears, the daily bar stops arriving, and the next scheduled rebalance
is up to 24 hours away. So this watch runs on its own hourly cadence and again
at rebalance start, and it is deliberately the *only* thing between rebalances
that can move a position — which is consistent with Invariant 1, because every
action it triggers reduces exposure.

What it does and does not do:

* It **detects and reports**; it never sends an order. ``check`` returns the
  symbols that must be closed and the caller hands them to the risk-cut
  executor. Splitting it this way keeps the watch free of trading state, and it
  is what lets the paper soak assert "status change -> flat within 5 minutes"
  against the same code the live engine runs.
* A symbol that has **vanished from ``exchangeInfo`` is delisted**: alert
  CRITICAL and close. The caller must then reconcile; if the position is still
  not flat after reconciliation it raises CRITICAL, because a position in an
  instrument the venue no longer lists cannot be managed by this engine at all.
* Alerts fire on a **transition**, not on every observation. Re-sending
  ``SYMBOL_STATUS`` every 60 minutes for the same SETTLING symbol would teach
  the operator to ignore the code; the state is held in memory, so a restart
  re-announces once, which is the right side to err on. That per-symbol memory
  replaces the AlertBus's own by-code suppression (``suppress_repeat=False``),
  which would otherwise deliver the first of two simultaneous delistings and
  silently drop the second.
"""

from __future__ import annotations

from aegis.core.context import Context
from aegis.core.types import Severity
from aegis.universe.service import UniverseService

#: The one status in which a perpetual is tradeable. Everything else — SETTLING,
#: PRE_DELIVERING, DELIVERING, DELIVERED, PRE_SETTLE, CLOSE, PENDING_TRADING —
#: means close now (5.10). Listed as "not TRADING" rather than as a denylist so a
#: status Binance adds tomorrow is treated as dangerous by default.
TRADING = "TRADING"

#: Sentinel status recorded for a symbol that left ``exchangeInfo`` entirely.
DELISTED = "DELISTED"


class StatusWatch:
    """Hourly (and at-rebalance) instrument status check."""

    def __init__(self, ctx: Context, universe: UniverseService | None = None) -> None:
        self.ctx = ctx
        self.universe = universe if universe is not None else UniverseService(ctx)
        self._reported: dict[str, str] = {}
        self._last_check_ms: int | None = None

    # ------------------------------------------------------------------ #
    # Status (US-T11 AC 2)
    # ------------------------------------------------------------------ #

    def is_due(self, now_ms: int) -> bool:
        """True when ``universe.status_watch_minutes`` have passed (or on first call)."""
        if self._last_check_ms is None:
            return True
        return now_ms - self._last_check_ms >= self.ctx.cfg.universe.status_watch_minutes * 60_000

    def check(self, now_ms: int) -> list[str]:
        """Symbols that must be closed immediately -> sorted list (5.10).

        Status other than ``TRADING`` raises ``WARN`` ``SYMBOL_STATUS``; a symbol
        absent from ``exchangeInfo`` raises ``CRITICAL`` ``SYMBOL_DELISTED``.
        Both are returned for closure by the risk-cut executor — **placing that
        order is the executor's job, not this one's**. After the close the caller
        reconciles, and a position that is still not flat is a CRITICAL the
        caller raises.

        Illiquid-flagged symbols are watched too (``all_symbols``): a flag stops
        us building a position, it does not mean we have none.
        """
        self._last_check_ms = now_ms
        symbols = self.universe.all_symbols(now_ms)
        if not symbols:
            return []

        info = self.ctx.gateway.exchange_info(refresh=True)
        closures: list[str] = []
        for symbol in symbols:
            listed = info.get(symbol)
            status = DELISTED if listed is None else listed.status
            if status == TRADING:
                self._reported.pop(symbol, None)
                continue
            closures.append(symbol)
            if self._reported.get(symbol) == status:
                continue
            self._reported[symbol] = status
            if status == DELISTED:
                self.ctx.alerts.emit(
                    Severity.CRITICAL,
                    "SYMBOL_DELISTED",
                    f"{symbol} has left exchangeInfo — treating as delisted, closing",
                    {"symbol": symbol},
                    suppress_repeat=False,
                )
            else:
                self.ctx.alerts.emit(
                    Severity.WARN,
                    "SYMBOL_STATUS",
                    f"{symbol} status {status} (not TRADING) — closing",
                    {"symbol": symbol, "status": status},
                    suppress_repeat=False,
                )
        return sorted(closures)

    # ------------------------------------------------------------------ #
    # Funding interval (US-T11 AC 3)
    # ------------------------------------------------------------------ #

    def check_funding_intervals(self, now_ms: int) -> dict[str, float]:
        """Detect and store ``fundingIntervalHours`` changes -> ``{symbol: new hours}``.

        The stored ``symbol_meta`` row is what the overlay annualises with
        (``rate x 8760 / interval``), so it is written *here*, before the next
        evaluation: a symbol that moves from 8h to 4h funding doubles its
        annualised carry, and sizing on the stale factor would halve a haircut
        that should have applied. A symbol seen for the first time is stored
        silently — there is no previous value for it to differ from.
        """
        symbols = self.universe.all_symbols(now_ms)
        if not symbols:
            return {}

        info = self.ctx.gateway.exchange_info()
        changed: dict[str, float] = {}
        updates = []
        for symbol in symbols:
            listed = info.get(symbol)
            if listed is None:
                continue
            stored = self.ctx.repos.symbol_meta.get(symbol)
            if stored is None:
                updates.append(listed)
                continue
            if stored.funding_interval_hours != listed.funding_interval_hours:
                changed[symbol] = listed.funding_interval_hours
                updates.append(listed)
        if updates:
            self.ctx.repos.symbol_meta.upsert_many(updates, now_ms)
        if changed:
            self.ctx.alerts.info(
                "FUNDING_INTERVAL_CHANGE",
                "funding interval changed: "
                + ", ".join(f"{s} -> {h:g}h" for s, h in sorted(changed.items())),
                {"intervals": changed},
            )
        return changed


__all__ = ["DELISTED", "TRADING", "StatusWatch"]
