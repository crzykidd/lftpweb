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
from lftpweb.api import files, health, jobs, settings as settings_api, stats, ws
from lftpweb.config import settings
from lftpweb.core.engine import Engine, load_host_config
from lftpweb.core.events import EventBus
from lftpweb.core.queue import TransferQueue
from lftpweb.db import connect, migrate
from lftpweb.logsetup import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.config_dir, settings.log_level)
    app.state.started_at = time.monotonic()
    app.state.config_dir = settings.config_dir
    app.state.db = await connect(settings.config_dir)
    await migrate(app.state.db)

    app.state.events = EventBus()
    app.state.engine = Engine(
        db=app.state.db,
        config_dir=settings.config_dir,
        events=app.state.events,
        scan_interval_s=settings.scan_interval_s,
    )

    async def _host_provider():
        return await load_host_config(app.state.db, settings.config_dir)

    app.state.queue = TransferQueue(
        db=app.state.db,
        config_dir=settings.config_dir,
        events=app.state.events,
        run_dir=settings.run_dir,
        tick_s=settings.transfer_tick_s,
        host_provider=_host_provider,
    )
    await app.state.engine.start()
    await app.state.queue.start()

    logger.info("lftpweb %s started", __version__)
    try:
        yield
    finally:
        await app.state.queue.stop()
        await app.state.engine.stop()
        await app.state.db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="lftpweb", version=__version__, lifespan=lifespan)

    app.include_router(health.router)
    app.include_router(stats.router)
    app.include_router(settings_api.router)
    app.include_router(files.router)
    app.include_router(jobs.router)
    app.include_router(ws.router)

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
