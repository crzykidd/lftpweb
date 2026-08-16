"""`api/history.py` -- filtering, the row cap, grouping-friendly `queue_id`/`queue_name`, and
the on-demand `output_tail` fetch. No lftp process or fake seedbox needed here (that's
`tests/test_history_e2e.py`); these exercise the SQL against an in-memory database, the same
way `tests/test_transfers_list_jobs.py` covers `core/queue.py.list_jobs()` — calling the route
functions directly with a minimal `Request` stand-in rather than going through HTTP, since the
thing under test is the query logic, not FastAPI's routing layer (a couple of TestClient-based
smoke tests in `tests/test_history_smoke.py` cover the HTTP wiring itself).
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.api import history
from lftpweb.core import audit
from lftpweb.db import migrate


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeState:
    def __init__(self, db):
        self.db = db


class _FakeApp:
    def __init__(self, db):
        self.state = _FakeState(db)


class _FakeRequest:
    def __init__(self, db):
        self.app = _FakeApp(db)


async def _make_queue(db, *, name: str = "q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/remote', '/local', 1, 'copy')",
        (host_id, name),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str, *, state: str = "DOWNLOADED") -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, 1000, 1000, ?)",
        (queue_id, rel_path, state),
    )
    await db.commit()
    return cursor.lastrowid


# --- *arr join (2026-08-16, prompts/2026-08-16-arr-chip-on-row-lines.md) -- the same
# `item.arr_status`/`arr_instance.name`/`arr_instance.kind` join `core/queue.py.list_jobs()`
# already carries, added here so the History job row can draw the identical *arr chip. ----------


async def _seed_arr_instance(db, *, name: str = "Sonarr", kind: str = "sonarr") -> int:
    now = "2026-08-16T00:00:00.000000Z"
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
        "notify_on_complete, created_at, updated_at) VALUES (?, ?, 'https://arr.test', "
        "'enc', 1, 0, ?, ?)",
        (name, kind, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_job(
    db,
    item_id: int,
    *,
    state: str,
    error_class: str | None = None,
    output_tail: str | None = None,
    finished_at: str | None = "2026-08-11T01:00:00.000000Z",
    queued_at: str = "2026-08-11T00:00:00.000000Z",
) -> int:
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queued_at, finished_at, "
        "bytes_done, error_class, output_tail) VALUES (?, 'pget', ?, 'main', 0, 1, ?, ?, 1000, ?, ?)",
        (item_id, state, queued_at, finished_at, error_class, output_tail),
    )
    await db.commit()
    return cursor.lastrowid


# --- /api/history/jobs -----------------------------------------------------------------


async def test_only_terminal_states_are_returned(db):
    queue_id = await _make_queue(db)
    succeeded = await _make_item(db, queue_id, "done.txt")
    await _make_job(db, succeeded, state="succeeded")
    failed = await _make_item(db, queue_id, "failed.txt", state="FAILED")
    await _make_job(db, failed, state="failed", error_class="AUTH_FAILED")
    running_item = await _make_item(db, queue_id, "running.txt", state="DOWNLOADING")
    await _make_job(db, running_item, state="running", finished_at=None)
    queued_item = await _make_item(db, queue_id, "queued.txt", state="QUEUED")
    await _make_job(db, queued_item, state="queued", finished_at=None)

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert {j.rel_path for j in resp.jobs} == {"done.txt", "failed.txt"}
    assert resp.total == 2


async def test_succeeded_jobs_are_included_here_unlike_the_transfers_page(db):
    """DESIGN.md §13 phase 3b: `list_jobs()` deliberately excludes `succeeded` -- this is
    where a completed transfer's own record belongs instead.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "movie.mkv")
    await _make_job(db, item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    assert resp.jobs[0].state == "succeeded"
    assert resp.jobs[0].rel_path == "movie.mkv"
    assert resp.jobs[0].bytes_total == 1000


async def test_dismissed_job_still_appears_here_with_its_dismissed_at_set(db):
    """2026-08-13, prompts/done/2026-08-13-dismiss-terminal-jobs.md: dismissing a job on the
    Transfers page (`core/queue.py.dismiss_job`) only ever sets `job.dismissed_at` -- the row
    itself, and this endpoint's view of it, must be completely unaffected. `dismissed_at` is
    surfaced (not just absent-from-Transfers) so History can say *why* a job isn't there.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "gone.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed", error_class="REMOTE_GONE")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert resp.jobs[0].dismissed_at is None

    await db.execute(
        "UPDATE job SET dismissed_at = '2026-08-13T12:00:00.000000Z' WHERE id = ?", (job_id,)
    )
    await db.commit()

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    assert resp.jobs[0].dismissed_at == "2026-08-13T12:00:00.000000Z"


async def test_output_tail_never_appears_in_the_list_payload(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    await _make_job(db, item, state="failed", error_class="AUTH_FAILED", output_tail="x" * 4000)

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    job = resp.jobs[0]
    assert job.error_class == "AUTH_FAILED"
    assert job.has_output_tail is True
    assert not hasattr(job, "output_tail")


async def test_history_job_carries_arr_facts_when_queue_is_bound(db):
    queue_id = await _make_queue(db)
    instance_id = await _seed_arr_instance(db, name="Radarr 4K", kind="radarr")
    await db.execute(
        "UPDATE path_queue SET arr_instance_id = ? WHERE id = ?", (instance_id, queue_id)
    )
    item = await _make_item(db, queue_id, "movie.mkv")
    await db.execute(
        "UPDATE item SET arr_status = 'imported', arr_status_at = ? WHERE id = ?",
        ("2026-08-16T01:00:00.000000Z", item),
    )
    await db.commit()
    await _make_job(db, item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    job = resp.jobs[0]
    assert job.arr_status == "imported"
    assert job.arr_status_at == "2026-08-16T01:00:00.000000Z"
    assert job.arr_instance_name == "Radarr 4K"
    assert job.arr_instance_kind == "radarr"


async def test_history_job_arr_fields_are_null_when_queue_has_no_bound_instance(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "movie.mkv")
    await _make_job(db, item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    job = resp.jobs[0]
    assert job.arr_status is None
    assert job.arr_status_at is None
    assert job.arr_instance_name is None
    assert job.arr_instance_kind is None


async def test_job_output_fetched_on_demand(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    job_id = await _make_job(
        db, item, state="failed", error_class="AUTH_FAILED", output_tail="the real lftp output"
    )

    out = await history.get_job_output(job_id, _FakeRequest(db))
    assert out.job_id == job_id
    assert out.error_class == "AUTH_FAILED"
    assert out.output_tail == "the real lftp output"


async def test_job_output_404_for_unknown_job(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await history.get_job_output(999999, _FakeRequest(db))
    assert exc_info.value.status_code == 404


async def test_filter_by_queue_id(db):
    q1 = await _make_queue(db, name="tv")
    q2 = await _make_queue(db, name="movies")
    item1 = await _make_item(db, q1, "show.mkv")
    await _make_job(db, item1, state="succeeded")
    item2 = await _make_item(db, q2, "film.mkv")
    await _make_job(db, item2, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db), queue_id=q1)
    assert len(resp.jobs) == 1
    assert resp.jobs[0].queue_id == q1
    assert resp.jobs[0].queue_name == "tv"


async def test_filter_by_item_id(db):
    """2026-08-13, prompts/2026-08-13-files-detail-inspector.md: the item drawer's bounded
    "load on open" history fetch needs one item's own jobs, not a whole queue's -- mirrors
    `list_history_events`'s pre-existing `item_id` filter, which this endpoint lacked.
    """
    queue_id = await _make_queue(db)
    item1 = await _make_item(db, queue_id, "show.mkv")
    await _make_job(db, item1, state="succeeded")
    item2 = await _make_item(db, queue_id, "film.mkv")
    await _make_job(db, item2, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db), item_id=item1)
    assert len(resp.jobs) == 1
    assert resp.jobs[0].item_id == item1
    assert resp.jobs[0].rel_path == "show.mkv"


async def test_filter_by_item_id_returns_every_attempt_for_that_item(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "retry.mkv", state="FAILED")
    await _make_job(db, item, state="failed", finished_at="2026-08-11T00:00:00.000000Z")
    await _make_job(db, item, state="succeeded", finished_at="2026-08-11T01:00:00.000000Z")
    other_item = await _make_item(db, queue_id, "other.mkv")
    await _make_job(db, other_item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db), item_id=item)
    assert len(resp.jobs) == 2
    assert {j.state for j in resp.jobs} == {"failed", "succeeded"}


async def test_filter_by_state(db):
    queue_id = await _make_queue(db)
    ok_item = await _make_item(db, queue_id, "ok.txt")
    await _make_job(db, ok_item, state="succeeded")
    bad_item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    await _make_job(db, bad_item, state="failed", error_class="DISK_FULL")

    resp = await history.list_history_jobs(_FakeRequest(db), state="failed")
    assert [j.rel_path for j in resp.jobs] == ["bad.txt"]


async def test_filter_by_state_rejects_non_terminal_value(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await history.list_history_jobs(_FakeRequest(db), state="running")
    assert exc_info.value.status_code == 422


async def test_filter_by_error_class(db):
    queue_id = await _make_queue(db)
    auth_item = await _make_item(db, queue_id, "auth.txt", state="FAILED")
    await _make_job(db, auth_item, state="failed", error_class="AUTH_FAILED")
    disk_item = await _make_item(db, queue_id, "disk.txt", state="FAILED")
    await _make_job(db, disk_item, state="failed", error_class="DISK_FULL")

    resp = await history.list_history_jobs(_FakeRequest(db), error_class="DISK_FULL")
    assert [j.rel_path for j in resp.jobs] == ["disk.txt"]


async def test_filter_by_date_range(db):
    queue_id = await _make_queue(db)
    old_item = await _make_item(db, queue_id, "old.txt")
    await _make_job(db, old_item, state="succeeded", finished_at="2026-01-01T00:00:00.000000Z")
    new_item = await _make_item(db, queue_id, "new.txt")
    await _make_job(db, new_item, state="succeeded", finished_at="2026-08-11T00:00:00.000000Z")

    resp = await history.list_history_jobs(_FakeRequest(db), since="2026-06-01T00:00:00.000000Z")
    assert [j.rel_path for j in resp.jobs] == ["new.txt"]

    resp2 = await history.list_history_jobs(_FakeRequest(db), until="2026-06-01T00:00:00.000000Z")
    assert [j.rel_path for j in resp2.jobs] == ["old.txt"]


async def test_row_cap_enforced_even_when_a_larger_limit_is_requested(db):
    queue_id = await _make_queue(db)
    for i in range(10):
        item = await _make_item(db, queue_id, f"file{i}.txt")
        await _make_job(
            db, item, state="succeeded", finished_at=f"2026-08-11T00:00:{i:02d}.000000Z"
        )

    resp = await history.list_history_jobs(_FakeRequest(db), limit=3)
    assert len(resp.jobs) == 3
    assert resp.total == 10
    assert resp.limit == 3

    # A caller asking for more than MAX_LIMIT is silently clamped, not rejected -- the row
    # cap is non-negotiable regardless of what the client requests.
    resp_over = await history.list_history_jobs(_FakeRequest(db), limit=100_000)
    assert resp_over.limit == history.MAX_LIMIT


# --- `queue_summaries` (2026-08-16, prompts/2026-08-16-history-jobs-group-collapse.md) --------
#
# The honest, filter-scoped aggregate `HistoryJobsSection.tsx`'s queue group headers need --
# History's `jobs` list is `LIMIT`/`OFFSET` paginated, so these have to be computed server-side
# over the *whole* filtered set, not just the loaded page (module docstring).


async def test_queue_summary_counts_by_outcome_and_sums_bytes(db):
    queue_id = await _make_queue(db, name="tv")
    ok1 = await _make_item(db, queue_id, "ok1.txt")
    await _make_job(db, ok1, state="succeeded")
    ok2 = await _make_item(db, queue_id, "ok2.txt")
    await _make_job(db, ok2, state="succeeded")
    bad = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    await _make_job(db, bad, state="failed", error_class="AUTH_FAILED")
    stopped = await _make_item(db, queue_id, "stopped.txt", state="STOPPED")
    await _make_job(db, stopped, state="cancelled")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.queue_summaries) == 1
    summary = resp.queue_summaries[0]
    assert summary.queue_id == queue_id
    assert summary.queue_name == "tv"
    assert summary.succeeded == 2
    assert summary.failed == 1
    assert summary.cancelled == 1
    assert summary.total_bytes_done == 4000  # 1000 bytes_done per _make_job call, x4


async def test_queue_summary_one_row_per_queue_ordered_by_name(db):
    q_movies = await _make_queue(db, name="movies")
    q_tv = await _make_queue(db, name="tv")
    movie_item = await _make_item(db, q_movies, "film.mkv")
    await _make_job(db, movie_item, state="succeeded")
    tv_item = await _make_item(db, q_tv, "show.mkv")
    await _make_job(db, tv_item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert [s.queue_name for s in resp.queue_summaries] == ["movies", "tv"]


async def test_queue_summary_honors_the_same_filters_as_the_jobs_list(db):
    q1 = await _make_queue(db, name="q1")
    q2 = await _make_queue(db, name="q2")
    q1_item = await _make_item(db, q1, "q1.txt")
    await _make_job(db, q1_item, state="succeeded")
    q2_item = await _make_item(db, q2, "q2.txt")
    await _make_job(db, q2_item, state="succeeded")

    resp = await history.list_history_jobs(_FakeRequest(db), queue_id=q1)
    assert [s.queue_id for s in resp.queue_summaries] == [q1]
    assert resp.queue_summaries[0].succeeded == 1

    resp_state = await history.list_history_jobs(_FakeRequest(db), state="failed")
    assert resp_state.queue_summaries == []


async def test_queue_summary_stays_bounded_regardless_of_row_cap(db):
    """The whole point: a queue's *true* total spans more jobs than the paginated `jobs` list
    returns. `limit=1` still reports the queue's real 5-job total, not just the one loaded row.
    """
    queue_id = await _make_queue(db)
    for i in range(5):
        item = await _make_item(db, queue_id, f"file{i}.txt")
        await _make_job(
            db, item, state="succeeded", finished_at=f"2026-08-11T00:00:{i:02d}.000000Z"
        )

    resp = await history.list_history_jobs(_FakeRequest(db), limit=1)
    assert len(resp.jobs) == 1
    assert len(resp.queue_summaries) == 1
    assert resp.queue_summaries[0].succeeded == 5
    assert resp.queue_summaries[0].total_bytes_done == 5000


async def test_queue_summary_empty_when_nothing_matches(db):
    resp = await history.list_history_jobs(_FakeRequest(db))
    assert resp.queue_summaries == []


async def test_pagination_offset_walks_through_results_newest_first(db):
    queue_id = await _make_queue(db)
    for i in range(5):
        item = await _make_item(db, queue_id, f"file{i}.txt")
        await _make_job(
            db, item, state="succeeded", finished_at=f"2026-08-11T00:00:{i:02d}.000000Z"
        )

    page1 = await history.list_history_jobs(_FakeRequest(db), limit=2, offset=0)
    page2 = await history.list_history_jobs(_FakeRequest(db), limit=2, offset=2)
    assert [j.rel_path for j in page1.jobs] == ["file4.txt", "file3.txt"]
    assert [j.rel_path for j in page2.jobs] == ["file2.txt", "file1.txt"]


# --- /api/history/events ----------------------------------------------------------------


async def test_events_delete_audit_is_legible(db):
    """DESIGN.md §7.3: every delete and every withheld delete must be reconstructable --
    queue, mode, and gating condition. `core/postprocess.py` already puts all of that into
    the event `message`; this endpoint just has to surface it with the item/queue resolved.
    """
    queue_id = await _make_queue(db, name="e2e-move")
    item = await _make_item(db, queue_id, "release/movie.mkv", state="VERIFIED")

    await audit.record_event(
        db,
        level="info",
        item_id=item,
        kind="remote_delete",
        message=f"queue {queue_id} ('e2e-move') mode=move: deleted verified remote copy /data/pickup/release/movie.mkv",
    )
    await audit.record_event(
        db,
        level="warning",
        item_id=item,
        kind="remote_delete_withheld",
        message=f"queue {queue_id} ('e2e-move') mode=move: delete withheld -- verification result was CORRUPT, not VERIFIED",
    )

    resp = await history.list_history_events(_FakeRequest(db))
    assert len(resp.events) == 2
    kinds = {e.kind for e in resp.events}
    assert kinds == {"remote_delete", "remote_delete_withheld"}
    for e in resp.events:
        assert e.queue_id == queue_id
        assert e.queue_name == "e2e-move"
        assert e.rel_path == "release/movie.mkv"
        assert "mode=move" in e.message


async def test_events_filter_by_kind(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="VERIFIED: ok")
    await audit.record_event(
        db, level="info", item_id=item, kind="remote_delete", message="deleted"
    )

    resp = await history.list_history_events(_FakeRequest(db), kind="remote_delete")
    assert [e.kind for e in resp.events] == ["remote_delete"]


async def test_events_filter_by_level(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="ok")
    await audit.record_event(
        db, level="error", item_id=item, kind="remote_delete_failed", message="boom"
    )

    resp = await history.list_history_events(_FakeRequest(db), level="error")
    assert [e.kind for e in resp.events] == ["remote_delete_failed"]


async def test_events_survive_their_item_being_deleted(db):
    """`event.item_id` is `ON DELETE SET NULL` -- an audit row must outlive the item/queue it
    describes. The event must still surface, just without the resolved context.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "gone.txt")
    await audit.record_event(
        db, level="info", item_id=item, kind="remote_delete", message="deleted gone.txt"
    )
    await db.execute("DELETE FROM item WHERE id = ?", (item,))
    await db.commit()

    resp = await history.list_history_events(_FakeRequest(db))
    assert len(resp.events) == 1
    assert resp.events[0].item_id is None
    assert resp.events[0].queue_id is None
    assert resp.events[0].message == "deleted gone.txt"


