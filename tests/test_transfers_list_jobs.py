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
import pytest

from lftpweb.core.events import EventBus
from lftpweb.core.queue import JobNotDismissableError, TransferQueue
from lftpweb.db import migrate


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


async def test_list_jobs_excludes_succeeded_and_unrelated_older_failures(db):
    queue_id = await _make_queue(db)
    succeeded_item = await _make_item(db, queue_id, "done.txt", state="DOWNLOADED")
    await _make_job(db, succeeded_item, state="succeeded")

    other_item = await _make_item(db, queue_id, "also-failed.txt", state="FAILED")
    await _make_job(db, other_item, state="failed", attempt=1)
    # A second, even older, superseded failure on a *different* item shouldn't leak in either.
    third_item = await _make_item(db, queue_id, "old-history.txt", state="DOWNLOADED")
    await _make_job(db, third_item, state="failed", attempt=1)
    await _make_job(db, third_item, state="succeeded", attempt=2)

    jobs = await _queue(db).list_jobs()
    assert {j["rel_path"] for j in jobs} == {"also-failed.txt"}


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
