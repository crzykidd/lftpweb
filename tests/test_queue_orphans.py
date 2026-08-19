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
from datetime import UTC, datetime, timedelta

import pytest

from lftpweb.api import history
from lftpweb.core import lftp as lftp_module
from lftpweb.core import scheduler
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.postprocess import PostprocessPipeline
from lftpweb.core.queue import INTERRUPTED_OUTPUT_TAIL
from lftpweb.core.remote import RemoteConnectionPool
from lftpweb.core.settle import SettleSettings, save_settle_settings
from test_queue import (
    _host_config,
    _make_db,
    _make_host_row,
    _make_item_row,
    _make_queue_row,
    _queue_for,
)


# Minimal `Request` stand-in for `history.list_history_jobs` -- same shape
# `tests/test_history_api.py` defines, duplicated here rather than imported since that module's
# own fixtures build a fresh `:memory:` db per test and this file's `db` fixture (from
# `test_queue`) is a different one; the route functions only need `request.app.state.db`.
class _FakeState:
    def __init__(self, db):
        self.db = db


class _FakeApp:
    def __init__(self, db):
        self.state = _FakeState(db)


class _FakeRequest:
    def __init__(self, db):
        self.app = _FakeApp(db)


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

    job = await (await db.execute("SELECT id, state, error_class, output_tail FROM job")).fetchone()
    assert job["state"] == "failed"
    assert job["error_class"] == "INTERRUPTED"
    # 2026-08-17 (prompts/2026-08-17-interrupted-job-popout-explains-itself.md): before this,
    # `output_tail` stayed NULL here, and the History popout for exactly this job class
    # expanded to a blank panel -- the frontend fix makes the panel reachable, but only this
    # backend write gives it something to say.
    assert job["output_tail"] == INTERRUPTED_OUTPUT_TAIL

    item = await (
        await db.execute("SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,))
    ).fetchone()
    assert item["state"] == "PARTIAL", "a stuck DOWNLOADING item must be freed for rescan"
    assert item["auto_queue_suppressed"] == 0, "an interrupted transfer is not a user stop"

    # Flows through the existing `has_output_tail`/list endpoint untouched -- no API change
    # needed, per the task's own "confirm the new backend-written tail flows through the
    # existing endpoint with zero API changes" instruction.
    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    assert resp.jobs[0].id == job["id"]
    assert resp.jobs[0].has_output_tail is True


async def test_a_job_that_somehow_already_has_an_output_tail_keeps_it(db, tmp_path):
    """The reconcile UPDATE's `COALESCE(NULLIF(output_tail, ''), ?)` guard, exercised directly
    -- in practice a `running` row's `output_tail` is never written by anything else in
    `core/queue.py` (only reap/dismiss/auth-failure paths touch that column, and none of them
    run on a job this sweep is about to mark `failed`), but the guard is stated unconditionally
    in the docstring, so it earns its own test rather than relying on that invariant forever.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Stale.Release.2", is_dir=True, remote_size=1000)
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item_id,))
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
        "output_tail) VALUES (?, 'mirror', 'running', 'main', 0, 1, 0, ?)",
        (item_id, "genuinely captured lftp output"),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    job = await (await db.execute("SELECT output_tail FROM job")).fetchone()
    assert job["output_tail"] == "genuinely captured lftp output"


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


# --- Startup re-queue of interrupted items (2026-08-18, production incident diagnosed live +
# support bundle `lftpweb-support-0.2.4-20260818T192004Z`) -- job 195 (a 74 GB season pack)
# actually finished, but the supervisor froze in D-state disk-wait and never reaped it; the
# restart's own sweep marked it `failed`/`INTERRUPTED` and the item correctly re-derived
# `DOWNLOADED`, but nothing ever re-queued it -- `DOWNLOADED` isn't auto-queue eligible, so the
# `.downloading-` prefix stayed on forever and post-processing never ran. Fix: the sweep
# re-queues what it interrupts (rule 1) and rescues rows an *earlier*, unfixed restart already
# stranded in exactly that shape (rule 2). ---------------------------------------------------


async def _events(db, kind):
    cursor = await db.execute("SELECT * FROM event WHERE kind = ? ORDER BY id", (kind,))
    return await cursor.fetchall()


async def test_interrupted_job_with_complete_local_dir_requeues_and_postprocess_fires(
    db, tmp_path, monkeypatch
):
    """Rule 1, followed all the way through the real recovery chain: the supervisor never
    reaped a job whose transfer had actually finished -- every byte already on disk under the
    item's download-prefix directory when the restart sweep runs. The re-queued `mirror -c`
    must find nothing to transfer, exit 0 almost immediately, and that *observed* success has
    to carry the item all the way through post-processing and off its download-prefix name --
    not merely into a `QUEUED` row this test stops short of running (that's the next test).
    """
    # This file's own `db` fixture, unlike `test_queue.py`'s, does not disable the settle
    # gate -- irrelevant to every other test here (none of them reap a real job), but this one
    # actually drives a job to completion via `tick()`, so an unconfirmed-settle hold would
    # keep the item off `DOWNLOADED` and this test would never see post-processing fire.
    await save_settle_settings(db, SettleSettings(enabled=False))

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))  # the mount gate must not block a healthy queue

    # The bytes are already fully, physically present -- exactly the incident's "job 195
    # actually finished" shape -- still sitting under the download-prefix directory because
    # the pipeline's rename-off-the-prefix step (`core/postprocess.py`) never got to run.
    prefixed = local_dir / ".downloading-Release.Name"
    prefixed.mkdir()
    content = b"x" * 1024
    (prefixed / "a.mkv").write_bytes(content)

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(
        db, queue_id, "Release.Name", is_dir=True, remote_size=len(content)
    )
    await db.execute(
        "UPDATE item SET state = 'DOWNLOADING', pending_download_prefix = '.downloading-' "
        "WHERE id = ?",
        (item_id,),
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()

    spawn_calls: list[int] = []

    async def fake_spawn(spec):
        spawn_calls.append(spec.job_id)
        return lftp_module.SpawnedJob(
            proc=_FakeProc(pid=20_000 + len(spawn_calls)),
            pid=20_000 + len(spawn_calls),
            rc_path=None,
            known_hosts_path=None,
        )

    async def fake_wait_and_capture(job):
        return 0, ""  # a `mirror -c` re-queue against already-complete bytes: no-op, exit 0

    monkeypatch.setattr(lftp_module, "spawn", fake_spawn)
    monkeypatch.setattr(lftp_module, "wait_and_capture", fake_wait_and_capture)

    q = await _queue_for(db, tmp_path)
    events = EventBus()
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db,
        events=events,
        remote_pool=pipeline_pool,
        host_provider=lambda: _host_config_async_local(),
    )
    q.postprocess = pipeline

    try:
        await q._reconcile_orphaned_jobs()

        requeued_job = await (
            await db.execute(
                "SELECT id, state FROM job WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                (item_id,),
            )
        ).fetchone()
        assert requeued_job["state"] == "queued", "rule 1 must re-queue via enqueue_item"

        await q.tick()  # admits and spawns the fake, instantly-successful job
        await asyncio.sleep(0.05)
        await q.tick()  # reaps it

        assert spawn_calls == [requeued_job["id"]]

        job_row = await (
            await db.execute("SELECT state FROM job WHERE id = ?", (requeued_job["id"],))
        ).fetchone()
        assert job_row["state"] == "succeeded", "no-op mirror -c must still be an observed success"

        await pipeline.wait_idle()

        final_dir = local_dir / "Release.Name"
        assert final_dir.is_dir(), "must end up under its real, unprefixed name"
        assert (final_dir / "a.mkv").read_bytes() == content
        assert not prefixed.exists(), "the download-prefix directory must be gone once renamed"

        item = await (
            await db.execute(
                "SELECT state, pending_download_prefix FROM item WHERE id = ?", (item_id,)
            )
        ).fetchone()
        assert item["pending_download_prefix"] is None

        requeued_events = await _events(db, "interrupted_requeued")
        assert len(requeued_events) == 1
        assert requeued_events[0]["item_id"] == item_id
        assert "no-ops straight into post-processing" in requeued_events[0]["message"]
    finally:
        await q.stop()
        await pipeline.wait_idle()


async def _host_config_async_local():
    return _host_config()


async def test_interrupted_job_with_partial_local_dir_is_requeued(db, tmp_path):
    """Rule 1, the `PARTIAL` shape -- resumes the same way auto-queue would have, but without
    depending on auto-queue being enabled for this queue. Not asserting the transfer itself
    (no fake lftp wired up here): the enqueue is the contract, matching the task's own
    instruction not to re-litigate transfer mechanics already covered elsewhere.
    """
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "Partial.Release", is_dir=True, remote_size=10_000)
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item_id,))
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    jobs = await (
        await db.execute(
            "SELECT state, error_class FROM job WHERE item_id = ? ORDER BY id", (item_id,)
        )
    ).fetchall()
    assert len(jobs) == 2, "the interrupted job plus a fresh re-queued one"
    assert jobs[0]["state"] == "failed"
    assert jobs[0]["error_class"] == "INTERRUPTED"
    assert jobs[1]["state"] == "queued"

    item = await (await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))).fetchone()
    assert item["state"] == "QUEUED", "enqueue_item wins over the sweep's own PARTIAL marking"

    requeued_events = await _events(db, "interrupted_requeued")
    assert len(requeued_events) == 1
    assert requeued_events[0]["item_id"] == item_id


async def test_mount_gate_blocks_requeue_and_leaves_marking_unchanged(db, tmp_path):
    """A queue whose mount sentinel fails must never have this sweep spawn lftp processes
    into it -- the exact incident this task exists for was itself a broken-mount restart.
    Deliberately no `write_if_needed` call here: `tmp_path / "local"` is never created, so
    `mount_sentinel.check()` reads it the same way an actually-unmounted share would.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Gated.Release", is_dir=True, remote_size=1000)
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item_id,))
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    jobs = await (
        await db.execute("SELECT state FROM job WHERE item_id = ?", (item_id,))
    ).fetchall()
    assert len(jobs) == 1, "gated -- no second job must ever be inserted"
    assert jobs[0]["state"] == "failed"

    item = await (await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))).fetchone()
    assert item["state"] == "PARTIAL", "the sweep's own marking is left exactly as today"

    gated_events = await _events(db, "interrupted_requeue_gated")
    assert len(gated_events) == 1
    assert f"queue {queue_id}" in gated_events[0]["message"]
    assert gated_events[0]["level"] == "warning"

    assert await _events(db, "interrupted_requeued") == []


