"""Live per-file progress inside a mirroring directory (`core/queue.py._publish_child_progress`,
`prompts/done/2026-08-12-live-child-progress.md`).

No seedbox needed -- unlike `tests/test_queue.py`, nothing here spawns a real lftp process.
`_sample_and_publish_progress`/`_publish_child_progress` only read the filesystem and the
database, so a `_RunningProcess` is built by hand (its `spawned`/`wait_task` fields are never
touched by the code path under test) pointed at a `tmp_path` directory whose file sizes are
grown directly -- the same shape `tests/test_queue_orphans.py` uses to test queue internals
without the fake-seedbox dependency.
"""

from __future__ import annotations

import asyncio

import pytest

from lftpweb.core.progress import DEFAULT_EMA_ALPHA, ActiveJob
from lftpweb.core.queue import PROGRESS_SAMPLE_TICKS, _RunningProcess
from lftpweb.core.settle import SettleSettings, save_settle_settings
from test_queue import _make_db, _make_host_row, _make_item_row, _make_queue_row, _queue_for
from test_queue_completeness import _fake_spawned, _resolved_wait_task


@pytest.fixture
async def db(tmp_path):
    conn = await _make_db(tmp_path)
    try:
        yield conn
    finally:
        await conn.close()


def _running_process(
    *, job_id: int, item_id: int, queue_id: int, rel_path: str, local_root: str, bytes_total: int
) -> _RunningProcess:
    """A `mirror` job's bookkeeping row, without an actual lftp child -- see module docstring."""
    return _RunningProcess(
        job_id=job_id,
        item_id=item_id,
        queue_id=queue_id,
        rel_path=rel_path,
        is_dir=True,
        kind="mirror",
        lane="main",
        rate_limit_bps=0,
        forced_full_rate=False,
        local_root=local_root,
        bytes_total=bytes_total,
        remote_mtime=None,
        spawned=None,
        wait_task=None,
    )


def _item_deltas(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["type"] == "item_delta"]


def _child_progress_items(messages: list[dict]) -> dict[int, float]:
    """Every `child_progress` message's `items`, flattened to `item_id -> speed_bps` -- this
    task's own new WS message (2026-08-14, "per-file speed inside a mirror").
    """
    out: dict[int, float] = {}
    for m in messages:
        if m["type"] != "child_progress":
            continue
        for it in m["items"]:
            out[it["item_id"]] = it["speed_bps"]
    return out


async def _drain(events, queue) -> list[dict]:
    """Every message an already-subscribed queue has accumulated, non-blocking (`publish` is
    synchronous `put_nowait`, so nothing here needs to await a producer).
    """
    messages = []
    while True:
        try:
            messages.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    events.unsubscribe(queue)
    return messages


async def _setup(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    release_dir = local_dir / "Release"
    release_dir.mkdir()

    parent_id = await _make_item_row(db, queue_id, "Release", is_dir=True, remote_size=1500)
    await db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (parent_id,))
    a_id = await _make_item_row(db, queue_id, "Release/a.rar", is_dir=False, remote_size=1000)
    b_id = await _make_item_row(db, queue_id, "Release/b.rar", is_dir=False, remote_size=500)
    await db.commit()

    q = await _queue_for(db, tmp_path)
    job_id = 1
    q._running[job_id] = _running_process(
        job_id=job_id,
        item_id=parent_id,
        queue_id=queue_id,
        rel_path="Release",
        local_root=str(release_dir),
        bytes_total=1500,
    )
    return q, release_dir, queue_id, {"parent": parent_id, "a": a_id, "b": b_id}


async def _tick_through_throttle(q) -> None:
    """Enough calls to `_sample_and_publish_progress` to reach the next tick that actually
    samples and publishes anything at all -- 2026-08-16, job-level sampling, the parent's
    `item_delta`, and child publishing all gate on the same `PROGRESS_SAMPLE_TICKS` counter now
    (previously only child publishing was throttled this way), so one shared helper drives all
    three instead of a child-only one.
    """
    for _ in range(PROGRESS_SAMPLE_TICKS):
        await q._sample_and_publish_progress()


