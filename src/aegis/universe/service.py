"""Monthly universe refresh — the scheduled half of Section 5.1 (US-T02 AC 2/3).

``select_universe`` is pure and knows nothing about time, venues or storage.
This service is everything around it: when the selection runs, what information
set it is fed, where the answer is kept and who is told. Keeping that split
means a backtest month and a live month run the *same* arithmetic, and the only
difference between them is which inputs this service hands over.

Three decisions worth stating, because they are what makes the refresh safe to
run twice:

* **A month is selected once.** ``refresh_if_due`` returns ``None`` when the
  current month already has a stored universe, so a restart at 00:06 on the 1st
  neither re-ranks nor re-alerts. ``force=True`` is the operator's override.
* **The refresh never changes today's book.** It writes ``universe_history`` and
  clears illiquid flags; entrants are bought and leavers are flattened by the
  *next scheduled rebalance* (Invariant 1 and 9 — nothing here increases risk,
  and nothing here trades).
* **Illiquid flags expire here.** 5.9 step 6 flags a symbol the executor could
  not trade; the flag is meant to last until the universe is reconsidered, which
  is exactly this moment, so ``clear_all`` runs as part of the refresh rather
  than on a timer of its own.
"""

from __future__ import annotations

from datetime import date

from aegis.bars.service import BarService
from aegis.core.clock import add_months, at_utc, from_ms, month_key, month_start
from aegis.core.context import Context
from aegis.core.types import SymbolInfo, UniverseResult
from aegis.universe.select import select_universe


