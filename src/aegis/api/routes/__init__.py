"""The dashboard's page routers, one module per page of US-T18.

``ROUTERS`` is the mount order used by ``create_app``. Keeping it a plain tuple
means a new page is one module plus one line here, and the app module never
grows a per-page import list.
"""

from fastapi import APIRouter

from aegis.api.routes import (
    attribution,
    backtest,
    controls,
    metrics,
    operations,
    overview,
    positions,
    rebalances,
    signals,
    universe,
)

ROUTERS: tuple[APIRouter, ...] = (
    overview.combined_router,
    overview.router,
    signals.router,
    positions.router,
    rebalances.router,
    attribution.router,
    metrics.router,
    operations.router,
    backtest.router,
    universe.router,
    controls.router,
)

__all__ = ["ROUTERS"]
