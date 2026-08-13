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

from lftpweb.core.queue import CHILD_PROGRESS_THROTTLE_TICKS, _RunningProcess
from test_queue import _make_db, _make_host_row, _make_item_row, _make_queue_row, _queue_for


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
    """Enough calls to reach the next tick that actually runs child publishing."""
    for _ in range(CHILD_PROGRESS_THROTTLE_TICKS):
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


# --- the parent's aggregate progress (job.bytes_done, item.local_size) is unaffected ----------


async def test_parent_aggregate_progress_still_updates_every_tick_not_just_throttled_ones(
    db, tmp_path
):
    q, release_dir, _queue_id, ids = await _setup(db, tmp_path)
    await db.execute(
        "INSERT INTO job (id, item_id, kind, state, lane, rank, attempt) "
        "VALUES (1, ?, 'mirror', 'running', 'main', 0, 1)",
        (ids["parent"],),
    )
    await db.commit()

    (release_dir / "a.rar").write_bytes(b"a" * 100)
    (release_dir / "b.rar").write_bytes(b"b" * 50)

    # A single tick -- not a multiple of CHILD_PROGRESS_THROTTLE_TICKS in general -- must still
    # move the job-centric and parent-item-centric aggregates; only the *child* path is
    # throttled.
    await q._sample_and_publish_progress()

    job_row = await (await db.execute("SELECT bytes_done FROM job WHERE id = 1")).fetchone()
    assert job_row["bytes_done"] == 150  # 100 + 50, summed by core/progress.py, every tick

    parent_row = await _item_row(db, ids["parent"])
    assert parent_row["local_size"] == 150
    assert parent_row["state"] == "DOWNLOADING"  # read back from `item`, not hardcoded
