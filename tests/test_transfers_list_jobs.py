"""`TransferQueue.list_jobs()` and `_publish_item_state` — no lftp process needed, so unlike
`tests/test_queue.py` (which needs the real fake seedbox for spawn/watch/reap coverage) these
run in the normal `uv run pytest`, no `docker compose` required.

Covers two phase 3b changes:

- `list_jobs()` was broadened beyond `queued`/`running` — DESIGN.md §9.2 explicitly requires
  the Transfers page to show a failed row's error class/output tail and requires "stop it and
  see it go STOPPED" without a page refresh, neither of which the phase 3a query (`state IN
  ('queued','running')`) could ever produce, since a stopped/failed job is immediately
  excluded the instant it's reaped. See docs/decisions.md for the full reasoning.
- `_publish_item_state` (the WS delta fix, DESIGN.md §2/§9) — publishes exactly one item row,
  not the tree, whenever a lifecycle transition happens outside a scan.
"""

from __future__ import annotations

import aiosqlite
import pydantic
import pytest

from lftpweb.api import jobs
from lftpweb.api.jobs import _job_out
from lftpweb.core.events import EventBus
from lftpweb.core.queue import JobNotDismissableError, TransferQueue
from lftpweb.db import migrate
from lftpweb.models import DismissAllRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_queue(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str, *, state: str = "QUEUED") -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, 100, 0, ?)",
        (queue_id, rel_path, state),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_job(db, item_id: int, *, state: str, attempt: int = 1) -> int:
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt) VALUES (?, 'pget', ?, 'main', 0, ?)",
        (item_id, state, attempt),
    )
    await db.commit()
    return cursor.lastrowid


def _queue(db) -> TransferQueue:
    return TransferQueue(db, "/config", EventBus())


async def _set_finished_at(db, job_id: int, finished_at: str) -> None:
    await db.execute("UPDATE job SET finished_at = ? WHERE id = ?", (finished_at, job_id))
    await db.commit()


async def test_list_jobs_includes_queued_and_running(db):
    queue_id = await _make_queue(db)
    item1 = await _make_item(db, queue_id, "a.txt")
    item2 = await _make_item(db, queue_id, "b.txt")
    await _make_job(db, item1, state="queued")
    await _make_job(db, item2, state="running")

    jobs = await _queue(db).list_jobs()
    assert {j["state"] for j in jobs} == {"queued", "running"}


