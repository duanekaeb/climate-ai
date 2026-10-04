"""FastAPI application: ``uvicorn climate.api.app:app``.

REST under /api (OpenAPI docs at /api/docs), a websocket at /api/ws, and the built web app
(``CLIMATE_WEB_DIR``, default /app/web) served at / with history fallback, so the owner's
nginx needs a single upstream.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from climate import __version__
from climate.api.routers import (
    agent,
    analytics,
    auth,
    control,
    experiments,
    reports,
    setup,
    status,
    ws,
)
from climate.api.schemas import Health
from climate.config import get_settings
from climate.store.db import session_scope

log = logging.getLogger("climate.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level)
    logging.getLogger("pyecobee").setLevel(logging.INFO)  # it logs tokens at DEBUG
    if os.environ.get("CLIMATE_MIGRATE_ON_START", "1") == "1":
        from climate.house import seed
        from climate.store.migrate import migrate

        migrate()
        with session_scope() as s:
            seed(s)
    await ws.hub.start()
    try:
        yield
    finally:
        await ws.hub.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Climate AI",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    @app.get("/api/health", response_model=Health, tags=["health"])
    def health() -> Health:  # public: used by docker healthchecks and the iOS wrapper
        db_ok = True
        try:
            with session_scope() as s:
                s.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_ok = False
        return Health(ok=db_ok, version=__version__, db=db_ok)

    for module in (auth, status, analytics, control, experiments, reports, agent, setup, ws):
        app.include_router(module.router, prefix="/api")

    @app.exception_handler(PermissionError)
    async def _perm(_: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    _mount_web(app)
    return app


def _mount_web(app: FastAPI) -> None:
    web_dir = Path(os.environ.get("CLIMATE_WEB_DIR", "/app/web"))
    index = web_dir / "index.html"
    if not index.exists():
        log.info("web build not found at %s; serving the API only", web_dir)
        return
    if (web_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (web_dir / path).resolve()
        if path and candidate.is_file() and web_dir.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()