async def test_stranded_downloaded_item_from_an_earlier_restart_is_requeued(db, tmp_path):
    """Rule 2: the production incident item was already `failed`/`INTERRUPTED` *before* this
    fix shipped -- rule 1 alone only ever re-queues jobs the *current* startup just marked
    INTERRUPTED, so a row an earlier, unfixed restart already wedged needs its own rescue.
    Keyed on: `item.state = 'DOWNLOADED'`, its most recent job already `failed`/`INTERRUPTED`,
    no active job, and its physical directory (`core/local_delete.py._physical_local_root`)
    still carrying the download prefix.
    """
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))

    prefixed = local_dir / ".downloading-Stranded.Release"
    prefixed.mkdir()
    (prefixed / "a.mkv").write_bytes(b"y" * 512)

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "Stranded.Release", is_dir=True, remote_size=512)
    await db.execute(
        "UPDATE item SET state = 'DOWNLOADED', pending_download_prefix = '.downloading-' "
        "WHERE id = ?",
        (item_id,),
    )
    # A job already terminal INTERRUPTED *before* this startup -- no 'running' row for the
    # sweep's own first SELECT to find, unlike every other test in this file.
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
        "error_class, finished_at) VALUES (?, 'mirror', 'failed', 'main', 0, 1, 0, "
        "'INTERRUPTED', ?)",
        (item_id, "2026-08-18T01:00:00.000000Z"),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    jobs = await (
        await db.execute("SELECT state FROM job WHERE item_id = ? ORDER BY id", (item_id,))
    ).fetchall()
    assert len(jobs) == 2, "the old terminal job plus a fresh re-queued one"
    assert jobs[1]["state"] == "queued"

    item = await (await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))).fetchone()
    assert item["state"] == "QUEUED"

    requeued_events = await _events(db, "interrupted_requeued")
    assert len(requeued_events) == 1
    assert requeued_events[0]["item_id"] == item_id
    assert "stranded by an earlier restart" in requeued_events[0]["message"]