async def test_list_jobs_includes_most_recent_failed_job(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    await _make_job(db, item, state="failed")

    jobs = await _queue(db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["rel_path"] == "failed.txt"


async def test_list_jobs_includes_most_recent_cancelled_job_stopped_stays_visible(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "stopped.txt", state="STOPPED")
    await _make_job(db, item, state="cancelled")

    jobs = await _queue(db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "cancelled"


async def test_list_jobs_older_failed_attempt_superseded_by_a_fresh_requeue(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "retried.txt", state="QUEUED")
    await _make_job(db, item, state="failed", attempt=1)
    second_job = await _make_job(db, item, state="queued", attempt=2)

    jobs = await _queue(db).list_jobs()
    # Only the fresh retry shows -- the old failed attempt for the same item is superseded,
    # not a second row for the same item.
    assert len(jobs) == 1
    assert jobs[0]["id"] == second_job
    assert jobs[0]["state"] == "queued"


async def test_list_jobs_includes_most_recent_succeeded_job_and_unrelated_older_failures(db):
    """2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md): `succeeded` joined the
    terminal set this method surfaces -- before this a job that finished cleanly vanished from
    Transfers the instant it was reaped, which is what made a real, seven-minute-long transfer
    look from the UI like nothing running and 0 B/s. Same `MAX(id)` superseding rule as
    `failed`/`cancelled` already had: `old-history.txt`'s older `failed` attempt is superseded
    by its own later `succeeded` retry, not shown alongside it.
    """
    queue_id = await _make_queue(db)
    succeeded_item = await _make_item(db, queue_id, "done.txt", state="DOWNLOADED")
    await _make_job(db, succeeded_item, state="succeeded")

    other_item = await _make_item(db, queue_id, "also-failed.txt", state="FAILED")
    await _make_job(db, other_item, state="failed", attempt=1)
    # A superseded failure on a *different* item, followed by a succeeded retry -- only the
    # succeeded retry should show for this item.
    third_item = await _make_item(db, queue_id, "old-history.txt", state="DOWNLOADED")
    await _make_job(db, third_item, state="failed", attempt=1)
    await _make_job(db, third_item, state="succeeded", attempt=2)

    jobs = await _queue(db).list_jobs()
    by_path = {j["rel_path"]: j["state"] for j in jobs}
    assert by_path == {
        "done.txt": "succeeded",
        "also-failed.txt": "failed",
        "old-history.txt": "succeeded",
    }


async def test_list_jobs_bytes_total_reflects_the_value_fixed_at_spawn_not_current_item_remote_size(
    db,
):
    """Defect 4 (prompts/2026-08-14-exit-zero-is-not-completion.md): a live incident returned
    `bytes_total: 31812118603` alongside `bytes_done: 38841560420` for the same job -- two
    different denominators, because `job.bytes_total` was never persisted at spawn and the API
    fell back to the *current* `item.remote_size`, which had since drifted away from whatever
    `job.bytes_done` was actually measured against. `core/queue.py._spawn_decision` now freezes
    `job.bytes_total` at spawn (`fixed at admission, never re-shaped`, DESIGN.md §4.5's own
    invariant applied to this field too); `list_jobs()` must return that frozen value, not a
    fresh join against `item.remote_size`, once it's been persisted.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "drifted.txt", state="DOWNLOADED")
    job_id = await _make_job(db, item, state="succeeded")
    await db.execute("UPDATE job SET bytes_total = 1000, bytes_done = 1000 WHERE id = ?", (job_id,))
    # The item's own remote_size has since drifted (a later scan, a pattern edit) -- the job's
    # own frozen total must not follow it.
    await db.execute("UPDATE item SET remote_size = 500 WHERE id = ?", (item,))
    await db.commit()

    jobs = await _queue(db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["bytes_total"] == 1000
    assert jobs[0]["bytes_done"] == 1000
    assert jobs[0]["bytes_done"] <= jobs[0]["bytes_total"]


# --- api/jobs.py._job_out: the same defect 4 fix, at the HTTP-shape layer -------------------


def _job_out_row(**overrides) -> dict:
    base = dict(
        id=1,
        item_id=1,
        queue_id=1,
        queue_name="q",
        queue_short_name=None,
        rel_path="a.txt",
        is_dir=False,
        kind="pget",
        state="succeeded",
        lane="main",
        rank=0.0,
        attempt=1,
        queued_at="2026-08-14T00:00:00.000000Z",
        started_at="2026-08-14T00:00:00.000000Z",
        finished_at="2026-08-14T00:01:00.000000Z",
        pid=None,
        rate_limit_bps=None,
        forced_full_rate=0,
        bytes_start=0,
        bytes_done=1000,
        bytes_total=1000,  # job.bytes_total, persisted at spawn (this task)
        remote_size=500,  # item.remote_size -- has since drifted away from bytes_total
        exit_code=0,
        error_class=None,
        output_tail=None,
        # 2026-08-15: the panel fields `_job_out` also reads off the row -- see
        # test_list_jobs_carries_item_processing_and_arr_facts above for the join that
        # populates these on a real `list_jobs()` row.
        verified_at=None,
        extracted_at=None,
        remote_deleted_at=None,
        arr_status=None,
        arr_status_at=None,
        arr_instance_name=None,
        # 2026-08-16 (prompts/2026-08-16-arr-chip-on-row-lines.md): the row chip's brand-logo
        # choice -- see test_list_jobs_carries_item_processing_and_arr_facts below.
        arr_instance_kind=None,
    )
    base.update(overrides)
    return base


def test_job_out_prefers_persisted_bytes_total_over_live_item_remote_size():
    """Defect 4: `job.bytes_total` (frozen at spawn) must win over `item.remote_size` (which
    can drift after spawn) -- this is the exact pair (`bytes_total: ...603`,
    `bytes_done: ...420`, the second exceeding the first) the live incident returned.
    """
    out = _job_out(_job_out_row())
    assert out.bytes_total == 1000
    assert out.bytes_done <= out.bytes_total


def test_job_out_falls_back_to_item_remote_size_when_job_bytes_total_is_null():
    """A `queued` job hasn't spawned yet, so `job.bytes_total` is still `NULL` -- the live
    `item.remote_size` is the best estimate available before admission freezes anything.
    """
    out = _job_out(_job_out_row(bytes_total=None, state="queued"))
    assert out.bytes_total == 500


# --- _publish_item_state: one row, not the tree ---------------------------------------------


async def test_publish_item_state_emits_exactly_one_row(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "watched.txt", state="QUEUED")

    q = _queue(db)
    subscription = q.events.subscribe()
    await q._publish_item_state(item)  # noqa: SLF001 - the thing under test

    message = subscription.get_nowait()
    assert message["type"] == "item_delta"
    assert message["queue_id"] == queue_id
    assert len(message["nodes"]) == 1
    assert message["nodes"][0]["rel_path"] == "watched.txt"
    assert message["nodes"][0]["state"] == "QUEUED"


# --- stop_item: stop-by-item, for the Files page (which never sees a job id) ---------------


async def test_stop_item_queued_job_marks_it_cancelled_and_stopped(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="QUEUED")
    job_id = await _make_job(db, item, state="queued")

    q = _queue(db)
    applied = await q.stop_item(item)
    assert applied is True

    job_row = await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
    assert (await job_row.fetchone())["state"] == "cancelled"
    item_row = await db.execute(
        "SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item,)
    )
    row = await item_row.fetchone()
    assert row["state"] == "STOPPED"
    assert row["auto_queue_suppressed"] == 1


async def test_stop_item_with_no_active_job_is_a_no_op(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "idle.txt", state="REMOTE_ONLY")

    applied = await _queue(db).stop_item(item)
    assert applied is False


async def test_publish_item_state_missing_item_is_a_no_op(db):
    q = _queue(db)
    subscription = q.events.subscribe()
    await q._publish_item_state(999999)  # noqa: SLF001
    assert subscription.empty()


# --- dismiss_job: display-only, terminal-only (2026-08-13,
# prompts/done/2026-08-13-dismiss-terminal-jobs.md) -------------------------------------------


async def test_dismiss_job_removes_a_failed_row_from_list_jobs(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "gone.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")

    q = _queue(db)
    await q.dismiss_job(job_id)

    assert await q.list_jobs() == []


async def test_dismiss_job_removes_a_cancelled_row_from_list_jobs(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "stopped.txt", state="STOPPED")
    job_id = await _make_job(db, item, state="cancelled")

    q = _queue(db)
    await q.dismiss_job(job_id)

    assert await q.list_jobs() == []


async def test_dismiss_job_removes_a_succeeded_row_from_list_jobs(db):
    """2026-08-14: `succeeded` joined the dismissable set alongside `list_jobs()` starting to
    surface a recently-succeeded job at all -- a completed transfer needs the same
    "stop showing this row" action a failed or stopped one already had.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "done.txt", state="DOWNLOADED")
    job_id = await _make_job(db, item, state="succeeded")

    q = _queue(db)
    assert len(await q.list_jobs()) == 1
    await q.dismiss_job(job_id)

    assert await q.list_jobs() == []


async def test_dismiss_job_rejects_a_queued_job(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt")
    job_id = await _make_job(db, item, state="queued")

    q = _queue(db)
    with pytest.raises(JobNotDismissableError):
        await q.dismiss_job(job_id)
    # Rejected, not silently ignored -- still visible afterward.
    assert len(await q.list_jobs()) == 1


async def test_dismiss_job_rejects_a_running_job(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="DOWNLOADING")
    job_id = await _make_job(db, item, state="running")

    q = _queue(db)
    with pytest.raises(JobNotDismissableError):
        await q.dismiss_job(job_id)
    assert len(await q.list_jobs()) == 1


async def test_dismiss_job_unknown_id_raises_value_error(db):
    with pytest.raises(ValueError, match="not found"):
        await _queue(db).dismiss_job(999999)


async def test_dismiss_job_does_not_touch_item_state_or_suppression(db):
    """The load-bearing guarantee (task's own wording): dismiss is a display action about the
    job row, never a decision about the item. A REMOTE_GONE item's suppression must survive a
    dismiss untouched -- undoing it here would silently re-enable auto-queue for an item whose
    remote is actually gone.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "remote-gone.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")
    await db.execute(
        "UPDATE item SET auto_queue_suppressed = 1, suppressed_reason = 'permanent_error', "
        "error_class = 'REMOTE_GONE' WHERE id = ?",
        (item,),
    )
    await db.commit()

    await _queue(db).dismiss_job(job_id)

    row = await (
        await db.execute(
            "SELECT state, auto_queue_suppressed, suppressed_reason, error_class FROM item WHERE id = ?",
            (item,),
        )
    ).fetchone()
    assert row["state"] == "FAILED"
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "permanent_error"
    assert row["error_class"] == "REMOTE_GONE"


# --- dismiss_all_terminal: the bulk counterpart (2026-08-15,
# prompts/2026-08-15-transfers-single-line-rows-with-detail.md) ----------------------------


async def test_dismiss_all_terminal_dismisses_every_terminal_job(db):
    queue_id = await _make_queue(db)
    failed_item = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    failed_job = await _make_job(db, failed_item, state="failed")
    cancelled_item = await _make_item(db, queue_id, "stopped.txt", state="STOPPED")
    cancelled_job = await _make_job(db, cancelled_item, state="cancelled")
    succeeded_item = await _make_item(db, queue_id, "done.txt", state="DOWNLOADED")
    succeeded_job = await _make_job(db, succeeded_item, state="succeeded")

    q = _queue(db)
    assert len(await q.list_jobs()) == 3

    dismissed = await q.dismiss_all_terminal()
    assert dismissed == 3
    assert await q.list_jobs() == []

    for job_id in (failed_job, cancelled_job, succeeded_job):
        row = await (
            await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert row["dismissed_at"] is not None


async def test_dismiss_all_terminal_never_touches_an_active_job(db):
    """The load-bearing guarantee: a lone `queued`/`running` job must survive a "Dismiss all"
    click untouched -- this is the bulk endpoint's whole reason for a `WHERE` clause rather than
    an unconditional `UPDATE job SET dismissed_at = ?`.
    """
    queue_id = await _make_queue(db)
    queued_item = await _make_item(db, queue_id, "a.txt", state="QUEUED")
    queued_job = await _make_job(db, queued_item, state="queued")
    running_item = await _make_item(db, queue_id, "b.txt", state="DOWNLOADING")
    running_job = await _make_job(db, running_item, state="running")
    failed_item = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    await _make_job(db, failed_item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal()
    assert dismissed == 1

    jobs = await q.list_jobs()
    assert {j["state"] for j in jobs} == {"queued", "running"}
    for job_id in (queued_job, running_job):
        row = await (
            await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert row["dismissed_at"] is None


async def test_dismiss_all_terminal_is_a_no_op_when_nothing_is_dismissable(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="QUEUED")
    await _make_job(db, item, state="queued")

    assert await _queue(db).dismiss_all_terminal() == 0


# --- dismiss_all_terminal(queue_id=...): the group-header "Dismiss Queue" scope (2026-08-17,
# prompts/2026-08-17-transfers-dismiss-per-queue.md) ---------------------------------------


async def test_dismiss_all_terminal_scoped_to_one_queue_leaves_other_queue_untouched(db):
    queue_a = await _make_queue(db)
    queue_b = await _make_queue(db)
    item_a = await _make_item(db, queue_a, "a.txt", state="FAILED")
    job_a = await _make_job(db, item_a, state="failed")
    item_b = await _make_item(db, queue_b, "b.txt", state="FAILED")
    job_b = await _make_job(db, item_b, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(queue_id=queue_a)
    assert dismissed == 1

    row_a = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_a,))
    ).fetchone()
    row_b = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_b,))
    ).fetchone()
    assert row_a["dismissed_at"] is not None
    assert row_b["dismissed_at"] is None


async def test_dismiss_all_terminal_omitted_queue_id_still_dismisses_across_queues(db):
    queue_a = await _make_queue(db)
    queue_b = await _make_queue(db)
    item_a = await _make_item(db, queue_a, "a.txt", state="FAILED")
    job_a = await _make_job(db, item_a, state="failed")
    item_b = await _make_item(db, queue_b, "b.txt", state="FAILED")
    job_b = await _make_job(db, item_b, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal()
    assert dismissed == 2

    for job_id in (job_a, job_b):
        row = await (
            await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert row["dismissed_at"] is not None


async def test_dismiss_all_terminal_unknown_queue_id_dismisses_nothing(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(queue_id=999999)
    assert dismissed == 0

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
    ).fetchone()
    assert row["dismissed_at"] is None


# --- dismiss_all_terminal(job_ids=...): the Transfers page's name filter + "Dismiss list"
# button (2026-08-19, prompts/2026-08-19-transfers-name-filter.md) -------------------------


async def test_dismiss_all_terminal_job_ids_dismisses_only_those_rows(db):
    queue_id = await _make_queue(db)
    item_a = await _make_item(db, queue_id, "a.txt", state="FAILED")
    job_a = await _make_job(db, item_a, state="failed")
    item_b = await _make_item(db, queue_id, "b.txt", state="STOPPED")
    job_b = await _make_job(db, item_b, state="cancelled")
    item_c = await _make_item(db, queue_id, "c.txt", state="DOWNLOADED")
    job_c = await _make_job(db, item_c, state="succeeded")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(job_ids=[job_a, job_b])
    assert dismissed == 2

    row_a = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_a,))
    ).fetchone()
    row_b = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_b,))
    ).fetchone()
    row_c = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_c,))
    ).fetchone()
    assert row_a["dismissed_at"] is not None
    assert row_b["dismissed_at"] is not None
    assert row_c["dismissed_at"] is None


async def test_dismiss_all_terminal_empty_job_ids_dismisses_nothing(db):
    """The dangerous edge this task's own instruction names explicitly: `[]` must never degrade
    into "no filter, so dismiss everything" -- it means the filter matched zero dismissable
    rows, and the correct answer to that is zero dismissed, not every terminal row in the db.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(job_ids=[])
    assert dismissed == 0

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
    ).fetchone()
    assert row["dismissed_at"] is None


async def test_dismiss_all_terminal_job_ids_never_overrides_the_terminal_state_guard(db):
    """`job_ids` is a *narrowing* of the existing terminal-state `WHERE`, never an override of
    it -- an id naming a still-active job must not be dismissed just because the client asked
    for it by id.
    """
    queue_id = await _make_queue(db)
    queued_item = await _make_item(db, queue_id, "a.txt", state="QUEUED")
    queued_job = await _make_job(db, queued_item, state="queued")
    failed_item = await _make_item(db, queue_id, "b.txt", state="FAILED")
    failed_job = await _make_job(db, failed_item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(job_ids=[queued_job, failed_job])
    assert dismissed == 1

    queued_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (queued_job,))
    ).fetchone()
    failed_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (failed_job,))
    ).fetchone()
    assert queued_row["dismissed_at"] is None
    assert failed_row["dismissed_at"] is not None