class UniverseService:
    """Runs, persists and announces the monthly selection."""

    def __init__(self, ctx: Context, bars: BarService | None = None) -> None:
        self.ctx = ctx
        self.bars = bars if bars is not None else BarService(ctx)
        self.repo = ctx.repos.universe

    # ------------------------------------------------------------------ #
    # The refresh
    # ------------------------------------------------------------------ #

    def refresh_if_due(self, now_ms: int, *, force: bool = False) -> UniverseResult | None:
        """Select the month's universe when due, else ``None`` (5.1, US-T02 AC 2).

        Due means: no universe has ever been stored (first start), or the current
        month has none and the clock has passed ``universe.refresh_time_utc`` on
        ``universe.refresh_day_utc``. The "has none" test is what makes the call
        idempotent within a month, and it also recovers a refresh missed while
        the engine was down: the selection still happens on the 3rd, with the
        same point-in-time cut-off it would have used on the 1st.
        """
        month = month_key(now_ms)
        if not force:
            if self.repo.month(month) is not None:
                return None
            if self.repo.latest_month() is not None and not self._schedule_reached(now_ms):
                return None

        exchange_info = self.ctx.gateway.exchange_info(refresh=True)
        self.ctx.repos.symbol_meta.upsert_many(exchange_info.values(), now_ms)

        candidates = self._candidates(exchange_info)
        self.bars.backfill(candidates)
        history = self.bars.volume_history(
            candidates, self.ctx.cfg.universe.min_history_days, before=month_start(month)
        )

        previous_month = self._previous_month(month)
        before = set(self.repo.symbols(previous_month)) if self.repo.month(previous_month) else None

        result = select_universe(exchange_info, history, self.ctx.cfg.universe, month)
        self.repo.save(result, now_ms)
        self.ctx.repos.illiquid.clear_all(now_ms)
        self._announce(result, before)
        return result

    def _schedule_reached(self, now_ms: int) -> bool:
        """True once the clock has passed this month's refresh moment."""
        now = from_ms(now_ms)
        cfg = self.ctx.cfg.universe
        day = date(now.year, now.month, min(cfg.refresh_day_utc, 28))
        return now_ms >= at_utc(day, cfg.refresh_time_utc)

    def _candidates(self, exchange_info: dict[str, SymbolInfo]) -> list[str]:
        """Symbols worth downloading history for.

        A download filter only — ``select_universe`` remains the sole authority on
        who is *in*. It is deliberately looser than the selector (no history gate)
        so that a symbol can accumulate the 400 days it needs while ineligible.
        """
        cfg = self.ctx.cfg.universe
        return sorted(
            s
            for s, i in exchange_info.items()
            if i.contract_type == "PERPETUAL"
            and i.quote_asset == cfg.quote_asset
            and i.base_asset not in cfg.exclude_bases
        )

    def _announce(self, result: UniverseResult, before: set[str] | None) -> None:
        """One alert per code, listing every symbol — see the AlertBus suppression note.

        ``AlertBus`` suppresses by ``code``, so an alert per entrant would deliver
        the first and swallow the rest. Entrants and leavers therefore travel as
        one message each, with reasons in the context the dashboard renders.
        """
        included = list(result.symbols)
        self.ctx.alerts.info(
            "UNIVERSE_REFRESH",
            f"universe {result.month}: {len(included)} symbols",
            {"month": result.month, "symbols": included},
        )
        if before is None:  # first ever selection: everything is an "entrant"
            return
        entrants = [s for s in included if s not in before]
        leavers = sorted(s for s in before if s not in included)
        if entrants:
            self.ctx.alerts.info(
                "UNIVERSE_ENTRY",
                f"universe {result.month} entrants: " + ", ".join(entrants),
                {"month": result.month, "symbols": entrants,
                 "reasons": {s: _reason(result, s) for s in entrants}},
            )
        if leavers:
            self.ctx.alerts.info(
                "UNIVERSE_EXIT",
                f"universe {result.month} leavers: " + ", ".join(leavers),
                {"month": result.month, "symbols": leavers,
                 "reasons": {s: _reason(result, s) for s in leavers}},
            )

    # ------------------------------------------------------------------ #
    # Reading the universe
    # ------------------------------------------------------------------ #

    def current_symbols(self, now_ms: int) -> list[str]:
        """Tradeable universe: the month's inclusions minus active illiquid flags.

        The flagged names are excluded from *sizing* (US-T11 AC 4), which means
        their target is absent rather than zero — the rebalance still flattens an
        existing position, it simply never builds a new one in a symbol the
        executor could not trade.
        """
        flagged = self.ctx.repos.illiquid.active_symbols(now_ms)
        return [s for s in self.all_symbols(now_ms) if s not in flagged]

    def all_symbols(self, now_ms: int) -> list[str]:
        """Every included symbol of the effective month, illiquid ones included.

        This is the set the status watch and the position reconciliation walk:
        an illiquid flag must never hide a symbol we still hold.
        """
        month = self.effective_month(now_ms)
        return self.repo.symbols(month) if month else []

    def leavers(self, now_ms: int) -> list[str]:
        """Symbols included last month but not this one (US-T02 AC 3).

        The caller flattens them at the next rebalance — a status change closes
        sooner (5.10), but an ordinary exit is not urgent and costs less inside
        the scheduled window.
        """
        month = self.effective_month(now_ms)
        if month is None:
            return []
        previous = self._previous_month(month)
        if self.repo.month(previous) is None:
            return []
        current = set(self.repo.symbols(month))
        return sorted(s for s in self.repo.symbols(previous) if s not in current)

    def result_for(self, month: str) -> UniverseResult | None:
        return self.repo.month(month)

    def effective_month(self, now_ms: int) -> str | None:
        """The month whose stored universe governs ``now_ms``.

        Normally the current calendar month. Between 00:00 and the refresh on the
        1st there is no row for it yet, and the answer is last month's list —
        which is correct: until the selection runs, last month's universe is the
        book we hold.
        """
        month = month_key(now_ms)
        if self.repo.month(month) is not None:
            return month
        return self.repo.latest_month()

    @staticmethod
    def _previous_month(month: str) -> str:
        return month_key(add_months(month_start(month), -1))


def _reason(result: UniverseResult, symbol: str) -> str:
    entry = result.entry(symbol)
    return entry.reason if entry is not None else "not in exchangeInfo"


__all__ = ["UniverseService"]
