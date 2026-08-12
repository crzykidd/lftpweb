"""Startup reconciliation of orphaned jobs — no seedbox needed, so this lives outside
tests/test_queue.py (whose module-level skipif gates everything on the fake seedbox being
reachable, which would silently skip this pure-database test).
"""

from __future__ import annotations

import pytest

from test_queue import _make_db, _make_host_row, _make_item_row, _make_queue_row, _queue_for


@pytest.fixture
async def db(tmp_path):
    conn = await _make_db(tmp_path)
    try:
        yield conn
    finally:
        await conn.close()


async def test_running_jobs_left_by_a_restart_are_cleared_on_start(db, tmp_path):
    """A `running` row can only be real while this process supervises its child.

    After a container restart the process is gone but the row survives, so `list_jobs()`
    reports a phantom transfer that never progresses — and because scans don't overwrite
    job-lifecycle states, the item stays DOWNLOADING forever. Seen on a real deployment after
    an image pull. Not suppressed for auto-queue: an interrupted transfer isn't a user
    decision, so it stays eligible to be retried.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Stale.Release", is_dir=True, remote_size=1000)
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item_id,))
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    job = await (await db.execute("SELECT state, error_class FROM job")).fetchone()
    assert job["state"] == "failed"
    assert job["error_class"] == "INTERRUPTED"

    item = await (
        await db.execute("SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,))
    ).fetchone()
    assert item["state"] == "PARTIAL", "a stuck DOWNLOADING item must be freed for rescan"
    assert item["auto_queue_suppressed"] == 0, "an interrupted transfer is not a user stop"
