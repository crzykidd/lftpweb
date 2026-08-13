"""The settle gate's own reproduction (prompts/open-issues.md "2 -- the settle gate";
`prompts/2026-08-12-settle-gate.md`) against the **real fake seedbox** (DESIGN.md §14) -- real
ssh, real sftp, a real `Engine.scan_queue` pass end to end (remote scan, reconcile, persist
with the settle gate wired in, `AutoQueue.on_scan`). Skipped automatically if the seedbox
isn't reachable (`docker compose -f docker-compose.test.yml up --build -d` first).

This file exists because the bug the settle gate fixes is specifically about *time* -- a
remote item observed across more than one scan -- which nothing that calls `reconcile()` or
`core/engine.py._persist` a single time can reproduce. Each test below drives at least two
real scans against real, freshly-uploaded remote content, growing it (or not) between them,
exactly the way an uploader would.

Deliberately does **not** reuse `docker/test-seedbox/seed_tree.sh`'s baked-in fixture -- these
tests write their own throwaway content into a uniquely-named remote subdirectory per test
(the same convention `test_postprocess_e2e.py` uses), so a run can never collide with any
other test's fixture data on the same shared container.
"""

from __future__ import annotations

import asyncio
import socket
from uuid import uuid4

import aiosqlite
import pytest

from lftpweb.core.autoqueue import AutoQueue
from lftpweb.core.engine import Engine, QueueConfig
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.remote import HostConfig, RemoteConnectionPool
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


class _Recorder:
    """A stand-in for `TransferQueue.enqueue_item` -- records what *would* have been queued,
    without spawning any lftp process. `AutoQueue`'s whole job is deciding whether to call
    this, not what it does once called.
    """

    def __init__(self) -> None:
        self.enqueued: list[int] = []

    async def __call__(self, item_id: int) -> int:
        self.enqueued.append(item_id)
        return item_id


async def _item_row(db, queue_id: int, rel_path: str):
    cursor = await db.execute(
        "SELECT state, substate FROM item WHERE queue_id = ? AND rel_path = ?",
        (queue_id, rel_path),
    )
    return await cursor.fetchone()


async def test_growing_remote_file_is_not_queued_until_it_settles(db, tmp_path):
    """Reproduction 1 of 2, per this task's handoff prompt: a single remote file, written in
    chunks with a real scan between each write. The settle gate's *eligibility* half
    (`core/autoqueue.py`) must not queue it while it's still visibly growing, and must queue
    it exactly once it stops.
    """
    remote_subdir = f"settle-file-{uuid4().hex[:12]}"
    remote_root = f"/data/pickup/{remote_subdir}"
    rel_path = "growing.bin"

    upload_pool = RemoteConnectionPool(tmp_path / "known_hosts_upload")
    host = _host_config()
    conn = await upload_pool.get_connection(host)
    try:
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(remote_root, exist_ok=True)

        async def _write(content: bytes) -> None:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(f"{remote_root}/{rel_path}", "wb") as f:
                    await f.write(content)
            await asyncio.sleep(1.1)  # force a real, distinct mtime for the next write

        await save_settle_settings(db, SettleSettings(enabled=True))

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        write_if_needed(str(local_dir))

        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
            "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
            (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
        )
        host_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
            "sync_mode, auto_queue_enabled) VALUES (?, 'e2e-settle', ?, ?, 1, 'copy', 1)",
            (host_id, remote_root, str(local_dir)),
        )
        queue_id = cursor.lastrowid
        await db.commit()

        recorder = _Recorder()
        engine = Engine(
            db=db,
            config_dir=str(tmp_path),
            events=EventBus(),
            autoqueue=AutoQueue(db, enqueue_item=recorder),
        )
        qcfg = QueueConfig(
            id=queue_id,
            host_id=host_id,
            name="e2e-settle",
            remote_path=remote_root,
            local_path=str(local_dir),
            staging_path=None,
            enabled=True,
            sync_mode="copy",
            auto_queue_enabled=True,
            auto_queue_patterns_only=False,
        )

        # Scan 1: first sighting of a 1000-byte file. Never settled on a first sighting.
        await _write(b"a" * 1000)
        await engine.scan_queue(qcfg, host)
        assert recorder.enqueued == [], "must not queue on first sighting"
        row = await _item_row(db, queue_id, rel_path)
        assert row["state"] == "REMOTE_ONLY"
        assert row["substate"] == "settling"

        # Scan 2: the file grew -- the fingerprint changed, so the counter must reset, not
        # advance. Still must not be queued.
        await _write(b"a" * 1500)
        await engine.scan_queue(qcfg, host)
        assert recorder.enqueued == [], "must not queue while still growing"
        row = await _item_row(db, queue_id, rel_path)
        assert row["state"] == "REMOTE_ONLY"
        assert row["substate"] == "settling"

        # Scan 3: unchanged since scan 2 -- two consecutive matching scans -- settled.
        await engine.scan_queue(qcfg, host)
        assert recorder.enqueued != [], "must queue once the fingerprint has held for 2 scans"
        assert len(recorder.enqueued) == 1
    finally:
        await upload_pool.close()


