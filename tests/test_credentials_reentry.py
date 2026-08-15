"""DESIGN.md §8's "credentials need re-entry" state, finished in phase 8 -- unit-level,
never touching the fake seedbox, because the whole point of these three code paths is that
they must act *without* attempting a network connection at all.

- `core/engine.py.load_host_config` flags a host whose stored password fails to decrypt with
  the current install secret (the restore-to-a-fresh-install case, §10.2).
- `core/engine.py.Engine.scan_queue` fails that queue's scan cleanly, with a stable message,
  without ever calling `RemoteConnectionPool.scan` (i.e. without opening an SSH connection).
- `core/queue.py.TransferQueue._admit` holds every decision the scheduler would otherwise
  spawn, rather than spawning lftp with no password and letting it fail `AUTH_FAILED`.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.engine import Engine, QueueConfig, load_host_config
from lftpweb.core.events import EventBus
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig
from lftpweb.db import migrate


async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db


@pytest.fixture
async def db():
    conn = await _make_db()
    yield conn
    await conn.close()


async def _insert_password_host(db: aiosqlite.Connection, config_dir: str) -> int:
    password_enc = encrypt_secret(config_dir, "hunter2")
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', 'example.invalid', 22, 'seeduser', "
        "'password', ?, 'strict')",
        (password_enc,),
    )
    await db.commit()
    return cursor.lastrowid


async def test_load_host_config_flags_credentials_need_reentry_after_restore(tmp_path, db):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    await _insert_password_host(db, str(dir_a))

    # Same install: decrypts fine, nothing flagged.
    host = await load_host_config(db, str(dir_a))
    assert host is not None
    assert host.password == "hunter2"
    assert host.credentials_need_reentry is False

    # A restore onto a fresh install (DESIGN.md §10.2: the encryption key is deliberately
    # excluded from backups) -- a different secret.key, so the ciphertext no longer decrypts.
    host_b = await load_host_config(db, str(dir_b))
    assert host_b is not None
    assert host_b.password is None
    assert host_b.credentials_need_reentry is True


async def test_load_host_config_flags_credentials_need_reentry_for_a_pasted_key_after_restore(
    tmp_path, db
):
    """migration 014, DESIGN.md §8: a pasted key that fails to decrypt must ride the exact same
    `credentials_need_reentry` flag a password does -- not a parallel one -- so `_admit` and
    `scan_queue` (already generic over the flag, proven by the two tests below) hold/skip
    cleanly for this case too without any changes of their own.
    """
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    ssh_key_enc = encrypt_secret(
        str(dir_a), "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, ssh_key_enc, "
        "known_hosts_policy) VALUES ('seedbox', 'example.invalid', 22, 'seeduser', 'key', "
        "?, 'strict')",
        (ssh_key_enc,),
    )
    await db.commit()

    # Same install: decrypts fine.
    host = await load_host_config(db, str(dir_a))
    assert host is not None
    assert (
        host.ssh_key
        == "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    assert host.credentials_need_reentry is False

    # A restore onto a fresh install -- different secret.key, ciphertext no longer decrypts.
    host_b = await load_host_config(db, str(dir_b))
    assert host_b is not None
    assert host_b.ssh_key is None
    assert host_b.credentials_need_reentry is True


async def test_scan_queue_holds_cleanly_without_attempting_a_connection(tmp_path, db):
    engine = Engine(db=db, config_dir=str(tmp_path), events=EventBus())
    q = QueueConfig(
        id=1,
        host_id=1,
        name="q",
        remote_path="/remote",
        local_path=str(tmp_path / "local"),
        staging_path=None,
        enabled=True,
        sync_mode="copy",
    )
    host = HostConfig(
        id=1,
        address="example.invalid",
        port=22,
        username="seeduser",
        auth_method="password",
        password=None,
        credentials_need_reentry=True,
    )

    await engine.scan_queue(q, host)

    assert engine.scan_errors[1] is not None
    assert "credentials need re-entry" in engine.scan_errors[1]
    # The pool must never have opened a connection -- the whole point is skipping the
    # attempt, not failing after making it (DESIGN.md §8: "rather than crashing or retrying").
    assert engine.pool.is_connected is False


async def test_admit_holds_every_decision_when_host_credentials_need_reentry(tmp_path, db):
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', 'example.invalid', 22, 'seeduser', 'password', 'strict')"
    )
    await db.commit()
    host_id = cursor.lastrowid

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(tmp_path / "local")),
    )
    await db.commit()
    queue_id = cursor.lastrowid

    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'release', 1, 100, 0, 'REMOTE_ONLY')",
        (queue_id,),
    )
    await db.commit()
    item_id = cursor.lastrowid

    needs_reentry_host = HostConfig(
        id=host_id,
        address="example.invalid",
        port=22,
        username="seeduser",
        auth_method="password",
        password=None,
        credentials_need_reentry=True,
    )

    async def host_provider():
        return needs_reentry_host

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

    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=0.1,
        host_provider=host_provider,
    )
    job_id = await q.enqueue_item(item_id)
    assert q.credentials_need_reentry is False

    await q._admit()

    assert q.credentials_need_reentry is True
    cursor = await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    # Never spawned -- no PID, still `queued`, and critically never `failed` with
    # AUTH_FAILED, which is the exact "wave of AUTH_FAILED jobs" DESIGN.md §8 forbids.
    assert row["state"] == "queued"
    assert row["pid"] is None
