"""`api/jobs.py.delete_item` and `api/settings.py`'s retention settings/preview routes
(`prompts/open-issues.md` "7 + 8" -- the first delete endpoint in this API). Same
call-the-route-function-directly harness `tests/test_history_api.py` uses -- the thing under
test is the route's own wiring (status codes, request/response shapes, guard-failure ->
HTTPException), not FastAPI's routing layer.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs
from lftpweb.api import settings_postprocess as settings_api
from lftpweb.core import local_delete
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.remote import HostConfig
from lftpweb.db import migrate
from lftpweb.models import DeleteItemRequest, RetentionPreviewRequest, RetentionSettingsIn


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeState:
    def __init__(self, db, *, postprocess=None, queue=None):
        self.db = db
        self.events = EventBus()
        self.postprocess = postprocess
        # 2026-08-13 (prompts/2026-08-13-delete-during-transfer.md): `None` by default, exactly
        # like every existing test in this file that never sets it -- `delete_item`'s
        # `getattr(..., "queue", None)` reading skips the stop-before-delete step entirely in
        # that case, which is what keeps every guard test above unaffected by this addition.
        self.queue = queue


class _FakeApp:
    def __init__(self, db, *, postprocess=None, queue=None):
        self.state = _FakeState(db, postprocess=postprocess, queue=queue)


class _FakeRequest:
    def __init__(self, db, *, postprocess=None, queue=None):
        self.app = _FakeApp(db, postprocess=postprocess, queue=queue)


class _FakeStopQueue:
    """A minimal stand-in for `core/queue.py.TransferQueue`, exposing only the one method
    `delete_item` calls (`stop_item`) -- controllable delay/result so the orchestration
    (always-call, bounded-wait-without-cancelling, propagate-a-real-failure) can be exercised
    without a real lftp process or the fake seedbox.
    """

    def __init__(self, *, delay_s: float = 0.0, result: bool = True, exc: Exception | None = None):
        self.delay_s = delay_s
        self.result = result
        self.exc = exc
        self.calls: list[int] = []

    async def stop_item(self, item_id: int) -> bool:
        self.calls.append(item_id)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeRemotePool:
    """Stands in for `core/remote.py.RemoteConnectionPool` -- the identical fake
    `tests/test_postprocess.py` uses for the automatic ladder, reused here so the manual source
    scope (`api/jobs.py._delete_source_manual`) is exercised the same way: no live SSH
    connection, just a record of every delete attempt so "did this actually try to touch the
    remote" is assertable.
    """

    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[HostConfig, str]] = []
        self.fail = fail

    async def delete_path(self, host: HostConfig, remote_path: str) -> None:
        self.calls.append((host, remote_path))
        if self.fail:
            raise RuntimeError("simulated remote delete failure")


def _host_config() -> HostConfig:
    return HostConfig(
        id=1, address="seedbox.invalid", port=22, username="u", auth_method="key", key_path="/k"
    )


class _FakePostprocess:
    """A minimal stand-in for `core/postprocess.py.PostprocessPipeline`, exposing only the
    surface `delete_item`'s source scope reads: `remote_pool` (a plain attribute, same as the
    real pipeline) and `resolve_host()` (the public wrapper this task added around
    `_host_provider`). `in_flight_item_ids()` is included too since the local scope's own guard
    reads it whenever a `postprocess` is supplied at all.
    """

    def __init__(self, *, remote_pool=None, host=None, in_flight=frozenset()):
        self.remote_pool = remote_pool
        self._host = host
        self._in_flight = in_flight

    def in_flight_item_ids(self):
        return self._in_flight

    async def resolve_host(self):
        return self._host


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


# --- Stop-before-delete orchestration (2026-08-13, prompts/2026-08-13-delete-during-transfer.md)
# ------------------------------------------------------------------------------------------------
# Real "stop a running lftp process, confirm it's gone" coverage lives in
# tests/test_delete_during_transfer_e2e.py against the fake seedbox -- these are the fast,
# no-process guard-behavior tests: `stop_item` is always called, a slow-but-eventually-
# successful stop still lets the delete through, a stop that never confirms in time withholds
# and records why, and a genuine failure from `stop_item` itself propagates rather than being
# swallowed.


async def test_delete_item_calls_stop_item_before_deleting_even_with_no_active_job(db, tmp_path):
    """`stop_item()` is a safe no-op when nothing is active for the item (its own docstring) --
    `delete_item` calls it unconditionally rather than pre-checking for a job row itself, so
    there is exactly one place ("is there anything to stop") that answer is computed.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    stop_queue = _FakeStopQueue()
    result = await jobs.delete_item(item_id, _FakeRequest(db, queue=stop_queue))

    assert stop_queue.calls == [item_id]
    assert result.deleted is True
    assert not target.exists()


