"""Change the site bandwidth limit from the Queue page
(`prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`, DESIGN.md §4.5).

DB/API-level only -- no lftp process or fake seedbox needed here: the setting write is a
`setting` row, the validation floors are arithmetic, and "apply to in-progress" is
`_pause_running_jobs` (already proven against a real child by `tests/test_queue_pause_e2e.py`)
plus a transient admission hold. The real stop-and-re-admit against a *running* lftp -- new
allocation, resumed from partial bytes, no `FAILED`, no suppression -- is
`tests/test_queue_bandwidth_e2e.py`'s job.

**The invariant under test throughout** (§4.5): a running job's allocation is never re-shaped.
This feature does not weaken it -- it re-admits, which is the invariant being obeyed, not an
exception to it. `core/scheduler.py` is untouched, and the tests below assert that a
setting-only change leaves every running allocation exactly where it was.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs
from lftpweb.core import scheduler
from lftpweb.core.events import EventBus
from lftpweb.core.queue import (
    BandwidthChangeOutcome,
    SiteBandwidthTooLowError,
    TransferQueue,
    TransferSettings,
    load_queue_pause_state,
    load_transfer_settings,
    save_transfer_settings,
)
from lftpweb.db import migrate
from lftpweb.models import QueueBandwidthRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


def _queue_obj(db) -> TransferQueue:
    return TransferQueue(db, "/config", EventBus())


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


class _FakeProc:
    """Enough of `_RunningProcess` for `len(self._running)` and the allocation assertions --
    these tests never let a real `_pause_running_jobs` run (it is stubbed out below), so no
    `SpawnedJob`/`wait_task` is needed. The one thing that matters is that a running job carries
    an allocation, and that a setting-only change leaves it alone.
    """

    def __init__(self, job_id: int, rate_limit_bps: int) -> None:
        self.job_id = job_id
        self.rate_limit_bps = rate_limit_bps


class _StopRecorder:
    """Stands in for `TransferQueue._pause_running_jobs` -- records that it was called, and
    (crucially) what `self._admission_hold` was *while* it ran, which is the only moment that
    flag is observable from outside.
    """

    def __init__(self, q: TransferQueue, *, raises: Exception | None = None) -> None:
        self.q = q
        self.calls = 0
        self.hold_during_call: bool | None = None
        self.raises = raises

    async def __call__(self) -> None:
        self.calls += 1
        self.hold_during_call = self.q._admission_hold
        if self.raises is not None:
            raise self.raises
        self.q._running.clear()


async def _settings(db, **overrides) -> TransferSettings:
    settings = TransferSettings(**overrides)
    await save_transfer_settings(db, settings)
    return settings


# --- the setting write itself ----------------------------------------------------------------


async def test_the_slider_writes_the_site_wide_max_bandwidth(db):
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)

    outcome = await q.set_site_bandwidth(4_000_000, apply_to_running=False)

    assert outcome == BandwidthChangeOutcome(max_bandwidth_bps=4_000_000)
    assert (await load_transfer_settings(db)).max_bandwidth_bps == 4_000_000


async def test_the_slider_leaves_every_other_transfer_setting_alone(db):
    """One control, one setting (the task's own framing) -- this is the *same* site-wide value
    Settings -> Transfer owns, so writing it must not read-modify-write the eleven fields the
    Queue tab never shows.
    """
    original = await _settings(
        db,
        max_bandwidth_bps=10_000_000,
        max_concurrent_transfers=5,
        small_item_threshold_bytes=42,
        small_lane_concurrency=7,
        small_lane_reserve_bps=123_456,
        min_share_floor_bps=100_000,
        mirror_parallel_transfer_count=8,
        mirror_use_pget_n=9,
        pget_default_n=3,
        max_attempts=11,
        retry_backoff_base_s=17.5,
        extra_lftp_settings="set foo bar",
    )
    q = _queue_obj(db)

    await q.set_site_bandwidth(3_000_000, apply_to_running=False)

    after = await load_transfer_settings(db)
    assert after.max_bandwidth_bps == 3_000_000
    for field in TransferSettings.__dataclass_fields__:
        if field == "max_bandwidth_bps":
            continue
        assert getattr(after, field) == getattr(original, field), field


async def test_the_new_limit_is_what_the_next_admission_computes_from(db):
    """ "Future items only" has to actually mean something -- the scheduler reads the current
    settings on every pass, so the very next admission uses the new ceiling. §4.5's worked
    example, with the ceiling moved: one queued item, nothing running, N=2 -> it gets the whole
    headroom.
    """
    await _settings(
        db, max_bandwidth_bps=10_000_000, small_lane_reserve_bps=0, max_concurrent_transfers=2
    )
    q = _queue_obj(db)

    await q.set_site_bandwidth(4_000_000, apply_to_running=False)

    sched_settings = (await load_transfer_settings(db)).scheduler_settings()
    decisions = scheduler.admit(
        sched_settings, [], [scheduler.QueuedJob(id=1, lane="main", queue_position=1.0)]
    )
    assert [d.rate_limit_bps for d in decisions] == [4_000_000]


async def test_the_setting_change_is_audited(db):
    await _settings(db)
    q = _queue_obj(db)

    await q.set_site_bandwidth(6_000_000, apply_to_running=False)

    row = await (
        await db.execute("SELECT kind, message FROM event ORDER BY id DESC LIMIT 1")
    ).fetchone()
    assert row["kind"] == "queue_bandwidth_changed"
    assert "6000000" in row["message"]


# --- "future items only": the invariant, untouched -------------------------------------------


async def test_future_items_only_never_reshapes_a_running_allocation(db, monkeypatch):
    """DESIGN.md §4.5's central invariant. A setting-only change must not stop anything, must
    not call the stop-and-requeue path, and must leave each running job's `rate_limit_bps`
    exactly as it was admitted.
    """
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    q._running[2] = _FakeProc(2, 5_000_000)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    outcome = await q.set_site_bandwidth(2_000_000, apply_to_running=False)

    assert recorder.calls == 0
    assert outcome.interrupted == 0
    assert sorted(p.rate_limit_bps for p in q._running.values()) == [5_000_000, 5_000_000]


# --- "also apply to in-progress": stop, re-queue in place, re-admit --------------------------


async def test_apply_to_running_stops_every_in_flight_transfer(db, monkeypatch):
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    q._running[2] = _FakeProc(2, 5_000_000)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    outcome = await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert recorder.calls == 1
    assert outcome.interrupted == 2
    assert outcome.skipped_because_paused is False


async def test_apply_to_running_reuses_pause_nows_stop_path_rather_than_a_second_one(db):
    """The task's own instruction: "do not write a second stop-and-respawn path." Asserted
    structurally -- `set_site_bandwidth` calls `_pause_running_jobs`, the exact method
    `pause(stop_running=True)` calls, so `_reap_one`'s `pause_requested` branch (requeue in
    place, no suppression, no `FAILED`) is inherited rather than reimplemented.
    """
    await _settings(db)
    q = _queue_obj(db)
    calls: list[str] = []

    async def _record() -> None:
        calls.append("stopped")

    q._pause_running_jobs = _record  # type: ignore[method-assign]
    q._running[1] = _FakeProc(1, 5_000_000)

    await q.set_site_bandwidth(2_000_000, apply_to_running=True)
    await q.pause(stop_running=True)

    assert calls == ["stopped", "stopped"]


async def test_apply_to_running_with_nothing_running_interrupts_nothing(db, monkeypatch):
    await _settings(db)
    q = _queue_obj(db)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    outcome = await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert recorder.calls == 0
    assert outcome.interrupted == 0
    assert (await load_transfer_settings(db)).max_bandwidth_bps == 2_000_000


async def test_apply_to_running_never_touches_the_pause_state_when_not_paused(db, monkeypatch):
    """The transient admission hold is *not* the persisted pause: an unpaused queue is still
    unpaused afterwards, and nothing was written to the pause `setting` row.
    """
    await _settings(db)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    monkeypatch.setattr(q, "_pause_running_jobs", _StopRecorder(q))

    await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert q.paused is False
    assert q._admission_hold is False
    assert (await load_queue_pause_state(db)).paused is False


# --- the transient admission hold -------------------------------------------------------------


async def test_admission_is_held_while_the_running_children_are_being_stopped(db, monkeypatch):
    await _settings(db)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert recorder.hold_during_call is True
    assert q._admission_hold is False, "the hold must be released again"


async def test_the_admission_hold_is_released_even_if_stopping_raises(db, monkeypatch):
    """A hold that leaked would wedge admission for the process lifetime with no persisted
    state to explain it -- strictly worse than the failure it followed.
    """
    await _settings(db)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    monkeypatch.setattr(q, "_pause_running_jobs", _StopRecorder(q, raises=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert q._admission_hold is False


async def test_admit_is_a_no_op_while_the_admission_hold_is_set(db):
    queue_id = await _make_queue_row(db)
    job_id = await _make_queued_job(db, queue_id)
    await _settings(db)
    q = _queue_obj(db)
    q._admission_hold = True

    await q._admit()

    row = await (await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))).fetchone()
    assert row["state"] == "queued"
    assert row["pid"] is None


# --- the already-paused queue: write the number, touch nothing else ---------------------------


async def test_apply_to_running_while_paused_does_not_unpause(db, monkeypatch):
    await _settings(db)
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    outcome = await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert q.paused is True
    assert (await load_queue_pause_state(db)).paused is True
    assert outcome.skipped_because_paused is True
    assert outcome.interrupted == 0
    assert recorder.calls == 0


async def test_apply_to_running_while_paused_preserves_a_timed_pauses_deadline(db, monkeypatch):
    """The case the pause-for-duration work (2026-08-21) created: a user who set "pause for 30
    minutes" and then moved the bandwidth slider must still be paused for 30 minutes. A literal
    "pause now then unpause" implementation would have overwritten the deadline *and* resumed
    the queue.
    """
    await _settings(db)
    q = _queue_obj(db)
    await q.pause(stop_running=False, duration_s=1800)
    deadline = q.paused_until
    assert deadline is not None
    monkeypatch.setattr(q, "_pause_running_jobs", _StopRecorder(q))

    await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert q.paused is True
    assert q.paused_until == deadline
    persisted = await load_queue_pause_state(db)
    assert persisted.paused is True
    assert persisted.paused_until == deadline


async def test_apply_to_running_while_paused_after_current_leaves_running_jobs_running(
    db, monkeypatch
):
    """A paused queue can still have running jobs -- "pause after current" leaves them alone on
    purpose. Stopping them here would silently upgrade the user's "pause after current" into a
    "pause now", *and* strand them as `queued` with admission closed until the pause ended.
    """
    await _settings(db)
    q = _queue_obj(db)
    await q.pause(stop_running=False)
    q._running[1] = _FakeProc(1, 5_000_000)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    outcome = await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert recorder.calls == 0
    assert set(q._running) == {1}
    assert q._running[1].rate_limit_bps == 5_000_000
    assert outcome.interrupted == 0


async def test_apply_to_running_while_paused_still_writes_the_new_limit(db):
    """Changing the *number* while paused is reasonable -- curating before resuming is what
    pausing is for (§4.5). Only the interruption is meaningless there.
    """
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)
    await q.pause(stop_running=False)

    await q.set_site_bandwidth(2_000_000, apply_to_running=True)

    assert (await load_transfer_settings(db)).max_bandwidth_bps == 2_000_000


# --- validation floors ------------------------------------------------------------------------


async def test_zero_is_rejected_because_it_is_not_unlimited(db):
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)

    with pytest.raises(SiteBandwidthTooLowError):
        await q.set_site_bandwidth(0, apply_to_running=False)

    assert (await load_transfer_settings(db)).max_bandwidth_bps == 10_000_000


async def test_a_negative_limit_is_rejected(db):
    await _settings(db, max_bandwidth_bps=10_000_000)
    q = _queue_obj(db)

    with pytest.raises(SiteBandwidthTooLowError):
        await q.set_site_bandwidth(-1, apply_to_running=False)


async def test_below_the_min_share_floor_is_rejected(db):
    """Reuses the existing `min_share_floor_bps` as the bound rather than inventing one -- a
    ceiling under the per-job floor means the very first admission already violates it.
    """
    await _settings(db, max_bandwidth_bps=10_000_000, min_share_floor_bps=500_000)
    q = _queue_obj(db)

    with pytest.raises(SiteBandwidthTooLowError):
        await q.set_site_bandwidth(499_999, apply_to_running=False)

    assert (await load_transfer_settings(db)).max_bandwidth_bps == 10_000_000


async def test_exactly_the_min_share_floor_is_accepted(db):
    await _settings(db, max_bandwidth_bps=10_000_000, min_share_floor_bps=500_000)
    q = _queue_obj(db)

    await q.set_site_bandwidth(500_000, apply_to_running=False)

    assert (await load_transfer_settings(db)).max_bandwidth_bps == 500_000


async def test_a_rejected_value_never_stops_anything(db, monkeypatch):
    await _settings(db, max_bandwidth_bps=10_000_000, min_share_floor_bps=500_000)
    q = _queue_obj(db)
    q._running[1] = _FakeProc(1, 5_000_000)
    recorder = _StopRecorder(q)
    monkeypatch.setattr(q, "_pause_running_jobs", recorder)

    with pytest.raises(SiteBandwidthTooLowError):
        await q.set_site_bandwidth(1, apply_to_running=True)

    assert recorder.calls == 0
    assert q._admission_hold is False


# --- the API layer -----------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, queue) -> None:
        class _App:
            def __init__(self, queue) -> None:
                class _State:
                    pass

                self.state = _State()
                self.state.queue = queue

        self.app = _App(queue)


class _RecordingQueue:
    def __init__(self, *, outcome: BandwidthChangeOutcome | None = None, raises=None) -> None:
        self.calls: list[tuple[int, bool]] = []
        self.outcome = outcome or BandwidthChangeOutcome(max_bandwidth_bps=1)
        self.raises = raises

    async def set_site_bandwidth(self, max_bandwidth_bps: int, *, apply_to_running: bool):
        self.calls.append((max_bandwidth_bps, apply_to_running))
        if self.raises is not None:
            raise self.raises
        return self.outcome


async def test_bandwidth_api_defaults_to_future_items_only():
    fake = _RecordingQueue()
    await jobs.set_queue_bandwidth(
        QueueBandwidthRequest(max_bandwidth_bps=5_000_000), _FakeRequest(fake)
    )
    assert fake.calls == [(5_000_000, False)]


async def test_bandwidth_api_passes_apply_to_running_through():
    fake = _RecordingQueue()
    await jobs.set_queue_bandwidth(
        QueueBandwidthRequest(max_bandwidth_bps=5_000_000, apply_to_running=True),
        _FakeRequest(fake),
    )
    assert fake.calls == [(5_000_000, True)]


async def test_bandwidth_api_returns_the_outcome():
    fake = _RecordingQueue(
        outcome=BandwidthChangeOutcome(max_bandwidth_bps=5_000_000, interrupted=3)
    )
    response = await jobs.set_queue_bandwidth(
        QueueBandwidthRequest(max_bandwidth_bps=5_000_000, apply_to_running=True),
        _FakeRequest(fake),
    )
    assert response.max_bandwidth_bps == 5_000_000
    assert response.interrupted == 3
    assert response.skipped_because_paused is False


async def test_bandwidth_api_surfaces_the_paused_skip():
    fake = _RecordingQueue(
        outcome=BandwidthChangeOutcome(max_bandwidth_bps=5_000_000, skipped_because_paused=True)
    )
    response = await jobs.set_queue_bandwidth(
        QueueBandwidthRequest(max_bandwidth_bps=5_000_000, apply_to_running=True),
        _FakeRequest(fake),
    )
    assert response.skipped_because_paused is True
    assert response.interrupted == 0


async def test_bandwidth_api_maps_a_too_low_limit_to_400():
    fake = _RecordingQueue(raises=SiteBandwidthTooLowError("below the floor"))
    with pytest.raises(HTTPException) as exc_info:
        await jobs.set_queue_bandwidth(
            QueueBandwidthRequest(max_bandwidth_bps=1), _FakeRequest(fake)
        )
    assert exc_info.value.status_code == 400
    assert "below the floor" in exc_info.value.detail


def test_bandwidth_request_rejects_a_non_positive_limit():
    """`gt=0` is the cheap half of the validation, checked without a DB read -- a 422 before the
    handler ever runs. The floor check (which depends on `min_share_floor_bps`) is the queue's.
    """
    with pytest.raises(ValueError):
        QueueBandwidthRequest(max_bandwidth_bps=0)