async def test_growing_remote_file_does_not_read_downloaded_until_settled(db, tmp_path):
    """Same growing file, but this time local content is written to match the remote's
    current size after every scan -- the "single file self-heals" scenario from
    `prompts/open-issues.md` #2. Before the settle gate, each snapshot alone looks complete
    (`local_size >= remote_size`); this proves the item is held at REMOTE_ONLY/settling rather
    than DOWNLOADED for as long as the remote keeps changing, and only reads DOWNLOADED once
    it genuinely stops.
    """
    remote_subdir = f"settle-file2-{uuid4().hex[:12]}"
    remote_root = f"/data/pickup/{remote_subdir}"
    rel_path = "growing.bin"

    upload_pool = RemoteConnectionPool(tmp_path / "known_hosts_upload2")
    host = _host_config()
    conn = await upload_pool.get_connection(host)
    try:
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(remote_root, exist_ok=True)

        async def _write_remote(content: bytes) -> None:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(f"{remote_root}/{rel_path}", "wb") as f:
                    await f.write(content)
            await asyncio.sleep(1.1)

        await save_settle_settings(db, SettleSettings(enabled=True))

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        write_if_needed(str(local_dir))
        local_file = local_dir / rel_path

        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
            "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
            (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
        )
        host_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
            "sync_mode) VALUES (?, 'e2e-settle2', ?, ?, 1, 'copy')",
            (host_id, remote_root, str(local_dir)),
        )
        queue_id = cursor.lastrowid
        await db.commit()

        engine = Engine(db=db, config_dir=str(tmp_path), events=EventBus())
        qcfg = QueueConfig(
            id=queue_id,
            host_id=host_id,
            name="e2e-settle2",
            remote_path=remote_root,
            local_path=str(local_dir),
            staging_path=None,
            enabled=True,
            sync_mode="copy",
        )

        # Scan 1: remote and local both 1000 bytes -- looks complete, but it's a first
        # sighting, so it must not be published DOWNLOADED.
        await _write_remote(b"a" * 1000)
        local_file.write_bytes(b"a" * 1000)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, rel_path)
        assert row["state"] == "REMOTE_ONLY", "a complete-looking first sighting must be held back"
        assert row["substate"] == "settling"

        # Scan 2: remote grows to 1500, local hasn't caught up yet -- genuinely PARTIAL, and
        # the settle gate has no reason to touch a PARTIAL reading.
        await _write_remote(b"a" * 1500)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, rel_path)
        assert row["state"] == "PARTIAL"

        # Local catches up to the new size; remote is unchanged since scan 2 -- settled.
        local_file.write_bytes(b"a" * 1500)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, rel_path)
        assert row["state"] == "DOWNLOADED"
        assert row["substate"] is None
    finally:
        await upload_pool.close()


async def test_directory_gaining_files_does_not_read_downloaded_off_the_partial_set(db, tmp_path):
    """Reproduction 2 of 2: the directory case from `prompts/open-issues.md` #2, the one that
    doesn't self-heal without this gate. A release directory gains a second file between
    scans; the first file, on its own, is already whole. Before this fix, the rollup in
    `core/reconcile.py` reads the directory as DOWNLOADED off the one-file partial set -- this
    test proves it no longer does, and that it correctly reaches DOWNLOADED once the directory
    genuinely stops changing.
    """
    remote_subdir = f"settle-dir-{uuid4().hex[:12]}"
    remote_root = f"/data/pickup/{remote_subdir}"
    release = "Release.Name"

    upload_pool = RemoteConnectionPool(tmp_path / "known_hosts_upload3")
    host = _host_config()
    conn = await upload_pool.get_connection(host)
    try:
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(f"{remote_root}/{release}", exist_ok=True)

        async def _write_remote(name: str, content: bytes) -> None:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(f"{remote_root}/{release}/{name}", "wb") as f:
                    await f.write(content)
            await asyncio.sleep(1.1)

        await save_settle_settings(db, SettleSettings(enabled=True))

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        write_if_needed(str(local_dir))
        local_release = local_dir / release
        local_release.mkdir()

        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
            "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
            (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
        )
        host_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
            "sync_mode) VALUES (?, 'e2e-settle3', ?, ?, 1, 'copy')",
            (host_id, remote_root, str(local_dir)),
        )
        queue_id = cursor.lastrowid
        await db.commit()

        engine = Engine(db=db, config_dir=str(tmp_path), events=EventBus())
        qcfg = QueueConfig(
            id=queue_id,
            host_id=host_id,
            name="e2e-settle3",
            remote_path=remote_root,
            local_path=str(local_dir),
            staging_path=None,
            enabled=True,
            sync_mode="copy",
        )

        # Scan 1: only file1 has arrived on the remote, and (as if lftpweb had already
        # transferred it on a previous pass) it's already fully present locally too. Byte
        # comparison alone says DOWNLOADED -- exactly the bug. Must read REMOTE_ONLY/settling.
        await _write_remote("file1.txt", b"a" * 100)
        (local_release / "file1.txt").write_bytes(b"a" * 100)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, release)
        assert row["state"] == "REMOTE_ONLY", "a partial set must not read DOWNLOADED"
        assert row["substate"] == "settling"

        # Scan 2: a second file lands remotely -- the release is still being uploaded. The
        # fingerprint changed (file_count 1 -> 2), so this must not count as a confirming
        # match either way; and now the directory is genuinely PARTIAL (file2 not local yet).
        await _write_remote("file2.txt", b"b" * 50)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, release)
        assert row["state"] == "PARTIAL"

        # file2 catches up locally too; remote is unchanged since scan 2 -- settled, and now
        # genuinely complete.
        (local_release / "file2.txt").write_bytes(b"b" * 50)
        await engine.scan_queue(qcfg, host)
        row = await _item_row(db, queue_id, release)
        assert row["state"] == "DOWNLOADED"
        assert row["substate"] is None
    finally:
        await upload_pool.close()
