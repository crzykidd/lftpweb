"""Queue/stop/retry/move-to-top/move/start-now/pause, the active+pending job list, and the
site-level transfer settings (DESIGN.md §4.5, §9.2 Transfers, §9.3). Backend only this phase —
the Transfers page and item drawer are phase 3b; this is the API they'll consume, verified here
through the API itself and the fake seedbox rather than through a UI that doesn't exist yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import audit, local_delete, settle
from lftpweb.core.itemview import ITEM_VIEW_COLUMNS_QUALIFIED, item_view
from lftpweb.core.lftp import build_transfer_command, effective_tuning_settings
from lftpweb.core.postprocess import perform_remote_delete
from lftpweb.core.preflight import PreflightRow
from lftpweb.core.queue import (
    JobNotDismissableError,
    JobNotQueuedError,
    NoSiteLimitConfiguredError,
    QueuePausedError,
    TransferSettings,
    load_transfer_settings,
    resolve_forced_rate_fraction,
    save_transfer_settings,
)
from lftpweb.core.remote import parse_connection_limit
from lftpweb.models import (
    CompleteJobsResponse,
    DeleteItemRequest,
    DeleteItemResponse,
    DismissAllRequest,
    DismissAllResponse,
    EffectiveLftpJobKind,
    EffectiveLftpSetting,
    EffectiveLftpSettingsOut,
    FileNode,
    ItemChildrenResponse,
    ItemEventOut,
    ItemEventsResponse,
    JobOut,
    JobsResponse,
    MoveJobRequest,
    PreflightGatedQueueOut,
    PreflightResponse,
    PreflightRowOut,
    QueueItemRequest,
    QueuePauseRequest,
    QueueResetRequest,
    ResetItemResponse,
    ResetPatternPreviewItem,
    ResetPatternPreviewRequest,
    ResetPatternPreviewResponse,
    ResetSummaryResponse,
    ResolveItemRequest,
    ResolveItemResponse,
    StartNowRequest,
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


def _now_iso() -> str:
    """The one wall-clock format this codebase writes (`core/audit.py.record_event`,
    `core/queue.py._now_iso`, `core/arrsync.py._now_iso`) -- `item.manual_outcome_at` must be
    comparable with every other persisted timestamp, so it uses the same one rather than a second
    convention invented here.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def _job_out(row: dict, *, include_output_tail: bool = True) -> JobOut:
    """`include_output_tail=False` (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1
    stage 4b) is `GET /api/jobs/complete`'s own use -- that endpoint is paginated but unbounded
    in total row count, so it never inlines the ~4KB `output_tail` blob, the identical trap
    `api/history.py`'s own docstring names for its own list endpoint. `has_output_tail` is
    always populated regardless (`row["output_tail"] is not None`), so the row's expand panel
    has one signal to decide whether an on-demand fetch (`GET /api/history/jobs/{id}/output`) is
    worth making, whichever endpoint the row came from.
    """
    return JobOut(
        id=row["id"],
        item_id=row["item_id"],
        queue_id=row["queue_id"],
        queue_name=row["queue_name"],
        queue_short_name=row["queue_short_name"],
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
        forced_rate_fraction=resolve_forced_rate_fraction(row),
        bytes_start=row["bytes_start"],
        # 2026-08-21 (prompts/done/2026-08-21-paused-item-progress.md, issue #14's second half):
        # for a `queued` row only, prefer `item.local_size` (the scanner/reconciler's own current
        # reading, DESIGN.md §1.3) over `job.bytes_done` when the row's own `local_size` is known
        # -- a fresh retry's `job.bytes_done` starts at 0 even when the item already carries real
        # partial bytes from an interrupted earlier attempt, while a paused-in-place row's
        # `job.bytes_done` and `item.local_size` already agree (both written together by the same
        # progress-sampler UPDATE, `TransferQueue._sample_and_publish_progress`), so this is a
        # no-op for that case and a real fix for the fresh-retry one. Mirrors `bytes_total`'s own
        # fallback to `item.remote_size` immediately below, for the identical "queued, nothing
        # frozen yet" reason. `.get`, not `row[...]`: `_job_out` is also called on rows built by
        # hand in tests that predate this join (see `_job_out_row()`), which never carry the key.
        bytes_done=(
            row["local_size"]
            if row["state"] == "queued" and row.get("local_size") is not None
            else row["bytes_done"]
        ),
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
        output_tail=row["output_tail"] if include_output_tail else None,
        has_output_tail=row["output_tail"] is not None,
        # 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): the
        # item-level facts the Transfers row's expand panel needs -- see `TransferQueue.
        # list_jobs()`'s own docstring for the join these come from. `arr_instance_name`/
        # `arr_instance_kind` (the latter added 2026-08-16 for the row chip's brand-logo choice,
        # prompts/2026-08-16-arr-chip-on-row-lines.md) are always projected (`NULL` when the
        # `LEFT JOIN arr_instance` finds no match), never absent from the row, so plain `row[...]`
        # is safe here too.
        verified_at=row["verified_at"],
        extracted_at=row["extracted_at"],
        remote_deleted_at=row["remote_deleted_at"],
        arr_status=row["arr_status"],
        arr_status_at=row["arr_status_at"],
        arr_instance_name=row["arr_instance_name"],
        arr_instance_kind=row["arr_instance_kind"],
        # 2026-08-20 (docs/transfers-redesign-spec.md §3.2's pipeline-completion rule): the
        # server-computed box assignment and its reason (`core/pipeline_flight.py`), projected by
        # both listing queries under these exact aliases. `.get`, not `row[...]`: `_job_out` is
        # also called on rows that never went through those queries in a handful of tests, and a
        # missing classification degrading to "complete" matches this predicate's own documented
        # fail-safe direction (unknown is never blocking).
        pipeline_in_flight=bool(row.get("pipeline_in_flight") or 0),
        pipeline_waiting_reason=row.get("pipeline_waiting_reason"),
        manual_outcome=row.get("manual_outcome"),
        manual_outcome_at=row.get("manual_outcome_at"),
    )