async def test_dismiss_all_terminal_job_ids_none_still_behaves_like_every_existing_call(db):
    """Every existing no-body call (`job_ids` never passed, defaulting to `None`) must keep
    behaving identically -- the same coverage `test_dismiss_all_terminal_dismisses_every_
    terminal_job` above already gives the `queue_id`-only path, repeated here for the
    `job_ids` default specifically.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")

    dismissed = await _queue(db).dismiss_all_terminal(job_ids=None)
    assert dismissed == 1

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
    ).fetchone()
    assert row["dismissed_at"] is not None


async def test_dismiss_all_request_rejects_job_ids_and_queue_id_together(db):
    """The mutual-exclusivity rule is enforced by `DismissAllRequest`'s own Pydantic validator
    (`models.py`), not in the endpoint body -- constructing the model with both set raises
    `ValidationError`, which FastAPI turns into a 422 for any real HTTP caller.
    """
    with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
        DismissAllRequest(queue_id=1, job_ids=[1, 2])


# --- outcome: the Complete box's "Dismiss" menu (2026-08-20, follow-up to phase 1 stage 4b,
# prompts/2026-08-20-transfers-dismiss-menu-and-counts.md) -- decided: `outcome` composes with
# `name_filter` (both are narrowings of the same set), but stays mutually exclusive with
# `job_ids`/`queue_id` (each of those already names an explicit/whole-queue scope). ------------


async def test_dismiss_all_request_rejects_outcome_with_job_ids():
    with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
        DismissAllRequest(outcome="failed", job_ids=[1])


async def test_dismiss_all_request_rejects_outcome_with_queue_id():
    with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
        DismissAllRequest(outcome="failed", queue_id=1)


async def test_dismiss_all_request_allows_outcome_with_name_filter():
    """The decided composition: unlike `job_ids`/`queue_id`, `outcome` and `name_filter` may be
    given together without raising -- "dismiss the failed ones matching `Married`" is a coherent
    request, not a scope conflict.
    """
    req = DismissAllRequest(outcome="failed", name_filter="married")
    assert req.outcome == "failed"
    assert req.name_filter == "married"
    assert req.job_ids is None
    assert req.queue_id is None


async def test_dismiss_all_request_outcome_alone_is_valid():
    req = DismissAllRequest(outcome="succeeded")
    assert req.outcome == "succeeded"


async def test_dismiss_all_request_rejects_unknown_outcome_value():
    """`outcome` is a closed `Literal` -- the same three states `isDismissable`/`dismiss_job`
    already allow, not an arbitrary string that could silently match zero rows forever.
    """
    with pytest.raises(pydantic.ValidationError):
        DismissAllRequest(outcome="queued")


class _FakeQueueApp:
    def __init__(self, queue):
        self.state = _FakeQueueState(queue)


class _FakeQueueState:
    def __init__(self, queue):
        self.queue = queue


class _FakeQueueRequest:
    def __init__(self, queue):
        self.app = _FakeQueueApp(queue)


async def test_dismiss_all_jobs_endpoint_threads_job_ids_through_to_the_queue(db):
    """The route-level wiring (`api/jobs.py.dismiss_all_jobs`) -- `body.job_ids` must reach
    `TransferQueue.dismiss_all_terminal` unchanged, exercised end to end against a real
    `TransferQueue` rather than a mock, the same "the thing under test is the route's own
    wiring" shape `tests/test_delete_api.py` already establishes for this router.
    """
    queue_id = await _make_queue(db)
    item_a = await _make_item(db, queue_id, "a.txt", state="FAILED")
    job_a = await _make_job(db, item_a, state="failed")
    item_b = await _make_item(db, queue_id, "b.txt", state="FAILED")
    job_b = await _make_job(db, item_b, state="failed")

    q = _queue(db)
    result = await jobs.dismiss_all_jobs(_FakeQueueRequest(q), DismissAllRequest(job_ids=[job_a]))
    assert result.dismissed == 1

    row_a = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_a,))
    ).fetchone()
    row_b = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_b,))
    ).fetchone()
    assert row_a["dismissed_at"] is not None
    assert row_b["dismissed_at"] is None


# --- list_jobs()/_job_out: the 2026-08-15 panel fields (verified_at/extracted_at/
# remote_deleted_at/arr_status/arr_status_at/arr_instance_name) --------------------------------


async def _seed_arr_instance(db, *, name: str = "Sonarr") -> int:
    now = "2026-08-15T00:00:00.000000Z"
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
        "notify_on_complete, created_at, updated_at) VALUES (?, 'sonarr', 'https://sonarr.test', "
        "'enc', 1, 0, ?, ?)",
        (name, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def test_list_jobs_carries_item_processing_and_arr_facts(db):
    queue_id = await _make_queue(db)
    instance_id = await _seed_arr_instance(db)
    await db.execute(
        "UPDATE path_queue SET arr_instance_id = ? WHERE id = ?", (instance_id, queue_id)
    )
    item = await _make_item(db, queue_id, "release", state="EXTRACTED")
    await db.execute(
        "UPDATE item SET verified_at = ?, extracted_at = ?, remote_deleted_at = ?, "
        "arr_status = 'imported', arr_status_at = ? WHERE id = ?",
        (
            "2026-08-15T01:00:00.000000Z",
            "2026-08-15T01:05:00.000000Z",
            "2026-08-15T01:06:00.000000Z",
            "2026-08-15T01:07:00.000000Z",
            item,
        ),
    )
    await db.commit()
    await _make_job(db, item, state="succeeded")

    jobs = await _queue(db).list_jobs()
    assert len(jobs) == 1
    row = jobs[0]
    assert row["verified_at"] == "2026-08-15T01:00:00.000000Z"
    assert row["extracted_at"] == "2026-08-15T01:05:00.000000Z"
    assert row["remote_deleted_at"] == "2026-08-15T01:06:00.000000Z"
    assert row["arr_status"] == "imported"
    assert row["arr_status_at"] == "2026-08-15T01:07:00.000000Z"
    assert row["arr_instance_name"] == "Sonarr"
    # 2026-08-16 (prompts/2026-08-16-arr-chip-on-row-lines.md): `kind` drives the row chip's
    # brand-logo choice -- `arr_instance_name` alone (free text) can't.
    assert row["arr_instance_kind"] == "sonarr"

    out = _job_out(row)
    assert out.verified_at == "2026-08-15T01:00:00.000000Z"
    assert out.arr_instance_name == "Sonarr"
    assert out.arr_instance_kind == "sonarr"


async def test_list_jobs_arr_instance_name_is_null_when_queue_has_no_bound_instance(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "release", state="DOWNLOADED")
    await _make_job(db, item, state="succeeded")

    jobs = await _queue(db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["arr_instance_name"] is None
    assert jobs[0]["arr_instance_kind"] is None
    assert jobs[0]["arr_status"] is None

    out = _job_out(jobs[0])
    assert out.arr_instance_name is None
    assert out.arr_instance_kind is None
    assert out.arr_status is None


async def test_retry_after_dismiss_produces_a_fresh_job_visible_again(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "retry-me.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")

    q = _queue(db)
    await q.dismiss_job(job_id)
    assert await q.list_jobs() == []

    new_job_id = await q.retry_item(item)

    jobs = await q.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == new_job_id
    assert jobs[0]["state"] == "queued"
    # Retry clears suppression regardless of the dismiss that preceded it (unchanged
    # behaviour -- enqueue_item's own contract) -- this is the "actually, try again" path.
    item_row = await (
        await db.execute(
            "SELECT auto_queue_suppressed, suppressed_reason FROM item WHERE id = ?", (item,)
        )
    ).fetchone()
    assert item_row["auto_queue_suppressed"] == 0
    assert item_row["suppressed_reason"] is None


# --- list_complete_jobs: the Queue tab's Complete box (2026-08-19,
# docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b, prompts/2026-08-19-transfers-
# paginated-boxes.md) -- terminal jobs, server-side paginated and filtered. ------------------


async def test_list_complete_jobs_excludes_active_jobs(db):
    queue_id = await _make_queue(db)
    queued_item = await _make_item(db, queue_id, "a.txt", state="QUEUED")
    await _make_job(db, queued_item, state="queued")
    running_item = await _make_item(db, queue_id, "b.txt", state="DOWNLOADING")
    await _make_job(db, running_item, state="running")
    failed_item = await _make_item(db, queue_id, "c.txt", state="FAILED")
    await _make_job(db, failed_item, state="failed")

    rows, total = await _queue(db).list_complete_jobs(limit=50, offset=0)
    assert total == 1
    assert [r["rel_path"] for r in rows] == ["c.txt"]


async def test_list_complete_jobs_excludes_dismissed_rows(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "gone.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")
    q = _queue(db)
    await q.dismiss_job(job_id)

    rows, total = await q.list_complete_jobs(limit=50, offset=0)
    assert total == 0
    assert rows == []


async def test_list_complete_jobs_excludes_superseded_terminal_job_after_retry(db):
    """Same `MAX(id)`-per-item rule `list_jobs()` uses for its own terminal rows -- an item
    that's been retried since its last terminal job must not show its old, superseded attempt
    here, so a row is never visible in both the Active/pending box and this one at once.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "retried.txt", state="QUEUED")
    await _make_job(db, item, state="failed", attempt=1)
    await _make_job(db, item, state="queued", attempt=2)

    rows, total = await _queue(db).list_complete_jobs(limit=50, offset=0)
    assert total == 0
    assert rows == []