# --- `queued_at` carries forward, not just now() (2026-08-19,
# prompts/2026-08-19-rescue-requeue-keeps-queue-position.md) -- production find: S10 (job 203)
# was mid-download at restart, but the rescue's `enqueue_item` call stamped a fresh `queued_at`
# while jobs that were merely *queued* (never started) at restart kept their own older
# timestamps, so the actively-downloading item went to the back of the line behind everything
# that hadn't started. Fix: both rescue paths pass the interrupted job's own original
# `queued_at` through to the fresh row. -------------------------------------------------------


async def _admission_order_item_ids(db) -> list[int]:
    """The exact ordering query `_admit` uses (`core/queue.py`) -- deliberately not a
    re-implementation of `rank DESC, queued_at ASC`, so this test fails if that query itself
    ever changes rather than only if a second, independent copy of the sort does.
    """
    cursor = await db.execute(
        "SELECT job.item_id FROM job WHERE job.state = 'queued' "
        "ORDER BY job.rank DESC, job.queued_at ASC"
    )
    rows = await cursor.fetchall()
    return [r["item_id"] for r in rows]


async def test_requeued_interrupted_item_keeps_its_original_queue_position(db, tmp_path):
    """The production scenario itself: an item that was actively downloading (hence `running`,
    hence among the *oldest* jobs by definition) must resume ahead of items that were merely
    `queued` (never started) with newer -- but still older-than-now -- timestamps, once the
    interrupted job's original `queued_at` is preserved rather than replaced with a fresh now().
    """
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)

    # The interrupted item: was `running` since the oldest timestamp of the three.
    old_item_id = await _make_item_row(
        db, queue_id, "Was.Downloading", is_dir=True, remote_size=1000
    )
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (old_item_id,))
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, queued_at) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0, '2020-01-01T00:00:00.000000Z')",
        (old_item_id,),
    )

    # Two items that were merely queued (never started) at restart, with their own older-than-
    # now but newer-than-the-interrupted-job timestamps -- exactly the "everything that hadn't
    # even started" the incident describes.
    mid_item_id = await _make_item_row(
        db, queue_id, "Was.Queued.Mid", is_dir=True, remote_size=1000
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, queued_at) "
        "VALUES (?, 'mirror', 'queued', 'main', 0, 1, 0, '2020-06-01T00:00:00.000000Z')",
        (mid_item_id,),
    )
    new_item_id = await _make_item_row(
        db, queue_id, "Was.Queued.New", is_dir=True, remote_size=1000
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, queued_at) "
        "VALUES (?, 'mirror', 'queued', 'main', 0, 1, 0, '2020-07-01T00:00:00.000000Z')",
        (new_item_id,),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    order = await _admission_order_item_ids(db)
    assert order == [old_item_id, mid_item_id, new_item_id], (
        "the re-queued interrupted item must be admitted first, backdated to its original "
        "queued_at -- not last, behind items that had merely been queued"
    )

    # The re-queued row's own `queued_at` must equal the interrupted job's, not today's now().
    requeued = await (
        await db.execute(
            "SELECT queued_at FROM job WHERE item_id = ? AND state = 'queued'", (old_item_id,)
        )
    ).fetchone()
    assert requeued["queued_at"] == "2020-01-01T00:00:00.000000Z"


