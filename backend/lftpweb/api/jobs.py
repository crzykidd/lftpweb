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
from lftpweb.core.lftp import build_transfer_command, effective_tuning_settings
from lftpweb.core.queue import (
    JobNotDismissableError,
    TransferSettings,
    load_transfer_settings,
    save_transfer_settings,
)
from lftpweb.core.remote import parse_connection_limit
from lftpweb.models import (
    DeleteItemResponse,
    EffectiveLftpJobKind,
    EffectiveLftpSetting,
    EffectiveLftpSettingsOut,
    JobOut,
    JobsResponse,
    QueueItemRequest,
    QueueResetRequest,
    ResetItemResponse,
    ResetPatternPreviewItem,
    ResetPatternPreviewRequest,
    ResetPatternPreviewResponse,
    ResetSummaryResponse,
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
        # 2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md, defect 4): prefer the
        # value `core/queue.py._spawn_decision` froze on `job.bytes_total` at admission --
        # `job.bytes_done`'s own denominator, fixed at the same moment (§4.5's "fixed at spawn,
        # never re-shaped" invariant). `item.remote_size` alone can drift after spawn (a later
        # scan, a pattern edit) while `bytes_done` stays put, which is exactly how a live
        # incident showed `bytes_total: 31812118603` next to `bytes_done: 38841560420` -- two
        # different denominators for the same job. Only a `queued` job (never spawned yet, so
        # `job.bytes_total` is still NULL) falls back to the live `item.remote_size`, which is
        # the best estimate available before admission fixes anything.
        bytes_total=row["bytes_total"] if row["bytes_total"] is not None else row["remote_size"],
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


@router.post("/api/jobs/{job_id}/dismiss", status_code=204)
async def dismiss_job(job_id: int, request: Request) -> None:
    """Dismiss a terminal (`failed`/`cancelled`) job from the Transfers page (2026-08-13,
    prompts/done/2026-08-13-dismiss-terminal-jobs.md) — the user's own report: a `REMOTE_GONE`
    failure had Retry as its only action, which is exactly the wrong one once the remote files
    are actually gone. See `core/queue.py.dismiss_job`'s own docstring for what this does and
    deliberately does not touch (the job row's `dismissed_at`, never `item.state` or its
    suppression).

    A `queued`/`running` job is a 409, not a 404 — the job exists, the request just isn't
    valid for its current state, same distinction `delete_item`'s withheld-guard 409 draws
    above.
    """
    try:
        await request.app.state.queue.dismiss_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobNotDismissableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


def _busy_context(request: Request) -> tuple[frozenset[int], Any]:
    """The two "something is already touching this item" reads every reset endpoint below
    needs, in the exact shape `core/local_delete.py._guard_busy` takes -- factored out once
    rather than repeated four times.
    """
    postprocess = getattr(request.app.state, "postprocess", None)
    in_flight = postprocess.in_flight_item_ids() if postprocess is not None else frozenset()
    delete_in_flight = getattr(request.app.state, "delete_in_flight", None)
    return in_flight, delete_in_flight


def _forget_and_rescan(
    request: Request, queue_id: int, affected_rel_paths: tuple[str, ...]
) -> None:
    """After a successful reset, tell `Engine` to drop the forgotten rows from its own
    in-memory model (`Engine.forget_rel_paths`'s own docstring for why only `Engine` can do
    this) and request an immediate rescan -- the same "retroactive" idiom
    `api/settings.py`'s pattern create/update/delete already use -- so a path that still exists
    on the seedbox reappears fresh within moments rather than waiting up to a whole
    `scan_interval_s`.
    """
    if not affected_rel_paths:
        return
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return
    engine.forget_rel_paths(queue_id, affected_rel_paths)
    engine.request_rescan()


@router.post("/api/items/{item_id}/reset", response_model=ResetItemResponse)
async def reset_item(item_id: int, request: Request) -> ResetItemResponse:
    """ "Reset item tracking" -- the selected-item(s) scope (DESIGN.md §9.2's Files page;
    `prompts/2026-08-13-reset-item-tracking.md`). Forgets this item's row, and every descendant
    of its subtree, from `item`/`item_settle`/`deleted_archive` -- **not** a delete
    (`core/local_delete.py.delete_local` is that; local files are never touched here) and
    **not** Clear History (`api/history.py`; that clears `job`/`event` rows and deliberately
    never touches `item`). A bulk reset from the Files page's multi-select is this same endpoint
    called once per selected row (`Promise.allSettled`, identical to bulk Delete) -- there is no
    separate bulk endpoint.

    A withheld guard (active job, in-flight post-processing, or a delete currently removing
    this item's files) is a 409, matching `delete_item`'s own convention so the frontend's
    existing per-item bulk-failure reporting covers this with no new plumbing.
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

    in_flight, delete_in_flight = _busy_context(request)
    outcome = await local_delete.reset_item(
        db,
        item=item,
        queue=queue,
        caller="manual",
        in_flight_item_ids=in_flight,
        delete_in_flight=delete_in_flight,
    )
    if outcome.reset_top_level == 0:
        reason = outcome.withheld[0]["reason"] if outcome.withheld else "nothing to reset"
        raise HTTPException(status_code=409, detail=reason)

    _forget_and_rescan(request, item["queue_id"], outcome.affected_rel_paths)
    return ResetItemResponse(
        reset=True, reason="reset", affected_rel_paths=list(outcome.affected_rel_paths)
    )


@router.post("/api/queues/{queue_id}/reset-all", response_model=ResetSummaryResponse)
async def reset_queue_all(
    queue_id: int, body: QueueResetRequest, request: Request
) -> ResetSummaryResponse:
    """ "Reset item tracking" -- the whole-queue scope, the clean-slate case. The most
    destructive action in the app (every item this queue has ever tracked, forgotten at once),
    so it requires a **typed confirmation**: `body.confirm_name` must equal the queue's own
    `name` exactly, checked again here as defense in depth alongside the frontend's own
    type-to-confirm UI -- a request that reaches this endpoint without having gone through that
    UI still has to get the name right.

    Never all-or-nothing: an item mid-transfer is withheld (its own guard, `ResetSummaryResponse
    .withheld`) while every other item in the queue still resets -- see
    `core/local_delete.py.reset_queue`'s own docstring.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
    queue = await cursor.fetchone()
    if queue is None:
        raise HTTPException(status_code=404, detail="queue not found")
    if body.confirm_name != queue["name"]:
        raise HTTPException(
            status_code=400,
            detail=f"confirm_name {body.confirm_name!r} does not match this queue's name",
        )

    in_flight, delete_in_flight = _busy_context(request)
    outcome = await local_delete.reset_queue(
        db,
        queue=queue,
        caller="manual",
        in_flight_item_ids=in_flight,
        delete_in_flight=delete_in_flight,
    )
    _forget_and_rescan(request, queue_id, outcome.affected_rel_paths)
    return ResetSummaryResponse(
        reset_top_level=outcome.reset_top_level,
        withheld=list(outcome.withheld),
        affected_count=len(outcome.affected_rel_paths),
    )


@router.post("/api/queues/{queue_id}/reset-all-preview", response_model=ResetPatternPreviewResponse)
async def reset_all_preview(queue_id: int, request: Request) -> ResetPatternPreviewResponse:
    """The All scope's own preview -- the whole-queue counterpart to `reset_preview` below, with
    no pattern to type since "All" means every top-level item this queue tracks. Reads
    `core/local_delete.py.reset_queue_targets`, the exact enumeration `reset_queue_all` below
    (via `local_delete.reset_queue`) executes against, so the count and list shown here can never
    drift from what a confirmed reset actually does -- the same invariant `reset_preview` already
    holds for the pattern scope, extended to close a real gap: before this endpoint existed, the
    frontend improvised the All scope's preview from the published Files tree, which does not
    show a row once it reaches a terminal removed state with nothing left in either tree
    (`core/engine.py`), so an already-removed-but-still-tracked item was invisible to the preview
    while a confirmed reset forgot it anyway (2026-08-14,
    prompts/2026-08-14-reset-all-preview-undercounts.md). Never resets anything itself.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT id FROM path_queue WHERE id = ?", (queue_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=404, detail="queue not found")
    rows = await local_delete.reset_queue_targets(db, queue_id=queue_id)
    return ResetPatternPreviewResponse(
        items=[
            ResetPatternPreviewItem(
                rel_path=row["rel_path"],
                is_dir=bool(row["is_dir"]),
                remote_size=row["remote_size"],
                local_size=row["local_size"],
            )
            for row in rows
        ]
    )


@router.post("/api/queues/{queue_id}/reset-preview", response_model=ResetPatternPreviewResponse)
async def reset_preview(
    queue_id: int, body: ResetPatternPreviewRequest, request: Request
) -> ResetPatternPreviewResponse:
    """The purge-by-pattern scope's own safety mechanism (mirrors `api/settings.py.
    pattern_preview`'s "what would this match" idiom): every top-level item `body.pattern`
    would reset, with enough per-item data for the frontend to compute the same real-numbers
    warning the other two scopes already show, before anything is confirmed. Never resets
    anything itself -- `core/local_delete.py.reset_pattern_matches` is the identical query
    `reset_by_pattern` below actually executes against, so a mistake is visible before it's
    acted on rather than discovered after.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT id FROM path_queue WHERE id = ?", (queue_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=404, detail="queue not found")
    rows = await local_delete.reset_pattern_matches(db, queue_id=queue_id, pattern=body.pattern)
    return ResetPatternPreviewResponse(
        items=[
            ResetPatternPreviewItem(
                rel_path=row["rel_path"],
                is_dir=bool(row["is_dir"]),
                remote_size=row["remote_size"],
                local_size=row["local_size"],
            )
            for row in rows
        ]
    )


@router.post("/api/queues/{queue_id}/reset-by-pattern", response_model=ResetSummaryResponse)
async def reset_by_pattern(
    queue_id: int, body: ResetPatternPreviewRequest, request: Request
) -> ResetSummaryResponse:
    """ "Reset item tracking" -- the purge-by-pattern scope, single-queue only (confirmed with
    the user rather than inferred -- items are keyed `(queue_id, rel_path)` and a cross-queue
    purge is a much bigger gun than "let me reuse this one release name on this one queue" ever
    asked for). No typed confirmation here, unlike the whole-queue scope -- reviewing
    `reset_preview`'s own list first *is* the confirmation this scope relies on, per the task
    this shipped from.
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
    queue = await cursor.fetchone()
    if queue is None:
        raise HTTPException(status_code=404, detail="queue not found")

    in_flight, delete_in_flight = _busy_context(request)
    outcome = await local_delete.reset_by_pattern(
        db,
        queue=queue,
        pattern=body.pattern,
        caller="manual",
        in_flight_item_ids=in_flight,
        delete_in_flight=delete_in_flight,
    )
    _forget_and_rescan(request, queue_id, outcome.affected_rel_paths)
    return ResetSummaryResponse(
        reset_top_level=outcome.reset_top_level,
        withheld=list(outcome.withheld),
        affected_count=len(outcome.affected_rel_paths),
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


# --- Settings -> Transfer's "effective lftp settings" readout (2026-08-14,
# prompts/2026-08-14-show-effective-lftp-settings.md) -------------------------------------
#
# A user typing into "Extra lftp settings" has no way to tell whether they're adding a
# setting, duplicating one, or fighting one that lftpweb already sets -- this is the read-only
# answer, placed next to that field. Credential-free by construction (see
# `core/lftp.py.effective_tuning_settings`'s own docstring): this handler never imports or
# touches `HostCreds`, `build_rc_text`, or anything that has ever seen a password or key path,
# so there is no rendered-output string to filter and nothing here to audit for a stray
# credential slipping through.

# Illustrative-only paths for the argv preview -- never a real queue's remote/local path, since
# this endpoint has no queue context (it's a site-level page). `build_transfer_command` doesn't
# care what the paths look like; only the flags around them are what this exists to show.
_ARGV_PREVIEW_REMOTE = "<remote-path>/<item>"
_ARGV_PREVIEW_LOCAL = "<local-path>/<item>"

_ARGV_WHY = {
    "pget": (
        "`-c` (continue) is what makes a restart resumable rather than starting over from "
        "byte zero (DESIGN.md §4.1) -- load-bearing for restart survivability. `-n` is "
        "connections for this one file."
    ),
    "mirror": (
        "`-c` (continue) is what makes a restart resumable rather than re-fetching everything "
        "already on disk (DESIGN.md §4.1). `--parallel` is files transferred at once within "
        "this job; `--use-pget-n` is connections per file. A queue with file-exclude patterns "
        "adds one `--exclude-glob '<pattern>'` per pattern (DESIGN.md §4.7) -- not shown here "
        "since this page has no queue context; see that queue's own pattern editor."
    ),
}

_BANDWIDTH_NOTE = (
    "A per-job bandwidth cap (net:limit-total-rate) is always set on a real job, computed at "
    "admission time (DESIGN.md §4.5) from how many jobs are currently sharing the ceiling -- "
    "see the live connection-count readout above for what a job admitted right now would get."
)


async def _effective_connection_limit(request: Request) -> int | None:
    cursor = await request.app.state.db.execute(
        "SELECT connection_overrides FROM host ORDER BY id LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return parse_connection_limit(row["connection_overrides"])


def _kind_settings(
    kind: str, settings: TransferSettings, connection_limit: int | None
) -> EffectiveLftpJobKind:
    pget_n = settings.mirror_use_pget_n if kind == "mirror" else settings.pget_default_n
    rc_settings = [
        EffectiveLftpSetting(key=ts.key, value=ts.value, why=ts.why, configurable=ts.configurable)
        for ts in effective_tuning_settings(
            # Not the numeric bandwidth cap -- see `_BANDWIDTH_NOTE` above for why that's prose
            # here rather than a value from this function.
            rate_limit_bps=None,
            connection_limit=connection_limit,
            parallel=settings.mirror_parallel_transfer_count,
            pget_n=pget_n,
            save_status_interval_s=1,  # always 1 -- see JobSpec's own default, never configurable
        )
    ]
    if kind == "mirror":
        argv = build_transfer_command(
            "mirror",
            _ARGV_PREVIEW_REMOTE,
            _ARGV_PREVIEW_LOCAL,
            parallel=settings.mirror_parallel_transfer_count,
            pget_n=pget_n,
            exclude_globs=(),
        )
    else:
        argv = build_transfer_command(
            "pget",
            _ARGV_PREVIEW_REMOTE,
            _ARGV_PREVIEW_LOCAL,
            parallel=settings.mirror_parallel_transfer_count,
            pget_n=pget_n,
            exclude_globs=(),
        )
    return EffectiveLftpJobKind(
        kind=kind, argv=argv, argv_why=_ARGV_WHY[kind], rc_settings=rc_settings
    )


@router.get("/api/settings/transfer/effective-lftp", response_model=EffectiveLftpSettingsOut)
async def get_effective_lftp_settings(request: Request) -> EffectiveLftpSettingsOut:
    settings = await load_transfer_settings(request.app.state.db)
    connection_limit = await _effective_connection_limit(request)
    kinds = [
        _kind_settings("mirror", settings, connection_limit),
        _kind_settings("pget", settings, connection_limit),
    ]
    return EffectiveLftpSettingsOut(kinds=kinds, bandwidth_note=_BANDWIDTH_NOTE)