async def test_list_complete_jobs_orders_newest_finished_first(db):
    queue_id = await _make_queue(db)
    older_item = await _make_item(db, queue_id, "older.txt", state="FAILED")
    older_job = await _make_job(db, older_item, state="failed")
    await _set_finished_at(db, older_job, "2026-08-19T01:00:00.000000Z")
    newer_item = await _make_item(db, queue_id, "newer.txt", state="DOWNLOADED")
    newer_job = await _make_job(db, newer_item, state="succeeded")
    await _set_finished_at(db, newer_job, "2026-08-19T02:00:00.000000Z")

    rows, total = await _queue(db).list_complete_jobs(limit=50, offset=0)
    assert total == 2
    assert [r["rel_path"] for r in rows] == ["newer.txt", "older.txt"]


async def test_list_complete_jobs_paginates(db):
    queue_id = await _make_queue(db)
    for i in range(5):
        item = await _make_item(db, queue_id, f"item-{i}.txt", state="FAILED")
        job_id = await _make_job(db, item, state="failed")
        await _set_finished_at(db, job_id, f"2026-08-19T0{i}:00:00.000000Z")

    q = _queue(db)
    page1, total1 = await q.list_complete_jobs(limit=2, offset=0)
    page2, total2 = await q.list_complete_jobs(limit=2, offset=2)
    page3, total3 = await q.list_complete_jobs(limit=2, offset=4)
    assert total1 == total2 == total3 == 5
    # Newest (item-4) first, oldest (item-0) last -- three pages of 2/2/1, no overlap.
    assert [r["rel_path"] for r in page1] == ["item-4.txt", "item-3.txt"]
    assert [r["rel_path"] for r in page2] == ["item-2.txt", "item-1.txt"]
    assert [r["rel_path"] for r in page3] == ["item-0.txt"]


