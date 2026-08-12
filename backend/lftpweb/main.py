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
from lftpweb.api import auth as auth_api
from lftpweb.api import backup as backup_api
from lftpweb.api import files, health, history, jobs, logs, settings as settings_api, stats, ws
from lftpweb.config import settings
from lftpweb.core import auth
from lftpweb.core.autoqueue import AutoQueue
from lftpweb.core.backup import BackupScheduler
from lftpweb.core.engine import Engine, load_host_config
from lftpweb.core.events import EventBus
from lftpweb.core.postprocess import PostprocessPipeline
from lftpweb.core.queue import TransferQueue
from lftpweb.db import connect, migrate
from lftpweb.logsetup import setup_logging
from lftpweb.middleware import AuthMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.config_dir, settings.log_level)
    app.state.started_at = time.monotonic()
    app.state.config_dir = settings.config_dir
    app.state.db = await connect(settings.config_dir)
    # config_dir wires in the pre-migration backup (DESIGN.md §10.2, core/backup.py) --
    # unconditional whenever a migration is actually about to run, not just when Settings →
    # Backup's own schedule happens to be enabled.
    await migrate(app.state.db, settings.config_dir)

    app.state.events = EventBus()
    # Phase 8 (DESIGN.md §8): in-memory only, per docs/decisions.md -- single-process (§2),
    # so no cross-instance coordination is needed, and clearing on restart is an accepted
    # trade for not persisting a table of failed-login timestamps forever.
    app.state.login_rate_limiter = auth.LoginRateLimiter()

    async def _host_provider():
        return await load_host_config(app.state.db, settings.config_dir)

    # TransferQueue is constructed before Engine, not after as in phases 1-3, because
    # AutoQueue (phase 4, DESIGN.md §4.7) needs `TransferQueue.enqueue_item` -- the same
    # "manual queue always wins, clears suppression" path a user action takes -- and Engine
    # is the one that invokes AutoQueue.on_scan() at the end of every scan pass.
    app.state.queue = TransferQueue(
        db=app.state.db,
        config_dir=settings.config_dir,
        events=app.state.events,
        run_dir=settings.run_dir,
        tick_s=settings.transfer_tick_s,
        host_provider=_host_provider,
    )
    autoqueue = AutoQueue(db=app.state.db, enqueue_item=app.state.queue.enqueue_item)
    app.state.engine = Engine(
        db=app.state.db,
        config_dir=settings.config_dir,
        events=app.state.events,
        scan_interval_s=settings.scan_interval_s,
        autoqueue=autoqueue,
    )
    # Phase 5 (DESIGN.md §6/§7.4): constructed after Engine, not TransferQueue, because it
    # needs Engine.pool — the one pooled asyncssh connection scanning, Test connection, and
    # now `move`-mode remote deletes all share (DESIGN.md §5: "exactly one code path"). Handed
    # to TransferQueue as a plain attribute rather than a constructor argument for exactly
    # this reason — see core/queue.py's own comment on `self.postprocess`.
    app.state.postprocess = PostprocessPipeline(
        db=app.state.db,
        events=app.state.events,
        remote_pool=app.state.engine.pool,
        host_provider=_host_provider,
    )
    app.state.queue.postprocess = app.state.postprocess

    # Phase 7 (DESIGN.md §10.2): the scheduled backup loop, independent of the pre-migration
    # backup above (which fires from db.py.migrate() regardless of this loop ever starting).
    app.state.backup_scheduler = BackupScheduler(db=app.state.db, config_dir=settings.config_dir)

    await app.state.engine.start()
    await app.state.queue.start()
    await app.state.backup_scheduler.start()

    logger.info("lftpweb %s started", __version__)
    try:
        yield
    finally:
        await app.state.backup_scheduler.stop()
        await app.state.queue.stop()
        # Let any in-flight postprocessing task (verify/extract/move, all still writing to
        # app.state.db) finish before the connection underneath it closes, rather than
        # cutting one off mid-write on shutdown.
        await app.state.postprocess.wait_idle()
        await app.state.engine.stop()
        await app.state.db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="lftpweb", version=__version__, lifespan=lifespan)
    # Phase 8 (DESIGN.md §8): one gate in front of everything under /api/ except the small
    # public allowlist (middleware.py.PUBLIC_API_PATHS) -- see that module's docstring for
    # why a single ASGI middleware was chosen over a per-router Depends(). Added before any
    # router so a newly mounted router is covered without a second decision at the call site.
    app.add_middleware(AuthMiddleware)

    app.include_router(health.router)
    app.include_router(stats.router)
    app.include_router(settings_api.router)
    app.include_router(files.router)
    app.include_router(jobs.router)
    app.include_router(history.router)
    app.include_router(logs.router)
    app.include_router(backup_api.router)
    app.include_router(auth_api.router)
    app.include_router(auth_api.settings_router)
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
