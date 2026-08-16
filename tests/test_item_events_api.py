"""`GET /api/items/{item_id}/events` (2026-08-15,
prompts/2026-08-15-transfers-single-line-rows-with-detail.md) -- the Transfers panel's
on-demand "processing story" fetch. Same "call the route function directly with a minimal
`Request` stand-in" shape `tests/test_history_api.py` already established, since the thing
under test is the query logic, not FastAPI's routing layer.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.api import jobs
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


async def _make_queue(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, 1000, 1000, 'DOWNLOADED')",
        (queue_id, rel_path),
    )
    await db.commit()
    return cursor.lastrowid


async def test_item_events_returns_only_this_items_rows_newest_first(db):
    """Item scoping: an event for a *different* item must never leak into this item's panel."""
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt")
    other_item = await _make_item(db, queue_id, "b.txt")

    await audit.record_event(db, level="info", item_id=item, kind="verify", message="first")
    await audit.record_event(db, level="info", item_id=item, kind="extract", message="second")
    await audit.record_event(
        db, level="info", item_id=other_item, kind="verify", message="other item"
    )

    resp = await jobs.item_events(item, _FakeRequest(db))
    assert [e.kind for e in resp.events] == ["extract", "verify"]
    assert all(e.message != "other item" for e in resp.events)


async def test_item_events_cap_is_enforced_regardless_of_requested_limit(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt")
    for i in range(jobs.ITEM_EVENTS_MAX_LIMIT + 20):
        await audit.record_event(db, level="info", item_id=item, kind="verify", message=str(i))

    resp = await jobs.item_events(item, _FakeRequest(db), limit=100000)
    assert len(resp.events) == jobs.ITEM_EVENTS_MAX_LIMIT


async def test_item_events_unknown_item_returns_an_empty_list_not_an_error(db):
    resp = await jobs.item_events(999999, _FakeRequest(db))
    assert resp.events == []


async def test_item_events_message_rides_verbatim(db):
    """The panel's whole "processing story" philosophy (History §7.3's precedent): the pipeline's
    own carefully-worded event message is what renders, unmodified.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.txt")
    await audit.record_event(
        db,
        level="warning",
        item_id=item,
        kind="remote_delete_withheld",
        message="manual: delete withheld -- verification failed (CORRUPT)",
    )

    resp = await jobs.item_events(item, _FakeRequest(db))
    assert len(resp.events) == 1
    assert resp.events[0].message == "manual: delete withheld -- verification failed (CORRUPT)"
    assert resp.events[0].level == "warning"
