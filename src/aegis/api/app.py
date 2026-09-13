"""The FastAPI application behind the dashboard (US-T18).

``create_app`` resolves one sleeve per configured strategy and mounts the page
routers under ``/api/{strategy}/...``. Three endpoints sit outside that prefix:
``/api/health`` (process liveness, answerable with no database at all),
``/api/strategies`` (what this host actually has — the strategy selector of
US-T18 AC 7 is built from it) and ``/api/overview`` (the combined "all sleeves"
view).

Nothing here holds a writable connection open; see ``aegis.api.deps``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aegis.api.deps import Registry, get_registry
from aegis.api.routes import ROUTERS
from aegis.core.clock import Clock


class HealthResponse(BaseModel):
    ok: bool
    now_ms: int
    started_ms: int
    uptime_s: float
    strategies: list[str]


class StrategyInfo(BaseModel):
    strategy: str
    mode: str
    phase: str
    db_path: str


class StrategiesResponse(BaseModel):
    strategies: list[StrategyInfo]


def create_app(config_paths: Mapping[str, str | Path] | None = None, clock: Clock | None = None) -> FastAPI:
    """Build the API over the sleeves named in ``config_paths``.

    ``config_paths`` maps a strategy name to its YAML file; the default is the
    repository's ``config/`` layout. A sleeve whose config or database is
    missing is skipped rather than raised — the dashboard must come up on a
    host running only one of the two containers.
    """
    registry = Registry.build(config_paths, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.registry.close()

    app = FastAPI(
        title="Aegis dashboard API",
        version="1.0.0",
        summary="Read-only views over the CARRY and TREND engine databases.",
        lifespan=lifespan,
    )
    app.state.registry = registry

    @app.get("/api/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        reg = app.state.registry
        now = reg.now_ms()
        return HealthResponse(
            ok=True,
            now_ms=now,
            started_ms=reg.started_ms,
            uptime_s=(now - reg.started_ms) / 1000.0,
            strategies=[str(s) for s in reg.strategies],
        )

    @app.get("/api/strategies", response_model=StrategiesResponse, tags=["ops"])
    def strategies() -> StrategiesResponse:
        reg = app.state.registry
        return StrategiesResponse(
            strategies=[
                StrategyInfo(
                    strategy=str(s),
                    mode=str(reg.get(str(s)).cfg.mode),
                    phase=str(reg.get(str(s)).cfg.phase.current),
                    db_path=str(reg.get(str(s)).db_path),
                )
                for s in reg.strategies
            ]
        )

    for router in ROUTERS:
        app.include_router(router)

    _mount_dashboard(app)
    return app


#: Where the built dashboard lands. In the container the Dockerfile copies it
#: here; in development ``npm run dev`` serves it instead and this is absent.
DASHBOARD_DIR = Path(os.environ.get("AEGIS_DASHBOARD_DIR", "/app/ui"))


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built single-page app, when there is one.

    Without this the React app only exists under ``vite dev``: the container runs
    uvicorn alone, so every page of US-T18 would 404 on the deployed host while
    the API answered perfectly.

    Mounted last, so it can never shadow an ``/api`` route, and silently skipped
    when the build is absent — the API must still come up on a host that only
    runs the engine.
    """
    index = DASHBOARD_DIR / "index.html"
    if not index.is_file():
        return

    assets = DASHBOARD_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    def dashboard(path: str = "") -> FileResponse:
        """Any non-API path returns index.html — the router runs in the browser."""
        candidate = (DASHBOARD_DIR / path).resolve() if path else index
        if path and candidate.is_file() and DASHBOARD_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


__all__ = ["HealthResponse", "StrategiesResponse", "StrategyInfo", "create_app", "get_registry"]