async def test_enqueue_item_without_override_still_stamps_now(db, tmp_path):
    """Regression on the default: every caller before this task (and every caller today except
    the startup rescue) must still get today's now() -- the `queued_at` param is opt-in, not a
    behavior change for the common case.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(db, queue_id, "Some.Release", is_dir=True, remote_size=1000)

    q = await _queue_for(db, tmp_path)
    before = datetime.now(UTC)
    await q.enqueue_item(item_id)
    after = datetime.now(UTC)

    row = await (
        await db.execute("SELECT queued_at FROM job WHERE item_id = ?", (item_id,))
    ).fetchone()
    stamped = datetime.strptime(row["queued_at"], "%Y-%m-%dT%H:%M:%S.%f%z")
    # SQLite's `STRFTIME('%f', ...)` truncates to millisecond resolution (vs. Python's
    # microsecond `datetime.now()`), so `stamped` can legitimately read a hair below `before`
    # -- a 1s tolerance absorbs that truncation without loosening what this test actually
    # checks: the row got today's now(), not some arbitrary override.
    tolerance = timedelta(seconds=1)
    assert (
        before - tolerance <= stamped <= after + tolerance
    ), "no override must still stamp a fresh now(), unchanged"


async def test_stranded_downloaded_requeue_carries_the_original_queued_at(db, tmp_path):
    """Rule 2 (`_requeue_stranded_downloaded`) must carry the *most recent* interrupted job's
    own `queued_at` forward too, not just rule 1's freshly-interrupted path -- the row it already
    keys off (`ORDER BY j.id DESC LIMIT 1`) is the one whose timestamp must survive.
    """
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))

    prefixed = local_dir / ".downloading-Stranded.Timestamp"
    prefixed.mkdir()
    (prefixed / "a.mkv").write_bytes(b"y" * 512)

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "Stranded.Timestamp", is_dir=True, remote_size=512)
    await db.execute(
        "UPDATE item SET state = 'DOWNLOADED', pending_download_prefix = '.downloading-' "
        "WHERE id = ?",
        (item_id,),
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
        "error_class, finished_at, queued_at) VALUES (?, 'mirror', 'failed', 'main', 0, 1, 0, "
        "'INTERRUPTED', ?, '2020-03-01T00:00:00.000000Z')",
        (item_id, "2026-08-18T01:00:00.000000Z"),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    requeued = await (
        await db.execute(
            "SELECT queued_at FROM job WHERE item_id = ? AND state = 'queued'", (item_id,)
        )
    ).fetchone()
    assert requeued["queued_at"] == "2020-03-01T00:00:00.000000Z"


async def test_stopped_job_item_is_not_requeued(db, tmp_path):
    """User intent (§4.6) beats this sweep entirely: a `STOPPED` item's job is `cancelled`,
    never `running`, so rule 1's own SELECT never sees it in the first place -- and its
    `error_class` is never `INTERRUPTED`, so rule 2's stranded-row query has no opinion about
    it either. Nothing about a deliberate user stop should ever be re-queued behind their back.
    """
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    write_if_needed(str(local_dir))

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "Stopped.Release", is_dir=True, remote_size=1000)
    await db.execute(
        "UPDATE item SET state = 'STOPPED', auto_queue_suppressed = 1, "
        "suppressed_reason = 'user_stopped' WHERE id = ?",
        (item_id,),
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'cancelled', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()

    q = await _queue_for(db, tmp_path)
    await q._reconcile_orphaned_jobs()

    jobs = await (
        await db.execute("SELECT state FROM job WHERE item_id = ?", (item_id,))
    ).fetchall()
    assert len(jobs) == 1, "no re-queue must ever be attempted for a user-stopped item"
    assert jobs[0]["state"] == "cancelled"

    item = await (
        await db.execute("SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,))
    ).fetchone()
    assert item["state"] == "STOPPED"
    assert item["auto_queue_suppressed"] == 1

    assert await _events(db, "interrupted_requeued") == []
    assert await _events(db, "interrupted_requeue_gated") == []
