"""GET /api/health — also the container HEALTHCHECK target (DESIGN.md §10.3)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from lftpweb import __version__
from lftpweb.config import settings
from lftpweb.db import is_healthy
from lftpweb.models import HealthResponse

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    db_ok = await is_healthy(request.app.state.db)

    # DESIGN.md §10.3: host reachability. `None` (not yet reported) when no host is
    # configured -- distinct from `False`, "a host is configured but the pooled connection is
    # currently down." Read from Engine's already-pooled connection (core/remote.py's
    # RemoteConnectionPool.is_connected) rather than opening a fresh SSH session on every poll
    # of this endpoint, which the UI hits continuously.
    host_reachable: bool | None = None
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        cursor = await request.app.state.db.execute("SELECT 1 FROM host LIMIT 1")
        if await cursor.fetchone() is not None:
            host_reachable = engine.pool.is_connected

    # DESIGN.md §10.3: "whether the scheduler loop is alive" -- core/queue.py.TransferQueue's
    # admission-control loop (§4.5).
    queue = getattr(request.app.state, "queue", None)
    scheduler_alive = bool(queue is not None and queue.is_alive)

    status = "ok" if db_ok and scheduler_alive and host_reachable is not False else "degraded"

    return HealthResponse(
        status=status,
        version=__version__,
        db=db_ok,
        uptime_s=time.monotonic() - request.app.state.started_at,
        repo_url=settings.repo_url,
        host_reachable=host_reachable,
        scheduler_alive=scheduler_alive,
    )
