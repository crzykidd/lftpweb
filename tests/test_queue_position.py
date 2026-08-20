"""The dense position model that replaced `rank DESC, queued_at ASC` (2026-08-19,
docs/transfers-redesign-spec.md §3.4/§3.5, migration 023, prompts/done/
2026-08-19-queue-position-order-model.md). No fake seedbox needed -- like
`tests/test_queue_orphans.py`, this is pure database/DB-adjacent behavior, so it lives outside
`tests/test_queue.py`'s module-level skipif.

Covers:

- `position_between` (`core/queue.py`), the one primitive every writer of `queue_position`
  shares -- exercised directly here, and indirectly by every other test below.
- Migration 023's backfill: an existing (pre-migration) queue must come out in the identical
  order the old `rank DESC, queued_at ASC, id ASC` query would have served it.
- `_insert_job`'s default (append at the back).
- `move_to_top`, reimplemented on `queue_position`.
- `move_job` (2026-08-19, stage 2, docs/transfers-redesign-spec.md §3.4,
  `prompts/2026-08-19-queue-reorder-chevrons.md`): the ▲/▼/▲▲ chevron actions, their edge
  cases, the duplicate-position (concurrent-move) tiebreak, and position-exhaustion
  renormalization.

The v0.2.6 startup-rescue re-derivation (`_rescue_position`) has its own tests in
`tests/test_queue_orphans.py`, alongside the rest of that startup-reconciliation behavior.
"""

from __future__ import annotations

import math

import aiosqlite
import pytest

import lftpweb.db as db_module
from lftpweb.core.events import EventBus
from lftpweb.core.queue import JobNotQueuedError, TransferQueue, position_between
from lftpweb.db import connect, migrate
from test_queue import _make_host_row, _make_item_row, _make_queue_row

# --- position_between: the pure primitive ----------------------------------------------------


def test_position_between_empty_ordering_returns_one():
    assert position_between(None, None) == 1.0


def test_position_between_no_lower_neighbour_goes_below_upper():
    assert position_between(None, 5.0) == 4.0


def test_position_between_no_upper_neighbour_goes_above_lower():
    assert position_between(5.0, None) == 6.0


def test_position_between_two_neighbours_is_the_exact_midpoint():
    # This is the primitive stage 2's chevrons will call directly
    # (docs/transfers-redesign-spec.md §3.4) -- no caller exists yet, so it's proven here.
    assert position_between(1.0, 3.0) == 2.0
    assert position_between(1.0, 2.0) == 1.5
    assert position_between(-3.0, 5.0) == 1.0


# --- Migration 023's backfill: a deep, mixed-shape queue must not be reshuffled -------------


