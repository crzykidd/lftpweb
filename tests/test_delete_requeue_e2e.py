"""End-to-end verification for the "manually re-queue what I deleted" path
(`prompts/2026-08-13-delete-must-mark-the-whole-subtree.md`). The user named this directly as
required behaviour and this task must not break it: "I should still see that it exists on the
remote host and it won't get autodownloaded again... but I should have an option to manually
queue it again." Against the real fake seedbox (§14), same fixtures/helpers as
`tests/test_history_e2e.py`.
"""

from __future__ import annotations

import asyncio
import socket
import time

import aiosqlite
import pytest

from lftpweb.core import local_delete
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.queue import TransferQueue, save_transfer_settings
from lftpweb.core.queue import TransferSettings as _TS
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up --build -d`",
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    # This file is about the delete/requeue path, not the settle gate (which now defaults on,
    # prompts/2026-08-12-settle-gate-followups.md item 3) -- without an `Engine` scan loop
    # running here, no fingerprint ever confirms and a successful job would sit forever at
    # REMOTE_ONLY/settling instead of reaching DOWNLOADED, exactly like `tests/test_local_delete.
    # py`'s own `_make_db` already disables it for the identical reason.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def _make_host(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', ?, ?, ?, 'password', 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queue(db, host_id: int, local_path) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'delete-requeue-e2e', '/data/pickup', ?, 1, 'copy')",
        (host_id, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str, *, remote_size: int) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, remote_size),
    )
    await db.commit()
    return cursor.lastrowid


async def _item_row(db, item_id: int):
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    return await cursor.fetchone()


async def _queue_row(db, queue_id: int):
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
    return await cursor.fetchone()


def _host_config():
    from lftpweb.core.remote import HostConfig

    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=SEEDBOX_PASSWORD,
        known_hosts_policy="insecure",
    )


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def _queue_for(db, tmp_path, *, tick_s: float = 0.2) -> TransferQueue:
    await save_transfer_settings(
        db,
        _TS(
            max_bandwidth_bps=10_000_000,
            max_concurrent_transfers=2,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=2,
            mirror_use_pget_n=2,
            pget_default_n=2,
        ),
    )

    async def host_provider():
        return _host_config()

    return TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=tick_s,
        host_provider=host_provider,
    )


async def test_deleted_item_can_be_manually_requeued_and_downloads_again(db, tmp_path):
    """All three parts the user named, asserted end to end rather than at any one call site:
    (1) a delete with a surviving remote copy reads `REMOVED_LOCAL`, not `REMOVED_BOTH`; (2) it
    is suppressed, so it won't get auto-downloaded again; (3) a manual Queue click
    (`core/queue.py.TransferQueue.enqueue_item`, the same call `FileTree.tsx`'s `rowAction`
    reaches, since `REMOVED_LOCAL` isn't `QUEUED`/`DOWNLOADING`/`LOCAL_ONLY`) clears the
    suppression and re-downloads it for real.
    """
    host_id = await _make_host(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir)
    item_id = await _make_item(db, queue_id, "loose-notes.txt", remote_size=512)

    q = await _queue_for(db, tmp_path)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def downloaded():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(downloaded), "initial transfer never succeeded"
        assert (local_dir / "loose-notes.txt").exists()

        # Manually delete the local copy -- the remote copy (item.remote_size) still exists.
        write_if_needed(str(local_dir))
        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert outcome.deleted is True
        assert not (local_dir / "loose-notes.txt").exists()

        item = await _item_row(db, item_id)
        assert (
            item["state"] == "REMOVED_LOCAL"
        ), "a remote copy survives -- this is not REMOVED_BOTH"
        assert item["auto_queue_suppressed"] == 1
        assert item["suppressed_reason"] == "deleted_local"

        # Manual re-queue: clears suppression, resets attempt, downloads again for real.
        job_id_2 = await q.enqueue_item(item_id)
        assert job_id_2 != job_id

        # `enqueue_item`'s own UPDATE clears suppression in the same write that sets `state`, so
        # this is race-free even though the queue's tick loop may already have moved `state`
        # past `QUEUED` (to `DOWNLOADING`, or even further) by the time this reads back.
        item_after_requeue = await _item_row(db, item_id)
        assert item_after_requeue["state"] in ("QUEUED", "DOWNLOADING")
        assert item_after_requeue["auto_queue_suppressed"] == 0
        assert item_after_requeue["suppressed_reason"] is None

        async def redownloaded():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id_2,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(redownloaded), "re-queued transfer never succeeded"
    finally:
        await q.stop()

    assert (local_dir / "loose-notes.txt").exists()
    item = await _item_row(db, item_id)
    assert item["state"] == "DOWNLOADED"
    assert item["auto_queue_suppressed"] == 0
    assert item["suppressed_reason"] is None
