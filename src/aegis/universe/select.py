"""Section 5.1 point-in-time universe selection — a pure function (US-T02 AC 1).

The whole point of this module is replayability: the live engine on 2025-03-01 and
a backtest replaying 2022-03-01 must run the *same* arithmetic over the *same*
information set. Two properties make that true, and both are enforced here rather
than left to the caller:

* **No look-ahead.** ``volume_history`` is silently filtered to bars dated strictly
  before the first day of ``month``. A caller that hands us today's ``exchangeInfo``
  and today's full bar history still gets a point-in-time answer for a past month:
  a symbol that only started trading in 2025 has zero bars before 2022-03-01 and so
  can never enter a 2022 universe (US-T02 AC 4). Survivorship works the other way
  and cannot be fixed here — the caller must feed point-in-time ``exchange_info``.
* **No hidden state.** No clock, no DB, no positions (ARCHITECTURE "Purity").

Decisions the PRD leaves open, resolved explicitly because the dashboard and the
backtest both depend on them:

* **Forced symbols displace, they do not extend.** BTCUSDT/ETHUSDT outside the
  volume-ranked top ``size`` are included and the lowest-ranked *non-forced*
  symbols drop out, so the included count stays exactly ``params.size``. Sizing
  divides by a fixed N = 16 (Section 5.5); letting the universe grow to 17 would
  silently lever the book.
* **A forced symbol that fails eligibility is not included.** Delisted, ``SETTLING``
  or short of history means it does not exist to trade; forcing it would produce
  orders the venue rejects. It appears in ``entries`` with ``included=False`` and
  the concrete reason. The force rule exists to survive a *volume* glitch, not a
  tradeability one.
* **Ties break by symbol name ascending**, so two symbols with identical median
  volume always rank in the same order across runs and machines.
* **Fewer than ``size`` eligible symbols means a smaller universe.** We never pad
  with ineligible names.
* **Median of an even-length window is the mean of the two middle values**
  (``numpy.median`` / ``statistics.median`` semantics), which is what the reference
  recomputation of the PRD used.

``entries`` carries *every* considered symbol, winners and losers alike, each with a
human-readable ``reason`` that the dashboard renders verbatim (US-T02 AC 2). The
reason strings are part of this module's contract: keep them stable.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from aegis.core.config import UniverseConfig
from aegis.core.errors import ConfigError
from aegis.core.types import DailyBar, SymbolInfo, UniverseEntry, UniverseResult

#: ``rank`` of an entry that never reached the volume ranking (ineligible symbols).
#: 0 rather than -1 so that the column stays non-negative in ``universe_history``.
UNRANKED = 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An eligible symbol with the two numbers the ranking needs."""

    symbol: str
    median_quote_volume: float
    history_days: int


def _month_start(month: str) -> date:
    """``"2022-03"`` -> ``date(2022, 3, 1)``. The point-in-time cut-off."""
    try:
        year_s, month_s = month.split("-")
        return date(int(year_s), int(month_s), 1)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"universe month must be YYYY-MM, got {month!r}") from exc


def _history_before(bars: Sequence[DailyBar], cutoff: date) -> list[DailyBar]:
    """Bars strictly before ``cutoff``, oldest first, one per calendar day.

    Duplicate days (a REST bar and an archive bar for the same session) would
    otherwise inflate ``history_days`` past the 400-day gate; the last occurrence
    wins, matching how the bar store upserts.
    """
    by_day: dict[date, DailyBar] = {}
    for bar in bars:
        if bar.day < cutoff:
            by_day[bar.day] = bar
    return [by_day[day] for day in sorted(by_day)]


def _ineligibility_reason(info: SymbolInfo, params: UniverseConfig) -> str | None:
    """Step 1 of 5.1, in PRD order. ``None`` means the symbol passes."""
    if info.contract_type != "PERPETUAL":
        return f"excluded: contract type {info.contract_type}"
    if info.quote_asset != params.quote_asset:
        return f"excluded: quote asset {info.quote_asset}"
    if info.status != "TRADING":
        return f"excluded: status {info.status}"
    if info.base_asset in params.exclude_bases:
        return "excluded: stable-pegged base"
    return None


