"""Daily bars and funding series — the only data the strategy ever reads (5.2, US-T03).

Two ideas shape this module.

**Signal continuity and P&L truth are different requirements.** A missing daily
bar (exchange downtime, a late kline) breaks an EMA chain: every span in 5.3 is
recursive, so one hole silently shifts three speed pairs for the next hundred
days. Forward-filling repairs that. But a forward-filled bar is an invention —
it never traded — and booking P&L or slippage against it would fabricate money.
So the two uses are separated *structurally* rather than by convention:
``closes()``/``log_returns()`` include filled bars because signals need an
unbroken series, and ``realised_bars()`` excludes them because everything that
touches money must only see prints that happened. P&L code that reaches for a
close therefore has to go through ``realised_bars`` and cannot accidentally get
a synthetic one.

**The bar deadline is a scheduling fact, not an error.** ``ensure_day`` retries
until ``deadline_ms`` (00:04 UTC by default) using ``ctx.clock.sleep`` — so the
backtest and the tests move through the retry loop instantly — and then reports
what is still missing instead of raising. A missing bar defers the rebalance
(Invariant 9: the rebalance is the only scheduled trading event, and skipping it
reduces nothing), it does not stop the engine.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise

from aegis.core.clock import DAY_MS, day_of, day_start_ms
from aegis.core.context import Context
from aegis.core.errors import DataGap, GatewayError
from aegis.core.types import DailyBar, FundingRate

#: Gap between retries inside ``ensure_day``. Small enough that the 00:02 -> 00:04
#: window gives several attempts, large enough not to hammer the venue.
RETRY_SECONDS = 15.0

#: Extra calendar days requested on top of ``min_days`` so that weekends of
#: exchange downtime, or a symbol whose first bars are sparse, still leave
#: ``min_days`` usable rows.
BACKFILL_BUFFER_DAYS = 30

_MIN_SLEEP_S = 0.001


class BarService:
    """Fetches, stores and serves the daily bar and funding series (US-T03)."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.bars = ctx.repos.bars
        self.funding = ctx.repos.funding

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #

    def backfill(self, symbols: Sequence[str], min_days: int | None = None) -> dict[str, int]:
        """Load at least ``min_days`` closed daily bars per symbol (AC 1).

        Defaults to ``cfg.universe.min_history_days`` (400) — the signal warm-up
        of 63 + 250 days plus buffer. ``gateway.daily_bars`` paginates internally,
        so one call per symbol is enough. Symbols that already hold enough rows
        are skipped, which makes a restart cheap and the call idempotent.

        Returns ``{symbol: rows stored}``.
        """
        want = min_days if min_days is not None else self.ctx.cfg.universe.min_history_days
        end = self.last_closed_day()
        start = end - timedelta(days=want + BACKFILL_BUFFER_DAYS)
        out: dict[str, int] = {}
        for symbol in symbols:
            if self.bars.count(symbol) >= want:
                out[symbol] = self.bars.count(symbol)
                continue
            try:
                fetched = self.ctx.gateway.daily_bars(symbol, start=start, end=end)
            except GatewayError:
                out[symbol] = self.bars.count(symbol)
                continue
            if fetched:
                self.bars.upsert_many(replace(b, source="backfill") for b in fetched)
            out[symbol] = self.bars.count(symbol)
        return out

    def fetch_closed_day(self, symbols: Sequence[str], day: date) -> tuple[set[str], set[str]]:
        """One attempt at the closed ``day`` bar for each symbol -> ``(fetched, missing)``.

        A symbol that already holds a *realised* row for the day counts as
        fetched; a forward-filled row does not, because the real print is still
        owed. Gateway failures are reported as missing rather than raised: the
        caller's answer to "no bar" and to "could not ask" is the same (retry,
        then defer).
        """
        fetched: set[str] = set()
        missing: set[str] = set()
        for symbol in symbols:
            existing = self.bars.series(symbol, start=day, end=day)
            if existing and not existing[0].filled:
                fetched.add(symbol)
                continue
            try:
                rows = [b for b in self.ctx.gateway.daily_bars(symbol, start=day, end=day) if b.day == day]
            except GatewayError:
                rows = []
            if rows:
                self.bars.upsert_many(rows)
                fetched.add(symbol)
            else:
                missing.add(symbol)
        return fetched, missing

    def ensure_day(self, symbols: Sequence[str], day: date, deadline_ms: int) -> tuple[set[str], set[str]]:
        """Retry ``fetch_closed_day`` until ``deadline_ms`` -> ``(fetched, missing)`` (AC 1).

        Sleeping goes through ``ctx.clock`` so the loop is instant in tests and in
        the backtester; a clock that has passed the deadline ends the loop after
        the current attempt. A symbol still missing at the deadline is returned in
        ``missing`` and raises ``WARN`` ``BAR_MISSING`` — the caller defers the
        rebalance rather than sizing on a stale close.
        """
        pending = set(symbols)
        fetched: set[str] = set()
        while True:
            got, pending = self.fetch_closed_day(sorted(pending), day)
            fetched |= got
            if not pending:
                break
            now = self.ctx.clock.now_ms()
            if now >= deadline_ms:
                break
            self.ctx.clock.sleep(max(min(RETRY_SECONDS, (deadline_ms - now) / 1000.0), _MIN_SLEEP_S))

        if pending:
            self.ctx.alerts.warn(
                "BAR_MISSING",
                f"{len(pending)} symbol(s) without a closed {day.isoformat()} bar at the deadline: "
                + ", ".join(sorted(pending)),
                {"day": day.isoformat(), "symbols": sorted(pending), "deadline_ms": deadline_ms},
            )
        return fetched, pending

    def forward_fill(self, symbols: Sequence[str], through: date) -> int:
        """Fill calendar gaps with the previous close, flagged ``filled`` (AC 2).

        **Signal continuity only.** The written rows carry ``source="synthetic"``,
        ``filled=True`` and zero volume, and are invisible to ``realised_bars``,
        so no P&L, fee, slippage or liquidity number can ever be computed from
        one. Only interior gaps are filled: nothing is invented before a symbol's
        first bar. Returns the number of rows written.
        """
        written = 0
        for symbol in symbols:
            series = self.bars.series(symbol)
            if not series:
                continue
            by_day = {b.day: b for b in series}
            previous = series[0]
            gaps: list[DailyBar] = []
            cursor = series[0].day + timedelta(days=1)
            while cursor <= through:
                actual = by_day.get(cursor)
                if actual is None:
                    gaps.append(_synthetic(previous, cursor))
                else:
                    previous = actual
                cursor += timedelta(days=1)
            if gaps:
                self.bars.upsert_many(gaps)
                written += len(gaps)
        return written

    # ------------------------------------------------------------------ #
    # Series for the strategy
    # ------------------------------------------------------------------ #

    def closes(self, symbol: str, end: date | None = None, limit: int | None = None) -> list[float]:
        """Close series, ascending, **including forward-filled bars** (signal use)."""
        return self.bars.closes(symbol, end=end, limit=limit)

    def log_returns(self, symbol: str, end: date | None = None, limit: int | None = None) -> list[float]:
        """``ln(P_t / P_{t-1})``, **including forward-filled bars** (5.2, signal use).

        A filled day contributes a zero return, which is exactly what continuity
        means: no information arrived. ``limit`` counts returns, not closes.
        """
        prices = self.closes(symbol, end=end, limit=None if limit is None else limit + 1)
        out: list[float] = []
        for previous, current in pairwise(prices):
            if previous <= 0 or current <= 0:
                raise DataGap(f"{symbol}: non-positive close in the return series")
            out.append(math.log(current / previous))
        return out

    def realised_bars(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[DailyBar]:
        """Bars that actually traded — forward-filled rows are **excluded** (AC 2).

        Every money and liquidity question goes through here: P&L, fees,
        slippage, the execution clip and the universe volume ranking. If a day is
        missing from this series the day genuinely has no print, and the caller
        must treat it as absent rather than carrying a price forward.
        """
        return self.bars.realised_series(symbol, start=start, end=end, limit=limit)

    def volume_history(
        self, symbols: Sequence[str], days: int, before: date | None = None
    ) -> dict[str, list[DailyBar]]:
        """The universe selector's input: the last ``days`` **realised** bars per symbol.

        ``before`` is exclusive (the point-in-time cut-off of 5.1). Synthetic bars
        are left out deliberately: they carry no traded volume and are no
        evidence of history, so including them would both understate a symbol's
        median volume and let a dark month count towards the 400-day gate.
        """
        end = before - timedelta(days=1) if before is not None else None
        return {s: self.realised_bars(s, end=end, limit=days) for s in symbols}

    def avg_daily_quote_volume(self, symbol: str, days: int = 30, before: date | None = None) -> float:
        """Mean realised quote volume over the last ``days`` — the execution clip input (5.9)."""
        end = before - timedelta(days=1) if before is not None else None
        rows = self.realised_bars(symbol, end=end, limit=days)
        if not rows:
            return 0.0
        return sum(b.quote_volume for b in rows) / len(rows)

    # ------------------------------------------------------------------ #
    # Funding (AC 3, AC 4)
    # ------------------------------------------------------------------ #

    def sync_funding(self, symbols: Sequence[str], now_ms: int) -> dict[str, int]:
        """Pull realised funding since the last stored settlement (AC 3).

        Idempotent by construction: the cursor is the stored ``last_time`` and the
        primary key is ``(strategy, symbol, funding_time)``, so a second sync over
        the same window writes nothing new. Returns ``{symbol: new settlements}``.
        """
        lookback_ms = self.ctx.cfg.universe.min_history_days * DAY_MS
        out: dict[str, int] = {}
        for symbol in symbols:
            last = self.funding.last_time(symbol)
            start_ms = last + 1 if last is not None else now_ms - lookback_ms
            try:
                rates = self.ctx.gateway.funding_history(symbol, start_ms=start_ms, end_ms=now_ms)
            except GatewayError:
                out[symbol] = 0
                continue
            fresh = [r for r in rates if last is None or r.funding_time_ms > last]
            if fresh:
                self.funding.upsert_many(fresh)
            out[symbol] = len(fresh)
        return out

    def predicted_funding(self, symbols: Sequence[str]) -> dict[str, float]:
        """Annualised predicted funding per symbol, read at rebalance start (AC 4).

        The annualisation uses the symbol's own ``fundingIntervalHours``: the
        stored ``symbol_meta`` value when there is one, because that is what the
        funding-interval watch (US-T11 AC 3) keeps current, and the gateway's
        otherwise. Rate x 8760 / interval is ``FundingRate.annualised``.
        """
        out: dict[str, float] = {}
        for symbol in symbols:
            try:
                rate = self.ctx.gateway.predicted_funding(symbol)
            except GatewayError:
                out[symbol] = 0.0
                continue
            meta = self.ctx.repos.symbol_meta.get(symbol)
            if meta is not None and meta.funding_interval_hours != rate.interval_hours:
                rate = replace(rate, interval_hours=meta.funding_interval_hours)
            out[symbol] = rate.annualised()
        return out

    def stored_funding(
        self, symbol: str, start_ms: int | None = None, end_ms: int | None = None
    ) -> list[FundingRate]:
        """Realised settlements as stored — the attribution and backtest input."""
        return self.funding.history(symbol, start_ms=start_ms, end_ms=end_ms)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def last_closed_day(self, now_ms: int | None = None) -> date:
        """The most recent day whose 00:00 UTC kline has closed — yesterday, UTC."""
        now = self.ctx.clock.now_ms() if now_ms is None else now_ms
        return day_of(now) - timedelta(days=1)


def _synthetic(previous: DailyBar, day: date) -> DailyBar:
    """A flat, zero-volume bar carrying ``previous``'s close. Never a P&L input."""
    open_ms = day_start_ms(day)
    return DailyBar(
        symbol=previous.symbol,
        day=day,
        open=previous.close,
        high=previous.close,
        low=previous.close,
        close=previous.close,
        volume=0.0,
        quote_volume=0.0,
        open_time_ms=open_ms,
        close_time_ms=open_ms + DAY_MS - 1,
        source="synthetic",
        filled=True,
    )


__all__ = ["BACKFILL_BUFFER_DAYS", "RETRY_SECONDS", "BarService"]
