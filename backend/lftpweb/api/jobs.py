"""Queue/stop/retry/move-to-top/start-now, the active+pending job list, and the site-level
transfer settings (DESIGN.md §4.5, §9.2 Transfers, §9.3). Backend only this phase — the
Transfers page and item drawer are phase 3b; this is the API they'll consume, verified here
through the API itself and the fake seedbox rather than through a UI that doesn't exist yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import audit, local_delete
from lftpweb.core.queue import TransferSettings, load_transfer_settings, save_transfer_settings
from lftpweb.models import (
    DeleteItemResponse,
    JobOut,
    JobsResponse,
    QueueItemRequest,
    TransferSettingsIn,
    TransferSettingsOut,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 2026-08-13 (prompts/2026-08-13-delete-during-transfer.md): the bound on how long `delete_item`
# waits for `TransferQueue.stop_item()` to confirm an active job is really gone before giving up
# and withholding the delete. Generous headroom over `core/queue.py.stop_job`'s own internal
# SIGTERM grace (`lftp.terminate(grace_s=10.0)`) plus SIGKILL-and-reap, which is normally
# near-instant once the kill lands -- this is a backstop for a genuinely wedged process (a stuck
# NFS write, say), not the expected path.
STOP_BEFORE_DELETE_TIMEOUT_S = 25.0

# A stop attempt that outlasts the timeout above is deliberately *not* cancelled (see
# `delete_item`'s own docstring) -- it keeps running so `core/queue.py`'s own bookkeeping
# finishes cleanly instead of being abandoned half-updated. asyncio only holds a *weak*
# reference to a `Task`, though (its own docs: "save a reference ... to avoid a task
# disappearing mid-execution due to garbage collection"), so a task nothing else references
# once this request returns is a real risk of being GC'd before it finishes, not a
# hypothetical one. This module-level set is that reference, for exactly the tasks that outlive
# the request that spawned them; `_forget_background_stop` (its own `add_done_callback`) is what
# keeps it from growing forever.
_background_stop_tasks: set[asyncio.Task] = set()


def _forget_background_stop(task: asyncio.Task) -> None:
    _background_stop_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Nothing is awaiting this task's result anymore (the request that spawned it already
        # returned a 409) -- log rather than let it vanish as an "exception was never retrieved"
        # warning, since a failed background stop is exactly the kind of thing an operator
        # diagnosing a stuck delete needs to find.
        logger.exception("background stop-before-delete failed", exc_info=exc)


def _run_stop_in_background(coro: Coroutine[Any, Any, bool]) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _background_stop_tasks.add(task)
    task.add_done_callback(_forget_background_stop)
    return task


def _job_out(row: dict) -> JobOut:
    return JobOut(
        id=row["id"],
        item_id=row["item_id"],
        queue_id=row["queue_id"],
        queue_name=row["queue_name"],
        rel_path=row["rel_path"],
        is_dir=bool(row["is_dir"]),
        kind=row["kind"],
        state=row["state"],
        lane=row["lane"],
        rank=row["rank"],
        attempt=row["attempt"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        pid=row["pid"],
        rate_limit_bps=row["rate_limit_bps"],
        forced_full_rate=bool(row["forced_full_rate"]),
        bytes_start=row["bytes_start"],
        bytes_done=row["bytes_done"],
        bytes_total=row["remote_size"],
        speed_bps=row.get("speed_bps"),
        eta_s=row.get("eta_s"),
        exit_code=row["exit_code"],
        error_class=row["error_class"],
        output_tail=row["output_tail"],
    )


@router.get("/api/jobs", response_model=JobsResponse)
async def list_jobs(request: Request) -> JobsResponse:
    rows = await request.app.state.queue.list_jobs()
    return JobsResponse(jobs=[_job_out(r) for r in rows])


@router.post("/api/jobs", response_model=JobOut, status_code=201)
async def queue_item(body: QueueItemRequest, request: Request) -> JobOut:
    q = request.app.state.queue
    try:
        job_id = await q.enqueue_item(body.item_id, forced_full_rate=body.start_now)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = await q.list_jobs()
    row = next((r for r in rows if r["id"] == job_id), None)
    if row is None:
        raise HTTPException(status_code=500, detail="job vanished immediately after being queued")
    return _job_out(row)


@router.post("/api/jobs/{job_id}/stop", status_code=204)
async def stop_job(job_id: int, request: Request) -> None:
    try:
        await request.app.state.queue.stop_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/jobs/{job_id}/move-to-top", status_code=204)
async def move_to_top(job_id: int, request: Request) -> None:
    await request.app.state.queue.move_to_top(job_id)


@router.post("/api/jobs/{job_id}/start-now", response_model=dict)
async def start_now(job_id: int, request: Request) -> dict:
    applied = await request.app.state.queue.start_now(job_id)
    return {"applied": applied}


@router.post("/api/items/{item_id}/stop", response_model=dict)
async def stop_item(item_id: int, request: Request) -> dict:
    applied = await request.app.state.queue.stop_item(item_id)
    return {"applied": applied}


@router.post("/api/items/{item_id}/delete", response_model=DeleteItemResponse)
async def delete_item(item_id: int, request: Request) -> DeleteItemResponse:
    """Manual "Delete local" (DESIGN.md §9.2's Files page; `prompts/open-issues.md` "7 + 8" --
    the first delete endpoint in this API, and the only manual caller of
    `core/local_delete.py.delete_local`). `require_nlink_guard=False`: a human clicking Delete
    on `LOCAL_ONLY` junk with exactly one copy is precisely the case the guard exists to *not*
    block (the module's own docstring). A withheld guard raises rather than returning
    `deleted=False`, so `FileTree.tsx`'s existing `Promise.allSettled` bulk-action reporting
    (Queue/Stop, phase 9) picks this up as a per-item failure with no new frontend plumbing.

    **Stop first, then delete** (2026-08-13, `prompts/2026-08-13-delete-during-transfer.md`).
    `delete_local`'s own "no active job" guard is unchanged and still correct -- see that
    module's docstring -- so a Delete request for an item mid-transfer used to just bounce off
    it with a 409. This endpoint now satisfies the guard itself first: `TransferQueue.
    stop_item()` (the identical stop path the Stop button drives, §4.6 -- SIGTERM, a grace
    period, SIGKILL, no second implementation) is always called before `delete_local`, whether
    or not the item actually has an active job -- it's already a safe no-op when it doesn't
    (`stop_item`'s own docstring). The call is run as a background task and awaited with a
    bound (`STOP_BEFORE_DELETE_TIMEOUT_S`) **without cancelling it on timeout**: cancelling
    mid-stop would abandon `core/queue.py`'s own bookkeeping (`self._running`, the job row)
    half-updated, which is exactly the inconsistency this feature exists to avoid introducing.
    A timeout instead just means this *request* stops waiting -- the stop attempt keeps running
    in the background and will still finish reaping and persisting the job's terminal state on
    its own -- and the delete is withheld with a 409 naming the reason, per the task's own
    instruction: "if the stop cannot be confirmed within a bounded time, withhold the delete and
    say why." By the time `stop_item()` *does* return, `_reap_one` has already persisted the
    job as terminal (`cancelled`) and the item as `STOPPED`/`user_stopped` -- so `delete_local`'s
    guard passes on this same call, and its own unconditional `suppressed_reason = 'deleted_local'`
    write (`_mark_subtree_removed`) overwrites `user_stopped` a moment later: the row that comes
    out the other end reads as a deliberate deletion, never a user stop.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    item = await cursor.fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (item["queue_id"],))
    queue = await cursor.fetchone()
    if queue is None:
        raise HTTPException(status_code=404, detail="item's queue no longer exists")

    q = getattr(request.app.state, "queue", None)
    if q is not None:
        stop_task = _run_stop_in_background(q.stop_item(item_id))
        done, _pending = await asyncio.wait({stop_task}, timeout=STOP_BEFORE_DELETE_TIMEOUT_S)
        if stop_task not in done:
            reason = (
                f"an active transfer for {item['rel_path']!r} (queue {queue['id']} "
                f"'{queue['name']}') could not be confirmed stopped within "
                f"{STOP_BEFORE_DELETE_TIMEOUT_S:.0f}s -- refusing to delete out from under a "
                "transfer that may still be running; the stop is still being attempted in the "
                "background and this item can be deleted once it settles"
            )
            await audit.record_event(
                db,
                level="error",
                item_id=item_id,
                kind="local_delete_withheld",
                message=f"manual: delete withheld -- {reason}",
            )
            raise HTTPException(status_code=409, detail=reason)
        # Propagate a genuine failure from stop_item itself (e.g. the job row vanished between
        # the SELECT above and stop_item's own lookup -- ValueError, per stop_job's contract)
        # rather than silently swallowing it and proceeding to delete anyway.
        stop_task.result()

    postprocess = getattr(request.app.state, "postprocess", None)
    in_flight = postprocess.in_flight_item_ids() if postprocess is not None else frozenset()
    delete_in_flight = getattr(request.app.state, "delete_in_flight", None)

    outcome = await local_delete.delete_local(
        db,
        item=item,
        queue=queue,
        caller="manual",
        require_nlink_guard=False,
        in_flight_item_ids=in_flight,
        events=request.app.state.events,
        delete_in_flight=delete_in_flight,
    )
    if not outcome.deleted:
        raise HTTPException(status_code=409, detail=outcome.reason)
    return DeleteItemResponse(
        deleted=outcome.deleted, reason=outcome.reason, bytes_freed=outcome.bytes_freed
    )


@router.post("/api/items/{item_id}/retry", response_model=JobOut, status_code=201)
async def retry_item(item_id: int, request: Request) -> JobOut:
    q = request.app.state.queue
    try:
        job_id = await q.retry_item(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = await q.list_jobs()
    row = next((r for r in rows if r["id"] == job_id), None)
    if row is None:
        raise HTTPException(status_code=500, detail="job vanished immediately after being queued")
    return _job_out(row)


# --- Settings -> Transfer (DESIGN.md §4.5's table, §9.2/§9.3) ---------------------------


def _settings_out(s: TransferSettings) -> TransferSettingsOut:
    return TransferSettingsOut(**{f: getattr(s, f) for f in TransferSettings.__dataclass_fields__})


@router.get("/api/settings/transfer", response_model=TransferSettingsOut)
async def get_transfer_settings(request: Request) -> TransferSettingsOut:
    settings = await load_transfer_settings(request.app.state.db)
    return _settings_out(settings)


@router.put("/api/settings/transfer", response_model=TransferSettingsOut)
async def put_transfer_settings(body: TransferSettingsIn, request: Request) -> TransferSettingsOut:
    settings = TransferSettings(**body.model_dump())
    await save_transfer_settings(request.app.state.db, settings)
    q = getattr(request.app.state, "queue", None)
    if q is not None:
        q.request_tick()
    return _settings_out(settings)
