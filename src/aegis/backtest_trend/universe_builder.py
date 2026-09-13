"""Point-in-time universe reconstruction (PRD 11.1, US-T16 AC 1, US-T02 AC 4).

The live selector is reused verbatim — ``select_universe`` is the same pure
function the engine calls at 00:05 on the 1st of the month. All this module does
is assemble its *inputs* as they existed before each month began, from the
archive's month files, so that a symbol delisted in 2023 is still in the March
2022 universe and a symbol listed in 2025 is in none of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aegis.core.clock import add_months, month_key, month_start
from aegis.core.config import UniverseConfig
from aegis.core.types import DailyBar, SymbolInfo, UniverseResult
from aegis.universe.select import select_universe


def months_between(start_month: str, end_month: str) -> list[str]:
    out, cursor = [], month_start(start_month)
    end = month_start(end_month)
    while cursor <= end:
        out.append(month_key(cursor))
        cursor = add_months(cursor, 1)
    return out


def synthesise_symbol_info(
    symbol: str,
    months: Sequence[str],
    as_of_month: str,
    *,
    quote_asset: str = "USDT",
    tick_size: float = 0.0001,
    step_size: float = 0.001,
    min_notional: float = 5.0,
) -> SymbolInfo:
    """What ``exchangeInfo`` would have said about this symbol at ``as_of_month``.

    The archive has no historical ``exchangeInfo``, so status is derived from the
    file inventory: a symbol with a kline file for the month before ``as_of_month``
    was trading then; one whose files stop earlier had been delisted. Using
    today's ``exchangeInfo`` instead is precisely the survivorship bias the
    point-in-time build exists to avoid (US-T02 AC 4).
    """
    previous = month_key(add_months(month_start(as_of_month), -1))
    status = "TRADING" if previous in set(months) else "DELISTED"
    base = symbol[: -len(quote_asset)] if symbol.endswith(quote_asset) else symbol
    return SymbolInfo(
        symbol=symbol,
        base_asset=base,
        quote_asset=quote_asset,
        status=status,
        contract_type="PERPETUAL",
        tick_size=tick_size,
        step_size=step_size,
        min_qty=step_size,
        min_notional=min_notional,
        price_precision=6,
        quantity_precision=3,
    )


class PointInTimeUniverse:
    """Builds the monthly universes a backtest run needs."""

    def __init__(
        self,
        inventory: Mapping[str, Sequence[str]],
        bars: Mapping[str, Sequence[DailyBar]],
        params: UniverseConfig,
    ) -> None:
        self.inventory = {s: list(m) for s, m in inventory.items()}
        self.bars = {s: sorted(b, key=lambda x: x.day) for s, b in bars.items()}
        self.params = params

    def build(self, months: Sequence[str]) -> dict[str, UniverseResult]:
        out: dict[str, UniverseResult] = {}
        for month in months:
            cutoff = month_start(month)
            exchange_info: dict[str, SymbolInfo] = {}
            volume_history: dict[str, list[DailyBar]] = {}
            for symbol, symbol_months in self.inventory.items():
                info = synthesise_symbol_info(
                    symbol, symbol_months, month, quote_asset=self.params.quote_asset
                )
                exchange_info[symbol] = info
                # select_universe filters to bars before the month itself, but
                # slicing here keeps the per-month input small on 5 years of data.
                volume_history[symbol] = [b for b in self.bars.get(symbol, ()) if b.day < cutoff]
            out[month] = select_universe(exchange_info, volume_history, self.params, month)
        return out


__all__ = ["PointInTimeUniverse", "months_between", "synthesise_symbol_info"]
