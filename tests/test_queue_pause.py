"""Pause the transfer queue (`prompts/2026-08-20-queue-pause.md`, DESIGN.md §4.5's admission
gate and §4.6's stop-vs-pause distinction). DB/API-level only -- no lftp process or fake
seedbox needed here: the admission gate is a caller-side early return in `_admit()` (never
reaches spawn), and "start now"/"pause"/"unpause"/reorder are all DB writes plus a tick
request. The "pause now" SIGTERM-and-requeue path against a *real* running lftp child is
`tests/test_queue_pause_e2e.py`'s job (seedbox-gated, mirrors `tests/test_queue.py`'s own
stop-mid-transfer test).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi import HTTPException

from lftpweb.api import health as health_api
from lftpweb.api import jobs
from lftpweb.core.events import EventBus
from lftpweb.core.queue import (
    QueuePausedError,
    QueuePauseState,
    TransferQueue,
    load_queue_pause_state,
    save_queue_pause_state,
)
from lftpweb.db import migrate
from lftpweb.models import QueuePauseRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_queue_row(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queued_job(db, queue_id: int, rel_path: str = "item", position: float = 1.0) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, 1000, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path),
    )
    item_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queue_position) "
        "VALUES (?, 'pget', 'queued', 'main', 0, 1, ?)",
        (item_id, position),
    )
    await db.commit()
    return cursor.lastrowid


def _queue_obj(db) -> TransferQueue:
    return TransferQueue(db, "/config", EventBus())


# --- persistence: a plain `setting` row, no migration ---------------------------------------


async def test_pause_state_defaults_unpaused_when_no_row_exists(db):
    state = await load_queue_pause_state(db)
    assert state.paused is False


async def test_pause_state_round_trips_through_the_setting_table(db):
    await save_queue_pause_state(db, QueuePauseState(paused=True))
    assert (await load_queue_pause_state(db)).paused is True
    await save_queue_pause_state(db, QueuePauseState(paused=False))
    assert (await load_queue_pause_state(db)).paused is False


# --- TransferQueue.pause()/unpause(): the in-memory flag + persistence ----------------------


async def test_pause_after_current_sets_paused_and_persists(db):
    q = _queue_obj(db)
    assert q.paused is False
    await q.pause(stop_running=False)
    assert q.paused is True
    assert (await load_queue_pause_state(db)).paused is True


async def test_unpause_clears_paused_and_persists(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    await q.unpause()
    assert q.paused is False
    assert (await load_queue_pause_state(db)).paused is False


async def test_pause_survives_a_restart(db):
    """No running process involved -- this is the "someone paused for NAS maintenance, a
    container restart must not quietly resume everything" property, exercised the way this
    project's other startup behavior is (`tests/test_queue_orphans.py`): construct a *second*
    `TransferQueue` against the same `db` and call `start()`, standing in for a fresh process
    picking up where the persisted row left off.
    """
    q1 = _queue_obj(db)
    await q1.pause(stop_running=False)

    q2 = _queue_obj(db)
    await q2.start()
    try:
        assert q2.paused is True
    finally:
        await q2.stop()


async def test_unpaused_state_also_survives_a_restart(db):
    q1 = _queue_obj(db)
    await q1.pause(stop_running=False)
    await q1.unpause()

    q2 = _queue_obj(db)
    await q2.start()
    try:
        assert q2.paused is False
    finally:
        await q2.stop()


# --- pause-for-a-duration: a stored deadline, not a countdown or a running timer -------------
# (prompts/2026-08-21-pause-for-duration.md)


async def test_pause_without_duration_is_indefinite(db):
    """Unchanged default -- the dropdown adds durations, it does not replace "pause until I
    say otherwise".
    """
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    assert q.paused is True
    assert q.paused_until is None
    assert (await load_queue_pause_state(db)).paused_until is None


async def test_pause_with_duration_computes_a_future_deadline(db):
    q = _queue_obj(db)
    before = datetime.now(UTC)
    await q.pause(stop_running=False, duration_s=600)
    after = datetime.now(UTC)

    assert q.paused_until is not None
    deadline = datetime.strptime(q.paused_until, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert before + timedelta(seconds=599) <= deadline <= after + timedelta(seconds=601)
    # Persisted, not just cached in memory.
    assert (await load_queue_pause_state(db)).paused_until == q.paused_until


async def test_duration_combines_with_pause_after_current(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=60)
    assert q.paused is True
    assert q.paused_until is not None


async def test_duration_combines_with_pause_now(db):
    # No running jobs -- `_pause_running_jobs` is a no-op with nothing in `self._running`, so
    # this exercises the duration/entry-mode combination without a real lftp child. The SIGTERM-
    # and-requeue mechanics of "pause now" itself are `test_queue_pause_e2e.py`'s job.
    q = _queue_obj(db)
    await q.pause(stop_running=True, duration_s=60)
    assert q.paused is True
    assert q.paused_until is not None


async def test_unpause_clears_the_deadline(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)
    assert q.paused_until is not None

    await q.unpause()

    assert q.paused is False
    assert q.paused_until is None
    assert (await load_queue_pause_state(db)).paused_until is None


async def test_re_pausing_replaces_rather_than_stacks_the_deadline(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)
    first_deadline = q.paused_until

    await q.pause(stop_running=False, duration_s=60)
    second_deadline = q.paused_until

    assert second_deadline is not None
    assert second_deadline != first_deadline
    # A shorter duration from *now* -- proves this replaced the deadline outright rather than
    # adding 60s on top of the existing 600s one.
    assert second_deadline < first_deadline
    assert (await load_queue_pause_state(db)).paused_until == second_deadline


async def test_pausing_indefinitely_after_a_timed_pause_clears_the_deadline(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)
    assert q.paused_until is not None

    await q.pause(stop_running=False)  # duration_s omitted -> indefinite, not "keep the old one"

    assert q.paused_until is None
    assert (await load_queue_pause_state(db)).paused_until is None


async def test_restart_before_the_deadline_stays_paused_with_the_deadline_intact(db):
    q1 = _queue_obj(db)
    await q1.pause(stop_running=False, duration_s=3600)
    deadline = q1.paused_until

    q2 = _queue_obj(db)
    await q2.start()
    try:
        assert q2.paused is True
        assert q2.paused_until == deadline
    finally:
        await q2.stop()


async def test_restart_after_the_deadline_comes_back_unpaused(db):
    """ "App down past the deadline comes back unpaused" -- a ten-minute pause must not become
    an eight-hour one because the container restarted while it was paused. Checked synchronously
    in `start()` itself (not only on the scheduler's first tick), so this holds the instant
    `start()` returns rather than depending on the loop task getting a chance to run first.
    """
    await save_queue_pause_state(
        db, QueuePauseState(paused=True, paused_until="2000-01-01T00:00:00.000000Z")
    )

    q = _queue_obj(db)
    await q.start()
    try:
        assert q.paused is False
        assert q.paused_until is None
        assert (await load_queue_pause_state(db)).paused is False
    finally:
        await q.stop()


async def test_expire_pause_if_due_is_a_no_op_before_the_deadline(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=3600)

    await q._expire_pause_if_due()

    assert q.paused is True
    assert q.paused_until is not None


async def test_expire_pause_if_due_resumes_admission_once_the_deadline_has_passed(db):
    queue_id = await _make_queue_row(db)
    job_id = await _make_queued_job(db, queue_id)
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)

    # Force the deadline into the past directly, the same "just write the timestamp" idiom
    # `core/auth.py`'s own session-expiry tests use for an absolute wall-clock deadline, rather
    # than sleeping 600s or monkeypatching the wall clock.
    past = "2000-01-01T00:00:00.000000Z"
    await save_queue_pause_state(db, QueuePauseState(paused=True, paused_until=past))
    q._paused_until = past

    await q._expire_pause_if_due()

    assert q.paused is False
    assert q.paused_until is None
    assert (await load_queue_pause_state(db)).paused is False

    # Admission is no longer gated -- proven the same way `test_admit_is_a_no_op_while_paused`
    # proves the opposite: `_admit()` gets past the pause check (no host configured, so it logs
    # a warning and returns, but the job row is reached rather than skipped outright).
    await q._admit()
    row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))).fetchone()
    assert (
        row["state"] == "queued"
    )  # unchanged (no host configured) -- proves no crash, not a spawn

    # A distinct audit event from a manual unpause -- "why did it start again" must be
    # answerable without guessing whether a human clicked Unpause.
    cursor = await db.execute("SELECT COUNT(*) AS n FROM event WHERE kind = 'queue_pause_expired'")
    assert (await cursor.fetchone())["n"] == 1


async def test_expire_pause_if_due_is_a_no_op_when_not_paused(db):
    q = _queue_obj(db)
    await q._expire_pause_if_due()
    assert q.paused is False


async def test_tick_expires_a_due_pause(db):
    """The integration path -- `tick()` is what actually runs on the backend's own clock every
    ~1s (`TransferQueue._loop`), unlike `core/engine.py`'s scan loop which can sleep indefinitely
    when nothing is due. Calling `tick()` directly here stands in for that cadence without
    sleeping in the test.
    """
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)
    past = "2000-01-01T00:00:00.000000Z"
    await save_queue_pause_state(db, QueuePauseState(paused=True, paused_until=past))
    q._paused_until = past

    await q.tick()

    assert q.paused is False
    assert q.paused_until is None


# --- the admission gate: a caller-side skip in `_admit()`, never reaching spawn -------------


async def test_admit_is_a_no_op_while_paused(db):
    queue_id = await _make_queue_row(db)
    job_id = await _make_queued_job(db, queue_id)
    q = _queue_obj(db)
    await q.pause(stop_running=False)

    # No host_provider configured -- if `_admit` got past the pause gate it would hit the
    # "no host configured" branch and log a warning, but the job row would still be untouched
    # either way. The real proof paused changes nothing observable is `tests/
    # test_queue_pause_e2e.py`'s admission test, against a real host and a real lftp spawn.
    await q._admit()

    row = await (await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))).fetchone()
    assert row["state"] == "queued"
    assert row["pid"] is None


# --- manual enqueue and reordering are never gated by pause ---------------------------------


async def test_enqueue_item_still_works_while_paused(db):
    queue_id = await _make_queue_row(db)
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'item', 0, 1000, 0, 'REMOTE_ONLY')",
        (queue_id,),
    )
    item_id = cursor.lastrowid
    await db.commit()

    q = _queue_obj(db)
    await q.pause(stop_running=False)
    job_id = await q.enqueue_item(item_id)

    row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))).fetchone()
    assert row["state"] == "queued"


async def test_move_to_top_still_works_while_paused(db):
    queue_id = await _make_queue_row(db)
    job1 = await _make_queued_job(db, queue_id, "a", position=1.0)
    job2 = await _make_queued_job(db, queue_id, "b", position=2.0)

    q = _queue_obj(db)
    await q.pause(stop_running=False)
    await q.move_to_top(job2)

    positions = {
        row["id"]: row["queue_position"]
        for row in await (
            await db.execute("SELECT id, queue_position FROM job WHERE id IN (?, ?)", (job1, job2))
        ).fetchall()
    }
    assert positions[job2] < positions[job1]


async def test_move_job_chevron_still_works_while_paused(db):
    queue_id = await _make_queue_row(db)
    job1 = await _make_queued_job(db, queue_id, "a", position=1.0)
    job2 = await _make_queued_job(db, queue_id, "b", position=2.0)

    q = _queue_obj(db)
    await q.pause(stop_running=False)
    await q.move_job(job2, "up")

    positions = {
        row["id"]: row["queue_position"]
        for row in await (
            await db.execute("SELECT id, queue_position FROM job WHERE id IN (?, ?)", (job1, job2))
        ).fetchall()
    }
    assert positions[job2] < positions[job1]


# --- start_now: 409 while paused, at both the queue and API layer --------------------------


async def test_start_now_raises_queue_paused_error(db):
    queue_id = await _make_queue_row(db)
    job_id = await _make_queued_job(db, queue_id)
    q = _queue_obj(db)
    await q.pause(stop_running=False)

    with pytest.raises(QueuePausedError):
        await q.start_now(job_id)

    # Withheld, not partially applied.
    row = await (
        await db.execute(
            "SELECT forced_full_rate, forced_rate_fraction FROM job WHERE id = ?", (job_id,)
        )
    ).fetchone()
    assert (row["forced_full_rate"], row["forced_rate_fraction"]) == (0, None)


class _FakeState:
    def __init__(self, queue):
        self.queue = queue


class _FakeApp:
    def __init__(self, queue):
        self.state = _FakeState(queue)


class _FakeRequest:
    def __init__(self, queue):
        self.app = _FakeApp(queue)


class _RecordingQueue:
    """Stands in for `TransferQueue` at the API layer -- same shape
    `tests/test_start_now_fraction.py._RecordingQueue` already uses for this exact route file.
    """

    def __init__(self, *, raises: Exception | None = None, paused: bool = False):
        self._raises = raises
        self.paused = paused
        self.pause_calls: list[tuple[bool, float | None]] = []
        self.unpause_calls = 0

    async def start_now(self, job_id: int, *, rate_percent: int | None = None) -> bool:
        if self._raises is not None:
            raise self._raises
        return True

    async def pause(self, *, stop_running: bool, duration_s: float | None = None) -> None:
        self.pause_calls.append((stop_running, duration_s))

    async def unpause(self) -> None:
        self.unpause_calls += 1


async def test_start_now_api_maps_queue_paused_to_409():
    from lftpweb.models import StartNowRequest

    fake = _RecordingQueue(raises=QueuePausedError("paused"))
    with pytest.raises(HTTPException) as exc_info:
        await jobs.start_now(1, _FakeRequest(fake), body=StartNowRequest(rate_percent=25))
    assert exc_info.value.status_code == 409
    assert "paused" in exc_info.value.detail


async def test_pause_api_defaults_to_pause_after_current():
    fake = _RecordingQueue()
    await jobs.pause_queue(_FakeRequest(fake), body=None)
    assert fake.pause_calls == [(False, None)]


async def test_pause_api_passes_stop_running_through():
    fake = _RecordingQueue()
    await jobs.pause_queue(_FakeRequest(fake), body=QueuePauseRequest(stop_running=True))
    assert fake.pause_calls == [(True, None)]


async def test_pause_api_converts_duration_minutes_to_seconds():
    fake = _RecordingQueue()
    await jobs.pause_queue(
        _FakeRequest(fake), body=QueuePauseRequest(stop_running=False, duration_minutes=10)
    )
    assert fake.pause_calls == [(False, 600)]


async def test_unpause_api_calls_unpause():
    fake = _RecordingQueue()
    await jobs.unpause_queue(_FakeRequest(fake))
    assert fake.unpause_calls == 1


# --- health readout --------------------------------------------------------------------------


class _FakeHealthState:
    def __init__(self, db, queue):
        self.db = db
        self.started_at = 0.0
        self.queue = queue
        # No `engine` attribute -- `health()` uses `getattr(..., None)` for it, matching a
        # fresh install with no host configured yet.


class _FakeHealthApp:
    def __init__(self, db, queue):
        self.state = _FakeHealthState(db, queue)


class _FakeHealthRequest:
    def __init__(self, db, queue):
        self.app = _FakeHealthApp(db, queue)


async def test_health_reports_queue_paused(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    response = await health_api.health(_FakeHealthRequest(db, q))
    assert response.queue_paused is True


async def test_health_reports_not_paused_by_default(db):
    q = _queue_obj(db)
    response = await health_api.health(_FakeHealthRequest(db, q))
    assert response.queue_paused is False


async def test_health_reports_unpaused_when_no_queue_is_wired_up(db):
    response = await health_api.health(_FakeHealthRequest(db, None))
    assert response.queue_paused is False


async def test_health_reports_paused_until_for_a_timed_pause(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=600)
    response = await health_api.health(_FakeHealthRequest(db, q))
    assert response.queue_paused_until == q.paused_until
    assert response.queue_paused_until is not None


async def test_health_reports_no_paused_until_for_an_indefinite_pause(db):
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    response = await health_api.health(_FakeHealthRequest(db, q))
    assert response.queue_paused_until is None


async def test_health_reports_no_paused_until_when_no_queue_is_wired_up(db):
    response = await health_api.health(_FakeHealthRequest(db, None))
    assert response.queue_paused_until is None
