"""`GET /api/items/{item_id}/children` (2026-08-20, docs/transfers-redesign-spec.md §3.3, phase 1
stage 5) -- the Transfers row's on-demand per-file expansion. Same "call the route function
directly with a minimal `Request` stand-in" shape `tests/test_item_events_api.py` already
established, since the thing under test is the query logic, not FastAPI's routing layer.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.api import jobs
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


async def _make_item(
    db,
    queue_id: int,
    rel_path: str,
    *,
    is_dir: bool = False,
    remote_size: int | None = 1000,
    local_size: int | None = 1000,
    state: str = "DOWNLOADED",
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (queue_id, rel_path, int(is_dir), remote_size, local_size, state),
    )
    await db.commit()
    return cursor.lastrowid


async def test_unknown_item_is_404(db):
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - HTTPException, imported lazily below
        await jobs.item_children(999999, _FakeRequest(db))
    assert exc_info.value.status_code == 404


async def test_non_directory_item_has_no_children(db):
    """A `pget` (single-file) job's own item row is never a directory -- `core/queue.py.
    _publish_child_progress`'s own "pget job: no children" branch, mirrored here: an empty list,
    not a 404 or an error, since a leaf file having no descendants is the expected, correct
    answer.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "movie.mkv", is_dir=False)

    resp = await jobs.item_children(item, _FakeRequest(db))
    assert resp.children == []
    assert resp.total == 0


async def test_returns_leaf_files_under_the_parent_ordered_by_rel_path(db):
    queue_id = await _make_queue(db)
    parent = await _make_item(
        db, queue_id, "Release", is_dir=True, remote_size=None, local_size=None
    )
    await _make_item(db, queue_id, "Release/b.mkv", state="DOWNLOADED")
    await _make_item(db, queue_id, "Release/a.mkv", state="PARTIAL", local_size=500)
    # A nested subdirectory (e.g. "Release/Season 01") and a sibling item outside the subtree
    # must never leak into this item's own children.
    await _make_item(
        db, queue_id, "Release/Season 01", is_dir=True, remote_size=None, local_size=None
    )
    await _make_item(db, queue_id, "Release/Season 01/c.mkv", state="DOWNLOADED")
    await _make_item(db, queue_id, "Other Release/z.mkv", state="DOWNLOADED")

    resp = await jobs.item_children(parent, _FakeRequest(db))
    paths = [c.rel_path for c in resp.children]
    assert paths == ["Release/Season 01/c.mkv", "Release/a.mkv", "Release/b.mkv"]
    assert resp.total == 3
    assert all(not c.is_dir for c in resp.children)

    partial = next(c for c in resp.children if c.rel_path == "Release/a.mkv")
    assert partial.state == "PARTIAL"
    assert partial.local_size == 500
    assert partial.remote_size == 1000


async def test_cap_is_enforced_regardless_of_requested_limit(db):
    queue_id = await _make_queue(db)
    parent = await _make_item(
        db, queue_id, "Release", is_dir=True, remote_size=None, local_size=None
    )
    for i in range(jobs.ITEM_CHILDREN_MAX_LIMIT + 20):
        await _make_item(db, queue_id, f"Release/{i:04d}.mkv")

    resp = await jobs.item_children(parent, _FakeRequest(db), limit=100000)
    assert len(resp.children) == jobs.ITEM_CHILDREN_MAX_LIMIT
    assert resp.total == jobs.ITEM_CHILDREN_MAX_LIMIT + 20


async def test_default_limit_is_generous_but_bounded(db):
    """The undocumented-but-real default a caller gets by omitting `limit` entirely -- generous
    enough for the realistic case (a season pack, "dozens of children") without inheriting the
    pathological-release cap by default.
    """
    queue_id = await _make_queue(db)
    parent = await _make_item(
        db, queue_id, "Release", is_dir=True, remote_size=None, local_size=None
    )
    for i in range(30):
        await _make_item(db, queue_id, f"Release/{i:02d}.mkv")

    resp = await jobs.item_children(parent, _FakeRequest(db))
    assert len(resp.children) == 30
    assert resp.limit == jobs.ITEM_CHILDREN_DEFAULT_LIMIT
