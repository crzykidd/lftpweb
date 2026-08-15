"""End-to-end proof that a *pasted* SSH private key (migration 014, DESIGN.md §8) is usable by
both credential paths this project has: asyncssh scanning (decrypted straight into memory, no
file ever written) and lftp transfers (materialised to a per-job file on the `/run` tmpfs, then
unlinked with the job's other credential files). Real ssh, real sftp, real lftp, against the
fake seedbox's own key-auth identity (`docker/test-seedbox/test_key` / `test_key.pub`, already
baked into its `authorized_keys` at image build time) -- skipped automatically if the seedbox
isn't reachable, the same convention as tests/test_queue.py.

Unit-level coverage of the pure functions (`core/remote.py._resolve_client_keys`,
`validate_private_key`, `core/lftp.py.spawn`'s file materialisation without a real connection)
lives in tests/test_remote.py and tests/test_lftp.py; this file is specifically the "does it
actually authenticate against a real sshd" proof neither of those can give on its own.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import aiosqlite
import pytest

from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.engine import load_host_config
from lftpweb.core.events import EventBus
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig, RemoteConnectionPool
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"

TEST_KEY_PATH = Path(__file__).resolve().parent.parent / "docker" / "test-seedbox" / "test_key"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- "
    "`docker compose -f docker-compose.test.yml up --build -d`",
)


def test_fake_seedbox_test_key_is_present_and_readable():
    # Guards the rest of this file against the fixture itself having gone missing or been
    # renamed -- every other test here silently no-ops (via the module-level skipif) if the
    # seedbox isn't reachable, but a missing key file alongside a *reachable* seedbox should
    # fail loudly rather than skip.
    assert TEST_KEY_PATH.exists(), f"expected fake seedbox test key at {TEST_KEY_PATH}"
    assert "PRIVATE KEY" in TEST_KEY_PATH.read_text()


async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def test_pasted_key_round_trips_through_encryption_and_decrypts_correctly(tmp_path):
    """Paste -> encrypted at rest -> decrypted. Proves the ciphertext in the `host` row is not
    the plaintext, and that `load_host_config` hands the real key back out unchanged.
    """
    key_text = TEST_KEY_PATH.read_text()

    db = await _make_db()
    try:
        ssh_key_enc = encrypt_secret(str(tmp_path), key_text)
        assert ssh_key_enc != key_text
        assert key_text not in ssh_key_enc  # the ciphertext genuinely doesn't contain it

        await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, ssh_key_enc, "
            "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'key', ?, 'insecure')",
            (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER, ssh_key_enc),
        )
        await db.commit()

        host = await load_host_config(db, str(tmp_path))
        assert host is not None
        assert host.ssh_key == key_text
        assert host.credentials_need_reentry is False

        # And the row itself never held plaintext -- only what was passed in.
        cursor = await db.execute("SELECT ssh_key_enc FROM host WHERE id = 1")
        row = await cursor.fetchone()
        assert key_text not in row["ssh_key_enc"]
    finally:
        await db.close()


async def test_pasted_key_scans_the_real_seedbox_over_asyncssh_without_a_file(tmp_path):
    """The asyncssh path: `RemoteConnectionPool.scan` against the real fake-seedbox sshd,
    authenticating with a key decrypted straight into memory -- never written to `key_path` or
    anywhere else on disk.
    """
    key_text = TEST_KEY_PATH.read_text()
    host = HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="key",
        ssh_key=key_text,
        known_hosts_policy="insecure",
    )
    known_hosts_dir = tmp_path / "known_hosts"
    known_hosts_dir.mkdir()
    pool = RemoteConnectionPool(known_hosts_dir)
    try:
        entries, _warning = await pool.scan(host, "/data/pickup")
        assert entries  # the seeded tree is non-empty -- a real, successful key-auth scan
        assert any(e.rel_path == "loose-notes.txt" for e in entries.values())
    finally:
        await pool.close()

    # Nothing under this test's own tmp_path (standing in for `/config`'s known_hosts store)
    # ever received the key material -- the whole point of the in-memory path.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert key_text not in path.read_text(errors="ignore")


async def test_pasted_key_transfers_a_real_file_via_lftp(tmp_path):
    """The lftp path: a real `TransferQueue` job, spawned with a `HostConfig` carrying only a
    pasted key (no `key_path`) -- `core/queue.py._spawn_decision` -> `core/lftp.py.spawn`
    materialises it to a per-job file on tmpfs (here, `tmp_path/run`, standing in for `/run`),
    `ssh -i <that file>` authenticates for real, and the file is gone again once the job ends.
    """
    key_text = TEST_KEY_PATH.read_text()
    db = await _make_db()
    await save_settle_settings(db, SettleSettings(enabled=False))
    try:
        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
            "VALUES ('seedbox', ?, ?, ?, 'key', 'insecure')",
            (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
        )
        await db.commit()
        host_id = cursor.lastrowid

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
            "sync_mode) VALUES (?, 'test', '/data/pickup', ?, 1, 'copy')",
            (host_id, str(local_dir)),
        )
        await db.commit()
        queue_id = cursor.lastrowid

        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, 'loose-notes.txt', 0, 512, 0, 'REMOTE_ONLY')",
            (queue_id,),
        )
        await db.commit()
        item_id = cursor.lastrowid

        await save_transfer_settings(
            db,
            TransferSettings(
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

        run_dir = tmp_path / "run"

        host = HostConfig(
            id=host_id,
            address=SEEDBOX_HOST,
            port=SEEDBOX_PORT,
            username=SEEDBOX_USER,
            auth_method="key",
            ssh_key=key_text,
            known_hosts_policy="insecure",
        )

        async def host_provider():
            return host

        q = TransferQueue(
            db=db,
            config_dir=str(tmp_path),
            events=EventBus(),
            run_dir=str(run_dir),
            tick_s=0.2,
            host_provider=host_provider,
        )
        await q.start()
        try:
            job_id = await q.enqueue_item(item_id)

            async def done():
                row = await (
                    await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
                ).fetchone()
                return row is not None and row["state"] in ("succeeded", "failed")

            assert await _wait_until(done, timeout_s=20)

            row = await (
                await db.execute(
                    "SELECT state, error_class, output_tail FROM job WHERE id = ?", (job_id,)
                )
            ).fetchone()
            assert row["state"] == "succeeded", (row["error_class"], row["output_tail"])

            target = local_dir / "loose-notes.txt"
            assert target.exists()
            assert target.stat().st_size == 512
        finally:
            await q.stop()

        # The per-job key file must be gone once the job has finished -- cleanup() unlinked it,
        # and nothing else in run_dir still carries the plaintext key.
        if run_dir.exists():
            for path in run_dir.rglob("*"):
                if path.is_file():
                    assert key_text not in path.read_text(errors="ignore")
    finally:
        await db.close()
