"""End-to-end verification for `prompts/2026-08-16-manual-delete-local-and-remote.md`: the
manual delete dialog's independent Source scope, against the **real fake seedbox** (DESIGN.md
§14) -- real ssh, real asyncssh delete, wired exactly the way `main.py` wires
`app.state.postprocess` (`PostprocessPipeline.resolve_host()`/`remote_pool`), never called
directly by test code the way `tests/test_delete_api.py`'s fast guard tests fake it out. Skipped
automatically if the seedbox isn't reachable (`docker compose -f docker-compose.test.yml up -d`
first).

The point of this file, stated plainly: `api/jobs.py._delete_source_manual` only ever *asks* the
remote to go away (`RemoteConnectionPool.delete_path`, an `rm -rf` over a real SSH connection) --
the fast tests in `test_delete_api.py` fake that call out entirely, so they can assert "the
right path was passed" but never "the file is actually gone." This file is the one place that
proves it, the same way `tests/test_postprocess_e2e.py` proves the automatic ladder's delete by
rescanning afterwards with an independent `RemoteConnectionPool`, not by trusting
`item.remote_deleted_at` alone.

Deliberately does **not** reuse `docker/test-seedbox/seed_tree.sh`'s baked-in fixture files --
this feature deletes its source, so each test uploads its own throwaway content into a
dedicated, uniquely-named remote subdirectory over SFTP at setup time, exactly like
`test_postprocess_e2e.py`'s own fixtures.
"""

from __future__ import annotations

import socket
from uuid import uuid4

import aiosqlite
import pytest

from lftpweb.api import jobs
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.postprocess import PostprocessPipeline
from lftpweb.core.remote import HostConfig, RemoteConnectionPool
from lftpweb.db import migrate
from lftpweb.models import DeleteItemRequest

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"