async def test_migration_023_backfill_matches_the_old_rank_and_queued_at_order(
    tmp_path, monkeypatch
):
    """A realistic mix -- several never-boosted (`rank = 0`) jobs at differing `queued_at`, plus
    a couple of moved-to-top jobs (ascending `rank`) -- must come out of migration 023 in
    exactly the order `rank DESC, queued_at ASC, id ASC` would have served it. Full sequence
    asserted, not a spot check.
    """
    real_migrations_dir = db_module.MIGRATIONS_DIR

    # Every migration except 023 -- an actual pre-upgrade database.
    staged = tmp_path / "migrations"
    staged.mkdir()
    for path in sorted(real_migrations_dir.glob("*.sql")):
        if int(path.stem.split("_")[0]) < 23:
            (staged / path.name).write_text(path.read_text())
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", staged)

    conn = await connect(str(tmp_path))
    try:
        await migrate(conn)

        await conn.execute(
            "INSERT INTO host (id, name, address, username, auth_method, known_hosts_policy) "
            "VALUES (1, 'h', 'a', 'u', 'agent', 'strict')"
        )
        await conn.execute(
            "INSERT INTO path_queue (id, host_id, name, remote_path, local_path, sync_mode) "
            "VALUES (1, 1, 'q', '/r', '/l', 'copy')"
        )
        # Five items, so `job.item_id` can double as a human-readable label below.
        for item_id in range(1, 6):
            await conn.execute(
                "INSERT INTO item (id, queue_id, rel_path, is_dir, state) "
                "VALUES (?, 1, ?, 0, 'QUEUED')",
                (item_id, f"item{item_id}"),
            )

        # Natural-zone jobs (rank 0), inserted with `queued_at` out of id order -- id order must
        # not leak into the result except as the final tiebreak.
        for item_id, queued_at in [(1, "2020-01-01"), (2, "2020-03-01"), (3, "2020-02-01")]:
            await conn.execute(
                "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
                "queued_at) VALUES (?, 'mirror', 'queued', 'main', 0, 1, 0, ?)",
                (item_id, queued_at),
            )
        # Two moved-to-top jobs, ascending rank (4 boosted before 5 -- 5 is "more recently
        # boosted," so it must come first).
        await conn.execute(
            "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
            "queued_at) VALUES (4, 'mirror', 'queued', 'main', 1, 1, 0, '2020-05-01')"
        )
        await conn.execute(
            "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
            "queued_at) VALUES (5, 'mirror', 'queued', 'main', 2, 1, 0, '2020-04-01')"
        )
        await conn.commit()

        # Now point at the real migrations directory (023 included) and migrate again.
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_migrations_dir)
        await migrate(conn)

        cursor = await conn.execute(
            "SELECT item_id, queue_position FROM job ORDER BY queue_position ASC, id ASC"
        )
        rows = await cursor.fetchall()
        assert [r["item_id"] for r in rows] == [5, 4, 1, 3, 2], (
            "5 (rank 2) then 4 (rank 1) -- both boosted, most-recently-boosted first -- then the "
            "natural zone by queued_at: 1 (01-01), 3 (02-01), 2 (03-01)"
        )
        # Correctness floor (2026-08-19, relaxed backfill-order requirement): every row still
        # gets a real, distinct, non-NULL position regardless of how exact the order is.
        positions = [r["queue_position"] for r in rows]
        assert all(p is not None for p in positions)
        assert len(set(positions)) == len(positions)
        assert positions == sorted(positions)
    finally:
        await conn.close()


# --- _insert_job's default: new work lands at the back ---------------------------------------


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


async def _queue_position(db, job_id: int) -> float:
    cursor = await db.execute("SELECT queue_position FROM job WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return row["queue_position"]


async def test_enqueue_item_appends_at_the_back(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "b", is_dir=True, remote_size=1000)
    item3 = await _make_item_row(db, queue_id, "c", is_dir=True, remote_size=1000)

    q = _queue_obj(db)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)
    job3 = await q.enqueue_item(item3)

    p1, p2, p3 = (
        await _queue_position(db, job1),
        await _queue_position(db, job2),
        await _queue_position(db, job3),
    )
    assert p1 < p2 < p3, "each new job must land strictly after every already-queued job"


async def test_enqueue_item_first_ever_job_gets_position_one(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)

    job1 = await _queue_obj(db).enqueue_item(item1)
    assert await _queue_position(db, job1) == 1.0


# --- move_to_top: front of the line, beats a previous move-to-top ----------------------------


async def test_move_to_top_lands_at_the_front(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "b", is_dir=True, remote_size=1000)

    q = _queue_obj(db)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)  # queued after job1

    await q.move_to_top(job2)

    assert await _queue_position(db, job2) < await _queue_position(db, job1)


async def test_move_to_top_beats_a_previous_move_to_top(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "b", is_dir=True, remote_size=1000)
    item3 = await _make_item_row(db, queue_id, "c", is_dir=True, remote_size=1000)

    q = _queue_obj(db)
    await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)
    job3 = await q.enqueue_item(item3)

    await q.move_to_top(job2)  # order: 2, 1, 3
    await q.move_to_top(job3)  # order: 3, 2, 1 -- the most recently boosted wins

    cursor = await db.execute("SELECT item_id FROM job ORDER BY queue_position ASC, id ASC")
    rows = await cursor.fetchall()
    assert [r["item_id"] for r in rows] == [item3, item2, item1]


# --- move_job: the ▲/▼/▲▲ chevron actions (2026-08-19, stage 2,
# docs/transfers-redesign-spec.md §3.4, prompts/2026-08-19-queue-reorder-chevrons.md) ----------