async def test_delete_item_proceeds_once_a_slow_stop_confirms_within_the_bound(db, tmp_path):
    """A stop that takes a little while (but well inside `STOP_BEFORE_DELETE_TIMEOUT_S`) is not
    a withhold -- the delete goes through exactly as if the stop had been instant.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    stop_queue = _FakeStopQueue(delay_s=0.05)
    result = await jobs.delete_item(item_id, _FakeRequest(db, queue=stop_queue))

    assert stop_queue.calls == [item_id]
    assert result.deleted is True
    assert not target.exists()


async def test_delete_item_withheld_when_stop_cannot_be_confirmed_in_time(
    db, tmp_path, monkeypatch
):
    """The task's own instruction, verified directly: "if the stop cannot be confirmed within a
    bounded time, withhold the delete and say why." Nothing is deleted, a 409 names the reason,
    an `event` row records it, and -- the part that matters most -- the stop attempt is not
    abandoned: it keeps running in the background rather than being cancelled the instant this
    request stops waiting for it (cancelling it would leave `core/queue.py`'s own bookkeeping
    half-updated, exactly the inconsistency this feature must not introduce).
    """
    monkeypatch.setattr(jobs, "STOP_BEFORE_DELETE_TIMEOUT_S", 0.05)

    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "still-downloading.txt"
    target.write_bytes(b"partial")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "still-downloading.txt")

    stop_queue = _FakeStopQueue(delay_s=0.5)
    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(item_id, _FakeRequest(db, queue=stop_queue))
    assert excinfo.value.status_code == 409
    assert "could not be confirmed stopped" in excinfo.value.detail

    # The withhold happened before delete_local ever ran -- nothing on disk moved.
    assert target.exists()

    cursor = await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
    events = await cursor.fetchall()
    assert [e["kind"] for e in events] == ["local_delete_withheld"]
    assert "still-downloading.txt" in events[0]["message"]

    # Not cancelled: give the background task time to actually finish and confirm it did.
    await asyncio.sleep(0.6)
    assert stop_queue.calls == [item_id]


async def test_delete_item_propagates_a_genuine_stop_failure(db, tmp_path):
    """`stop_item()` raising (the `ValueError` `stop_job` documents for "job vanished between
    lookup and stop") must surface as a real error, not be swallowed on the way to a delete that
    then runs anyway against a job whose fate is actually unknown.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    stop_queue = _FakeStopQueue(exc=ValueError("job 999 not found"))
    with pytest.raises(ValueError, match="job 999 not found"):
        await jobs.delete_item(item_id, _FakeRequest(db, queue=stop_queue))

    # Never reached delete_local -- the file must still be there.
    assert target.exists()


# --- Manual source scope (2026-08-16, the delete dialog's independent Local/Source checkboxes,
# prompts/2026-08-16-manual-delete-local-and-remote.md) -- the first manual remote-delete path in
# the API. Real asyncssh-against-the-fake-seedbox coverage lives in
# tests/test_manual_source_delete_e2e.py; these are the fast, no-process guard/wiring tests.
# ------------------------------------------------------------------------------------------------


async def test_delete_item_requires_at_least_one_scope(db, tmp_path):
    queue_id = await _make_queue(db, tmp_path / "local")
    item_id = await _make_item(db, queue_id, "junk.txt")

    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(
            item_id, _FakeRequest(db), body=DeleteItemRequest(local=False, source=False)
        )
    assert excinfo.value.status_code == 400


async def test_delete_item_source_only_deletes_remote_and_leaves_local_untouched(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"still here")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(
        item_id,
        _FakeRequest(db, postprocess=postprocess),
        body=DeleteItemRequest(local=False, source=True),
    )

    assert result.deleted is True
    assert result.source_deleted is True
    assert result.source_reason == "deleted"
    # Local scope was never requested -- the file must still be there.
    assert target.exists()
    assert pool.calls == [(_host_config(), "/remote/junk.txt")]

    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["remote_deleted_at"] is not None
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "deleted_source"

    cursor = await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
    events = await cursor.fetchall()
    assert [e["kind"] for e in events] == ["remote_delete"]
    assert "manual" in events[0]["message"]
    assert "deleted by user request" in events[0]["message"]


async def test_delete_item_source_only_refuses_409_when_job_active(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank) VALUES (?, 'pget', 'running', 'main', 0)",
        (item_id,),
    )
    await db.commit()

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(
            item_id,
            _FakeRequest(db, postprocess=postprocess),
            body=DeleteItemRequest(local=False, source=True),
        )
    assert excinfo.value.status_code == 409
    assert "active transfer" in excinfo.value.detail
    # Refused before ever touching the remote -- unlike local, a source-only request never
    # stops the job itself.
    assert pool.calls == []

    cursor = await db.execute("SELECT kind FROM event WHERE item_id = ?", (item_id,))
    events = await cursor.fetchall()
    assert [e["kind"] for e in events] == ["remote_delete_withheld"]