async def _item_row(db, item_id):
    cursor = await db.execute("SELECT state, local_size FROM item WHERE id = ?", (item_id,))
    return await cursor.fetchone()


# --- children progress and reach DOWNLOADED independently of the parent and each other -------


async def test_children_progress_and_reach_downloaded_independently(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    (release_dir / "a.rar").write_bytes(b"a" * 400)  # partial
    (release_dir / "b.rar").write_bytes(b"b" * 500)  # complete

    events_queue = q.events.subscribe()
    await _tick_through_throttle(q)
    messages = await _drain(q.events, events_queue)

    deltas = _item_deltas(messages)
    assert deltas, "at least one item_delta must have been published"
    published_by_id = {node["id"]: node for d in deltas for node in d["nodes"]}
    assert ids["a"] in published_by_id
    assert ids["b"] in published_by_id
    assert published_by_id[ids["a"]]["state"] == "PARTIAL"
    assert published_by_id[ids["a"]]["local_size"] == 400
    assert published_by_id[ids["b"]]["state"] == "DOWNLOADED"
    assert published_by_id[ids["b"]]["local_size"] == 500

    a_row = await _item_row(db, ids["a"])
    assert a_row["state"] == "PARTIAL"
    assert a_row["local_size"] == 400
    b_row = await _item_row(db, ids["b"])
    assert b_row["state"] == "DOWNLOADED"
    assert b_row["local_size"] == 500

    # a.rar finishes on a later throttled tick, independently of b.rar (already done above) --
    # this is the "individually, not in one batch" behaviour the defect broke.
    (release_dir / "a.rar").write_bytes(b"a" * 1000)
    await _tick_through_throttle(q)
    a_row2 = await _item_row(db, ids["a"])
    assert a_row2["state"] == "DOWNLOADED"
    assert a_row2["local_size"] == 1000


# --- the `.lftp` temp-file suffix is stripped for the child->item mapping --------------------


async def test_lftp_temp_suffix_is_stripped_for_child_mapping(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    # lftp writes the temp file under `xfer:use-temp-file` (DESIGN.md §4.4b) -- `local_scan`
    # reports it under its final name, and this must map to the item row at that final
    # rel_path, not to a nonexistent "a.rar.lftp" item.
    (release_dir / "a.rar.lftp").write_bytes(b"a" * 250)

    await _tick_through_throttle(q)

    a_row = await _item_row(db, ids["a"])
    assert a_row["local_size"] == 250
    assert a_row["state"] == "PARTIAL"


# --- an unchanged child publishes nothing on a later throttled tick --------------------------


async def test_unchanged_child_publishes_nothing_on_the_next_throttled_tick(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    (release_dir / "a.rar").write_bytes(b"a" * 300)

    await _tick_through_throttle(q)  # first throttled tick: a.rar goes from unseen to 300

    events_queue = q.events.subscribe()
    await _tick_through_throttle(q)  # no writes since -- nothing should have changed
    messages = await _drain(q.events, events_queue)

    deltas = _item_deltas(messages)
    child_ids = {node["id"] for d in deltas for node in d["nodes"]} - {ids["parent"]}
    assert child_ids == set(), "an unchanged child must not be republished"


# --- the safety cap truncates and logs, rather than silently dropping work -------------------


async def test_cap_truncates_and_logs(db, tmp_path, monkeypatch, caplog):
    q, release_dir, queue_id, ids = await _setup(db, tmp_path)
    c_id = await _make_item_row(db, queue_id, "Release/c.rar", is_dir=False, remote_size=100)
    await db.commit()

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    (release_dir / "b.rar").write_bytes(b"b" * 200)
    (release_dir / "c.rar").write_bytes(b"c" * 50)

    monkeypatch.setattr("lftpweb.core.queue.MAX_CHILD_PROGRESS_UPDATES_PER_TICK", 2)

    with caplog.at_level("WARNING"):
        await _tick_through_throttle(q)

    assert any("capped at 2 update" in r.message for r in caplog.records)

    rows = await (
        await db.execute(
            "SELECT id, local_size FROM item WHERE id IN (?, ?, ?)", (ids["a"], ids["b"], c_id)
        )
    ).fetchall()
    updated = {r["id"] for r in rows if r["local_size"]}
    assert len(updated) == 2, "exactly the cap's worth of children must have been persisted"

    # The child the cap skipped is not forgotten -- it must be picked up on a later tick.
    caplog.clear()
    monkeypatch.setattr("lftpweb.core.queue.MAX_CHILD_PROGRESS_UPDATES_PER_TICK", 100)
    await _tick_through_throttle(q)
    rows2 = await (
        await db.execute(
            "SELECT id, local_size FROM item WHERE id IN (?, ?, ?)", (ids["a"], ids["b"], c_id)
        )
    ).fetchall()
    assert all(r["local_size"] for r in rows2), "the deferred child must catch up"


# --- the parent's aggregate progress (job.bytes_done, item.local_size) is gated with children -


async def test_parent_aggregate_progress_is_gated_on_the_same_counter_as_children(db, tmp_path):
    """2026-08-16 unification: job-level sampling (`job.bytes_done`, `item.local_size`) used to
    run on *every* call to `_sample_and_publish_progress`, independently of the child-progress
    throttle -- exactly the split cadence (job ~1 Hz, child every 3rd tick) that produced two
    disagreeing EMA-smoothed speeds for a one-file directory. Now both gate on
    `PROGRESS_SAMPLE_TICKS`: a single call (not the Nth) must do nothing at all, and the Nth
    call must move both aggregates together.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    await db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await db.commit()

    (release_dir / "a.rar").write_bytes(b"a" * 100)
    (release_dir / "b.rar").write_bytes(b"b" * 50)

    # A single call -- not the PROGRESS_SAMPLE_TICKS-th -- must move neither aggregate now.
    await q._sample_and_publish_progress()
    job_row = await (await db.execute("SELECT bytes_done FROM job WHERE id = 1")).fetchone()
    assert job_row["bytes_done"] == 0, "job-level sampling must not run before the gate opens"
    parent_row = await _item_row(db, ids["parent"])
    assert parent_row["local_size"] == 0

    # The remaining calls up to PROGRESS_SAMPLE_TICKS open the gate; both aggregates move
    # together, on the exact same call as the children (see `_tick_through_throttle`).
    for _ in range(PROGRESS_SAMPLE_TICKS - 1):
        await q._sample_and_publish_progress()

    job_row = await (await db.execute("SELECT bytes_done FROM job WHERE id = 1")).fetchone()
    assert job_row["bytes_done"] == 150  # 100 + 50, summed by core/progress.py

    parent_row = await _item_row(db, ids["parent"])
    assert parent_row["local_size"] == 150
    assert parent_row["state"] == "DOWNLOADING"  # read back from `item`, not hardcoded


async def test_job_and_child_progress_publish_on_the_same_gated_tick(db, tmp_path):
    """The mechanism the unification exists for: a `progress` (job-level) message and a
    `child_progress` message must come out of the *same* `_sample_and_publish_progress` call --
    never one a few ticks ahead of the other, which is what let a one-file directory's job speed
    and child speed disagree before 2026-08-16. The first `PROGRESS_SAMPLE_TICKS - 1` calls
    below publish nothing at all (proven separately by the "is gated" test above); only the
    final, gate-opening call can have produced either message, so both landing in the same
    drain -- taken only after all `PROGRESS_SAMPLE_TICKS` calls -- proves they came out of that
    one call together.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    await q.db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await q.db.commit()
    (release_dir / "a.rar").write_bytes(b"a" * 300)

    events_queue = q.events.subscribe()
    await _tick_through_throttle(q)
    messages = await _drain(q.events, events_queue)

    types = {m["type"] for m in messages}
    assert "progress" in types
    assert "child_progress" in types, "job and child publishes must land on the same gated tick"


async def test_job_speed_after_five_ticks_matches_delta_over_real_elapsed_time(
    db, tmp_path, monkeypatch
):
    """The job-level speed math after the unification is exactly
    `core/progress.py.ProgressSampler.sample`'s real-elapsed-time derivation over the *actual*
    gap between two sampled ticks (here, `PROGRESS_SAMPLE_TICKS` calls apart) -- never
    `tick_s * PROGRESS_SAMPLE_TICKS` assumed. A fake, controlled `time.monotonic()` proves it:
    the two sampled ticks are set exactly 5.0s apart regardless of how many calls separate them.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    await q.db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await q.db.commit()

    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock["t"]

    monkeypatch.setattr("lftpweb.core.progress.time.monotonic", fake_monotonic)
    monkeypatch.setattr("lftpweb.core.queue.time.monotonic", fake_monotonic)

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    (release_dir / "b.rar").write_bytes(b"b" * 200)

    for i in range(PROGRESS_SAMPLE_TICKS):
        clock["t"] = float(i + 1)  # a real 1.0s per underlying tick, gate opens at t=5.0
        await q._sample_and_publish_progress()

    job_row = await (await db.execute("SELECT bytes_done FROM job WHERE id = 1")).fetchone()
    assert job_row["bytes_done"] == 500  # first sample: no prior history, speed 0

    events_queue = q.events.subscribe()
    (release_dir / "a.rar").write_bytes(b"a" * 800)  # +500 bytes total across both files
    for _ in range(PROGRESS_SAMPLE_TICKS):
        clock["t"] += 1.0  # 6.0 .. 10.0 -- another real 5.0s elapsed by the next gated tick
        await q._sample_and_publish_progress()
    messages = await _drain(q.events, events_queue)

    progress_msgs = [m for m in messages if m["type"] == "progress"]
    assert len(progress_msgs) == 1, "exactly one job-level publish per PROGRESS_SAMPLE_TICKS"
    job = next(j for j in progress_msgs[0]["jobs"] if j["job_id"] == 1)
    # instantaneous = 500 bytes / 5.0s real elapsed; ema_step seeds its base at the previous
    # (real) speed, 0.0 after the first sample above -- not at `instantaneous` itself.
    expected = DEFAULT_EMA_ALPHA * (500.0 / 5.0)
    assert job["speed_bps"] == pytest.approx(expected)


async def test_reap_is_not_gated_by_the_progress_sample_counter(db, tmp_path):
    """Stop/reap latency must be unaffected by the progress-sampling gate (2026-08-16 unify):
    `tick()` calls `_reap_finished()` before `_sample_and_publish_progress`, unconditionally,
    every real `tick_s` -- a finished job must be reaped on the very next `tick()` regardless of
    which of the `PROGRESS_SAMPLE_TICKS` phases the progress gate happens to be in.
    """
    q, _release_dir, _queue_id, ids = await _setup(db, tmp_path)
    # Advance the shared counter to a non-gated phase (1, not a multiple of
    # PROGRESS_SAMPLE_TICKS) so this reap happens on a tick that does *not* also sample
    # progress -- proving the two are independent, not merely coincidentally both running.
    q._progress_tick_count = 1
    await q.db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await q.db.commit()

    finished_proc = _RunningProcess(
        job_id=1,
        item_id=ids["parent"],
        queue_id=_queue_id,
        rel_path="Release",
        is_dir=True,
        kind="mirror",
        lane="main",
        rate_limit_bps=0,
        forced_full_rate=False,
        local_root=str(_release_dir),
        bytes_total=1500,
        remote_mtime=None,
        spawned=_fake_spawned(tmp_path, 1),
        wait_task=_resolved_wait_task(0, "ok"),
    )
    q._running[1] = finished_proc
    # Let the already-resolved coroutine actually run to completion -- `asyncio.create_task`
    # only schedules it, so `_reap_finished`'s `wait_task.done()` filter needs one real event
    # loop turn before it can see it, same as any other freshly created task.
    await asyncio.sleep(0)
    assert finished_proc.wait_task.done()

    await q.tick()

    assert 1 not in q._running, "a finished job must reap on the very next tick, gate or not"
    job_row = await (await db.execute("SELECT state FROM job WHERE id = 1")).fetchone()
    assert job_row["state"] == "succeeded"


# --- _flush_child_progress_final: the reap-time correction for a stale throttled reading -----
#
# 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md, defect 3). The throttled sampler
# above only persists a child on a tick that's a multiple of PROGRESS_SAMPLE_TICKS, and
# only for files whose size changed since the *previous* throttled tick -- so a job that
# finishes between two throttled ticks can leave a child's row reading a stale PARTIAL forever
# if nothing else ever revisits it (the exact shape a `move` queue produces: post-processing
# relocates the release out of both trees before any further engine scan gets the chance).
# `_flush_child_progress_final` is `_reap_one`'s fix -- one more, unthrottled, unconditional
# walk of the job's own directory the moment it reaps successfully.


async def test_flush_child_progress_final_corrects_a_stale_partial_left_by_the_throttle(
    db, tmp_path
):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    # A throttled tick catches a.rar mid-transfer and persists PARTIAL -- exactly the reading
    # the bug leaves behind if nothing else ever runs again for this job.
    (release_dir / "a.rar").write_bytes(b"a" * 400)
    (release_dir / "b.rar").write_bytes(b"b" * 500)
    await _tick_through_throttle(q)
    assert (await _item_row(db, ids["a"]))["state"] == "PARTIAL"

    # The job finishes for real between throttled ticks -- a.rar reaches its full size on disk,
    # but nothing has sampled it since the PARTIAL write above.
    (release_dir / "a.rar").write_bytes(b"a" * 1000)

    proc = q._running[1]
    events_queue = q.events.subscribe()
    await q._flush_child_progress_final(proc)
    messages = await _drain(q.events, events_queue)

    a_row = await _item_row(db, ids["a"])
    assert a_row["state"] == "DOWNLOADED", "the stale PARTIAL must not survive the final flush"
    assert a_row["local_size"] == 1000
    b_row = await _item_row(db, ids["b"])
    assert b_row["state"] == "DOWNLOADED"
    assert b_row["local_size"] == 500

    deltas = _item_deltas(messages)
    published_by_id = {node["id"]: node for d in deltas for node in d["nodes"]}
    assert published_by_id[ids["a"]]["state"] == "DOWNLOADED"


async def test_flush_child_progress_final_is_a_no_op_for_a_pget_job(db, tmp_path):
    """A `pget` job is a single file with no children -- there is nothing for this to walk, and
    it must not raise trying.
    """
    q, _release_dir, queue_id, _ids = await _setup(db, tmp_path)
    single_id = await _make_item_row(db, queue_id, "loose.txt", is_dir=False, remote_size=10)
    await db.commit()
    proc = _RunningProcess(
        job_id=2,
        item_id=single_id,
        queue_id=queue_id,
        rel_path="loose.txt",
        is_dir=False,
        kind="pget",
        lane="main",
        rate_limit_bps=0,
        forced_full_rate=False,
        local_root=str(tmp_path / "local" / "loose.txt"),
        bytes_total=10,
        remote_mtime=None,
        spawned=None,
        wait_task=None,
    )
    await q._flush_child_progress_final(proc)  # must not raise


async def test_flush_child_progress_final_is_a_no_op_when_local_root_no_longer_exists(db, tmp_path):
    """A job whose directory has already been relocated (or never existed) by the time this
    runs must not crash -- `local_scan.scan_local` already returns `{}` for a missing root.
    """
    q, release_dir, _queue_id, _ids = await _setup(db, tmp_path)
    import shutil

    shutil.rmtree(release_dir)
    proc = q._running[1]
    await q._flush_child_progress_final(proc)  # must not raise


# --- per-child speed (this task, 2026-08-14: "per-file speed inside a mirror") ----------------
#
# `_publish_child_progress` now also emits a third WS message, `child_progress`, carrying a
# live EMA-smoothed rate per changed child -- these tests exercise it directly (bypassing
# `_sample_and_publish_progress`'s own `time.monotonic()` call) so the elapsed time between two
# samples is exactly controlled, the same shape `tests/test_progress.py` already uses for
# `ProgressSampler.sample`'s own `now` parameter.


async def test_child_progress_message_is_published_with_a_zero_rate_on_first_sample(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    (release_dir / "a.rar").write_bytes(b"a" * 300)

    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]
    results = q.progress.sample(active, now=0.0)
    by_queue: dict = {}
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results, by_queue, now=0.0)
    messages = await _drain(q.events, events_queue)

    speeds = _child_progress_items(messages)
    assert speeds.get(ids["a"]) == 0.0  # no prior sample to derive a rate from


async def test_child_progress_rate_uses_the_real_elapsed_time_between_two_samples(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results1 = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results1, {}, now=0.0)

    (release_dir / "a.rar").write_bytes(b"a" * 800)  # +500 bytes
    results2 = q.progress.sample(active, now=1.0)  # a real 1.0s later
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=1.0)
    messages = await _drain(q.events, events_queue)

    speeds = _child_progress_items(messages)
    # ema_step(instantaneous=500/1.0, prev_speed=0.0, alpha=DEFAULT_EMA_ALPHA=0.3)
    assert speeds[ids["a"]] == pytest.approx(0.3 * 500.0)


async def test_child_progress_rate_is_not_derived_from_tick_count_but_a_real_timestamp(
    db, tmp_path
):
    """The denominator is `now - prev_time` (both real `time.monotonic()`-shaped values), never
    `tick_s * PROGRESS_SAMPLE_TICKS` -- a slow pass between two throttled ticks must still
    produce the correct rate, not one derived from an assumed cadence. Same delta as the
    test above, but 5 real seconds apart instead of 1 -- a materially different rate proves the
    elapsed time, not a constant, drove the computation.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results1 = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results1, {}, now=0.0)

    (release_dir / "a.rar").write_bytes(b"a" * 800)  # +500 bytes, this time over 5s
    results2 = q.progress.sample(active, now=5.0)
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=5.0)
    messages = await _drain(q.events, events_queue)

    speeds = _child_progress_items(messages)
    assert speeds[ids["a"]] == pytest.approx(0.3 * (500.0 / 5.0))


async def test_child_progress_zero_delta_reads_as_zero_rate(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results1 = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results1, {}, now=0.0)

    (release_dir / "b.rar").write_bytes(b"b" * 500)  # a *different* child changes, not a.rar
    results2 = q.progress.sample(active, now=1.0)
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=1.0)
    messages = await _drain(q.events, events_queue)

    # a.rar did not change on this tick, so it is not republished at all (the existing
    # unchanged-child rule) -- only b.rar, itself a first sample, appears at 0.0.
    speeds = _child_progress_items(messages)
    assert ids["a"] not in speeds
    assert speeds[ids["b"]] == 0.0


