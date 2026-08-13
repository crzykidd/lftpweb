"""`api/jobs.py.delete_item` and `api/settings.py`'s retention settings/preview routes
(`prompts/open-issues.md` "7 + 8" -- the first delete endpoint in this API). Same
call-the-route-function-directly harness `tests/test_history_api.py` uses -- the thing under
test is the route's own wiring (status codes, request/response shapes, guard-failure ->
HTTPException), not FastAPI's routing layer.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs, settings as settings_api
from lftpweb.core import local_delete
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.db import migrate
from lftpweb.models import RetentionPreviewRequest, RetentionSettingsIn


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeState:
    def __init__(self, db, *, postprocess=None):
        self.db = db
        self.events = EventBus()
        self.postprocess = postprocess


class _FakeApp:
    def __init__(self, db, *, postprocess=None):
        self.state = _FakeState(db, postprocess=postprocess)


class _FakeRequest:
    def __init__(self, db, *, postprocess=None):
        self.app = _FakeApp(db, postprocess=postprocess)


async def _make_queue(db, local_path) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id, rel_path, *, local_size=100) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, ?, 'DOWNLOADED')",
        (queue_id, rel_path, local_size, local_size),
    )
    await db.commit()
    return cursor.lastrowid


async def test_delete_item_404s_on_unknown_item(db):
    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(999, _FakeRequest(db))
    assert excinfo.value.status_code == 404


async def test_delete_item_succeeds_and_reports_bytes_freed(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"x" * 42)
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt", local_size=42)

    result = await jobs.delete_item(item_id, _FakeRequest(db))
    assert result.deleted is True
    assert result.bytes_freed == 42
    assert not target.exists()


async def test_delete_item_withheld_guard_raises_409(db, tmp_path):
    """Manual delete still goes through every guard except nlink -- a mount-sentinel failure
    (or any other withhold) must surface as an HTTP error, not a quiet `deleted: false`, so
    the frontend's `Promise.allSettled` bulk reporting picks it up as a failure.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "junk.txt").write_bytes(b"x")
    # No write_if_needed(): mount sentinel missing.

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(item_id, _FakeRequest(db))
    assert excinfo.value.status_code == 409
    assert "sentinel" in excinfo.value.detail


async def test_delete_item_manual_guard_is_off_for_nlink(db, tmp_path):
    """The one place manual and retention differ: manual deletes a single-link file that
    retention's `require_nlink_guard=True` would refuse.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    result = await jobs.delete_item(item_id, _FakeRequest(db))
    assert result.deleted is True
    assert not target.exists()


# --- Settings -> retention ---------------------------------------------------------------


async def test_get_retention_settings_default_off(db):
    result = await settings_api.get_retention_settings(_FakeRequest(db))
    assert result.enabled is False
    assert result.retention_days == 30.0


async def test_put_retention_settings_round_trips(db):
    body = RetentionSettingsIn(enabled=True, retention_days=10.0)
    result = await settings_api.put_retention_settings(body, _FakeRequest(db))
    assert result.enabled is True
    assert result.retention_days == 10.0

    loaded = await local_delete.load_retention_settings(db)
    assert loaded == local_delete.RetentionSettings(enabled=True, retention_days=10.0)


async def test_retention_preview_reports_count_and_total_bytes(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    pickup = tmp_path / "arr-library"
    pickup.mkdir()
    for name, size in (("A.Release", 100), ("B.Release", 250)):
        d = local_root / name
        d.mkdir()
        f = d / "a.mkv"
        f.write_bytes(b"x" * size)
        os.link(f, pickup / f"{name}.mkv")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for name, size in (("A.Release", 100), ("B.Release", 250)):
        await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, downloaded_at) "
            "VALUES (?, ?, 1, ?, ?, 'DOWNLOADED', ?)",
            (queue_id, name, size, size, old_ts),
        )
    await db.commit()

    result = await settings_api.retention_preview(
        RetentionPreviewRequest(retention_days=30.0), _FakeRequest(db)
    )
    assert result.count == 2
    assert result.total_bytes == 350
    assert {i.rel_path for i in result.items} == {"A.Release", "B.Release"}

    # Nothing on disk was touched by a preview.
    assert (local_root / "A.Release").exists()
    assert (local_root / "B.Release").exists()
