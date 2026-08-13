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
