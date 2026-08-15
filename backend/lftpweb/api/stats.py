"""GET /api/stats — the header-bar shape (DESIGN.md §9.1). Phase 1 stubbed this at all
zeros precisely so phase 3 could fill it in once the scheduler (`core/queue.py`) has real
numbers to report — current speed and allocated-vs-ceiling come from the transfer queue;
queued count/bytes come straight from `job`/`item`; 24h transferred comes from the same
`metric_sample` throughput store (`core/metrics.py`) the Dashboard's bytes-per-hour chart
reads (2026-08-13, prompts/2026-08-13-header-24h-from-metrics.md, docs/decisions.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request

from lftpweb.core import metrics
from lftpweb.core.queue import load_transfer_settings
from lftpweb.models import StatsResponse

router = APIRouter()

# Same bucket width `api/metrics.py`'s `_RANGES["24h"]` uses for the Dashboard's own 24h
# range -- not load-bearing for the *total* (queue_breakdown's WHERE, not its GROUP BY, is
# what bounds the sum), but keeping it identical means this call reads as the exact same
# query shape the Dashboard makes, not a coincidentally-equal one.
_BUCKET_SECONDS_24H = 3600


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

    # "24h transferred" (DESIGN.md §9.1) — bytes actually moved over the wire in the last 24h,
    # per `metric_sample` (migration 005), not bytes of jobs that happened to finish in that
    # window. Usage is the point of this figure (a quick glance), so it counts every byte a
    # transfer moved even if that attempt later failed or was stopped -- and, unlike a sum over
    # `job`, it can't be zeroed by clearing History (`api/history.py` deliberately never
    # touches `metric_sample`; docs/decisions.md).
    #
    # Reuses `metrics.queue_breakdown` -- the exact function `api/metrics.py.get_throughput`
    # calls for the Dashboard's own 24h chart -- rather than a second query over the same
    # table, so the header and the Dashboard are structurally incapable of disagreeing for the
    # same window. `queue_id=None` is the "site total" shape, driven by
    # `idx_metric_sample_ts_queue` (ts, queue_id, bytes_delta) -- an index-only scan, verified
    # with `EXPLAIN QUERY PLAN` when that index was added (migrations/005_throughput_metrics.sql,
    # docs/decisions.md).
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    breakdown = await metrics.queue_breakdown(
        db,
        start_ts=start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        end_ts=end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        bucket_seconds=_BUCKET_SECONDS_24H,
    )
    transferred_24h_bytes = sum(bytes_ for _, _, bytes_ in breakdown)

    return StatsResponse(
        current_speed_bps=queue_stats["current_speed_bps"],
        allocated_bps=queue_stats["allocated_bps"],
        ceiling_bps=queue_stats["ceiling_bps"],
        queued_count=row["n"],
        queued_bytes=row["bytes"],
        transferred_24h_bytes=transferred_24h_bytes,
    )
