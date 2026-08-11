"""FastAPI app factory. Lifespan opens the DB and runs migrations; the static mount serves
the built SPA with an SPA fallback so client-side routes (e.g. /settings/logs) deep-link
correctly instead of 404ing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from lftpweb import __version__
from lftpweb.api import health, stats
from lftpweb.config import settings
from lftpweb.db import connect, migrate
from lftpweb.logsetup import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.config_dir, settings.log_level)
    app.state.started_at = time.monotonic()
    app.state.db = await connect(settings.config_dir)
    await migrate(app.state.db)
    logger.info("lftpweb %s started", __version__)
    try:
        yield
    finally:
        await app.state.db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="lftpweb", version=__version__, lifespan=lifespan)

    app.include_router(health.router)
    app.include_router(stats.router)

    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            candidate = static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