@router.get("/api/jobs", response_model=JobsResponse)
async def list_jobs(request: Request) -> JobsResponse:
    rows = await request.app.state.queue.list_jobs()
    return JobsResponse(jobs=[_job_out(r) for r in rows])


# --- The Queue tab's Complete box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1
# stage 4b) -- terminal jobs, split out of `list_jobs()`'s bounded row set into their own
# server-paginated endpoint, 50/page, newest-finished first. Same `LIMIT`/`OFFSET` clamp shape
# `api/history.py._clamp_paging` uses -- kept as its own tiny copy here rather than importing
# that module's private helper, since the two endpoints' response shapes (and default/max page
# sizes) are otherwise independent. -------------------------------------------------------------

COMPLETE_JOBS_DEFAULT_LIMIT = 50
COMPLETE_JOBS_MAX_LIMIT = 200


def _clamp_complete_paging(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, COMPLETE_JOBS_MAX_LIMIT)), max(0, offset)


@router.get("/api/jobs/complete", response_model=CompleteJobsResponse)
async def list_complete_jobs(
    request: Request,
    name_filter: str | None = None,
    limit: int = COMPLETE_JOBS_DEFAULT_LIMIT,
    offset: int = 0,
) -> CompleteJobsResponse:
    """The Complete box's own fetch -- `TransferQueue.list_complete_jobs`'s own docstring has
    the full reasoning (why this is split from `list_jobs()`, the `MAX(id)`-per-item rule, why
    `output_tail` is never inlined here). `name_filter` is the server-side twin of the Active
    box's client-side `lib/transferPanel.ts.filterTransferJobs` -- case-insensitive substring
    over `rel_path`, empty string matching every row, `None`/omitted meaning no filter at all.
    """
    limit, offset = _clamp_complete_paging(limit, offset)
    rows, total = await request.app.state.queue.list_complete_jobs(
        limit=limit, offset=offset, name_filter=name_filter
    )
    return CompleteJobsResponse(
        jobs=[_job_out(r, include_output_tail=False) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


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


@router.post("/api/jobs/dismiss-all", response_model=DismissAllResponse)
async def dismiss_all_jobs(
    request: Request, body: DismissAllRequest | None = None
) -> DismissAllResponse:
    """ "Dismiss all" at the top of the Transfers page (2026-08-15, user addition to
    prompts/2026-08-15-transfers-single-line-rows-with-detail.md) -- the bulk counterpart to
    `dismiss_job` above. A single server-side `UPDATE` (`TransferQueue.dismiss_all_terminal`'s
    own docstring), not a client-side loop over every dismissable row's own `/dismiss` call --
    the task's own stated preference, and it means there is no per-row network round trip to
    partially fail the way `Promise.allSettled` bulk actions elsewhere in this app (Files page's
    Queue/Stop, Transfers' own "Clear all failed") have to account for. Never touches a `queued`/
    `running` job, by construction of the `UPDATE`'s own `WHERE` -- see that method's docstring.

    2026-08-17 (`prompts/2026-08-17-transfers-dismiss-per-queue.md`): `body.queue_id`, when
    given, scopes the bulk dismiss to that one queue's own terminal jobs -- the group-header
    "Dismiss Queue" control's own endpoint, reusing this one rather than adding a second. An
    omitted body (`body is None`, the pre-existing no-body call every caller before this task
    still makes) or an explicit `queue_id: null` both mean the original every-queue behavior,
    byte-for-byte -- see `DismissAllRequest`'s own docstring.

    2026-08-19 (`prompts/2026-08-19-transfers-name-filter.md`): `body.job_ids`, when given,
    scopes the bulk dismiss to that explicit set of job ids instead -- the name filter's own
    "Dismiss list" button, again reusing this one endpoint rather than adding a second.

    2026-08-19 (docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b): `body.name_filter`
    supersedes `job_ids` for "Dismiss list" now that the Complete box (`GET /api/jobs/complete`)
    is server-paginated -- a filter can match more rows than one page's worth of ids can name.
    See `DismissAllRequest.name_filter`'s own docstring for the full reasoning and
    `TransferQueue.dismiss_all_terminal`'s for the SQL.

    2026-08-20 (follow-up to phase 1 stage 4b, `prompts/2026-08-20-transfers-dismiss-menu-and-
    counts.md`): `body.outcome` narrows the same bulk dismiss to one terminal state -- the
    Complete box's own "Dismiss" menu, now moved into that box's header. Unlike `job_ids`/
    `queue_id`, `outcome` **composes** with `name_filter` (both can be given together) -- see
    `DismissAllRequest`'s own docstring for the decided reasoning and its restructured validator
    for what's still rejected.

    `job_ids` and `queue_id` stay mutually exclusive with every other scope, including each
    other, at the request-model level (`DismissAllRequest`'s own validator, a 422 for anything
    incoherent); this handler just threads whichever ones were given straight through to
    `dismiss_all_terminal`.
    """
    queue_id = body.queue_id if body is not None else None
    job_ids = body.job_ids if body is not None else None
    name_filter = body.name_filter if body is not None else None
    outcome = body.outcome if body is not None else None
    dismissed = await request.app.state.queue.dismiss_all_terminal(
        queue_id=queue_id, job_ids=job_ids, name_filter=name_filter, outcome=outcome
    )
    return DismissAllResponse(dismissed=dismissed)


# --- Item events (2026-08-15, prompts/2026-08-15-transfers-single-line-rows-with-detail.md)
# ---------------------------------------------------------------------------------------------
#
# The Transfers panel's "processing story" -- `core/postprocess.py`/`core/arrsync.py` already
# write every branch's reasoning as `event` rows (`core/audit.py`), keyed directly by
# `event.item_id` (an ON DELETE SET NULL column, migration 001). Bounded and on-demand, the same
# shape `api/history.py`'s own endpoints already establish: never fetched inline on the jobs
# list (which is bounded by row count, not by how much *history* each row could accumulate), and
# capped regardless of what a caller asks for.

ITEM_EVENTS_DEFAULT_LIMIT = 50
ITEM_EVENTS_MAX_LIMIT = 200


@router.get("/api/items/{item_id}/events", response_model=ItemEventsResponse)
async def item_events(
    item_id: int, request: Request, limit: int = ITEM_EVENTS_DEFAULT_LIMIT
) -> ItemEventsResponse:
    """The Transfers panel's Processing group, enriched (module comment above): every `event`
    row for this one item, newest first, capped at `ITEM_EVENTS_MAX_LIMIT` regardless of what
    `limit` asks for -- the same clamp shape `api/history.py._clamp_paging` uses, kept as a
    simple `min()` here since this endpoint has no `offset` to clamp alongside it (the panel
    wants "the recent story," not a paginated archive; `api/history.py` already is that for
    anyone who wants the full trail). No existence check on `item_id` itself -- an unknown or
    since-forgotten item (`api/jobs.py.reset_item`, "reset item tracking") simply yields an
    empty list, which is indistinguishable from, and just as correct a response as, "this item
    exists but nothing happened to it yet."
    """
    capped_limit = max(1, min(limit, ITEM_EVENTS_MAX_LIMIT))
    cursor = await request.app.state.db.execute(
        "SELECT id, ts, level, kind, message, job_id FROM event "
        "WHERE item_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (item_id, capped_limit),
    )
    rows = await cursor.fetchall()
    return ItemEventsResponse(
        events=[
            ItemEventOut(
                id=row["id"],
                ts=row["ts"],
                level=row["level"],
                kind=row["kind"],
                message=row["message"],
                job_id=row["job_id"],
            )
            for row in rows
        ]
    )


# --- Per-file children (2026-08-20, docs/transfers-redesign-spec.md §3.3, phase 1 stage 5) -----
#
# The Transfers row's per-file expansion -- "the thing Files is currently used for, moved to
# where the ordering lives" (the spec's own words). **Not a second data source**:
# `core/queue.py._publish_child_progress` already computes, persists, and publishes each child
# file's size/state/rate from the same filesystem walk the running job performs; the Files tree
# is one renderer of that data, and this is a second one, both reading the `item` table back
# through `core/itemview.py.item_view` -- "nothing may publish a state it did not read back from
# the `item` table" holds here exactly as it does for `GET /api/files`.
#
# **Bounded, on-demand, same shape as `item_events` immediately above**: fetched once, when a
# row's panel expands, never inlined into `GET /api/jobs`/`GET /api/jobs/complete` -- a season
# pack has dozens of children; inlining even that modest a payload onto a list endpoint the
# frontend polls is exactly the trap `api/history.py`'s own module docstring names for
# `output_tail`, just for a different field. `ITEM_CHILDREN_MAX_LIMIT` is the backstop against a
# genuinely pathological release (thousands of files) rather than the expected case -- chosen to
# match `api/history.py.MAX_LIMIT` (500), the closest existing precedent for "how big a capped
# list on this app gets," rather than inventing a new number with no anchor. Once expanded, the
# row does **not** re-poll this endpoint for live updates -- see `TransfersPage.tsx`'s own
# comment on why the existing `item_delta`/`child_progress` WebSocket stream (already open,
# already subscribed) is what keeps an expanded row current, so N expanded rows never mean N
# independent polls.

ITEM_CHILDREN_DEFAULT_LIMIT = 200
ITEM_CHILDREN_MAX_LIMIT = 500


def _clamp_children_paging(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, ITEM_CHILDREN_MAX_LIMIT)), max(0, offset)


@router.get("/api/items/{item_id}/children", response_model=ItemChildrenResponse)
async def item_children(
    item_id: int,
    request: Request,
    limit: int = ITEM_CHILDREN_DEFAULT_LIMIT,
    offset: int = 0,
) -> ItemChildrenResponse:
    """Every leaf file nested under `item_id`'s own `rel_path`, ordered by `rel_path` -- capped
    at `ITEM_CHILDREN_MAX_LIMIT` regardless of what `limit` asks for, same clamp shape
    `api/history.py._clamp_paging`/`item_events` already use. 404 for an unknown `item_id` --
    unlike `item_events`, which treats an unknown id as "no history yet" (an event row outlives
    the item it described), a children fetch is meaningless without a real parent row to resolve
    `rel_path`/`queue_id` from.

    **A non-directory item (a `pget` job's own single-file row) has no children by
    construction** -- `is_dir = 0` at the top level is exactly the condition
    `core/queue.py._publish_child_progress`'s own "pget job: no children" branch keys off
    (`JobProgress.children is None`), so this returns an empty list rather than querying for
    descendants that structurally cannot exist. Not a 404 or an error: an empty subtree is a
    valid, expected answer for a real item, not a fault.

    **Descendants, not just direct children** -- `instr(item.rel_path, ?) = 1` (the same
    substring-prefix technique `core/engine.py`'s in-flight-descendant clause and
    `core/queue.py._relevant_remote_total` already use, chosen over `LIKE` specifically because a
    literal `%`/`_` in a real release name needs no escaping against it) matches every row whose
    `rel_path` starts with `"{parent.rel_path}/"`, at any depth -- a season pack with a `Season
    01/`/`Season 02/` split still shows every episode, not just the top level. `AND is_dir = 0`
    scopes this to leaf files only, the same scope `_publish_child_progress` itself has ("only
    leaf files nested under a currently downloading mirror item") -- an intermediate directory
    row carries no progress of its own worth showing here.

    **`LEFT JOIN item_settle`/`deleted_archive`**, identical to `api/files.py.get_files`'s own
    join -- so a child that happens to be a spent, on-purpose-deleted archive volume renders the
    same grey "Extracted" reading here that it would on the Files page, rather than a plainer
    `EXCLUDED` this endpoint would otherwise show for want of the join.
    """
    limit, offset = _clamp_children_paging(limit, offset)
    db = request.app.state.db

    cursor = await db.execute(
        "SELECT queue_id, rel_path, is_dir FROM item WHERE id = ?", (item_id,)
    )
    parent = await cursor.fetchone()
    if parent is None:
        raise HTTPException(status_code=404, detail="item not found")
    if not parent["is_dir"]:
        return ItemChildrenResponse(children=[], total=0, limit=limit, offset=offset)

    queue_id = parent["queue_id"]
    prefix = f"{parent['rel_path']}/"

    count_cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM item WHERE queue_id = ? AND is_dir = 0 AND instr(rel_path, ?) = 1",
        (queue_id, prefix),
    )
    total_row = await count_cursor.fetchone()
    total = total_row["c"] if total_row is not None else 0

    cursor = await db.execute(
        f"SELECT {ITEM_VIEW_COLUMNS_QUALIFIED}, "  # noqa: S608 - module constants, not user input
        "settle.matched_scans AS settle_matched_scans, "
        "settle.updated_at AS settle_first_matched_at, "
        "settle.total_bytes AS settle_total_bytes, "
        "settle.first_observed_at AS settle_first_observed_at, "
        "settle.last_changed_at AS settle_last_changed_at, "
        "deleted_archive.deleted_at AS deleted_archive_at "
        "FROM item "
        "LEFT JOIN item_settle AS settle "
        "ON settle.queue_id = item.queue_id AND settle.rel_path = item.rel_path "
        "LEFT JOIN deleted_archive "
        "ON deleted_archive.queue_id = item.queue_id AND deleted_archive.rel_path = item.rel_path "
        "WHERE item.queue_id = ? AND item.is_dir = 0 AND instr(item.rel_path, ?) = 1 "
        "ORDER BY item.rel_path "
        "LIMIT ? OFFSET ?",
        (queue_id, prefix, limit, offset),
    )
    rows = await cursor.fetchall()
    return ItemChildrenResponse(
        children=[FileNode(**item_view(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/api/jobs/{job_id}/move-to-top", status_code=204)
async def move_to_top(job_id: int, request: Request) -> None:
    await request.app.state.queue.move_to_top(job_id)


@router.post("/api/jobs/{job_id}/move", status_code=204)
async def move_job(job_id: int, body: MoveJobRequest, request: Request) -> None:
    """The chevron reorder controls (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
    `prompts/2026-08-19-queue-reorder-chevrons.md`) -- one endpoint for **▲ up one**, **▼ down
    one**, and **▲▲ to top**, rather than three near-identical routes. `body.direction` outside
    `{'up', 'down', 'top'}` is a 422 for free via `MoveJobRequest`'s own `Literal`, never reaching
    this handler.

    An unknown `job_id` is a 404 (`ValueError`, matching every other not-found guard in this
    file); a `job_id` that exists but is no longer `queued` -- it started running, or reached a
    terminal state, between the page rendering its chevrons and the click landing -- is a 409
    (`core/queue.py.JobNotQueuedError`), the same "the job exists, the request just isn't valid
    for its current state" distinction `dismiss_job` above already draws. Already-at-the-edge
    (top row + `'up'`/`'top'`, bottom row + `'down'`) and a queue with only one job are silent
    no-ops, not errors -- `TransferQueue.move_job`'s own docstring.
    """
    try:
        await request.app.state.queue.move_job(job_id, body.direction)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobNotQueuedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/jobs/{job_id}/start-now", response_model=dict)
async def start_now(job_id: int, request: Request, body: StartNowRequest | None = None) -> dict:
    """ "Start now" (DESIGN.md §4.5), now a menu -- 10%/25%/50%/75%/Max of the site total limit
    (2026-08-19, prompts/done/2026-08-19-start-now-bandwidth-fractions.md). `body` omitted (no
    request payload at all -- every caller before this task) means Max, exactly as before;
    `rate_percent` outside `{10, 25, 50, 75, 100}` is a 422 for free via `StartNowRequest`'s own
    `Literal` field, never reaching this handler. A fraction with no site bandwidth limit
    configured is a 409 (`core/queue.py.NoSiteLimitConfiguredError`) -- see that class's own
    docstring for why this never silently substitutes Max instead.
    """
    rate_percent = body.rate_percent if body is not None else None
    try:
        applied = await request.app.state.queue.start_now(job_id, rate_percent=rate_percent)
    except NoSiteLimitConfiguredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueuePausedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"applied": applied}


@router.post("/api/queue/pause", status_code=204)
async def pause_queue(request: Request, body: QueuePauseRequest | None = None) -> None:
    """Transfers -> Queue tab's Pause control (this task, 2026-08-20,
    `prompts/2026-08-20-queue-pause.md`). `body` omitted (or `stop_running` omitted/`false`)
    is "pause after current" -- running jobs finish normally, nothing new is admitted. `{
    "stop_running": true }` is "pause now" -- additionally SIGTERMs every in-flight lftp child
    and returns it to `queued` at its same position, never `STOPPED`/suppressed (see
    `core/queue.py.TransferQueue.pause`'s own docstring for why `stop_job`'s §4.6 semantics
    would be wrong here). Idempotent: pausing an already-paused queue with `stop_running=true`
    still stops whatever is running at the moment of the call.

    `duration_minutes` (2026-08-21, `prompts/2026-08-21-pause-for-duration.md`): one of
    `{1, 10, 30, 60}`, or omitted/`null` for an indefinite pause (unchanged default). Converted
    to seconds here rather than in `core/queue.py`, which takes a plain `duration_s: float` so
    it has no opinion on what unit the API's dropdown happens to offer. Re-pausing an
    already-paused queue **replaces** whatever deadline was set before, it does not stack.
    """
    stop_running = body.stop_running if body is not None else False
    duration_s = (
        body.duration_minutes * 60
        if body is not None and body.duration_minutes is not None
        else None
    )
    await request.app.state.queue.pause(stop_running=stop_running, duration_s=duration_s)


@router.post("/api/queue/unpause", status_code=204)
async def unpause_queue(request: Request) -> None:
    """Resume admission immediately, in `queue_position` order -- see
    `core/queue.py.TransferQueue.unpause`.
    """
    await request.app.state.queue.unpause()


def _merge_preflight_rows(
    arr_rows: list[PreflightRow], settle_rows: list[PreflightRow]
) -> list[PreflightRow]:
    """Cross-source composition for `get_preflight` below -- the one place allowed to know both
    sources exist (`core/preflight.py`'s own docstring: nothing in that module may name either
    one). Two decisions, both made with the user (`docs/decisions.md`):

    **A settle row wins over an *arr row for the same release.** A settle-gated row means the
    bytes are actually on the seedbox, sized and fingerprinted; an *arr row only means the *arr's
    own queue mentions a download that hasn't reached here yet -- strictly less information about
    the identical release. In practice `core/arrsync.py._preflight_candidates` already excludes
    any record matching an existing `item` row regardless of state, and a settle-gated item
    always *is* one, so this only ever fires on a genuine title/attribution mismatch between the
    two sources -- cheap insurance against showing the same release twice, not the primary
    mechanism. Identity is `(queue_id, title)`, the same pair both sources already expose.

    **Ordering is alphabetical by title, case-insensitively, across the merged set** -- the same
    boring-default rule `ArrSyncScheduler.preflight_rows`/`AutoQueue.preflight_rows` each already
    apply within their own source, re-applied here so it stays true globally rather than grouping
    by source in an arbitrary order (arr-then-settle or the reverse would just move the same
    arbitrariness up a level).
    """
    settle_identities = {(r.queue_id, r.title) for r in settle_rows}
    filtered_arr = [r for r in arr_rows if (r.queue_id, r.title) not in settle_identities]
    merged = [*filtered_arr, *settle_rows]
    merged.sort(key=lambda r: r.title.casefold())
    return merged


@router.get("/api/queue/preflight", response_model=PreflightResponse)
async def get_preflight(request: Request) -> PreflightResponse:
    """The Queue tab's Preflight box (docs/transfers-redesign-spec.md §4, prefigured; this
    task's own handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its follow-up
    prompts/2026-08-20-preflight-waiting-sources.md) -- things lftpweb already knows about but
    has no work to do on yet. **A pure projection**, source-agnostic at this layer
    (`core/preflight.py.PreflightRow`) -- no table, no migration, nothing persisted.

    **Two sources are wired up: the *arr poller** (`core/arrsync.py.ArrSyncScheduler.
    preflight_rows`, fed by `_update_preflight`'s attribution rule and flap-tolerance hold) **and
    the settle gate's own eligibility check** (`core/autoqueue.py.AutoQueue.preflight_rows`, fed
    by `on_scan`'s wholesale-replace cache). Each is gated by its own live "is this source
    configured at all" query below -- an enabled *arr instance with an enabled bound queue for
    the first, the settle gate on plus at least one auto-queue-enabled queue for the second --
    ORed together into `source_configured` rather than either query changing the other's shape.
    `_merge_preflight_rows` above is where the two sources' rows meet: cross-source precedence
    and the final ordering, so neither `core/arrsync.py` nor `core/autoqueue.py` has to know the
    other exists.

    Every live query below is fresh, not read off either source's own cache, so a change (an
    instance disabled, a queue's auto-queue toggled off, the settle setting flipped) hides
    immediately rather than waiting for a cache to catch up -- `preflight_rows` on both sources
    is filtered to exactly these same live sets for the identical reason. `ArrSyncScheduler.
    preflight_rows` is now itself `async` for the same reason (2026-08-21, "eviction latency"):
    it re-asks "does a matching `item` exist" fresh on every call too, rather than only when the
    *arr poller happens to run, closing the poll-interval-sized latency the earlier evict-on-
    handover fix (`_update_preflight`'s own `retired` set) left behind. `source_configured =
    False` means "no row source is configured at all" -- the frontend hides the row list for
    that case rather than showing an empty "Nothing in preflight" that would be meaningless for a
    user with nothing configured (`gated_queues` is independent of this and can still be
    non-empty -- see `PreflightResponse`'s own docstring).
    """
    db = request.app.state.db
    cursor = await db.execute(
        "SELECT DISTINCT arr_instance.id FROM arr_instance "
        "JOIN path_queue ON path_queue.arr_instance_id = arr_instance.id "
        "WHERE arr_instance.enabled = 1 AND path_queue.enabled = 1"
    )
    enabled_instance_ids = {row["id"] for row in await cursor.fetchall()}

    arr_sync = getattr(request.app.state, "arr_sync", None)
    arr_rows = (
        await arr_sync.preflight_rows(enabled_instance_ids)
        if arr_sync is not None and enabled_instance_ids
        else []
    )

    # The settle-gated source (this task) -- `core/autoqueue.py.AutoQueue` owns the eligibility
    # query the settle gate itself sits inside, so it also owns this projection; see that
    # module's own "Preflight" section. `queue_names` doubles as the live "is this queue
    # currently auto-queue-eligible at all" set both for filtering settle rows and for naming
    # the mount-gate banner below -- one query answers both questions.
    engine = getattr(request.app.state, "engine", None)
    autoqueue = getattr(engine, "autoqueue", None) if engine is not None else None

    settle_settings = await settle.load_settle_settings(db)
    cursor = await db.execute(
        "SELECT id, name FROM path_queue WHERE enabled = 1 AND auto_queue_enabled = 1"
    )
    auto_queue_rows = await cursor.fetchall()
    queue_names = {row["id"]: row["name"] for row in auto_queue_rows}

    gated_ids = set(autoqueue.gated) if autoqueue is not None else set()
    active_settle_queue_ids = queue_names.keys() - gated_ids
    settle_rows = (
        autoqueue.preflight_rows(active_settle_queue_ids)
        if autoqueue is not None and settle_settings.enabled
        else []
    )
    settle_configured = settle_settings.enabled and bool(queue_names)

    rows = _merge_preflight_rows(arr_rows, settle_rows)

    # The mount-gate banner (decided with the user: one line per gated queue, never one row per
    # affected item) -- `AutoQueue.gated`'s own reason string, verbatim, named against a queue
    # still in `queue_names` so a queue disabled since it was last gated doesn't linger here.
    gated_queues = sorted(
        (
            PreflightGatedQueueOut(queue_name=queue_names[qid], reason=reason)
            for qid, reason in (autoqueue.gated.items() if autoqueue is not None else ())
            if qid in queue_names
        ),
        key=lambda g: g.queue_name.casefold(),
    )

    return PreflightResponse(
        source_configured=bool(enabled_instance_ids) or settle_configured,
        rows=[
            PreflightRowOut(
                source=r.source,
                queue_id=r.queue_id,
                queue_name=r.queue_name,
                queue_short_name=r.queue_short_name,
                title=r.title,
                status_label=r.status_label,
                source_label=r.source_label,
                source_kind=r.source_kind,
                size_bytes=r.size_bytes,
                size_remaining_bytes=r.size_remaining_bytes,
                remaining_s=r.remaining_s,
                download_client=r.download_client,
                wait_scans=r.wait_scans,
                wait_since=r.wait_since,
            )
            for r in rows
        ],
        gated_queues=gated_queues,
    )


@router.post("/api/items/{item_id}/stop", response_model=dict)
async def stop_item(item_id: int, request: Request) -> dict:
    applied = await request.app.state.queue.stop_item(item_id)
    return {"applied": applied}


async def _publish_item_delta(request: Request, db: Any, item_id: int) -> None:
    """The same "persist -> read back -> publish" idiom every writer in this codebase follows
    (DESIGN.md §2.2) -- factored out here because the source scope below is the first thing in
    `api/jobs.py` that mutates `item` outside of `local_delete`/`TransferQueue` (which already
    publish their own deltas). A no-op if the events bus or the row itself is unavailable, the
    same graceful-degradation shape `core/postprocess.py.PostprocessPipeline._publish` uses.
    """
    events = getattr(request.app.state, "events", None)
    if events is None:
        return
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is None:
        return
    events.publish({"type": "item_delta", "queue_id": row["queue_id"], "nodes": [item_view(row)]})


async def _delete_source_manual(
    request: Request, db: Any, item: Any, queue: Any
) -> tuple[bool, str]:
    """The manual delete dialog's **Source** scope (2026-08-16, the first manual remote-delete
    path in the API, `prompts/2026-08-16-manual-delete-local-and-remote.md`). Reuses
    `core/postprocess.py.perform_remote_delete` with `caller="manual"` -- itself built on
    `RemoteConnectionPool.delete_path`, never a second SSH-delete implementation -- so the event
    trail (`remote_delete`/`remote_delete_failed`) reads exactly like a ladder-authorized delete
    except for the message, which says "deleted by user request" instead of citing a verify
    rung. `remote_pool`/host resolution reuse `app.state.postprocess`'s own seam
    (`PostprocessPipeline.resolve_host()`) -- the identical closure `main.py` hands
    `TransferQueue`/`ArrSyncScheduler` too -- rather than a second `load_host_config` call built
    fresh at this layer.

    **Idempotent.** `item['remote_deleted_at']` already set -- the move-mode ladder beat this
    request to it, or an earlier manual call already ran -- short-circuits to a no-op success
    without an SSH round trip; `rm -rf` on an already-gone path is not an error either
    (`RemoteConnectionPool.delete_path`'s own docstring), this just skips asking. On a genuine
    success this also clears a stale `item.remote_delete_pending`, so "delete source for an item
    mid-ladder" really does complete the ladder early: `core/arrsync.py.
    _maybe_delete_remote_on_import`'s own guard already no-ops on `remote_deleted_at` being set,
    but clearing the pending marker too means a later History read never shows a *arr-import
    wait that no longer means anything.

    No existence check against `item['remote_size']` -- the frontend only shows the Source
    checkbox when `hasRemoteCopy(node)`, and every other delete in this codebase (`delete_path`
    itself, `perform_remote_delete`) is unconditional-and-idempotent rather than probe-first;
    adding a check here would be a second way to answer a question this codebase already
    answers by just trying.
    """
    item_id = item["id"]
    if item["remote_deleted_at"] is not None:
        return True, "source already deleted -- idempotent no-op"

    postprocess = getattr(request.app.state, "postprocess", None)
    if postprocess is None or postprocess.remote_pool is None:
        reason = "no remote connection available"
        await audit.record_event(
            db,
            level="error",
            item_id=item_id,
            kind="remote_delete_withheld",
            message=f"manual: source delete withheld -- {reason}",
        )
        return False, reason

    host = await postprocess.resolve_host()
    if host is None:
        reason = "no host configured"
        await audit.record_event(
            db,
            level="error",
            item_id=item_id,
            kind="remote_delete_withheld",
            message=f"manual: source delete withheld -- {reason}",
        )
        return False, reason

    remote_full = queue["remote_path"].rstrip("/") + "/" + item["rel_path"]
    ok = await perform_remote_delete(
        db,
        postprocess.remote_pool,
        host,
        item_id=item_id,
        queue_id=queue["id"],
        queue_name=queue["name"],
        remote_full=remote_full,
        caller="manual",
    )
    if not ok:
        return False, "remote delete failed -- see the event log for the exact error"

    await db.execute("UPDATE item SET remote_delete_pending = NULL WHERE id = ?", (item_id,))
    await db.commit()
    return True, "deleted"


@router.post("/api/items/{item_id}/delete", response_model=DeleteItemResponse)
async def delete_item(
    item_id: int, request: Request, body: DeleteItemRequest | None = None
) -> DeleteItemResponse:
    """Manual delete (DESIGN.md §9.2's Files page; `prompts/open-issues.md` "7 + 8" -- the
    first delete endpoint in this API). Two independent scopes, `body.local`/`body.source`
    (2026-08-16, the delete dialog's independent Local/Source checkboxes,
    `prompts/2026-08-16-manual-delete-local-and-remote.md`) -- an omitted body means exactly the
    pre-existing behavior, `local=True, source=False`, so every caller that predates this task
    is unaffected.

    **Local** (`require_nlink_guard=False`: a human clicking Delete on `LOCAL_ONLY` junk with
    exactly one copy is precisely the case the guard exists to *not* block -- the module's own
    docstring) is unchanged from before this task, including **stop first, then delete**
    (2026-08-13, `prompts/2026-08-13-delete-during-transfer.md`): `TransferQueue.stop_item()`
    (the identical stop path the Stop button drives, §4.6 -- SIGTERM, a grace period, SIGKILL,
    no second implementation) is always called before `delete_local`, run as a background task
    and awaited with a bound (`STOP_BEFORE_DELETE_TIMEOUT_S`) **without cancelling it on
    timeout** -- cancelling mid-stop would abandon `core/queue.py`'s own bookkeeping half-
    updated, which is exactly the inconsistency this exists to avoid. A timeout instead means
    this *request* stops waiting -- the stop keeps running in the background -- and the delete
    is withheld with a 409 naming the reason.

    **Source** (new) is the first *manual* remote-delete path in the API -- see
    `_delete_source_manual`'s own docstring for the mechanism. A **source-only** request
    (`local=False`) refuses (409) rather than stopping an active transfer itself: unlike local,
    which already has its own stop-then-delete two-step above, a bare source request has no
    "delete" of its own that would justify silently killing a live transfer, so it simply
    declines when one is running. A **combined** request (`local=True, source=True`, the delete
    dialog's default for a `move` queue) needs no separate check here -- local's own
    stop-then-delete already satisfies the guard, so by the time source runs any active job is
    already gone.

    **Ordering and partial failure.** Local runs first when requested: if it is withheld or
    fails, this raises 409 immediately and source is never attempted -- byte-for-byte the
    pre-existing single-scope behavior. If local succeeds (or was not requested) and source then
    fails, this does **not** raise: the local side effect, if any, already happened and cannot
    be undone, so a 409 here would misrepresent a request that partially succeeded. The response
    instead carries `source_deleted=False`/`source_reason` alongside `deleted=True` (or the
    source-only equivalent) -- 409 is reserved for a request that accomplished *nothing* at all.

    **Suppression.** A source-only success (`local=False`) marks the item
    `auto_queue_suppressed=1, suppressed_reason='deleted_source'` (migration 020) -- the same
    mechanism `core/local_delete.py.delete_local` already uses for a local delete, applied here
    because a source-only delete is most often reached exactly when an item still sits in an
    auto-queue-*eligible* state (`REMOTE_ONLY`/`PARTIAL` -- a failed or never-imported item, the
    dialog's own stated use case) and nothing else would stop a later reappearance under the same
    `rel_path` from being auto-queued straight back, undoing a deliberate cleanup action. A
    **combined** request deliberately skips this write -- `delete_local` already suppresses the
    row with `suppressed_reason='deleted_local'`, the more complete fact about a row whose local
    copy is also gone, and this must not stomp it back to a less-complete reason afterward.
    """
    scope = body if body is not None else DeleteItemRequest()
    if not scope.local and not scope.source:
        raise HTTPException(
            status_code=400, detail="at least one of local/source must be requested"
        )

    db = request.app.state.db
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    item = await cursor.fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (item["queue_id"],))
    queue = await cursor.fetchone()
    if queue is None:
        raise HTTPException(status_code=404, detail="item's queue no longer exists")

    local_outcome: local_delete.DeleteOutcome | None = None
    if scope.local:
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
            # Propagate a genuine failure from stop_item itself (e.g. the job row vanished
            # between the SELECT above and stop_item's own lookup -- ValueError, per stop_job's
            # contract) rather than silently swallowing it and proceeding to delete anyway.
            stop_task.result()

        postprocess = getattr(request.app.state, "postprocess", None)
        in_flight = postprocess.in_flight_item_ids() if postprocess is not None else frozenset()
        delete_in_flight = getattr(request.app.state, "delete_in_flight", None)

        local_outcome = await local_delete.delete_local(
            db,
            item=item,
            queue=queue,
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=in_flight,
            events=request.app.state.events,
            delete_in_flight=delete_in_flight,
        )
        if not local_outcome.deleted:
            raise HTTPException(status_code=409, detail=local_outcome.reason)
    else:
        # Source-only: refuse rather than stopping an active transfer ourselves -- see the
        # docstring's "Source" paragraph. Checked once, here, rather than inside
        # `_delete_source_manual`, since a combined request must never reach this check at all
        # (local's own stop-then-delete already satisfies it by the time source runs).
        cursor = await db.execute(
            "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
            (item_id,),
        )
        if await cursor.fetchone() is not None:
            reason = (
                f"an active transfer exists for {item['rel_path']!r} (queue {queue['id']} "
                f"'{queue['name']}') -- stop it first, or also delete the local copy (which "
                "stops it for you)"
            )
            await audit.record_event(
                db,
                level="error",
                item_id=item_id,
                kind="remote_delete_withheld",
                message=f"manual: source delete withheld -- {reason}",
            )
            raise HTTPException(status_code=409, detail=reason)

    source_deleted: bool | None = None
    source_reason: str | None = None
    if scope.source:
        source_deleted, source_reason = await _delete_source_manual(request, db, item, queue)
        if source_deleted and not scope.local:
            # Suppression (docstring's "Suppression" paragraph) -- a combined request skips this
            # deliberately, since `delete_local` above already suppressed the row with the more
            # complete 'deleted_local' reason.
            await db.execute(
                "UPDATE item SET auto_queue_suppressed = 1, suppressed_reason = 'deleted_source' "
                "WHERE id = ?",
                (item_id,),
            )
            await db.commit()
        if not source_deleted and not scope.local:
            raise HTTPException(status_code=409, detail=source_reason)
        await _publish_item_delta(request, db, item_id)

    return DeleteItemResponse(
        deleted=local_outcome.deleted if local_outcome is not None else bool(source_deleted),
        reason=local_outcome.reason if local_outcome is not None else (source_reason or ""),
        bytes_freed=local_outcome.bytes_freed if local_outcome is not None else None,
        source_deleted=source_deleted,
        source_reason=source_reason,
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


@router.post("/api/items/{item_id}/resolve", response_model=ResolveItemResponse)
async def resolve_item(
    item_id: int, request: Request, body: ResolveItemRequest | None = None
) -> ResolveItemResponse:
    """**Manually resolve a wedged row out of the Queue tab's Active/pending box** (2026-08-20,
    docs/transfers-redesign-spec.md §3.2's pipeline-completion rule, migration 025). The Active
    box now holds a row until its *whole* pipeline finishes; every blocking condition has a
    bounded automatic exit (`core/pipeline_flight.py`), but automatic exits are necessary rather
    than sufficient, and a box that can accumulate rows nothing is working on stops being
    trustworthy. This is the human override of last resort.

    **It is a CLASSIFICATION ONLY.** It writes exactly two columns, `item.manual_outcome` and
    `item.manual_outcome_at`, which are read by exactly one thing: the split predicate. It must
    never be read as evidence by any other subsystem -- not as a confirmed *arr import, not as a
    rung on the `move`-mode delete ladder, not as a trigger for notify/cleanup/retention/post-
    processing, not as an input to auto-queue's eligibility. Neither `core/postprocess.py` nor
    `core/arrsync.py` reads either column, deliberately; migration 025's own comment says so at
    length, and DESIGN.md §7.3 explains why the bar for the irreversible step is a *confirmed*
    import held across two consecutive poller passes rather than a button click.

    **Refused (409) while the item's lftp job is still `queued`/`running`.** A transfer that is
    genuinely running is not something a classification button gets to hide -- Stop is the control
    for that -- and the predicate itself refuses to let a manual outcome override an active job,
    so allowing the write here would just produce a resolution that appeared to do nothing.

    **Undo.** `outcome: null` clears both columns and puts the row straight back through the
    normal predicate. That is the answer to "resolved by mistake."

    **A real terminal outcome does not supersede a manual one** (decided 2026-08-20,
    `docs/decisions.md`). If the *arr does import the release an hour later, the manual outcome
    stands and the row stays filed. The alternative -- a real outcome clearing the manual flag --
    can only ever move a row *back into Active* after the user deliberately filed it, which is
    exactly the "this box can't be trusted" failure this whole change exists to fix; and nothing
    is lost by standing pat, because the real outcome is still written to the item's own columns,
    still drawn on the row's *arr chip, and still in the Events page's forensic trail.

    Every call writes an audit event (`core/audit.py`) -- a human overriding the system's own
    judgement belongs in the same forensic trail as a remote delete or a withheld delete.
    """
    scope = body if body is not None else ResolveItemRequest()
    db = request.app.state.db
    cursor = await db.execute(
        "SELECT id, rel_path, queue_id, manual_outcome FROM item WHERE id = ?", (item_id,)
    )
    item = await cursor.fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    cursor = await db.execute(
        "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
        (item_id,),
    )
    if await cursor.fetchone() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{item['rel_path']!r} still has a queued or running transfer -- stop it first "
                "if you want it out of Active/pending; marking it resolved would not hide a job "
                "that is genuinely running"
            ),
        )

    now = _now_iso()
    await db.execute(
        "UPDATE item SET manual_outcome = ?, manual_outcome_at = ? WHERE id = ?",
        (scope.outcome, now if scope.outcome is not None else None, item_id),
    )
    await db.commit()

    previous = item["manual_outcome"]
    if scope.outcome is None:
        message = (
            f"manual: cleared the manual resolution ({previous!r}) on {item['rel_path']!r} -- "
            "the row is classified by the pipeline again"
        )
        kind = "manual_resolution_cleared"
    else:
        message = (
            f"manual: marked {item['rel_path']!r} {scope.outcome!r} -- a classification only, "
            "filing the row under Complete; no source delete, no *arr import, no post-processing "
            "and no cleanup is implied or performed by this"
        )
        kind = "manual_resolution_set"
    await audit.record_event(db, level="info", item_id=item_id, kind=kind, message=message)

    await _publish_item_delta(request, db, item_id)
    return ResolveItemResponse(
        item_id=item_id,
        manual_outcome=scope.outcome,
        manual_outcome_at=now if scope.outcome is not None else None,
    )


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