async def test_child_progress_never_produces_a_negative_rate_when_a_file_shrinks(db, tmp_path):
    """A file replaced or truncated mid-transfer can report a smaller size than the previous
    sample -- must read as "no progress", never a negative rate.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 800)
    results1 = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results1, {}, now=0.0)

    (release_dir / "a.rar").write_bytes(b"a" * 300)  # shrinks -- replaced/truncated
    results2 = q.progress.sample(active, now=1.0)
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=1.0)
    messages = await _drain(q.events, events_queue)

    speeds = _child_progress_items(messages)
    assert speeds[ids["a"]] >= 0


async def test_child_progress_zero_or_sub_second_elapsed_reads_as_zero_not_a_crash(db, tmp_path):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results1 = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results1, {}, now=0.0)

    (release_dir / "a.rar").write_bytes(b"a" * 800)
    results2 = q.progress.sample(active, now=0.0)  # same instant -- no elapsed time at all
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=0.0)
    messages = await _drain(q.events, events_queue)

    speeds = _child_progress_items(messages)
    assert speeds[ids["a"]] == 0.0


async def test_child_progress_is_a_third_message_never_folded_into_progress_or_item_delta(
    db, tmp_path
):
    """`progress` (job-centric) and `item_delta` (`item_view()` projections) must keep their
    existing shapes exactly -- a live per-child rate is neither a job nor a persisted column.
    """
    q, release_dir, queue_id, ids = await _setup(db, tmp_path)
    await q.db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await q.db.commit()
    (release_dir / "a.rar").write_bytes(b"a" * 300)

    events_queue = q.events.subscribe()
    await _tick_through_throttle(q)
    messages = await _drain(q.events, events_queue)

    progress_msgs = [m for m in messages if m["type"] == "progress"]
    assert progress_msgs, "the job-centric progress message must still be published"
    for job in progress_msgs[0]["jobs"]:
        assert set(job) == {"job_id", "item_id", "bytes_done", "bytes_total", "speed_bps", "eta_s"}
        assert "children" not in job  # no per-child pseudo-entries leaking into this message

    item_delta_msgs = _item_deltas(messages)
    assert item_delta_msgs
    for node in item_delta_msgs[0]["nodes"]:
        # `item_view`'s exact projected shape -- a live rate is not one of these keys.
        assert "speed_bps" not in node

    child_msgs = [m for m in messages if m["type"] == "child_progress"]
    assert child_msgs, "a.rar's change must have produced a child_progress message too"
    for m in child_msgs:
        assert set(m) == {"type", "items"}
        for it in m["items"]:
            assert set(it) == {"item_id", "speed_bps"}
    assert ids["a"] in _child_progress_items(child_msgs)


async def test_child_speed_history_is_pruned_when_the_job_reaps(db, tmp_path):
    """`_reap_one` must clear `_prev_child_times`/`_child_speed` alongside the existing
    `_prev_child_sizes` prune -- otherwise a future job id reusing job 1 (SQLite `INTEGER
    PRIMARY KEY` reuse is not guaranteed impossible) could inherit a stale rate history.
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    await save_settle_settings(q.db, SettleSettings(enabled=False))
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results = q.progress.sample(active, now=0.0)
    await q._publish_child_progress(results, {}, now=0.0)
    assert 1 in q._prev_child_sizes
    assert 1 in q._prev_child_times
    assert 1 in q._child_speed

    await q.db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await q.db.commit()

    old_proc = q._running[1]
    reaped_proc = _RunningProcess(
        job_id=1,
        item_id=old_proc.item_id,
        queue_id=old_proc.queue_id,
        rel_path=old_proc.rel_path,
        is_dir=True,
        kind="mirror",
        lane="main",
        rate_limit_bps=0,
        forced_full_rate=False,
        local_root=old_proc.local_root,
        bytes_total=old_proc.bytes_total,
        remote_mtime=None,
        spawned=_fake_spawned(tmp_path, 1),
        wait_task=_resolved_wait_task(0, "ok"),
    )
    q._running[1] = reaped_proc
    await q._reap_one(reaped_proc)

    assert 1 not in q._prev_child_sizes
    assert 1 not in q._prev_child_times
    assert 1 not in q._child_speed