_CONTENT = b"manual source-delete e2e fixture -- safe to delete, uploaded fresh by this test\n"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up -d`",
)


def _host_config() -> HostConfig:
    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=SEEDBOX_PASSWORD,
        known_hosts_policy="insecure",
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeState:
    def __init__(self, db, *, postprocess):
        self.db = db
        self.events = EventBus()
        self.postprocess = postprocess
        self.delete_in_flight = None
        self.queue = None  # no active job in any of these tests


class _FakeApp:
    def __init__(self, db, *, postprocess):
        self.state = _FakeState(db, postprocess=postprocess)


class _FakeRequest:
    def __init__(self, db, *, postprocess):
        self.app = _FakeApp(db, postprocess=postprocess)


async def _upload_fixture(pool: RemoteConnectionPool, host: HostConfig, remote_full: str) -> int:
    conn = await pool.get_connection(host)
    async with conn.start_sftp_client() as sftp:
        await sftp.makedirs(str(remote_full.rsplit("/", 1)[0]), exist_ok=True)
        async with sftp.open(remote_full, "wb") as f:
            await f.write(_CONTENT)
    entries, _ = await pool.scan(host, remote_full.rsplit("/", 1)[0])
    rel = remote_full.rsplit("/", 1)[1]
    assert rel in entries, "setup fixture failed to upload"
    return entries[rel].size


async def _make_host_and_queue(db, *, remote_path: str, local_path, sync_mode: str = "copy") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'e2e-manual-source-delete', ?, ?, 1, ?)",
        (host_id, remote_path, str(local_path), sync_mode),
    )
    await db.commit()
    return cursor.lastrowid


async def test_manual_source_only_delete_actually_removes_the_remote_tree(db, tmp_path):
    """The dialog's own stated use case: a failed/never-imported item -- no local copy at all
    (`local_size` NULL, `state='REMOTE_ONLY'`) -- cleaned up entirely from the app without
    SSHing into the seedbox by hand. Local is never requested here, and there is nothing local
    to touch anyway.
    """
    remote_subdir = f"manual-source-delete-e2e-{uuid4().hex[:12]}"
    rel_path = "target.txt"
    remote_full = f"/data/pickup/{remote_subdir}/{rel_path}"

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    host = _host_config()
    try:
        remote_size = await _upload_fixture(scan_pool, host, remote_full)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        queue_id = await _make_host_and_queue(
            db, remote_path=f"/data/pickup/{remote_subdir}", local_path=local_dir
        )
        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, ?, 0, ?, NULL, 'REMOTE_ONLY')",
            (queue_id, rel_path, remote_size),
        )
        item_id = cursor.lastrowid
        await db.commit()

        async def host_provider():
            return host

        pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
        try:
            pipeline = PostprocessPipeline(
                db=db, events=EventBus(), remote_pool=pipeline_pool, host_provider=host_provider
            )

            result = await jobs.delete_item(
                item_id,
                _FakeRequest(db, postprocess=pipeline),
                body=DeleteItemRequest(local=False, source=True),
            )
            assert result.deleted is True
            assert result.source_deleted is True
        finally:
            await pipeline_pool.close()

        item_row = await (
            await db.execute(
                "SELECT remote_deleted_at, auto_queue_suppressed, suppressed_reason "
                "FROM item WHERE id = ?",
                (item_id,),
            )
        ).fetchone()
        assert item_row["remote_deleted_at"] is not None
        assert item_row["auto_queue_suppressed"] == 1
        assert item_row["suppressed_reason"] == "deleted_source"

        event_rows = await (
            await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [r["kind"] for r in event_rows]
        assert "remote_delete" in kinds
        assert "remote_delete_failed" not in kinds
        manual_events = [r for r in event_rows if r["kind"] == "remote_delete"]
        assert any(
            "manual" in r["message"] and "deleted by user request" in r["message"]
            for r in manual_events
        )

        # --- The actual proof: rescan the remote and confirm the file is gone -----------------
        verify_pool = RemoteConnectionPool(tmp_path / "known_hosts_verify")
        try:
            entries_after, _ = await verify_pool.scan(host, f"/data/pickup/{remote_subdir}")
        finally:
            await verify_pool.close()
        assert (
            rel_path not in entries_after
        ), f"remote file {remote_full} is still present after a manual source-only delete"
    finally:
        try:
            await scan_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        await scan_pool.close()


async def test_manual_combined_delete_removes_both_local_and_remote(db, tmp_path):
    """The delete dialog's `move`-queue default (both boxes checked): local and source both
    actually gone afterwards, confirmed independently on both sides -- the local file off disk,
    the remote file off the seedbox via a fresh rescan.
    """
    remote_subdir = f"manual-combined-delete-e2e-{uuid4().hex[:12]}"
    rel_path = "target.txt"
    remote_full = f"/data/pickup/{remote_subdir}/{rel_path}"

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    host = _host_config()
    try:
        remote_size = await _upload_fixture(scan_pool, host, remote_full)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        local_file = local_dir / rel_path
        local_file.write_bytes(_CONTENT)
        write_if_needed(str(local_dir))

        queue_id = await _make_host_and_queue(
            db, remote_path=f"/data/pickup/{remote_subdir}", local_path=local_dir, sync_mode="move"
        )
        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, ?, 0, ?, ?, 'DOWNLOADED')",
            (queue_id, rel_path, remote_size, len(_CONTENT)),
        )
        item_id = cursor.lastrowid
        await db.commit()

        async def host_provider():
            return host

        pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
        try:
            pipeline = PostprocessPipeline(
                db=db, events=EventBus(), remote_pool=pipeline_pool, host_provider=host_provider
            )

            result = await jobs.delete_item(
                item_id,
                _FakeRequest(db, postprocess=pipeline),
                body=DeleteItemRequest(local=True, source=True),
            )
            assert result.deleted is True
            assert result.source_deleted is True
        finally:
            await pipeline_pool.close()

        assert not local_file.exists()

        item_row = await (
            await db.execute(
                "SELECT remote_deleted_at, suppressed_reason FROM item WHERE id = ?", (item_id,)
            )
        ).fetchone()
        assert item_row["remote_deleted_at"] is not None
        # Combined request: local's own write is the one that sticks (endpoint docstring).
        assert item_row["suppressed_reason"] == "deleted_local"

        verify_pool = RemoteConnectionPool(tmp_path / "known_hosts_verify")
        try:
            entries_after, _ = await verify_pool.scan(host, f"/data/pickup/{remote_subdir}")
        finally:
            await verify_pool.close()
        assert (
            rel_path not in entries_after
        ), f"remote file {remote_full} is still present after a manual combined delete"
    finally:
        try:
            await scan_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        await scan_pool.close()
