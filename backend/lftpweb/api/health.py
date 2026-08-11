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
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        db=db_ok,
        uptime_s=time.monotonic() - request.app.state.started_at,
        repo_url=settings.repo_url,
    )
