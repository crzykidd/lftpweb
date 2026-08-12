"""GET /api/history/jobs, GET /api/history/events -- the History page (DESIGN.md §9.2, phase
6). This is where a completed transfer's own job record lives (phase 3b's `list_jobs()`
deliberately excludes `succeeded` jobs from the Transfers page -- see docs/decisions.md), and
where the phase 5 delete audit trail (`core/audit.py`'s `event` rows) becomes visible.

**Row cap, not "return everything."** A busy install accumulates thousands of `job`/`event`
rows. Both endpoints are `LIMIT`/`OFFSET` paginated, capped at `MAX_LIMIT` regardless of what
a caller asks for, and report `total` (the filtered count, ignoring the page) so the UI can
offer "load more" without a second unbounded query.

**`output_tail` is never in the list payload.** Phase 3a stores up to ~4KB per failed job
specifically so the UI can show *why*, not a red dot -- but shipping that inline on every row
of an unbounded history list defeats the row cap. `HistoryJobOut.has_output_tail` says whether
there's anything to fetch; `GET /api/history/jobs/{id}/output` fetches it, on demand, exactly
as the phase 6 prompt asks for.

**Grouping by queue is a display concern, done by the frontend** over this endpoint's flat,
already-filtered/paginated page -- see docs/decisions.md. Both endpoints return `queue_id`/
`queue_name` (jobs always; events when the underlying item still exists) precisely so the
frontend can group without a second request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.models import (
    HistoryEventOut,
    HistoryEventsResponse,
    HistoryJobOut,
    HistoryJobOutputOut,
    HistoryJobsResponse,
)

router = APIRouter(prefix="/api/history")

# Terminal job states only -- DESIGN.md §9.2: "every completed, failed, and cancelled
# transfer." `queued`/`running` are the Transfers page's domain (api/jobs.py), not this one's.
_TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _clamp_paging(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _job_out(row: Any) -> HistoryJobOut:
    return HistoryJobOut(
        id=row["id"],
        item_id=row["item_id"],
        queue_id=row["queue_id"],
        queue_name=row["queue_name"],
        rel_path=row["rel_path"],
        is_dir=bool(row["is_dir"]),
        kind=row["kind"],
        state=row["state"],
        attempt=row["attempt"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        bytes_total=row["bytes_total"],
        bytes_done=row["bytes_done"],
        exit_code=row["exit_code"],
        error_class=row["error_class"],
        has_output_tail=row["output_tail"] is not None,
    )


@router.get("/jobs", response_model=HistoryJobsResponse)
async def list_history_jobs(
    request: Request,
    queue_id: int | None = None,
    state: str | None = None,
    error_class: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> HistoryJobsResponse:
    """Completed/failed/cancelled jobs (DESIGN.md §9.2), newest-finished first. `since`/
    `until` compare against `COALESCE(finished_at, queued_at)` (ISO-8601 UTC strings,
    lexicographically comparable) so a cancelled-before-start job -- which has no
    `started_at` and may have no `finished_at` either in edge cases -- still sorts and
    filters sensibly.
    """
    if state is not None and state not in _TERMINAL_JOB_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {_TERMINAL_JOB_STATES}",
        )
    limit, offset = _clamp_paging(limit, offset)
    db = request.app.state.db

    where = ["job.state IN ('succeeded','failed','cancelled')"]
    params: list[Any] = []
    if queue_id is not None:
        where.append("item.queue_id = ?")
        params.append(queue_id)
    if state is not None:
        where.append("job.state = ?")
        params.append(state)
    if error_class is not None:
        where.append("job.error_class = ?")
        params.append(error_class)
    if since is not None:
        where.append("COALESCE(job.finished_at, job.queued_at) >= ?")
        params.append(since)
    if until is not None:
        where.append("COALESCE(job.finished_at, job.queued_at) <= ?")
        params.append(until)
    where_sql = " AND ".join(where)

    count_cursor = await db.execute(
        f"SELECT COUNT(*) AS c FROM job JOIN item ON item.id = job.item_id WHERE {where_sql}",
        params,
    )
    total_row = await count_cursor.fetchone()
    total = total_row["c"] if total_row is not None else 0

    cursor = await db.execute(
        "SELECT job.id, job.item_id, item.queue_id, path_queue.name AS queue_name, "
        "item.rel_path, item.is_dir, job.kind, job.state, job.attempt, job.queued_at, "
        "job.started_at, job.finished_at, item.remote_size AS bytes_total, job.bytes_done, "
        "job.exit_code, job.error_class, job.output_tail "
        "FROM job "
        "JOIN item ON item.id = job.item_id "
        "JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE {where_sql} "
        "ORDER BY COALESCE(job.finished_at, job.queued_at) DESC, job.id DESC "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    rows = await cursor.fetchall()
    return HistoryJobsResponse(
        jobs=[_job_out(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}/output", response_model=HistoryJobOutputOut)
async def get_job_output(job_id: int, request: Request) -> HistoryJobOutputOut:
    """The on-demand fetch `HistoryJobOut.has_output_tail` points at -- rendered on request,
    never inline in the list (see the module docstring).
    """
    cursor = await request.app.state.db.execute(
        "SELECT id, error_class, output_tail FROM job WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    return HistoryJobOutputOut(
        job_id=row["id"], error_class=row["error_class"], output_tail=row["output_tail"]
    )


def _event_out(row: Any) -> HistoryEventOut:
    return HistoryEventOut(
        id=row["id"],
        ts=row["ts"],
        level=row["level"],
        kind=row["kind"],
        message=row["message"],
        item_id=row["item_id"],
        job_id=row["job_id"],
        queue_id=row["queue_id"],
        queue_name=row["queue_name"],
        rel_path=row["rel_path"],
    )


@router.get("/events", response_model=HistoryEventsResponse)
async def list_history_events(
    request: Request,
    kind: str | None = None,
    level: str | None = None,
    item_id: int | None = None,
    queue_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> HistoryEventsResponse:
    """The full `event` table (DESIGN.md §3.1/§7.3/§7.4) -- every remote delete, every
    withheld delete with its gating precondition, and every verify/extract/move outcome
    `core/postprocess.py` records. `LEFT JOIN`, not `JOIN`: `event.item_id` is `ON DELETE SET
    NULL` (an audit row must outlive the item/queue it describes), so an event whose item or
    queue was since removed still surfaces -- with `queue_id`/`queue_name`/`rel_path` as
    `None` rather than silently vanishing from the audit trail.
    """
    limit, offset = _clamp_paging(limit, offset)
    db = request.app.state.db

    where: list[str] = []
    params: list[Any] = []
    if kind is not None:
        where.append("event.kind = ?")
        params.append(kind)
    if level is not None:
        where.append("event.level = ?")
        params.append(level)
    if item_id is not None:
        where.append("event.item_id = ?")
        params.append(item_id)
    if queue_id is not None:
        where.append("item.queue_id = ?")
        params.append(queue_id)
    if since is not None:
        where.append("event.ts >= ?")
        params.append(since)
    if until is not None:
        where.append("event.ts <= ?")
        params.append(until)
    where_sql = " AND ".join(where) if where else "1 = 1"

    count_cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM event "
        "LEFT JOIN item ON item.id = event.item_id "
        f"WHERE {where_sql}",
        params,
    )
    total_row = await count_cursor.fetchone()
    total = total_row["c"] if total_row is not None else 0

    cursor = await db.execute(
        "SELECT event.id, event.ts, event.level, event.kind, event.message, "
        "event.item_id, event.job_id, item.queue_id AS queue_id, "
        "path_queue.name AS queue_name, item.rel_path AS rel_path "
        "FROM event "
        "LEFT JOIN item ON item.id = event.item_id "
        "LEFT JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE {where_sql} "
        "ORDER BY event.ts DESC, event.id DESC "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    rows = await cursor.fetchall()
    return HistoryEventsResponse(
        events=[_event_out(r) for r in rows], total=total, limit=limit, offset=offset
    )