async def test_list_complete_jobs_name_filter_matches_case_insensitive_substring(db):
    queue_id = await _make_queue(db)
    matching = await _make_item(db, queue_id, "Married.At.First.Sight.S12E15", state="FAILED")
    await _make_job(db, matching, state="failed")
    other = await _make_item(db, queue_id, "other-release", state="FAILED")
    await _make_job(db, other, state="failed")

    rows, total = await _queue(db).list_complete_jobs(limit=50, offset=0, name_filter="married")
    assert total == 1
    assert rows[0]["rel_path"] == "Married.At.First.Sight.S12E15"


async def test_list_complete_jobs_name_filter_empty_string_matches_everything(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    await _make_job(db, item, state="failed")

    rows, total = await _queue(db).list_complete_jobs(limit=50, offset=0, name_filter="")
    assert total == 1
    assert rows[0]["rel_path"] == "a.txt"


async def test_list_complete_jobs_name_filter_no_match_returns_empty(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    await _make_job(db, item, state="failed")

    rows, total = await _queue(db).list_complete_jobs(
        limit=50, offset=0, name_filter="zzz-no-such-release"
    )
    assert total == 0
    assert rows == []


# --- GET /api/jobs/complete: the route-level wiring ------------------------------------------


async def test_list_complete_jobs_endpoint_paginates_and_reports_total(db):
    queue_id = await _make_queue(db)
    for i in range(3):
        item = await _make_item(db, queue_id, f"item-{i}.txt", state="FAILED")
        job_id = await _make_job(db, item, state="failed")
        await _set_finished_at(db, job_id, f"2026-08-19T0{i}:00:00.000000Z")

    q = _queue(db)
    result = await jobs.list_complete_jobs(_FakeQueueRequest(q), limit=2, offset=0)
    assert result.total == 3
    assert result.limit == 2
    assert result.offset == 0
    assert [j.rel_path for j in result.jobs] == ["item-2.txt", "item-1.txt"]


async def test_list_complete_jobs_endpoint_never_inlines_output_tail(db):
    """The phase-6 unbounded-list trap `api/history.py`'s own docstring names -- this endpoint
    is paginated but unbounded in total row count, so `output_tail` (~4KB/row) must never ride
    along inline; `has_output_tail` is the on-demand-fetch signal instead.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed")
    await db.execute(
        "UPDATE job SET output_tail = 'some captured lftp output' WHERE id = ?", (job_id,)
    )
    await db.commit()

    q = _queue(db)
    result = await jobs.list_complete_jobs(_FakeQueueRequest(q))
    assert len(result.jobs) == 1
    assert result.jobs[0].output_tail is None
    assert result.jobs[0].has_output_tail is True


# --- _job_out(include_output_tail=...) --------------------------------------------------------


def test_job_out_default_inlines_output_tail_and_reports_has_output_tail():
    out = _job_out(_job_out_row(output_tail="some captured output"))
    assert out.output_tail == "some captured output"
    assert out.has_output_tail is True


def test_job_out_include_output_tail_false_omits_blob_but_still_reports_has_output_tail():
    out = _job_out(_job_out_row(output_tail="some captured output"), include_output_tail=False)
    assert out.output_tail is None
    assert out.has_output_tail is True


def test_job_out_has_output_tail_false_when_none_captured():
    out = _job_out(_job_out_row(output_tail=None))
    assert out.output_tail is None
    assert out.has_output_tail is False


# --- dismiss_all_terminal(name_filter=...): "Dismiss list" over the paginated Complete box
# (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b,
# prompts/2026-08-19-transfers-paginated-boxes.md) --------------------------------------------


async def test_dismiss_all_terminal_name_filter_dismisses_only_matching_rows(db):
    queue_id = await _make_queue(db)
    matching = await _make_item(db, queue_id, "Married.At.First.Sight", state="FAILED")
    matching_job = await _make_job(db, matching, state="failed")
    other = await _make_item(db, queue_id, "unrelated-release", state="FAILED")
    other_job = await _make_job(db, other, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="married")
    assert dismissed == 1

    matching_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (matching_job,))
    ).fetchone()
    other_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (other_job,))
    ).fetchone()
    assert matching_row["dismissed_at"] is not None
    assert other_row["dismissed_at"] is None


async def test_dismiss_all_terminal_name_filter_acts_on_every_match_not_just_one_page(db):
    """The whole point of moving "Dismiss list" onto a server-side filter (task's own framing):
    a filter can match more rows than fit on one Complete-box page (50/page). This seeds more
    than one page's worth of matching rows and asserts every one of them is dismissed in the
    single bulk call, not just a page's worth.
    """
    queue_id = await _make_queue(db)
    job_ids = []
    for i in range(60):
        item = await _make_item(db, queue_id, f"match-release-{i}", state="FAILED")
        job_ids.append(await _make_job(db, item, state="failed"))
    # One deliberately non-matching row, to prove the filter is actually selective.
    other = await _make_item(db, queue_id, "totally-different", state="FAILED")
    other_job = await _make_job(db, other, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="match-release")
    assert dismissed == 60

    for job_id in job_ids:
        row = await (
            await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert row["dismissed_at"] is not None
    other_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (other_job,))
    ).fetchone()
    assert other_row["dismissed_at"] is None


async def test_dismiss_all_terminal_name_filter_no_match_dismisses_nothing_not_everything(db):
    """The load-bearing guarantee this task calls out explicitly: an empty *result* (the filter
    text matches zero dismissable rows) must dismiss nothing -- it must never degrade into "no
    filter was effectively applied, so dismiss everything." Seeds several real dismissable rows
    so a bug that silently dropped the filter clause would be caught dismissing them.
    """
    queue_id = await _make_queue(db)
    for i in range(3):
        item = await _make_item(db, queue_id, f"real-release-{i}", state="FAILED")
        await _make_job(db, item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="zzz-nothing-matches-this")
    assert dismissed == 0

    rows = await (await db.execute("SELECT dismissed_at FROM job")).fetchall()
    assert all(r["dismissed_at"] is None for r in rows)


async def test_dismiss_all_terminal_name_filter_never_touches_an_active_job(db):
    queue_id = await _make_queue(db)
    queued_item = await _make_item(db, queue_id, "queued-release", state="QUEUED")
    queued_job = await _make_job(db, queued_item, state="queued")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="queued")
    assert dismissed == 0

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (queued_job,))
    ).fetchone()
    assert row["dismissed_at"] is None


async def test_dismiss_all_terminal_name_filter_matches_the_same_predicate_as_the_listing(db):
    """`dismiss_all_terminal`'s `name_filter` branch deliberately re-adds the `MAX(id)`-per-item
    restriction `list_complete_jobs` filters its own listing on -- so a superseded terminal
    attempt (an item that's been retried and is active again) is never swept up by a filter
    dismiss just because its old, no-longer-visible row happens to match the text. Without that
    restriction this would dismiss the stale row too, since the plain terminal-state guard alone
    doesn't know about superseding.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "retried-release", state="QUEUED")
    old_failed = await _make_job(db, item, state="failed", attempt=1)
    await _make_job(db, item, state="queued", attempt=2)

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="retried")
    assert dismissed == 0

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (old_failed,))
    ).fetchone()
    assert row["dismissed_at"] is None


# --- dismiss_all_terminal(outcome=...): the Complete box's "Dismiss" menu (2026-08-20,
# follow-up to phase 1 stage 4b, prompts/2026-08-20-transfers-dismiss-menu-and-counts.md) -------


async def test_dismiss_all_terminal_outcome_dismisses_only_matching_state(db):
    queue_id = await _make_queue(db)
    failed_item = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    failed_job = await _make_job(db, failed_item, state="failed")
    succeeded_item = await _make_item(db, queue_id, "done.txt", state="DOWNLOADED")
    succeeded_job = await _make_job(db, succeeded_item, state="succeeded")
    cancelled_item = await _make_item(db, queue_id, "stopped.txt", state="STOPPED")
    cancelled_job = await _make_job(db, cancelled_item, state="cancelled")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(outcome="failed")
    assert dismissed == 1

    failed_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (failed_job,))
    ).fetchone()
    succeeded_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (succeeded_job,))
    ).fetchone()
    cancelled_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (cancelled_job,))
    ).fetchone()
    assert failed_row["dismissed_at"] is not None
    assert succeeded_row["dismissed_at"] is None
    assert cancelled_row["dismissed_at"] is None


