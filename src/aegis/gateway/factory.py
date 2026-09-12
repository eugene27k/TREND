"""Mode -> gateway. One place decides which venue the engine talks to.

Keeping this decision in a single function is what makes ``mode`` meaningful:
there is no other construction site where a live client could be built by
accident, and ``paper`` provably cannot send an order because the Binance
client it wraps is constructed read-only.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from aegis.core.clock import Clock
from aegis.core.config import AppConfig
from aegis.core.errors import ConfigError
from aegis.core.types import Mode
from aegis.gateway.binance import BinanceGateway

if TYPE_CHECKING:
    from aegis.gateway.base import ExchangeGateway


def build_gateway(cfg: AppConfig, clock: Clock, inner: ExchangeGateway | None = None) -> ExchangeGateway:
    """Return the gateway for ``cfg.mode``.

    ``inner`` overrides the market-data source that ``paper`` mode wraps; it is
    how a test (or a replay) feeds the paper simulator without a network.
    """
    mode = cfg.mode
    if mode is Mode.BACKTEST:
        raise ConfigError(
            "backtest mode has no gateway: the simulator owns its own data path "
            "(aegis.backtest_trend), it does not talk to a venue"
        )
    if mode is Mode.PAPER:
        data = inner if inner is not None else BinanceGateway(cfg, clock, read_only=True)
        return _build_paper(cfg, clock, data)
    if mode in (Mode.DEMO, Mode.LIVE):
        return BinanceGateway(cfg, clock)
    raise ConfigError(f"unsupported mode {mode!r}")


def _build_paper(cfg: AppConfig, clock: Clock, data: ExchangeGateway) -> ExchangeGateway:
    """Construct ``PaperGateway``, imported lazily so its module is optional here."""
    from aegis.gateway.paper import PaperGateway

    candidates: dict[str, Any] = {
        "cfg": cfg,
        "config": cfg,
        "clock": clock,
        "inner": data,
        "data": data,
        "source": data,
        "upstream": data,
        "market": data,
        "gateway": data,
    }
    try:
        params = inspect.signature(PaperGateway).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return PaperGateway(data, clock, cfg)
    kwargs = {name: value for name, value in candidates.items() if name in params}
    return PaperGateway(**kwargs)


__all__ = ["build_gateway"]