async def _three_queued_jobs(db, tmp_path) -> tuple[TransferQueue, int, int, int]:
    """Three items queued in order -- `job1` first (lowest position), `job3` last -- the
    fixture every basic swap/edge-case test below starts from.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "b", is_dir=True, remote_size=1000)
    item3 = await _make_item_row(db, queue_id, "c", is_dir=True, remote_size=1000)

    q = _queue_obj(db)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)
    job3 = await q.enqueue_item(item3)
    return q, job1, job2, job3


async def _queued_job_ids_in_order(db) -> list[int]:
    cursor = await db.execute(
        "SELECT id FROM job WHERE state = 'queued' ORDER BY queue_position ASC, id ASC"
    )
    rows = await cursor.fetchall()
    return [r["id"] for r in rows]


async def test_move_up_swaps_with_the_row_above_in_global_order(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    await q.move_job(job2, "up")
    assert await _queued_job_ids_in_order(db) == [job2, job1, job3]


async def test_move_down_swaps_with_the_row_below_in_global_order(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    await q.move_job(job2, "down")
    assert await _queued_job_ids_in_order(db) == [job1, job3, job2]


async def test_move_up_on_the_front_row_is_a_silent_noop(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    before = await _queued_job_ids_in_order(db)
    await q.move_job(job1, "up")  # already at the front of the global order
    assert await _queued_job_ids_in_order(db) == before


async def test_move_down_on_the_back_row_is_a_silent_noop(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    before = await _queued_job_ids_in_order(db)
    await q.move_job(job3, "down")  # already at the back of the global order
    assert await _queued_job_ids_in_order(db) == before


async def test_move_with_only_one_queued_job_is_a_noop_either_direction(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    q = _queue_obj(db)
    job1 = await q.enqueue_item(item1)

    await q.move_job(job1, "up")
    await q.move_job(job1, "down")
    assert await _queued_job_ids_in_order(db) == [job1]


async def test_move_top_direction_reuses_move_to_top(db, tmp_path):
    # `move_job(..., "top")` must not be a second implementation of "front of the line" --
    # it delegates straight to `move_to_top`, which also bumps `rank` (the boosted/natural
    # discriminator `_rescue_position` depends on).
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    await q.move_job(job3, "top")
    assert await _queued_job_ids_in_order(db) == [job3, job1, job2]
    cursor = await db.execute("SELECT rank FROM job WHERE id = ?", (job3,))
    assert (await cursor.fetchone())[
        "rank"
    ] > 0, "top' must bump rank exactly like a direct move_to_top() call would"


async def test_move_job_unknown_id_raises_value_error(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    missing_id = job3 + 1000
    for direction in ("up", "down", "top"):
        with pytest.raises(ValueError):
            await q.move_job(missing_id, direction)


async def test_move_job_on_a_running_job_raises_job_not_queued(db, tmp_path):
    # A job that started running between the page render and the click -- reordering it is
    # meaningless (DESIGN.md §4.5: its allocation is fixed at spawn and never re-shaped).
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    await db.execute("UPDATE job SET state = 'running' WHERE id = ?", (job2,))
    await db.commit()
    for direction in ("up", "down", "top"):
        with pytest.raises(JobNotQueuedError):
            await q.move_job(job2, direction)


async def test_move_job_on_a_terminal_job_raises_job_not_queued(db, tmp_path):
    q, job1, job2, job3 = await _three_queued_jobs(db, tmp_path)
    await db.execute(
        "UPDATE job SET state = 'failed', finished_at = '2026-01-01T00:00:00.000000Z' "
        "WHERE id = ?",
        (job2,),
    )
    await db.commit()
    with pytest.raises(JobNotQueuedError):
        await q.move_job(job2, "up")


async def test_duplicate_queue_positions_tiebreak_deterministically_by_id(db, tmp_path):
    """Two `move_job` calls racing each other can, in principle, both compute the same
    position before either commits (`move_job`'s own docstring). This proves the survivability
    claim directly: `id ASC` is the deterministic final tiebreak, so a collision never produces
    an ambiguous or corrupted order -- the exact ordering `_admit`/`list_jobs`/`move_job`'s own
    neighbour query all already use.
    """
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item1 = await _make_item_row(db, queue_id, "a", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "b", is_dir=True, remote_size=1000)
    q = _queue_obj(db)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)

    await db.execute("UPDATE job SET queue_position = 5.0 WHERE id IN (?, ?)", (job1, job2))
    await db.commit()

    assert await _queued_job_ids_in_order(db) == sorted([job1, job2])

    # A further move against the colliding pair must still succeed cleanly (no crash, no
    # ambiguous result) rather than the duplicate position itself being a landmine.
    higher_id_job = max(job1, job2)
    await q.move_job(higher_id_job, "up")
    assert await _queued_job_ids_in_order(db) == [higher_id_job, min(job1, job2)]


# --- Position exhaustion: renormalize-and-retry (2026-08-19, this task, "handle it" per the
# task's own instruction) -- driven directly to float-adjacency rather than via ~50 successive
# inserts at the same spot. ------------------------------------------------------------------


async def test_move_up_renormalizes_when_the_midpoint_is_exhausted(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item0 = await _make_item_row(db, queue_id, "z0", is_dir=True, remote_size=1000)
    item1 = await _make_item_row(db, queue_id, "z1", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "z2", is_dir=True, remote_size=1000)
    q = _queue_obj(db)
    job0 = await q.enqueue_item(item0)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)

    # job0 and job1 are float-adjacent -- position_between(1.0, nextafter(1.0, 2.0)) rounds
    # straight back to 1.0, exactly the exhaustion `move_job`'s own guard must catch.
    adjacent = math.nextafter(1.0, 2.0)
    assert (1.0 + adjacent) / 2.0 == 1.0, "the test's own premise: these two floats are adjacent"
    await db.execute("UPDATE job SET queue_position = 1.0 WHERE id = ?", (job0,))
    await db.execute("UPDATE job SET queue_position = ? WHERE id = ?", (adjacent, job1))
    await db.execute("UPDATE job SET queue_position = 5.0 WHERE id = ?", (job2,))
    await db.commit()
    assert await _queued_job_ids_in_order(db) == [job0, job1, job2]

    # Moving job2 "up" needs the midpoint between job0 and job1 -- the adjacent pair -- which
    # must renormalize the whole queued set (to 1.0/2.0/3.0) and retry, rather than silently
    # landing on top of job0 or job1 or raising.
    await q.move_job(job2, "up")

    assert await _queued_job_ids_in_order(db) == [job0, job2, job1], (
        "job2 swapped with job1 (its immediate predecessor) exactly as a normal 'up' would, "
        "despite the exhausted midpoint forcing a renormalize first"
    )
    positions = {
        job0: await _queue_position(db, job0),
        job1: await _queue_position(db, job1),
        job2: await _queue_position(db, job2),
    }
    assert len(set(positions.values())) == 3, "renormalize-and-retry must yield distinct positions"
    assert positions[job1] == 2.0, "job1's own position is untouched by the retry (only job2 moves)"


async def test_move_down_renormalizes_when_the_midpoint_is_exhausted(db, tmp_path):
    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item0 = await _make_item_row(db, queue_id, "z0", is_dir=True, remote_size=1000)
    item1 = await _make_item_row(db, queue_id, "z1", is_dir=True, remote_size=1000)
    item2 = await _make_item_row(db, queue_id, "z2", is_dir=True, remote_size=1000)
    q = _queue_obj(db)
    job0 = await q.enqueue_item(item0)
    job1 = await q.enqueue_item(item1)
    job2 = await q.enqueue_item(item2)

    # job1 and job2 are float-adjacent this time -- job0 moving "down" needs the midpoint
    # between them.
    adjacent = math.nextafter(5.0, 6.0)
    assert (5.0 + adjacent) / 2.0 == 5.0, "the test's own premise: these two floats are adjacent"
    await db.execute("UPDATE job SET queue_position = 1.0 WHERE id = ?", (job0,))
    await db.execute("UPDATE job SET queue_position = 5.0 WHERE id = ?", (job1,))
    await db.execute("UPDATE job SET queue_position = ? WHERE id = ?", (adjacent, job2))
    await db.commit()
    assert await _queued_job_ids_in_order(db) == [job0, job1, job2]

    await q.move_job(job0, "down")

    assert await _queued_job_ids_in_order(db) == [job1, job0, job2], (
        "job0 swapped with job1 (its immediate successor) exactly as a normal 'down' would, "
        "despite the exhausted midpoint forcing a renormalize first"
    )
    positions = {
        job0: await _queue_position(db, job0),
        job1: await _queue_position(db, job1),
        job2: await _queue_position(db, job2),
    }
    assert len(set(positions.values())) == 3, "renormalize-and-retry must yield distinct positions"
    assert positions[job1] == 2.0, "job1's own position is untouched by the retry (only job0 moves)"