async def test_dismiss_all_terminal_outcome_never_touches_an_active_job(db):
    queue_id = await _make_queue(db)
    queued_item = await _make_item(db, queue_id, "queued-release", state="QUEUED")
    queued_job = await _make_job(db, queued_item, state="queued")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(outcome="failed")
    assert dismissed == 0

    row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (queued_job,))
    ).fetchone()
    assert row["dismissed_at"] is None


async def test_dismiss_all_terminal_outcome_no_match_dismisses_nothing_not_everything(db):
    """The same load-bearing guarantee `name_filter`'s own no-match test states, for `outcome`:
    a state that matches none of the dismissable rows present must dismiss nothing -- never
    degrade into "no restriction was effectively applied, so dismiss everything." Seeds real
    dismissable rows of *other* outcomes so a bug that silently dropped the `state = ?` clause
    would be caught dismissing them.
    """
    queue_id = await _make_queue(db)
    for i in range(3):
        item = await _make_item(db, queue_id, f"real-failure-{i}", state="FAILED")
        await _make_job(db, item, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(outcome="succeeded")
    assert dismissed == 0

    rows = await (await db.execute("SELECT dismissed_at FROM job")).fetchall()
    assert all(r["dismissed_at"] is None for r in rows)


async def test_dismiss_all_terminal_outcome_composes_with_name_filter(db):
    """The decided composition (`DismissAllRequest`'s own docstring, `docs/decisions.md`):
    `outcome` and `name_filter` narrow the same set together, `AND`ed -- only a row matching
    *both* is dismissed, not the union of either alone.
    """
    queue_id = await _make_queue(db)
    both = await _make_item(db, queue_id, "Married.At.First.Sight", state="FAILED")
    both_job = await _make_job(db, both, state="failed")
    # Matches the name filter, wrong outcome.
    name_only = await _make_item(db, queue_id, "Married.Again", state="DOWNLOADED")
    name_only_job = await _make_job(db, name_only, state="succeeded")
    # Matches the outcome, wrong name.
    outcome_only = await _make_item(db, queue_id, "Unrelated.Release", state="FAILED")
    outcome_only_job = await _make_job(db, outcome_only, state="failed")

    q = _queue(db)
    dismissed = await q.dismiss_all_terminal(name_filter="married", outcome="failed")
    assert dismissed == 1

    both_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (both_job,))
    ).fetchone()
    name_only_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (name_only_job,))
    ).fetchone()
    outcome_only_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (outcome_only_job,))
    ).fetchone()
    assert both_row["dismissed_at"] is not None
    assert name_only_row["dismissed_at"] is None
    assert outcome_only_row["dismissed_at"] is None


