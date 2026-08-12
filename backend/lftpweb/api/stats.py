"""GET /api/stats — the header-bar shape (DESIGN.md §9.1). Phase 1 stubbed this at all
zeros precisely so phase 3 could fill it in once the scheduler (`core/queue.py`) has real
numbers to report — current speed and allocated-vs-ceiling come from the transfer queue;
queued count/bytes and 24h transferred come straight from the database.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from lftpweb.core.queue import load_transfer_settings
from lftpweb.models import StatsResponse

router = APIRouter()


@router.get("/api/stats", response_model=StatsResponse)
async def stats(request: Request) -> StatsResponse:
    db = request.app.state.db
    settings = await load_transfer_settings(db)
    queue_stats = request.app.state.queue.stats(settings)

    cursor = await db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(item.remote_size), 0) AS bytes "
        "FROM job JOIN item ON item.id = job.item_id WHERE job.state = 'queued'"
    )
    row = await cursor.fetchone()

    # "24h transferred" (DESIGN.md §9.1) — bytes moved by jobs that finished (successfully)
    # in the last 24h. `bytes_done` at a successful finish equals the file's full size
    # because `cmd:fail-exit true` guarantees exit 0 only on a complete transfer (§4.3); a
    # job that was stopped or failed mid-flight is deliberately excluded, since its partial
    # bytes will be counted again on whatever attempt eventually finishes it.
    cursor = await db.execute(
        "SELECT COALESCE(SUM(bytes_done), 0) AS bytes FROM job "
        "WHERE state = 'succeeded' AND finished_at >= STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')"
    )
    transferred_row = await cursor.fetchone()

    return StatsResponse(
        current_speed_bps=queue_stats["current_speed_bps"],
        allocated_bps=queue_stats["allocated_bps"],
        ceiling_bps=queue_stats["ceiling_bps"],
        queued_count=row["n"],
        queued_bytes=row["bytes"],
        transferred_24h_bytes=transferred_row["bytes"],
    )