async def test_delete_item_source_only_idempotent_when_already_gone(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")
    await db.execute(
        "UPDATE item SET remote_deleted_at = '2026-08-16T00:00:00.000000Z' WHERE id = ?",
        (item_id,),
    )
    await db.commit()

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(
        item_id,
        _FakeRequest(db, postprocess=postprocess),
        body=DeleteItemRequest(local=False, source=True),
    )
    assert result.source_deleted is True
    assert "idempotent" in result.source_reason
    # No SSH round trip for an already-gone remote copy.
    assert pool.calls == []


async def test_delete_item_source_only_clears_a_stale_remote_delete_pending(db, tmp_path):
    """ "Deleting source for an item mid-ladder simply completes the ladder early" (the task's
    own instruction) -- a manual source delete on an item the automatic pipeline had deferred
    (`item.remote_delete_pending` set, awaiting *arr import) clears that marker too, so a later
    History read never shows a wait that no longer means anything.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(
        item_id,
        _FakeRequest(db, postprocess=postprocess),
        body=DeleteItemRequest(local=False, source=True),
    )
    assert result.source_deleted is True

    cursor = await db.execute("SELECT remote_delete_pending FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["remote_delete_pending"] is None


async def test_delete_item_source_withholds_409_when_no_host_configured(db, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    postprocess = _FakePostprocess(remote_pool=_FakeRemotePool(), host=None)

    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(
            item_id,
            _FakeRequest(db, postprocess=postprocess),
            body=DeleteItemRequest(local=False, source=True),
        )
    assert excinfo.value.status_code == 409
    assert "no host configured" in excinfo.value.detail


async def test_delete_item_combined_deletes_both_and_keeps_deleted_local_suppression(db, tmp_path):
    """A combined request (the delete dialog's `move`-queue default) must not have the source
    step's own suppression write stomp `delete_local`'s `suppressed_reason = 'deleted_local'`
    back to `'deleted_source'` -- 'deleted_local' is the more complete fact about a row whose
    local copy is also gone.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(
        item_id,
        _FakeRequest(db, postprocess=postprocess),
        body=DeleteItemRequest(local=True, source=True),
    )
    assert result.deleted is True
    assert result.source_deleted is True
    assert not target.exists()
    assert pool.calls == [(_host_config(), "/remote/junk.txt")]

    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["remote_deleted_at"] is not None
    assert row["auto_queue_suppressed"] == 1
    assert row["suppressed_reason"] == "deleted_local"


async def test_delete_item_combined_local_failure_never_attempts_source(db, tmp_path):
    """Local runs first: if it is withheld, source must never be attempted at all -- the
    pre-existing single-scope 409 behavior, unchanged, and the combined request's own "local
    failure blocks the whole thing" rule from the endpoint's docstring.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "junk.txt").write_bytes(b"x")
    # No write_if_needed(): mount sentinel missing, so delete_local withholds.

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    with pytest.raises(HTTPException) as excinfo:
        await jobs.delete_item(
            item_id,
            _FakeRequest(db, postprocess=postprocess),
            body=DeleteItemRequest(local=True, source=True),
        )
    assert excinfo.value.status_code == 409
    assert pool.calls == []


async def test_delete_item_combined_source_failure_after_local_success_is_200_not_409(db, tmp_path):
    """A partial failure -- local already succeeded, source then fails -- must not raise: the
    local side effect already happened and cannot be undone, so a 409 would misrepresent a
    request that partially succeeded. The response instead reports the two scopes honestly.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    pool = _FakeRemotePool(fail=True)
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(
        item_id,
        _FakeRequest(db, postprocess=postprocess),
        body=DeleteItemRequest(local=True, source=True),
    )
    assert result.deleted is True
    assert not target.exists()
    assert result.source_deleted is False
    assert result.source_reason is not None

    cursor = await db.execute("SELECT kind FROM event WHERE item_id = ? ORDER BY id", (item_id,))
    kinds = [e["kind"] for e in await cursor.fetchall()]
    assert "remote_delete_failed" in kinds

    # Combined-but-source-failed must not fall back to source-only suppression -- local's own
    # 'deleted_local' write is what's there.
    cursor = await db.execute("SELECT suppressed_reason FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["suppressed_reason"] == "deleted_local"


async def test_delete_item_local_only_default_body_never_touches_remote(db, tmp_path):
    """An omitted body means exactly the pre-existing behavior -- even with a `postprocess`
    (and therefore a remote pool) available, a plain `delete_item(item_id, request)` call must
    never make a remote delete attempt.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    queue_id = await _make_queue(db, local_root)
    item_id = await _make_item(db, queue_id, "junk.txt")

    pool = _FakeRemotePool()
    postprocess = _FakePostprocess(remote_pool=pool, host=_host_config())

    result = await jobs.delete_item(item_id, _FakeRequest(db, postprocess=postprocess))
    assert result.deleted is True
    assert result.source_deleted is None
    assert result.source_reason is None
    assert not target.exists()
    assert pool.calls == []

    cursor = await db.execute("SELECT remote_deleted_at FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row["remote_deleted_at"] is None


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