async def test_dismiss_all_terminal_name_filter_count_matches_list_complete_jobs_total(db):
    """The property `DismissAllRequest.name_filter`'s own docstring promises: for the same
    filter text, `dismiss_all_terminal`'s dismissed count and `list_complete_jobs`'s `total`
    always agree, since both are built from the identical predicate. **Extended 2026-08-20**
    (rather than duplicated -- the task's own instruction) to the composed `outcome` +
    `name_filter` case: the same agreement must hold once `outcome` also narrows the set, so a
    future change to either method's `WHERE` in isolation can't quietly break the pairing.
    """
    queue_id = await _make_queue(db)
    for i in range(4):
        item = await _make_item(db, queue_id, f"agreement-release-{i}", state="FAILED")
        await _make_job(db, item, state="failed")
    # A superseded row matching the same text -- must be excluded from both sides identically.
    superseded_item = await _make_item(db, queue_id, "agreement-release-retried", state="QUEUED")
    await _make_job(db, superseded_item, state="failed", attempt=1)
    await _make_job(db, superseded_item, state="queued", attempt=2)

    q = _queue(db)
    _, total = await q.list_complete_jobs(limit=50, offset=0, name_filter="agreement-release")
    dismissed = await q.dismiss_all_terminal(name_filter="agreement-release")
    assert dismissed == total == 4

    # The composed case: re-seed a fresh set (the plain-name-filter dismiss above already
    # consumed the rows above) and check the same agreement holds with `outcome` added.
    for i in range(3):
        item = await _make_item(db, queue_id, f"agreement-composed-{i}", state="FAILED")
        await _make_job(db, item, state="failed")
    also_matches_name_wrong_outcome = await _make_item(
        db, queue_id, "agreement-composed-succeeded", state="DOWNLOADED"
    )
    await _make_job(db, also_matches_name_wrong_outcome, state="succeeded")

    _, composed_total = await q.list_complete_jobs(
        limit=50, offset=0, name_filter="agreement-composed", outcome="failed"
    )
    composed_dismissed = await q.dismiss_all_terminal(
        name_filter="agreement-composed", outcome="failed"
    )
    assert composed_dismissed == composed_total == 3


