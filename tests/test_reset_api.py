"""`api/jobs.py`'s reset-item-tracking endpoints (`prompts/2026-08-13-reset-item-tracking.md`).
Same call-the-route-function-directly harness `tests/test_delete_api.py` uses -- the thing
under test is the route's own wiring (status codes, guard-failure -> `HTTPException`,
`Engine.forget_rel_paths`/`request_rescan` being called on success), not FastAPI's routing
layer.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs
from lftpweb.core.events import EventBus
from lftpweb.db import migrate
from lftpweb.models import QueueResetRequest, ResetPatternPreviewRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeEngine:
    """Stands in for `core/engine.py.Engine` -- only the two methods the reset endpoints call."""

    def __init__(self) -> None:
        self.forgotten: list[tuple[int, tuple[str, ...]]] = []
        self.rescanned = False

    def forget_rel_paths(self, queue_id, rel_paths) -> None:
        self.forgotten.append((queue_id, tuple(rel_paths)))

    def request_rescan(self) -> None:
        self.rescanned = True


class _FakePostprocess:
    def __init__(self, in_flight: frozenset[int] = frozenset()) -> None:
        self._in_flight = in_flight

    def in_flight_item_ids(self) -> frozenset[int]:
        return self._in_flight


class _FakeState:
    def __init__(self, db, *, postprocess=None, delete_in_flight=None, engine=None):
        self.db = db
        self.events = EventBus()
        self.postprocess = postprocess
        self.delete_in_flight = delete_in_flight
        self.engine = engine


class _FakeApp:
    def __init__(self, db, **kwargs):
        self.state = _FakeState(db, **kwargs)


class _FakeRequest:
    def __init__(self, db, **kwargs):
        self.app = _FakeApp(db, **kwargs)


async def _make_queue(db, name="q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/remote', '/local', 1, 'copy')",
        (host_id, name),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id, rel_path, *, is_dir=False) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, "
        "auto_queue_suppressed, suppressed_reason) "
        "VALUES (?, ?, ?, 100, 100, 'STOPPED', 1, 'user_stopped')",
        (queue_id, rel_path, 1 if is_dir else 0),
    )
    await db.commit()
    return cursor.lastrowid


# --- POST /api/items/{item_id}/reset ------------------------------------------------------


async def test_reset_item_404s_on_unknown_item(db):
    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_item(999, _FakeRequest(db))
    assert excinfo.value.status_code == 404


async def test_reset_item_succeeds_and_evicts_the_engine_cache(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "Release", is_dir=True)
    engine = _FakeEngine()

    result = await jobs.reset_item(item_id, _FakeRequest(db, engine=engine))

    assert result.reset is True
    assert result.affected_rel_paths == ["Release"]
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    assert await cursor.fetchone() is None
    assert engine.forgotten == [(queue_id, ("Release",))]
    assert engine.rescanned is True


async def test_reset_item_409s_when_an_active_job_exists(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "Release", is_dir=True)
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (item_id,)
    )
    await db.commit()
    engine = _FakeEngine()

    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_item(item_id, _FakeRequest(db, engine=engine))
    assert excinfo.value.status_code == 409
    assert "active job" in excinfo.value.detail
    # Nothing was forgotten -- refused, not raced.
    assert engine.forgotten == []
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    assert await cursor.fetchone() is not None


async def test_reset_item_409s_when_postprocess_in_flight(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "Release", is_dir=True)

    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_item(
            item_id, _FakeRequest(db, postprocess=_FakePostprocess(frozenset({item_id})))
        )
    assert excinfo.value.status_code == 409
    assert "post-processing" in excinfo.value.detail


async def test_reset_item_works_without_an_engine_attached(db):
    """`engine` is `None` for plenty of existing tests (`test_delete_api.py`'s own convention) --
    the reset must still succeed; it just skips the cache-eviction/rescan step.
    """
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "Release", is_dir=True)

    result = await jobs.reset_item(item_id, _FakeRequest(db))
    assert result.reset is True


# --- POST /api/queues/{queue_id}/reset-all ------------------------------------------------


async def test_reset_queue_all_404s_on_unknown_queue(db):
    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_queue_all(999, QueueResetRequest(confirm_name="q"), _FakeRequest(db))
    assert excinfo.value.status_code == 404


async def test_reset_queue_all_400s_when_confirm_name_does_not_match(db):
    queue_id = await _make_queue(db, name="my-queue")
    await _make_item(db, queue_id, "Release", is_dir=True)

    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_queue_all(
            queue_id, QueueResetRequest(confirm_name="wrong-name"), _FakeRequest(db)
        )
    assert excinfo.value.status_code == 400
    # Nothing touched -- the row must still exist.
    cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
    assert len(await cursor.fetchall()) == 1


async def test_reset_queue_all_resets_everything_once_confirmed(db):
    queue_id = await _make_queue(db, name="my-queue")
    await _make_item(db, queue_id, "One", is_dir=True)
    await _make_item(db, queue_id, "Two", is_dir=True)
    engine = _FakeEngine()

    result = await jobs.reset_queue_all(
        queue_id, QueueResetRequest(confirm_name="my-queue"), _FakeRequest(db, engine=engine)
    )

    assert result.reset_top_level == 2
    assert result.affected_count == 2
    assert result.withheld == []
    cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
    assert await cursor.fetchall() == []
    assert engine.rescanned is True


async def test_reset_queue_all_reports_withheld_items_without_failing_the_rest(db):
    queue_id = await _make_queue(db, name="my-queue")
    busy_id = await _make_item(db, queue_id, "Busy", is_dir=True)
    await _make_item(db, queue_id, "Idle", is_dir=True)
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (busy_id,)
    )
    await db.commit()

    result = await jobs.reset_queue_all(
        queue_id, QueueResetRequest(confirm_name="my-queue"), _FakeRequest(db)
    )

    assert result.reset_top_level == 1
    assert result.withheld == [{"rel_path": "Busy", "reason": "an active job exists for this item"}]
    cursor = await db.execute("SELECT rel_path FROM item WHERE queue_id = ?", (queue_id,))
    assert [r["rel_path"] for r in await cursor.fetchall()] == ["Busy"]


# --- POST /api/queues/{queue_id}/reset-all-preview ----------------------------------------
#
# 2026-08-14, prompts/2026-08-14-reset-all-preview-undercounts.md: before this endpoint existed,
# the frontend improvised the All scope's preview from the published Files tree, which
# `core/engine.py` stops publishing a row from once it reaches a terminal removed state with
# nothing left in either tree (`a4a626d`) -- so a `REMOVED_BOTH` row already off the wire was
# invisible to that preview while a confirmed reset-all forgot it regardless. These tests assert
# the fix's own invariant directly: the preview and the execute path report the same count.


async def test_reset_all_preview_404s_on_unknown_queue(db):
    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_all_preview(999, _FakeRequest(db))
    assert excinfo.value.status_code == 404


async def test_reset_all_preview_never_touches_the_database(db):
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "One", is_dir=True)
    await _make_item(db, queue_id, "Two", is_dir=True)

    result = await jobs.reset_all_preview(queue_id, _FakeRequest(db))

    assert {i.rel_path for i in result.items} == {"One", "Two"}
    cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
    assert len(await cursor.fetchall()) == 2  # nothing was reset by a preview


async def test_reset_all_preview_includes_a_row_no_longer_published(db):
    """The regression case that matters most: a queue with one live item and one `REMOVED_BOTH`
    item that `core/engine.py` would no longer publish to the Files tree (both sizes NULL,
    nothing left in either tree). The All preview must report both, and `reset-all`'s own
    outcome count must equal the preview's count exactly -- asserted directly, not by eyeballing
    two numbers.
    """
    queue_id = await _make_queue(db, name="my-queue")
    await _make_item(db, queue_id, "Live", is_dir=True)
    await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, "
        "auto_queue_suppressed, suppressed_reason) "
        "VALUES (?, 'Gone', 1, NULL, NULL, 'REMOVED_BOTH', 0, NULL)",
        (queue_id,),
    )
    await db.commit()

    preview = await jobs.reset_all_preview(queue_id, _FakeRequest(db))
    assert {i.rel_path for i in preview.items} == {"Live", "Gone"}

    result = await jobs.reset_queue_all(
        queue_id, QueueResetRequest(confirm_name="my-queue"), _FakeRequest(db)
    )

    assert result.reset_top_level == len(preview.items)
    assert result.affected_count == len(preview.items)
    cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
    assert await cursor.fetchall() == []


async def test_reset_all_preview_and_pattern_star_report_the_same_set(db):
    """`All` is supposed to be a superset of any pattern match -- for a bare `*` (matches
    everything, `core/patterns.py.pattern_matches`), the two scopes must report the identical
    set for the same queue.
    """
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "One", is_dir=True)
    await _make_item(db, queue_id, "Two", is_dir=True)

    all_preview = await jobs.reset_all_preview(queue_id, _FakeRequest(db))
    pattern_preview = await jobs.reset_preview(
        queue_id, ResetPatternPreviewRequest(pattern="*"), _FakeRequest(db)
    )

    assert {i.rel_path for i in all_preview.items} == {i.rel_path for i in pattern_preview.items}


# --- POST /api/queues/{queue_id}/reset-preview and reset-by-pattern ------------------------


async def test_reset_preview_404s_on_unknown_queue(db):
    with pytest.raises(HTTPException) as excinfo:
        await jobs.reset_preview(999, ResetPatternPreviewRequest(pattern="*"), _FakeRequest(db))
    assert excinfo.value.status_code == 404


async def test_reset_preview_never_touches_the_database(db):
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "Junk.Release", is_dir=True)
    await _make_item(db, queue_id, "Keep.This", is_dir=True)

    result = await jobs.reset_preview(
        queue_id, ResetPatternPreviewRequest(pattern="junk*"), _FakeRequest(db)
    )

    assert [i.rel_path for i in result.items] == ["Junk.Release"]
    cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
    assert len(await cursor.fetchall()) == 2  # nothing was reset by a preview


async def test_reset_by_pattern_resets_only_matches_in_this_queue(db):
    queue_a = await _make_queue(db, name="qa")
    queue_b = await _make_queue(db, name="qb")
    await _make_item(db, queue_a, "Junk.Release", is_dir=True)
    await _make_item(db, queue_a, "Keep.This", is_dir=True)
    await _make_item(db, queue_b, "Junk.Release", is_dir=True)
    engine = _FakeEngine()

    result = await jobs.reset_by_pattern(
        queue_a, ResetPatternPreviewRequest(pattern="junk*"), _FakeRequest(db, engine=engine)
    )

    assert result.reset_top_level == 1
    assert result.affected_count == 1
    cursor = await db.execute("SELECT rel_path FROM item WHERE queue_id = ?", (queue_a,))
    assert [r["rel_path"] for r in await cursor.fetchall()] == ["Keep.This"]
    # The identical rel_path in the other queue is untouched.
    cursor = await db.execute("SELECT rel_path FROM item WHERE queue_id = ?", (queue_b,))
    assert [r["rel_path"] for r in await cursor.fetchall()] == ["Junk.Release"]
    assert engine.forgotten == [(queue_a, ("Junk.Release",))]