async def test_child_progress_disappears_once_the_parent_job_is_no_longer_running(db, tmp_path):
    """Once a job leaves `self._running` (reaped, stopped, or simply gone from the active set),
    `_sample_and_publish_progress` no longer includes it in `results` at all, so
    `_publish_child_progress` has nothing to say about any of its children on the next tick --
    the freshness-by-construction half of the frontend's gating (`docs/decisions.md`).
    """
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    active = [ActiveJob(job_id=1, kind="mirror", local_root=str(release_dir), bytes_total=1500)]

    (release_dir / "a.rar").write_bytes(b"a" * 300)
    results1 = q.progress.sample(active, now=0.0)
    events_queue_first = q.events.subscribe()
    await q._publish_child_progress(results1, {}, now=0.0)
    first_messages = await _drain(q.events, events_queue_first)
    assert ids["a"] in _child_progress_items(first_messages), "sanity: it does show up first"

    # The job is gone from the active set -- `results` no longer has an entry for job_id 1 at
    # all, exactly what happens once `_reap_one` runs (or the job was never admitted this tick).
    del q._running[1]
    results2 = q.progress.sample([], now=1.0)  # nothing active
    events_queue = q.events.subscribe()
    await q._publish_child_progress(results2, {}, now=1.0)
    messages = await _drain(q.events, events_queue)

    assert _child_progress_items(messages) == {}
