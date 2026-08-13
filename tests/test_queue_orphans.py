"""Startup reconciliation of orphaned jobs — no seedbox needed, so this lives outside
tests/test_queue.py (whose module-level skipif gates everything on the fake seedbox being
reachable, which would silently skip this pure-database test).

Also holds the duplicate-job / duplicate-process regression tests for
`prompts/2026-08-13-lftp-timestamped-temp-files.md`'s root cause (`core/queue.py.enqueue_item`
and `_admit`) — these monkeypatch `lftp.spawn`/`lftp.wait_and_capture` rather than talking to a
real process, so they belong here rather than in the seedbox-gated `test_queue.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from lftpweb.core import lftp as lftp_module
from lftpweb.core import scheduler
from test_queue import (
    _host_config,
    _make_db,
    _make_host_row,
    _make_item_row,
    _make_queue_row,
    _queue_for,
)


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


# --- Duplicate jobs / duplicate processes (2026-08-13,
# prompts/2026-08-13-lftp-timestamped-temp-files.md, this task's root cause) -----------------


class _FakeProc:
    """Stands in for `asyncio.subprocess.Process` -- just enough surface for `_spawn_decision`
    (`.pid`) and `core/lftp.py.terminate` (`.terminate()`/`.kill()`/`.wait()`, used by
    `TransferQueue.stop()` during test teardown) that a test doesn't have to wait out a real
    grace period for: `.wait()` blocks until `.terminate()`/`.kill()` sets the exit event, not
    until a real process actually exits.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.returncode = 0
        self._exited.set()

    def kill(self) -> None:
        self.returncode = -9
        self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode


def _fake_spawn_and_wait(monkeypatch, spawn_calls: list[int]):
    """Monkeypatch `lftp.spawn`/`lftp.wait_and_capture` so `core/queue.py` never touches a real
    process -- `spawn_calls` records every `job_id` an lftp process was actually (fake-)spawned
    for, which is the thing every test below asserts on: never more than one per item.
    """

    async def fake_spawn(spec):
        spawn_calls.append(spec.job_id)
        return lftp_module.SpawnedJob(
            proc=_FakeProc(pid=10_000 + len(spawn_calls)),
            pid=10_000 + len(spawn_calls),
            rc_path=None,  # cleanup() no-ops on None (missing_ok=True guards every unlink)
            known_hosts_path=None,
        )

    async def fake_wait_and_capture(job):
        await asyncio.sleep(100)
        return 0, ""

    monkeypatch.setattr(lftp_module, "spawn", fake_spawn)
    monkeypatch.setattr(lftp_module, "wait_and_capture", fake_wait_and_capture)


async def _insert_job_row(db, item_id: int, *, state: str = "queued", kind: str = "mirror") -> int:
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, ?, ?, 'main', 0, 1, 0)",
        (item_id, kind, state),
    )
    await db.commit()
    return cursor.lastrowid


async def test_enqueue_item_is_idempotent_against_an_existing_active_job(db, tmp_path):
    """Two `enqueue_item` calls for one item must produce exactly one job -- the root-cause fix
    (a double-click, or Queue on an item auto-queue just picked up, used to insert a second
    `job` row unconditionally). Idempotent (return the existing job's id), not rejecting: a
    double-click is not a mistake worth surfacing as an error, and it's kinder than a 4xx.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Some.Release", is_dir=True, remote_size=1000)

    q = await _queue_for(db, tmp_path)
    job_id_1 = await q.enqueue_item(item_id)
    job_id_2 = await q.enqueue_item(item_id)

    assert (
        job_id_1 == job_id_2
    ), "a second enqueue_item call must return the existing job, not insert a new one"

    rows = await (await db.execute("SELECT id FROM job WHERE item_id = ?", (item_id,))).fetchall()
    assert len(rows) == 1


async def test_enqueue_item_allows_a_fresh_job_once_the_previous_one_is_terminal(db, tmp_path):
    """The idempotency guard must not outlive the job it's guarding -- a genuinely new attempt
    (retry after failure, a fresh queue click once the item finished) has to be able to insert
    a new row once nothing active remains.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Some.Release", is_dir=True, remote_size=1000)

    q = await _queue_for(db, tmp_path)
    job_id_1 = await q.enqueue_item(item_id)
    await db.execute("UPDATE job SET state = 'failed' WHERE id = ?", (job_id_1,))
    await db.commit()

    job_id_2 = await q.enqueue_item(item_id)
    assert job_id_2 != job_id_1

    rows = await (
        await db.execute("SELECT id, state FROM job WHERE item_id = ?", (item_id,))
    ).fetchall()
    assert len(rows) == 2