def select_universe(
    exchange_info: dict[str, SymbolInfo],
    volume_history: Mapping[str, Sequence[DailyBar]],
    params: UniverseConfig,
    month: str,
) -> UniverseResult:
    """Select the trading universe for ``month`` (``YYYY-MM``) per Section 5.1.

    Eligibility, then the >= ``min_history_days`` gate, then a descending ranking on
    the median of the last ``volume_window_days`` quote volumes, then the top
    ``size`` with ``force_include`` displacing the weakest non-forced names. Only
    bars dated before the first of ``month`` are consulted.

    Raises ``ConfigError`` if ``month`` is not ``YYYY-MM``. An empty
    ``exchange_info`` yields an empty result, not an error: a universe can be empty.
    """
    cutoff = _month_start(month)
    forced = tuple(params.force_include)

    candidates: list[_Candidate] = []
    rejected: list[UniverseEntry] = []

    for symbol in sorted(exchange_info):
        info = exchange_info[symbol]
        bars = _history_before(volume_history.get(symbol, ()), cutoff)
        history_days = len(bars)

        reason = _ineligibility_reason(info, params)
        if reason is None and history_days < params.min_history_days:
            reason = f"excluded: {history_days} < {params.min_history_days} days history"
        if reason is not None:
            rejected.append(
                UniverseEntry(
                    symbol=symbol,
                    rank=UNRANKED,
                    median_quote_volume_30d=0.0,
                    history_days=history_days,
                    included=False,
                    reason=reason,
                )
            )
            continue

        window = bars[-params.volume_window_days :]
        median = float(statistics.median(b.quote_volume for b in window))
        candidates.append(_Candidate(symbol, median, history_days))

    # A forced symbol absent from exchangeInfo is delisted: say so rather than
    # leaving the dashboard to wonder why BTCUSDT vanished from the table.
    for symbol in forced:
        if symbol not in exchange_info:
            rejected.append(
                UniverseEntry(
                    symbol=symbol,
                    rank=UNRANKED,
                    median_quote_volume_30d=0.0,
                    history_days=len(_history_before(volume_history.get(symbol, ()), cutoff)),
                    included=False,
                    reason="excluded: not listed in exchangeInfo",
                )
            )

    ranked = sorted(candidates, key=lambda c: (-c.median_quote_volume, c.symbol))
    rank_of = {c.symbol: i + 1 for i, c in enumerate(ranked)}

    top = ranked[: params.size]
    forced_below = [c for c in ranked[params.size :] if c.symbol in forced]
    droppable = [c for c in reversed(top) if c.symbol not in forced]
    displaced = {c.symbol for c in droppable[: len(forced_below)]}

    included = [c for c in top if c.symbol not in displaced] + forced_below
    included.sort(key=lambda c: rank_of[c.symbol])
    included_symbols = {c.symbol for c in included}

    entries: list[UniverseEntry] = []
    for candidate in included + [c for c in ranked if c.symbol not in included_symbols]:
        rank = rank_of[candidate.symbol]
        is_in = candidate.symbol in included_symbols
        if is_in and rank > params.size:
            reason = "forced include"
        elif is_in:
            reason = f"top-{params.size} by volume"
        elif candidate.symbol in displaced:
            reason = f"rank {rank} displaced by forced include"
        else:
            reason = f"rank {rank} > {params.size}"
        entries.append(
            UniverseEntry(
                symbol=candidate.symbol,
                rank=rank,
                median_quote_volume_30d=candidate.median_quote_volume,
                history_days=candidate.history_days,
                included=is_in,
                reason=reason,
            )
        )

    entries.extend(sorted(rejected, key=lambda e: e.symbol))
    return UniverseResult(month=month, entries=tuple(entries))


__all__ = ["UNRANKED", "select_universe"]
