"""End-to-end verification for `prompts/2026-08-13-delete-during-transfer.md`: "need to be able
to delete a folder or file when in progress ... it should say active copy going, are you sure
confirm, and then let the delete happen ... a cancelled job doesn't get auto added again?"

The reproduction this task exists for: deleting an item that has an active transfer must stop
the job first -- through `core/queue.py`'s own SIGTERM -> grace -> SIGKILL stop path, confirmed
dead and reaped -- and only then remove the local copy, rather than either the old behaviour
(withhold outright, DESIGN.md never let a user actually delete a downloading item) or the wrong
fix (delete the tree out from under a still-running lftp process). Against the real fake seedbox
(§14), same fixtures/helpers as `tests/test_queue.py`/`tests/test_delete_requeue_e2e.py`. Fast,
no-process guard-behavior coverage (stop-called-unconditionally, timeout withholds, a genuine
`stop_item` failure propagates) lives in `tests/test_delete_api.py` instead -- this file is only
for what actually requires a real lftp child.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time

import aiosqlite
import pytest

from lftpweb.api import jobs
from lftpweb.core.autoqueue import (
    AutoQueue,
    AutoQueueSettings,
    QueueAutoConfig,
    save_autoqueue_settings,
)
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.queue import TransferQueue, save_transfer_settings
from lftpweb.core.queue import TransferSettings as _TS
from lftpweb.core.remote import HostConfig
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
    # Isolates this file to what it actually tests, same reasoning as
    # tests/test_delete_requeue_e2e.py's own fixture.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


class _FakeState:
    def __init__(self, db, *, queue):
        self.db = db
        self.events = EventBus()
        self.postprocess = None
        self.delete_in_flight = None
        self.queue = queue


class _FakeApp:
    def __init__(self, db, *, queue):
        self.state = _FakeState(db, queue=queue)


class _FakeRequest:
    def __init__(self, db, *, queue):
        self.app = _FakeApp(db, queue=queue)


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
        "VALUES (?, 'delete-during-transfer-e2e', '/data/pickup', ?, 1, 'copy')",
        (host_id, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str, *, is_dir: bool, remote_size: int) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, ?, ?, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, 1 if is_dir else 0, remote_size),
    )
    await db.commit()
    return cursor.lastrowid


async def _item_row(db, item_id: int):
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    return await cursor.fetchone()


async def _job_row(db, job_id: int):
    cursor = await db.execute("SELECT * FROM job WHERE id = ?", (job_id,))
    return await cursor.fetchone()


async def _events_for(db, item_id: int):
    cursor = await db.execute(
        "SELECT kind, message FROM event WHERE item_id = ? ORDER BY id", (item_id,)
    )
    return await cursor.fetchall()


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


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def _queue_for(
    db, tmp_path, *, max_bandwidth_bps: int, max_concurrent_transfers: int = 1
) -> TransferQueue:
    await save_transfer_settings(
        db,
        _TS(
            max_bandwidth_bps=max_bandwidth_bps,
            max_concurrent_transfers=max_concurrent_transfers,
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
        tick_s=0.2,
        host_provider=host_provider,
    )


async def _assert_never_requeued_by_autoqueue(db, queue_id: int, local_root) -> None:
    """The user's own question, answered directly, both ways: a cancelled-for-delete job must
    never get auto-added again, whether or not the site additionally opts into re-fetching
    externally-removed content (`AutoQueueSettings.re_download_externally_removed`) -- the flag
    only ever widens *which state names* are eligible (DESIGN.md §4.6), never overrides
    `auto_queue_suppressed`, which is what this delete actually set.
    """
    enqueued: list[int] = []

    async def _enqueue(item_id: int) -> int:
        enqueued.append(item_id)
        return item_id

    aq = AutoQueue(db, _enqueue)
    for setting_on in (False, True):
        await save_autoqueue_settings(
            db, AutoQueueSettings(re_download_externally_removed=setting_on)
        )
        enqueued.clear()
        queued = await aq.on_scan(
            QueueAutoConfig(
                id=queue_id,
                local_path=str(local_root),
                auto_queue_enabled=True,
                patterns_only=False,
            )
        )
        assert queued == 0, f"re_download_externally_removed={setting_on} must not re-queue it"
        assert enqueued == []


async def test_delete_directory_item_mid_transfer_stops_job_and_removes_tree(db, tmp_path):
    """The reproduction: a directory (`mirror`) job is genuinely `running`, with a real PID and
    real partial bytes on disk, when the delete request lands. The lftp process must be
    confirmed dead and reaped -- not just signalled -- before any unlink happens; no job row is
    left `running`; the whole tree is gone; and nothing reappears on the next scan.
    """
    host_id = await _make_host(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir)
    # The same real 20 MB directory tests/test_queue.py's own concurrency test mirrors --
    # large enough that a low bandwidth cap keeps it genuinely mid-transfer for this test's
    # window rather than racing a fast loopback transfer.
    item_id = await _make_item(
        db, queue_id, "Movie.Title.2024.2160p", is_dir=True, remote_size=20_971_520
    )
    write_if_needed(str(local_dir))

    q = await _queue_for(db, tmp_path, max_bandwidth_bps=300_000, max_concurrent_transfers=1)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def running():
            row = await _job_row(db, job_id)
            return row is not None and row["state"] == "running" and row["pid"] is not None

        assert await _wait_until(running, timeout_s=15), "job never reached running"
        await asyncio.sleep(2.0)  # let real bytes accumulate under the cap

        pid = (await _job_row(db, job_id))["pid"]
        # "Folder prefix during transfer" defaults ON as of 2026-08-14, so an in-flight directory
        # item physically lives under `<prefix><name>/` and its logical name does not exist on
        # disk at all until the transfer completes. Asserting the prefixed path is the point:
        # this is now the default shape of a mid-transfer delete, and it exercises
        # `core/local_delete.py._physical_local_root` end to end (the bug where delete looked for
        # the logical path, found nothing, and refused with "does not exist -- nothing to
        # delete").
        target_dir = local_dir / ".downloading-Movie.Title.2024.2160p"
        logical_dir = local_dir / "Movie.Title.2024.2160p"
        assert target_dir.exists(), "the mirror job must have created its prefixed directory by now"
        assert (
            not logical_dir.exists()
        ), "the logical name must not appear until the transfer completes"

        result = await jobs.delete_item(item_id, _FakeRequest(db, queue=q))

        assert result.deleted is True
        # The process must be confirmed dead and reaped -- not merely signalled -- before this
        # point. `terminate()` awaits `proc.wait()` itself (SIGTERM's grace window, or after
        # SIGKILL), so by the time `stop_item()` returned inside `delete_item`, `/proc/<pid>`
        # was already gone; asserting it again here confirms the ordering held end to end.
        assert not os.path.exists(f"/proc/{pid}"), "lftp process must not survive the delete"

        job_row = await _job_row(db, job_id)
        assert job_row["state"] != "running", "no job row may be left running after a delete"
        assert job_row["state"] == "cancelled"

        item = await _item_row(db, item_id)
        assert item["state"] in ("REMOVED_LOCAL", "REMOVED_BOTH")
        assert item["auto_queue_suppressed"] == 1
        # The row must read as a deliberate delete, not the stop that got it there -- the stop
        # path's own `user_stopped` must have been overwritten by delete_local's unconditional
        # `deleted_local` write, not left standing.
        assert item["suppressed_reason"] == "deleted_local"

        assert not target_dir.exists(), "the whole prefixed tree must be gone"
        assert not logical_dir.exists(), "and nothing may be left under the logical name either"

        await _assert_never_requeued_by_autoqueue(db, queue_id, local_dir)
    finally:
        await q.stop()


async def test_delete_loose_file_item_mid_transfer_removes_lftp_temp_via_api(db, tmp_path):
    """The loose-top-level-file case, end to end through the real API path this time (unit
    coverage for `_do_remove_from_disk` itself lives in
    `tests/test_local_delete.py::test_loose_file_delete_removes_lftp_temp_leftover`): the file's
    own final name never lands on disk until the transfer completes, so a delete that reaches it
    once the job is confirmed stopped finds only `<name>.lftp` (plus its
    `.lftp-pget-status` sidecar) -- both must be gone, or the delete leaves exactly the bytes it
    was asked to remove sitting there under a different name.
    """
    host_id = await _make_host(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir)
    item_id = await _make_item(
        db,
        queue_id,
        "Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv",
        is_dir=False,
        remote_size=20_971_520,
    )
    write_if_needed(str(local_dir))

    q = await _queue_for(db, tmp_path, max_bandwidth_bps=300_000, max_concurrent_transfers=1)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def running():
            row = await _job_row(db, job_id)
            return row is not None and row["state"] == "running" and row["pid"] is not None

        assert await _wait_until(running, timeout_s=15), "job never reached running"
        await asyncio.sleep(2.0)

        target = local_dir / "Movie.Title.2024.2160p" / "Movie.Title.2024.2160p.mkv"
        temp = target.with_name(target.name + ".lftp")
        assert not target.exists(), "must not have been renamed to its final name yet"
        assert (
            temp.exists()
        ), "the in-flight .lftp temp file must exist for this test to mean anything"

        result = await jobs.delete_item(item_id, _FakeRequest(db, queue=q))

        assert result.deleted is True
        assert not target.exists()
        assert (
            not temp.exists()
        ), "the in-flight .lftp temp file must be removed, not just the final name"

        item = await _item_row(db, item_id)
        assert item["auto_queue_suppressed"] == 1
        assert item["suppressed_reason"] == "deleted_local"
    finally:
        await q.stop()


async def test_existing_stopped_item_delete_path_is_unchanged(db, tmp_path):
    """Regression check (task's own risk list): an item that was *already* `STOPPED` (no active
    job -- `stop_item()` is a no-op here) must delete exactly as it always has. This is the same
    scenario `tests/test_delete_api.py::test_delete_item_calls_stop_item_before_deleting_even_with_no_active_job`
    covers with a fake queue; this one runs it through the real `TransferQueue` to confirm a real
    no-op stop behaves identically.
    """
    host_id = await _make_host(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir)
    item_id = await _make_item(db, queue_id, "loose-notes.txt", is_dir=False, remote_size=512)
    write_if_needed(str(local_dir))

    q = await _queue_for(db, tmp_path, max_bandwidth_bps=10_000_000)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def downloaded():
            row = await _job_row(db, job_id)
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(downloaded, timeout_s=20)
        target = local_dir / "loose-notes.txt"
        assert target.exists()

        result = await jobs.delete_item(item_id, _FakeRequest(db, queue=q))
        assert result.deleted is True
        assert not target.exists()

        item = await _item_row(db, item_id)
        # A surviving remote copy (`remote_size=512` above) -- REMOVED_LOCAL, not REMOVED_BOTH.
        assert item["state"] == "REMOVED_LOCAL"
        assert item["suppressed_reason"] == "deleted_local"
    finally:
        await q.stop()