async def test_admit_refuses_a_second_process_for_an_item_with_two_queued_rows(
    db, tmp_path, monkeypatch
):
    """The spawn layer's own guard -- independent of `enqueue_item`'s -- for exactly the case
    the task calls out: two `job` rows already exist for one item (inserted directly here,
    bypassing `enqueue_item` entirely, the same way a race between two concurrent calls could
    leave two `queued` rows despite that method's own check). `_admit` must never let both
    become running lftp processes, regardless of how they got there.
    """
    spawn_calls: list[int] = []
    _fake_spawn_and_wait(monkeypatch, spawn_calls)

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Some.Release", is_dir=True, remote_size=1000)
    job_id_1 = await _insert_job_row(db, item_id)
    job_id_2 = await _insert_job_row(db, item_id)

    q = await _queue_for(db, tmp_path, max_concurrent_transfers=2)
    try:
        await q.tick()  # first admission pass -- must admit only one of the two
        await q.tick()  # second pass -- the other must still be refused (the first is running)

        assert spawn_calls == [job_id_1], "must spawn exactly one process, for the first-ranked job"

        rows = await (
            await db.execute("SELECT id, state FROM job WHERE item_id = ? ORDER BY id", (item_id,))
        ).fetchall()
        by_id = {r["id"]: r["state"] for r in rows}
        assert by_id[job_id_1] == "running"
        assert by_id[job_id_2] == "queued", "the duplicate row must stay queued, never spawned"
    finally:
        await q.stop()


async def test_spawn_decision_itself_refuses_a_second_process_for_a_running_item(
    db, tmp_path, monkeypatch
):
    """Belt-and-suspenders test for `_spawn_decision`'s own guard, called directly (bypassing
    `_admit`'s dedup entirely) so this specific check is verified in isolation rather than only
    ever being exercised alongside the layer that would normally prevent it from being reached.
    """
    spawn_calls: list[int] = []
    _fake_spawn_and_wait(monkeypatch, spawn_calls)

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Some.Release", is_dir=True, remote_size=1000)
    job_id_1 = await _insert_job_row(db, item_id)
    job_id_2 = await _insert_job_row(db, item_id)

    from lftpweb.core.queue import load_transfer_settings

    q = await _queue_for(db, tmp_path)
    settings = await load_transfer_settings(db)
    host = _host_config()
    decision1 = scheduler.AdmitDecision(job_id=job_id_1, lane="main", rate_limit_bps=1_000_000)
    decision2 = scheduler.AdmitDecision(job_id=job_id_2, lane="main", rate_limit_bps=1_000_000)

    try:
        await q._spawn_decision(decision1, host, settings)
        await q._spawn_decision(decision2, host, settings)

        assert spawn_calls == [job_id_1], "the second decision for the same item must never spawn"

        rows = await (
            await db.execute("SELECT id, state FROM job WHERE item_id = ? ORDER BY id", (item_id,))
        ).fetchall()
        by_id = {r["id"]: r["state"] for r in rows}
        assert by_id[job_id_1] == "running"
        assert by_id[job_id_2] == "queued"
    finally:
        await q.stop()
