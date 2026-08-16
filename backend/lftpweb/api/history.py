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

**But per-queue *aggregates* are not a display concern** (2026-08-16,
prompts/2026-08-16-history-jobs-group-collapse.md) -- the distinction the task itself draws.
`list_history_jobs`'s `jobs` list is one `LIMIT`/`OFFSET` page; a queue's *true* counts/total
size can span many pages. Summing only what happens to be loaded would show an honest-looking
but wrong number the instant a queue has more matching jobs than are on the page (or the page
hasn't loaded yet). `HistoryJobsResponse.queue_summaries` is a second, cheap `GROUP BY` query

**`arr_status`/`arr_instance_name`/`arr_instance_kind` on every job row** (2026-08-16,
prompts/2026-08-16-arr-chip-on-row-lines.md) -- the same `item.arr_status`/`item.arr_status_at`
plus `path_queue.arr_instance_id -> arr_instance.name`/`arr_instance.kind` join `core/queue.py.
list_jobs()` already carries for the Transfers row's *arr chip, added here so the History job
row can draw the identical chip. **Not** the phase-6 unbounded-list trap this module's own
docstring warns about above -- that trap is about shipping a *blob* (`output_tail`) on every row
of an unbounded list; these are two short scalar strings plus a status code, no bigger than
`queue_name` already sitting on every row.
(`_queue_summaries` below) run against the exact same `_jobs_where_clause` output as the `jobs`
list beside it -- same filter, so the two can never disagree -- one row per queue (bounded by
queue count, not job count), never a per-row blob (the phase-6 trap this module's own history
already names above). Inlined onto the existing response rather than a second `GET
/api/history/jobs/summary` endpoint: the frontend's `HistoryJobsSection` already refetches this
list on every filter change (queue/state/error class/date range, or Refresh), so a summary
endpoint would just be a second round trip computed from the identical filter on every one of
those triggers, for a payload that's a handful of rows regardless.

**`DELETE /api/history/jobs[/{id}]`, `DELETE /api/history/events[/{id}]`** (2026-08-13,
prompts/2026-08-13-clear-history.md) -- clearing History. This is a *different* action from
`api/jobs.py`'s `dismiss_job` (2026-08-13, prompts/done/2026-08-13-dismiss-terminal-jobs.md):
dismiss only ever sets `job.dismissed_at` and hides a row from the *Transfers* page while
leaving the row (and this page's view of it) completely intact; clear deletes the row from
*History* outright, here, and is irreversible. The two are adjacent, not overlapping -- a
dismissed job can still be cleared, and clearing never un-dismisses anything, because clearing
either removes the row or it doesn't exist to have a `dismissed_at` at all.

Bulk clearing (`DELETE` with no `{id}`) takes the exact same filter parameters as the matching
`GET` -- built by the same `_jobs_where_clause`/`_events_where_clause` helpers below, so the
two can never drift apart -- and deletes every row currently matching them in one SQL
statement, server-side, rather than the client fetching ids and issuing N requests
(`DESIGN.md`/phase-9's `Promise.allSettled` bulk pattern is for calls that can independently
fail for different *reasons* per row, like a stop-then-delete race; a single `DELETE ... WHERE`
either runs or it doesn't, so there is nothing per-row to partially fail). The jobs builder's
base clause (`job.state IN (...)`) is never optional, so no filter combination -- including no
filter at all, "clear all" -- can ever reach a `queued`/`running` job; the single-job `DELETE`
checks the same thing explicitly (404/409) since it bypasses the builder's WHERE by going
straight to a row id. Events have no such "active" concept, so every event -- including the
delete-audit kinds (`remote_delete`/`remote_delete_withheld`/`local_delete`/`archive_cleanup`)
-- clears the same way; see docs/decisions.md for why no category is protected.

**What clearing never touches:** `item` rows, `item.auto_queue_suppressed`,
`item.auto_queue_suppressed_reason`, or `metric_sample`/`metric_heartbeat` (the Dashboard's own
tables, `core/metrics.py`). Deleting a `job` row nulls any surviving `event.job_id` that
pointed at it (`ON DELETE SET NULL`, `001_initial_schema.sql:140`) -- the event still renders,
just without the job link -- but nothing here ever deletes an `item`, and `item.id` is not a
foreign key either table's `DELETE` touches. Logs (`core/logs.py`) and backups
(`core/backup.py`) are separate, deliberately out of scope, and unaffected.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.models import (
    HistoryClearResponse,
    HistoryEventOut,
    HistoryEventsResponse,
    HistoryJobOut,
    HistoryJobOutputOut,
    HistoryJobsResponse,
    HistoryQueueSummaryOut,
)

router = APIRouter(prefix="/api/history")

# Terminal job states only -- DESIGN.md §9.2: "every completed, failed, and cancelled
# transfer." `queued`/`running` are the Transfers page's domain (api/jobs.py), not this one's.
_TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def _clamp_paging(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


# --- Shared WHERE builders (2026-08-13, prompts/2026-08-13-clear-history.md) ------------
#
# `list_history_jobs`/`list_history_events` and their new `DELETE` counterparts below filter
# on exactly the same columns -- "clear what I am currently looking at" is the whole point, so
# there is deliberately one filter vocabulary, not a GET one and a separate DELETE one that
# could drift apart. Both builders always include a base clause (the terminal-state guard for
# jobs; nothing extra for events), so a caller can never construct a filter that reaches a
# `queued`/`running` job -- clearing history is bounded to history by construction, not by a
# separate check the DELETE path has to remember to run.


def _jobs_where_clause(
    *,
    item_id: int | None,
    queue_id: int | None,
    state: str | None,
    error_class: str | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[Any]]:
    where = ["job.state IN ('succeeded','failed','cancelled')"]
    params: list[Any] = []
    if item_id is not None:
        where.append("job.item_id = ?")
        params.append(item_id)
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
    return " AND ".join(where), params


def _events_where_clause(
    *,
    kind: str | None,
    level: str | None,
    item_id: int | None,
    queue_id: int | None,
    since: str | None,
    until: str | None,
) -> tuple[str, list[Any]]:
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
    return (" AND ".join(where) if where else "1 = 1"), params


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
        dismissed_at=row["dismissed_at"],
        arr_status=row["arr_status"],
        arr_status_at=row["arr_status_at"],
        arr_instance_name=row["arr_instance_name"],
        arr_instance_kind=row["arr_instance_kind"],
    )


async def _queue_summaries(
    db: Any, where_sql: str, params: list[Any]
) -> list[HistoryQueueSummaryOut]:
    """The `queue_summaries` half of `list_history_jobs`'s response -- one `GROUP BY queue_id,
    state` pass over the same filtered set the `jobs` page is drawn from (module docstring).
    One row per `(queue, state)` combination -- at most 3 per queue, since `where_sql` always
    carries the terminal-state base clause -- folded here into one row per queue with
    `succeeded`/`failed`/`cancelled` counts and a summed `bytes_done`. Ordered by queue name to
    match `groupJobsByQueue`'s own ordering on the frontend (`lib/transferPanel.ts`), though the
    frontend does not depend on that order -- it groups by `queue_id` regardless.
    """
    cursor = await db.execute(
        "SELECT item.queue_id AS queue_id, path_queue.name AS queue_name, job.state AS state, "
        "COUNT(*) AS cnt, COALESCE(SUM(job.bytes_done), 0) AS bytes_done "
        "FROM job "
        "JOIN item ON item.id = job.item_id "
        "JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE {where_sql} "
        "GROUP BY item.queue_id, path_queue.name, job.state "
        "ORDER BY path_queue.name",
        params,
    )
    rows = await cursor.fetchall()

    by_queue: dict[int, HistoryQueueSummaryOut] = {}
    order: list[int] = []
    for row in rows:
        queue_id = row["queue_id"]
        summary = by_queue.get(queue_id)
        if summary is None:
            summary = HistoryQueueSummaryOut(
                queue_id=queue_id,
                queue_name=row["queue_name"],
                succeeded=0,
                failed=0,
                cancelled=0,
                total_bytes_done=0,
            )
            by_queue[queue_id] = summary
            order.append(queue_id)
        state = row["state"]
        if state == "succeeded":
            summary.succeeded = row["cnt"]
        elif state == "failed":
            summary.failed = row["cnt"]
        elif state == "cancelled":
            summary.cancelled = row["cnt"]
        summary.total_bytes_done += row["bytes_done"]

    return [by_queue[qid] for qid in order]


@router.get("/jobs", response_model=HistoryJobsResponse)
async def list_history_jobs(
    request: Request,
    item_id: int | None = None,
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

    `item_id` (2026-08-13, prompts/2026-08-13-files-detail-inspector.md): one item's own
    transfer history, for the item drawer's bounded "load on open" fetch -- the mirror of
    `list_history_events`'s existing `item_id` filter below, which this endpoint lacked.
    """
    if state is not None and state not in _TERMINAL_JOB_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {_TERMINAL_JOB_STATES}",
        )
    limit, offset = _clamp_paging(limit, offset)
    db = request.app.state.db

    where_sql, params = _jobs_where_clause(
        item_id=item_id,
        queue_id=queue_id,
        state=state,
        error_class=error_class,
        since=since,
        until=until,
    )

    count_cursor = await db.execute(
        f"SELECT COUNT(*) AS c FROM job JOIN item ON item.id = job.item_id WHERE {where_sql}",
        params,
    )
    total_row = await count_cursor.fetchone()
    total = total_row["c"] if total_row is not None else 0

    cursor = await db.execute(
        "SELECT job.id, job.item_id, item.queue_id, path_queue.name AS queue_name, "
        "item.rel_path, item.is_dir, job.kind, job.state, job.attempt, job.queued_at, "
        # 2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md, defect 4): prefer the
        # value frozen on `job.bytes_total` at spawn (`core/queue.py._spawn_decision`) --
        # `job.bytes_done`'s own denominator, fixed at the same moment -- over the live
        # `item.remote_size`, which can drift after this job finished. Only a job that never
        # reached `running` (so `job.bytes_total` is still NULL -- a spawn failure, or a row
        # from before this column was populated) falls back to the item's current value.
        "job.started_at, job.finished_at, COALESCE(job.bytes_total, item.remote_size) AS bytes_total, job.bytes_done, "
        "job.exit_code, job.error_class, job.output_tail, job.dismissed_at, "
        "item.arr_status, item.arr_status_at, "
        "arr_instance.name AS arr_instance_name, arr_instance.kind AS arr_instance_kind "
        "FROM job "
        "JOIN item ON item.id = job.item_id "
        "JOIN path_queue ON path_queue.id = item.queue_id "
        "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
        f"WHERE {where_sql} "
        "ORDER BY COALESCE(job.finished_at, job.queued_at) DESC, job.id DESC "
        "LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    rows = await cursor.fetchall()
    queue_summaries = await _queue_summaries(db, where_sql, params)
    return HistoryJobsResponse(
        jobs=[_job_out(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
        queue_summaries=queue_summaries,
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


@router.delete("/jobs/{job_id}", response_model=HistoryClearResponse)
async def delete_history_job(job_id: int, request: Request) -> HistoryClearResponse:
    """Clear one job record from History (module docstring). Unlike `dismiss_job`
    (`api/jobs.py`), this deletes the row -- 404 if it never existed, 409 if it still does but
    isn't terminal (`queued`/`running` is Transfers' domain, not history to clear), and only
    otherwise a real `DELETE`. `item`/`auto_queue_suppressed` are never touched -- see the
    module docstring.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if row["state"] not in _TERMINAL_JOB_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"job {job_id} is still {row['state']!r} -- an active transfer is not "
                "history and cannot be cleared"
            ),
        )
    cursor = await db.execute("DELETE FROM job WHERE id = ?", (job_id,))
    await db.commit()
    return HistoryClearResponse(deleted=cursor.rowcount)


@router.delete("/jobs", response_model=HistoryClearResponse)
async def clear_history_jobs(
    request: Request,
    item_id: int | None = None,
    queue_id: int | None = None,
    state: str | None = None,
    error_class: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> HistoryClearResponse:
    """Clear every job matching the given filters -- the same ones `GET /jobs` accepts, built
    by the same `_jobs_where_clause` (module docstring). No filters at all clears every
    terminal job, i.e. "clear all"; `state='failed'` alone is "clear by outcome"; any
    combination is "clear what I'm currently looking at". One `DELETE ... WHERE`, so this
    either runs or it doesn't -- there's no partial-failure case to report per row.
    """
    if state is not None and state not in _TERMINAL_JOB_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"state must be one of {_TERMINAL_JOB_STATES}",
        )
    where_sql, params = _jobs_where_clause(
        item_id=item_id,
        queue_id=queue_id,
        state=state,
        error_class=error_class,
        since=since,
        until=until,
    )
    db = request.app.state.db
    cursor = await db.execute(
        "DELETE FROM job WHERE id IN ("
        "SELECT job.id FROM job JOIN item ON item.id = job.item_id "
        f"WHERE {where_sql})",
        params,
    )
    await db.commit()
    return HistoryClearResponse(deleted=cursor.rowcount)


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

    where_sql, params = _events_where_clause(
        kind=kind, level=level, item_id=item_id, queue_id=queue_id, since=since, until=until
    )

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


@router.delete("/events/{event_id}", response_model=HistoryClearResponse)
async def delete_history_event(event_id: int, request: Request) -> HistoryClearResponse:
    """Clear one event record from History (module docstring). Events have no `queued`/
    `running` concept the way jobs do -- there's no active state to reject -- so this is a
    plain `DELETE`, 404 if the id doesn't exist. No category (including the delete-audit
    kinds) is protected -- see docs/decisions.md.
    """
    db = request.app.state.db
    cursor = await db.execute("DELETE FROM event WHERE id = ?", (event_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="event not found")
    return HistoryClearResponse(deleted=cursor.rowcount)


@router.delete("/events", response_model=HistoryClearResponse)
async def clear_history_events(
    request: Request,
    kind: str | None = None,
    level: str | None = None,
    item_id: int | None = None,
    queue_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> HistoryClearResponse:
    """Clear every event matching the given filters -- the same ones `GET /events` accepts,
    built by the same `_events_where_clause` (module docstring). No filters clears every event
    in the table, delete-audit rows included by design (docs/decisions.md); `kind=
    'remote_delete'` alone is "clear by outcome". Same single-`DELETE` shape as
    `clear_history_jobs` above, for the same reason.
    """
    where_sql, params = _events_where_clause(
        kind=kind, level=level, item_id=item_id, queue_id=queue_id, since=since, until=until
    )
    db = request.app.state.db
    cursor = await db.execute(
        "DELETE FROM event WHERE id IN ("
        "SELECT event.id FROM event LEFT JOIN item ON item.id = event.item_id "
        f"WHERE {where_sql})",
        params,
    )
    await db.commit()
    return HistoryClearResponse(deleted=cursor.rowcount)
