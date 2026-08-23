"""FastAPI app factory. Lifespan opens the DB and runs migrations; the static mount serves
the built SPA with an SPA fallback so client-side routes (e.g. /settings/logs) deep-link
correctly instead of 404ing.
"""

from __future__ import annotations

import logging
import os.path
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
from lftpweb.api import browse, disk_review, files, health, history, jobs, logs, stats, ws
from lftpweb.api import metrics as metrics_api
from lftpweb.api import (
    settings_arr,
    settings_clients,
    settings_host,
    settings_postprocess,
    settings_queues,
)
from lftpweb.api import support_bundle
from lftpweb.config import settings
from lftpweb.core import auth
from lftpweb.core.arrsync import ArrSyncScheduler
from lftpweb.core.autoqueue import AutoQueue
from lftpweb.core.backup import BackupScheduler
from lftpweb.core.clientsync import ClientSyncScheduler
from lftpweb.core.local_delete import DeleteInFlight, RetentionScheduler
from lftpweb.core.metrics import MetricsRetentionScheduler
from lftpweb.core.engine import Engine, load_host_config
from lftpweb.core.events import EventBus
from lftpweb.core.postprocess import PostprocessPipeline
from lftpweb.core.queue import TransferQueue
from lftpweb.db import connect, migrate
from lftpweb.logsetup import setup_logging
from lftpweb.middleware import AuthMiddleware, SecurityHeadersMiddleware

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
        config_dir=settings.config_dir,
    )
    app.state.queue.postprocess = app.state.postprocess
    # Engine needs it too, for one read: which items a verify/extract worker is running for
    # right now, so a scan pass doesn't overwrite VERIFYING/EXTRACTING mid-run
    # (Engine._protected_rel_paths). Same plain-attribute wiring, same reason as above.
    app.state.engine.postprocess = app.state.postprocess

    # 2026-08-13 (`prompts/2026-08-13-delete-state-truthfulness.md`): the live, in-memory
    # record of which items a `delete_local()` call is currently removing from disk -- the
    # identical "one instance, shared by every writer and reader, plain-attribute wiring"
    # shape `postprocess` above uses, for the identical reason (`DeleteInFlight`'s own
    # docstring). `Engine` needs it for the same read `postprocess.in_flight_item_ids()`
    # already gets: `_protected_rel_paths` shielding a row's state from a racing scan while
    # its files are still disappearing.
    app.state.delete_in_flight = DeleteInFlight()
    app.state.engine.delete_in_flight = app.state.delete_in_flight

    # Phase 7 (DESIGN.md §10.2): the scheduled backup loop, independent of the pre-migration
    # backup above (which fires from db.py.migrate() regardless of this loop ever starting).
    app.state.backup_scheduler = BackupScheduler(db=app.state.db, config_dir=settings.config_dir)
    # Throughput metrics (this task, DESIGN.md — new section proposed): retention pruning is
    # its own loop, same shape as the backup scheduler above -- independent of
    # `TransferQueue.metrics` (core/metrics.py.ThroughputSampler), which is constructed
    # inside `TransferQueue.__init__` and ticks from `TransferQueue.tick()` itself, not from
    # here.
    app.state.metrics_retention = MetricsRetentionScheduler(db=app.state.db)
    # Local-file retention (prompts/open-issues.md "7 + 8", DESIGN.md §7.1/§7.3/§7.4's bar for
    # an irreversible-delete feature): same background-loop shape as the schedulers above,
    # default off (core/local_delete.py.RetentionSettings). `in_flight_provider` is a thin
    # closure over `app.state.postprocess`, constructed above, rather than a direct reference,
    # for the same "can't hand over an instance that doesn't exist yet at construction time"
    # reason `Engine.postprocess` is wired as a plain attribute instead of a constructor arg.
    app.state.retention_scheduler = RetentionScheduler(
        db=app.state.db,
        events=app.state.events,
        in_flight_provider=lambda: app.state.postprocess.in_flight_item_ids(),
        delete_in_flight=app.state.delete_in_flight,
    )
    # Sonarr/Radarr integration, phase A (docs/arr-integration-spec.md "The poller"): its own
    # clock, independent of the scan pass -- same background-loop shape as the schedulers
    # above, default off (every `arr_instance` row starts `enabled = 0`, migration 018 inserts
    # none). `events` wired the same plain-attribute-not-constructor-arg way `postprocess`
    # is above, so an `item_delta` published mid-poll reaches connected browsers.
    # Phase B (docs/arr-integration-spec.md "Cleanup") adds `in_flight_provider`/
    # `delete_in_flight` -- the identical seam `RetentionScheduler` above takes, so cleanup's
    # own filesystem work is shielded from (and shields) a racing scan the same way every other
    # deleter in this codebase already is. Both `app.state.postprocess` and
    # `app.state.delete_in_flight` already exist by this point (constructed above).
    # Rung 4 of the move-mode delete ladder (prompts/done/2026-08-16-move-delete-gate-ladder.md):
    # `remote_pool`/`host_provider` are the identical seam `app.state.postprocess` above already
    # gets, for the identical reason -- `core/arrsync.py`'s deferred delete on a confirmed
    # `imported` transition needs the same pooled asyncssh connection and host config.
    app.state.arr_sync = ArrSyncScheduler(
        db=app.state.db,
        config_dir=settings.config_dir,
        events=app.state.events,
        in_flight_provider=lambda: app.state.postprocess.in_flight_item_ids(),
        delete_in_flight=app.state.delete_in_flight,
        remote_pool=app.state.engine.pool,
        host_provider=_host_provider,
    )
    # Download-client poller, stage 2a of #18 (docs/download-client-framework-spec.md §9,
    # `core/clientsync.py`, this task): its own clock, independent of both the scan pass and
    # the *arr poller above -- same background-loop shape, default off (every `download_client`
    # row starts `enabled = 0`, migration 027 inserts none). No `events`/`in_flight_provider`/
    # `delete_in_flight` seam, unlike `arr_sync` -- this scheduler never writes `item.state` or
    # touches the filesystem (this module's own docstring), so it has nothing those seams exist
    # to protect.
    app.state.client_sync = ClientSyncScheduler(db=app.state.db, config_dir=settings.config_dir)
    # Stage 2b of #18 (docs/download-client-framework-spec.md §14, prompts/2026-08-23-settle-
    # gate-skip.md): AutoQueue needs the poller's completed-transfer cache for the settle-gate
    # skip, but it's constructed above, before `ClientSyncScheduler` exists -- same
    # plain-attribute wiring `app.state.engine.postprocess`/`delete_in_flight` use for the
    # identical "can't hand over an instance that doesn't exist yet at construction time"
    # reason. Off (`settle.SettleSettings.client_skip_enabled` defaults `False`) until a user
    # opts in, so this wiring alone changes nothing for an existing install.
    app.state.engine.autoqueue.client_sync = app.state.client_sync

    await app.state.engine.start()
    await app.state.queue.start()
    await app.state.backup_scheduler.start()
    await app.state.metrics_retention.start()
    await app.state.retention_scheduler.start()
    await app.state.arr_sync.start()
    await app.state.client_sync.start()

    logger.info("lftpweb %s started", __version__)
    try:
        yield
    finally:
        await app.state.client_sync.stop()
        await app.state.arr_sync.stop()
        await app.state.retention_scheduler.stop()
        await app.state.metrics_retention.stop()
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
    # Added after AuthMiddleware so it wraps it (Starlette applies middleware outermost-last):
    # every response, including a 401/403 the auth gate itself produces and every static/SPA
    # response, carries the headers. See middleware.py for why this is the safe header subset
    # and why no CSP/HSTS is set here (audit S4).
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(health.router)
    app.include_router(stats.router)
    # Settings routers, split out of the former monolithic api/settings.py (audit P2). All three
    # share the /api/settings prefix; order is cosmetic.
    app.include_router(settings_host.router)
    app.include_router(settings_queues.router)
    app.include_router(settings_postprocess.router)
    app.include_router(settings_arr.router)
    app.include_router(settings_clients.router)
    app.include_router(browse.router)
    app.include_router(files.router)
    app.include_router(disk_review.router)
    app.include_router(jobs.router)
    app.include_router(history.router)
    app.include_router(logs.router)
    app.include_router(support_bundle.router)
    app.include_router(backup_api.router)
    app.include_router(auth_api.router)
    app.include_router(auth_api.settings_router)
    app.include_router(metrics_api.router)
    app.include_router(metrics_api.settings_router)
    app.include_router(ws.router)

    static_dir = Path(settings.static_dir)
    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

        static_root = os.path.realpath(str(static_dir))
        index_html = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            # `full_path` is request-controlled and reaches this handler percent-decoded but
            # *not* `..`-normalized -- a client can send `..%2f..%2fetc/passwd` and uvicorn
            # passes it straight through. This route is also outside the /api/ auth gate
            # (middleware.py only gates /api/), so serving `static_dir / full_path` blindly is
            # an unauthenticated arbitrary file read. Guard it with the exact form CodeQL's own
            # `py/path-injection` remediation recommends and recognises as a barrier: resolve
            # the joined path (`realpath` follows `..` *and* symlinks) and confirm it is the
            # static root or sits beneath it via a `startswith(root + sep)` check, falling back
            # to the SPA shell on any escape rather than 403ing (a normal deep link that happens
            # not to exist on disk must still render the SPA). See docs/audit-v0.1.0.md finding
            # S1; the earlier `pathlib.relative_to` form was equivalent but not modelled by
            # CodeQL, so it flagged its own fix.
            requested = os.path.realpath(os.path.join(static_root, full_path))
            # Containment via `os.path.commonpath`, the barrier CodeQL's own py/path-injection
            # remediation recognises, evaluated in a *positive* branch that wraps the file use:
            # `commonpath([root, requested]) == root` is true exactly when `requested` is the
            # static root or a descendant of it (both are absolute `realpath` results, so
            # `commonpath` can't raise on mismatched/relative kinds), so `requested` reaches
            # `isfile`/`FileResponse` only where it is provably contained. Anything else --
            # a `..%2f…` escape, a symlink out of the tree -- falls through to the SPA shell.
            # See docs/audit-v0.1.0.md finding S1.
            if os.path.commonpath([static_root, requested]) == static_root:
                if full_path and os.path.isfile(requested):
                    return FileResponse(requested)
            return FileResponse(index_html)

    return app


app = create_app()