async def test_events_row_cap(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    for i in range(10):
        await audit.record_event(db, level="info", item_id=item, kind="verify", message=f"m{i}")

    resp = await history.list_history_events(_FakeRequest(db), limit=4)
    assert len(resp.events) == 4
    assert resp.total == 10


# --- Clearing (2026-08-13, prompts/2026-08-13-clear-history.md) -------------------------
#
# "Dismiss" (`api/jobs.py.dismiss_job`, `prompts/done/2026-08-13-dismiss-terminal-jobs.md`)
# only hides a row from Transfers and leaves it in History untouched -- that's covered by
# `test_dismissed_job_still_appears_here_with_its_dismissed_at_set` above. These tests cover
# the different, irreversible action: deleting the row from History outright.


async def _job_row_exists(db, job_id: int) -> bool:
    cursor = await db.execute("SELECT 1 FROM job WHERE id = ?", (job_id,))
    return await cursor.fetchone() is not None


async def _event_row_exists(db, event_id: int) -> bool:
    cursor = await db.execute("SELECT 1 FROM event WHERE id = ?", (event_id,))
    return await cursor.fetchone() is not None


async def test_clear_single_job_deletes_the_row(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "done.txt")
    job_id = await _make_job(db, item, state="succeeded")

    resp = await history.delete_history_job(job_id, _FakeRequest(db))
    assert resp.deleted == 1
    assert not await _job_row_exists(db, job_id)


async def test_clear_single_job_404_for_unknown_job(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await history.delete_history_job(999999, _FakeRequest(db))
    assert exc_info.value.status_code == 404


async def test_clear_single_job_rejects_an_active_job(db):
    """The important guard: an active (`queued`/`running`) job is not history and must not be
    clearable, rejected server-side (409), not just hidden from a UI button.
    """
    from fastapi import HTTPException

    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "running.txt", state="DOWNLOADING")
    job_id = await _make_job(db, item, state="running", finished_at=None)

    with pytest.raises(HTTPException) as exc_info:
        await history.delete_history_job(job_id, _FakeRequest(db))
    assert exc_info.value.status_code == 409
    assert await _job_row_exists(db, job_id)

    queued_item = await _make_item(db, queue_id, "queued.txt", state="QUEUED")
    queued_job_id = await _make_job(db, queued_item, state="queued", finished_at=None)
    with pytest.raises(HTTPException) as exc_info:
        await history.delete_history_job(queued_job_id, _FakeRequest(db))
    assert exc_info.value.status_code == 409
    assert await _job_row_exists(db, queued_job_id)


async def test_clear_all_jobs_leaves_the_active_job_alone(db):
    """ "Clear all" is `DELETE /jobs` with no filters -- it must still never reach a
    `queued`/`running` job, because the base WHERE clause (`job.state IN (...)`) is never
    optional (module docstring).
    """
    queue_id = await _make_queue(db)
    ok_item = await _make_item(db, queue_id, "ok.txt")
    ok_job = await _make_job(db, ok_item, state="succeeded")
    bad_item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    bad_job = await _make_job(db, bad_item, state="failed", error_class="AUTH_FAILED")
    running_item = await _make_item(db, queue_id, "running.txt", state="DOWNLOADING")
    running_job = await _make_job(db, running_item, state="running", finished_at=None)

    resp = await history.clear_history_jobs(_FakeRequest(db))
    assert resp.deleted == 2
    assert not await _job_row_exists(db, ok_job)
    assert not await _job_row_exists(db, bad_job)
    assert await _job_row_exists(db, running_job)


async def test_clear_jobs_by_outcome_state(db):
    queue_id = await _make_queue(db)
    ok_item = await _make_item(db, queue_id, "ok.txt")
    ok_job = await _make_job(db, ok_item, state="succeeded")
    bad_item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    bad_job = await _make_job(db, bad_item, state="failed", error_class="AUTH_FAILED")

    resp = await history.clear_history_jobs(_FakeRequest(db), state="failed")
    assert resp.deleted == 1
    assert await _job_row_exists(db, ok_job)
    assert not await _job_row_exists(db, bad_job)


async def test_clear_jobs_rejects_non_terminal_state_filter(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await history.clear_history_jobs(_FakeRequest(db), state="running")
    assert exc_info.value.status_code == 422


async def test_clear_jobs_filters_compose_leaving_everything_else_alone(db):
    """Clearing "failed jobs in queue X" must leave failed jobs in other queues, and
    succeeded jobs in queue X, alone.
    """
    q1 = await _make_queue(db, name="q1")
    q2 = await _make_queue(db, name="q2")
    q1_failed_item = await _make_item(db, q1, "q1-failed.txt", state="FAILED")
    q1_failed = await _make_job(db, q1_failed_item, state="failed", error_class="AUTH_FAILED")
    q1_ok_item = await _make_item(db, q1, "q1-ok.txt")
    q1_ok = await _make_job(db, q1_ok_item, state="succeeded")
    q2_failed_item = await _make_item(db, q2, "q2-failed.txt", state="FAILED")
    q2_failed = await _make_job(db, q2_failed_item, state="failed", error_class="AUTH_FAILED")

    resp = await history.clear_history_jobs(_FakeRequest(db), queue_id=q1, state="failed")
    assert resp.deleted == 1
    assert not await _job_row_exists(db, q1_failed)
    assert await _job_row_exists(db, q1_ok)
    assert await _job_row_exists(db, q2_failed)


async def test_clearing_a_job_never_touches_the_item_row(db):
    """The important one: clearing a job must not touch `item`, `auto_queue_suppressed`, or
    `suppressed_reason` -- "I cleared my history and it re-downloaded everything" is the
    failure this must design out.
    """
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    await db.execute(
        "UPDATE item SET auto_queue_suppressed = 1, suppressed_reason = 'retries_exhausted' "
        "WHERE id = ?",
        (item_id,),
    )
    await db.commit()
    job_id = await _make_job(db, item_id, state="failed", error_class="AUTH_FAILED")

    await history.delete_history_job(job_id, _FakeRequest(db))

    cursor = await db.execute(
        "SELECT state, auto_queue_suppressed, suppressed_reason FROM item WHERE id = ?",
        (item_id,),
    )
    row = await cursor.fetchone()
    assert row is not None, "clearing a job must never delete the item row"
    assert row["state"] == "FAILED"
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "retries_exhausted"


async def test_clearing_all_jobs_never_touches_any_item_row(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    await db.execute(
        "UPDATE item SET auto_queue_suppressed = 1, suppressed_reason = 'permanent_error' "
        "WHERE id = ?",
        (item_id,),
    )
    await db.commit()
    await _make_job(db, item_id, state="failed", error_class="AUTH_FAILED")

    await history.clear_history_jobs(_FakeRequest(db))

    cursor = await db.execute(
        "SELECT auto_queue_suppressed, suppressed_reason FROM item WHERE id = ?", (item_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "permanent_error"


async def test_clearing_a_job_nulls_but_does_not_remove_the_surviving_event(db):
    """`event.job_id` is `ON DELETE SET NULL` (001_initial_schema.sql:140) -- a cleared job's
    surviving event must still render, just with `job_id` gone.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "bad.txt", state="FAILED")
    job_id = await _make_job(db, item, state="failed", error_class="AUTH_FAILED")
    await audit.record_event(
        db, level="error", item_id=item, job_id=job_id, kind="remote_delete_failed", message="x"
    )

    await history.delete_history_job(job_id, _FakeRequest(db))

    resp = await history.list_history_events(_FakeRequest(db))
    assert len(resp.events) == 1
    assert resp.events[0].job_id is None
    assert resp.events[0].message == "x"


async def test_clear_single_event_deletes_the_row(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="ok")
    # `audit.record_event` returns `None` by design (its own docstring) -- fetch the id it
    # just wrote instead of relying on a return value.
    row = await (await db.execute("SELECT id FROM event WHERE message = 'ok'")).fetchone()
    event_id = row["id"]

    resp = await history.delete_history_event(event_id, _FakeRequest(db))
    assert resp.deleted == 1
    assert not await _event_row_exists(db, event_id)


async def test_clear_single_event_404_for_unknown_event(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await history.delete_history_event(999999, _FakeRequest(db))
    assert exc_info.value.status_code == 404


async def test_clear_all_events_has_no_protected_categories(db):
    """The user's own decision (docs/decisions.md): delete-audit events are not protected --
    `remote_delete`/`remote_delete_withheld`/`local_delete`/`archive_cleanup` clear the same
    as any other event kind.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="ok")
    await audit.record_event(
        db, level="info", item_id=item, kind="remote_delete", message="deleted"
    )
    await audit.record_event(
        db, level="warning", item_id=item, kind="remote_delete_withheld", message="withheld"
    )
    await audit.record_event(db, level="info", item_id=item, kind="local_delete", message="x")
    await audit.record_event(db, level="info", item_id=item, kind="archive_cleanup", message="x")

    resp = await history.clear_history_events(_FakeRequest(db))
    assert resp.deleted == 5

    remaining = await history.list_history_events(_FakeRequest(db))
    assert remaining.total == 0


async def test_clear_events_by_kind(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="ok")
    await audit.record_event(
        db, level="info", item_id=item, kind="remote_delete", message="deleted"
    )

    resp = await history.clear_history_events(_FakeRequest(db), kind="remote_delete")
    assert resp.deleted == 1

    remaining = await history.list_history_events(_FakeRequest(db))
    assert [e.kind for e in remaining.events] == ["verify"]


async def test_clear_events_by_level(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await audit.record_event(db, level="info", item_id=item, kind="verify", message="ok")
    await audit.record_event(
        db, level="error", item_id=item, kind="remote_delete_failed", message="boom"
    )

    resp = await history.clear_history_events(_FakeRequest(db), level="error")
    assert resp.deleted == 1

    remaining = await history.list_history_events(_FakeRequest(db))
    assert [e.kind for e in remaining.events] == ["verify"]


async def test_clearing_events_never_touches_the_item_row(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "x.txt", state="VERIFIED")
    await db.execute(
        "UPDATE item SET auto_queue_suppressed = 1, suppressed_reason = 'user_stopped' "
        "WHERE id = ?",
        (item_id,),
    )
    await db.commit()
    await audit.record_event(
        db, level="info", item_id=item_id, kind="remote_delete", message="deleted"
    )

    await history.clear_history_events(_FakeRequest(db))

    cursor = await db.execute(
        "SELECT state, auto_queue_suppressed, suppressed_reason FROM item WHERE id = ?",
        (item_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["state"] == "VERIFIED"
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "user_stopped"


async def test_clearing_history_does_not_change_dashboard_metrics(db):
    """`metric_sample` (migration 005) holds only `queue_id`/`ts`/`bytes_delta` -- no job/item
    reference -- and `core/metrics.py` never queries `job` or `event`. Clearing History must
    leave the Dashboard's own data, and what its queries return, completely unchanged.
    """
    from lftpweb.core import metrics

    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "x.txt")
    await _make_job(db, item, state="succeeded")
    await audit.record_event(
        db, level="info", item_id=item, kind="remote_delete", message="deleted"
    )
    await db.execute(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
        (queue_id, "2026-08-13T00:00:30.000000Z", 12345),
    )
    await db.execute("INSERT INTO metric_heartbeat (ts) VALUES ('2026-08-13T00:00:30.000000Z')")
    await db.commit()

    before = await metrics.queue_breakdown(
        db,
        start_ts="2026-08-13T00:00:00.000000Z",
        end_ts="2026-08-13T00:01:00.000000Z",
        bucket_seconds=60,
    )

    await history.clear_history_jobs(_FakeRequest(db))
    await history.clear_history_events(_FakeRequest(db))

    after = await metrics.queue_breakdown(
        db,
        start_ts="2026-08-13T00:00:00.000000Z",
        end_ts="2026-08-13T00:01:00.000000Z",
        bucket_seconds=60,
    )
    assert len(before) == 1
    assert before[0][1] == queue_id
    assert before[0][2] == 12345
    assert before == after, "clearing job/event history changed a Dashboard query's result"

    sample_count = await (await db.execute("SELECT COUNT(*) AS c FROM metric_sample")).fetchone()
    heartbeat_count = await (
        await db.execute("SELECT COUNT(*) AS c FROM metric_heartbeat")
    ).fetchone()
    assert sample_count["c"] == 1
    assert heartbeat_count["c"] == 1
