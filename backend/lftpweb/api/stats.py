"""GET /api/stats — the header-bar shape (DESIGN.md §9.1). Returns zeros until the
scheduler (phase 3) has real numbers; wiring the shape now means phase 3 fills values in
rather than reshaping the UI.
"""

from __future__ import annotations

from fastapi import APIRouter

from lftpweb.models import StatsResponse

router = APIRouter()


@router.get("/api/stats", response_model=StatsResponse)
async def stats() -> StatsResponse:
    return StatsResponse(
        current_speed_bps=0,
        allocated_bps=0,
        ceiling_bps=0,
        queued_count=0,
        queued_bytes=0,
        transferred_24h_bytes=0,
    )