async def test_dismiss_all_request_rejects_name_filter_with_job_ids():
    with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
        DismissAllRequest(name_filter="x", job_ids=[1])


async def test_dismiss_all_request_rejects_name_filter_with_queue_id():
    with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
        DismissAllRequest(name_filter="x", queue_id=1)


async def test_dismiss_all_request_name_filter_alone_is_valid():
    req = DismissAllRequest(name_filter="married")
    assert req.name_filter == "married"
    assert req.job_ids is None
    assert req.queue_id is None


async def test_dismiss_all_jobs_endpoint_threads_name_filter_through_to_the_queue(db):
    queue_id = await _make_queue(db)
    matching = await _make_item(db, queue_id, "married-release", state="FAILED")
    matching_job = await _make_job(db, matching, state="failed")
    other = await _make_item(db, queue_id, "other-release", state="FAILED")
    other_job = await _make_job(db, other, state="failed")

    q = _queue(db)
    result = await jobs.dismiss_all_jobs(
        _FakeQueueRequest(q), DismissAllRequest(name_filter="married")
    )
    assert result.dismissed == 1

    matching_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (matching_job,))
    ).fetchone()
    other_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (other_job,))
    ).fetchone()
    assert matching_row["dismissed_at"] is not None
    assert other_row["dismissed_at"] is None


async def test_dismiss_all_jobs_endpoint_threads_outcome_through_to_the_queue(db):
    """`body.outcome`'s own route-level wiring test -- same shape as `name_filter`'s above."""
    queue_id = await _make_queue(db)
    failed_item = await _make_item(db, queue_id, "a.txt", state="FAILED")
    failed_job = await _make_job(db, failed_item, state="failed")
    succeeded_item = await _make_item(db, queue_id, "b.txt", state="DOWNLOADED")
    succeeded_job = await _make_job(db, succeeded_item, state="succeeded")

    q = _queue(db)
    result = await jobs.dismiss_all_jobs(_FakeQueueRequest(q), DismissAllRequest(outcome="failed"))
    assert result.dismissed == 1

    failed_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (failed_job,))
    ).fetchone()
    succeeded_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (succeeded_job,))
    ).fetchone()
    assert failed_row["dismissed_at"] is not None
    assert succeeded_row["dismissed_at"] is None


async def test_dismiss_all_jobs_endpoint_threads_composed_outcome_and_name_filter(db):
    """The composed case threaded end to end through the route -- both scopes reach
    `dismiss_all_terminal` together, not silently dropped to just one of them.
    """
    queue_id = await _make_queue(db)
    both = await _make_item(db, queue_id, "married-failure", state="FAILED")
    both_job = await _make_job(db, both, state="failed")
    wrong_outcome = await _make_item(db, queue_id, "married-success", state="DOWNLOADED")
    wrong_outcome_job = await _make_job(db, wrong_outcome, state="succeeded")

    q = _queue(db)
    result = await jobs.dismiss_all_jobs(
        _FakeQueueRequest(q), DismissAllRequest(name_filter="married", outcome="failed")
    )
    assert result.dismissed == 1

    both_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (both_job,))
    ).fetchone()
    wrong_outcome_row = await (
        await db.execute("SELECT dismissed_at FROM job WHERE id = ?", (wrong_outcome_job,))
    ).fetchone()
    assert both_row["dismissed_at"] is not None
    assert wrong_outcome_row["dismissed_at"] is None
