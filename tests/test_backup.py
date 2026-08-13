from __future__ import annotations

import sqlite3
import time

import pytest

from lftpweb.core.backup import (
    BackupScheduler,
    BackupSettings,
    backup_dir,
    backup_file_path,
    create_backup,
    list_backups,
    load_backup_settings,
    prune_backups,
    save_backup_settings,
)
from lftpweb.core.crypto import encrypt_secret, ensure_install_secret
from lftpweb.db import connect, migrate


async def _fresh_db(config_dir: str):
    conn = await connect(config_dir)
    await migrate(conn)
    return conn


async def test_create_backup_produces_a_valid_openable_database(tmp_path):
    """DESIGN.md §10.2: VACUUM INTO, never a file copy. Prove it by opening the *backup*
    file with an independent connection and querying real data out of it -- not just
    stat()ing that a file landed on disk.
    """
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    await conn.execute(
        "INSERT INTO host (name, address, username, auth_method) VALUES (?, ?, ?, ?)",
        ("seedbox", "1.2.3.4", "user", "key"),
    )
    await conn.commit()

    info = await create_backup(conn, config_dir, reason="test")
    backup_path = backup_dir(config_dir) / info.filename
    assert backup_path.is_file()
    assert info.size_bytes == backup_path.stat().st_size

    # Independent connection -- not aiosqlite, not the same handle -- proves this is a real,
    # standalone SQLite file and not e.g. a WAL side file or a torn copy.
    raw_conn = sqlite3.connect(str(backup_path))
    try:
        rows = raw_conn.execute("SELECT name, address FROM host").fetchall()
        assert rows == [("seedbox", "1.2.3.4")]
        tables = {
            r[0] for r in raw_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "item" in tables and "job" in tables
    finally:
        raw_conn.close()
    await conn.close()


async def test_create_backup_survives_a_writer_holding_an_open_transaction(tmp_path):
    """Regression test for the race that shipped broken in `:dev` from `fe80aaf`
    (docs/decisions.md, DESIGN.md §10.2): `VACUUM` cannot run on a connection anyone else has
    a transaction open on, and every writer holds one between its own `execute` and `commit`
    -- so a backup landing in that window died with `sqlite3.OperationalError: cannot VACUUM
    from within a transaction`. CI caught it because it's timing-dependent in practice; this
    reproduces it deterministically by holding the transaction open on purpose instead of
    hoping a background writer collides with a backup mid-run.
    """
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    await conn.execute(
        "INSERT INTO host (name, address, username, auth_method) VALUES (?, ?, ?, ?)",
        ("seedbox", "1.2.3.4", "user", "key"),
    )
    assert conn.in_transaction is True  # the condition that makes VACUUM raise on `conn`

    info = await create_backup(conn, config_dir, reason="test")
    backup_path = backup_dir(config_dir) / info.filename
    assert backup_path.is_file()

    # The backup must reflect this connection's committed state, not the uncommitted INSERT
    # above -- proving the fix is real isolation (a dedicated connection) rather than some
    # workaround that happens to let VACUUM run without actually respecting transaction
    # boundaries.
    raw_conn = sqlite3.connect(str(backup_path))
    try:
        rows = raw_conn.execute("SELECT name FROM host").fetchall()
        assert rows == []  # the INSERT above was never committed
    finally:
        raw_conn.close()

    await conn.commit()
    await conn.close()


async def test_backup_taken_during_wal_writes_is_still_consistent(tmp_path):
    """The whole reason DESIGN.md specifies VACUUM INTO over a file copy: WAL-safe. Write
    inside an open transaction's worth of pending WAL frames, then back up without an
    intervening checkpoint, and confirm the backup reflects a consistent, complete state.
    """
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    for i in range(50):
        await conn.execute(
            "INSERT INTO host (name, address, username, auth_method) VALUES (?, ?, ?, ?)",
            (f"host{i}", "1.2.3.4", "user", "key"),
        )
    await conn.commit()

    info = await create_backup(conn, config_dir, reason="test")
    raw_conn = sqlite3.connect(str(backup_dir(config_dir) / info.filename))
    try:
        (count,) = raw_conn.execute("SELECT COUNT(*) FROM host").fetchone()
        assert count == 50
    finally:
        raw_conn.close()
    await conn.close()


async def test_backup_never_contains_the_encryption_secret(tmp_path):
    """DESIGN.md §8/§10.2: the encryption secret must never end up in a backup. Assert it
    directly rather than assume VACUUM INTO can't reach a file outside the database: byte-
    search the backup for the raw secret, and confirm secret.key never lands under
    <config>/backups/ in the first place.
    """
    config_dir = str(tmp_path)
    secret = ensure_install_secret(config_dir)
    assert len(secret) == 32

    conn = await _fresh_db(config_dir)
    encrypted = encrypt_secret(config_dir, "hunter2-seedbox-password")
    await conn.execute(
        "INSERT INTO host (name, address, username, auth_method, password_enc) "
        "VALUES (?, ?, ?, ?, ?)",
        ("seedbox", "1.2.3.4", "user", "password", encrypted),
    )
    await conn.commit()

    info = await create_backup(conn, config_dir, reason="test")
    backup_path = backup_dir(config_dir) / info.filename
    raw_bytes = backup_path.read_bytes()

    assert secret not in raw_bytes
    assert not (backup_dir(config_dir) / "secret.key").exists()
    # The backup does contain the *ciphertext* -- confirms this test would actually catch a
    # regression rather than passing vacuously because nothing sensitive was ever stored.
    assert encrypted.encode("ascii") in raw_bytes
    await conn.close()


async def test_retention_prunes_oldest_first_to_keep_count(tmp_path):
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    filenames = []
    for _ in range(5):
        info = await create_backup(conn, config_dir, reason="test")
        filenames.append(info.filename)
        time.sleep(1.05)  # distinct YYYYMMDD-HHMMSS timestamps, oldest-first is unambiguous

    removed = await prune_backups(config_dir, keep=2)
    assert removed == filenames[:3]  # the three oldest

    remaining = {b.filename for b in await list_backups(config_dir)}
    assert remaining == set(filenames[3:])
    await conn.close()


async def test_list_backups_sorted_newest_first(tmp_path):
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    filenames = []
    for _ in range(3):
        info = await create_backup(conn, config_dir, reason="test")
        filenames.append(info.filename)
        time.sleep(1.05)

    infos = await list_backups(config_dir)
    assert [b.filename for b in infos] == list(reversed(filenames))
    await conn.close()


async def test_backup_settings_default_daily_keep_7(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    settings = await load_backup_settings(conn)
    assert settings.interval_days == 1.0
    assert settings.keep_count == 7
    await conn.close()


async def test_backup_settings_round_trip(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await save_backup_settings(conn, BackupSettings(interval_days=3.0, keep_count=14))
    loaded = await load_backup_settings(conn)
    assert loaded.interval_days == 3.0
    assert loaded.keep_count == 14
    await conn.close()


async def test_backup_file_path_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        backup_file_path(str(tmp_path), "../../etc/passwd")
    with pytest.raises(ValueError):
        backup_file_path(str(tmp_path), "not-a-backup.db")
    # A well-formed name is accepted even if the file doesn't exist yet -- existence is the
    # caller's problem (a 404), not this function's.
    resolved = backup_file_path(str(tmp_path), "lftpweb-20260101-000000.db")
    assert resolved.name == "lftpweb-20260101-000000.db"


async def test_scheduler_run_if_due_takes_and_prunes(tmp_path):
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    await save_backup_settings(conn, BackupSettings(interval_days=1.0, keep_count=1))
    scheduler = BackupScheduler(db=conn, config_dir=config_dir)

    first = await scheduler.run_if_due()
    assert first is not None
    assert len(await list_backups(config_dir)) == 1

    # Immediately due again? No -- interval_days=1.0 hasn't elapsed since the one just taken.
    second = await scheduler.run_if_due()
    assert second is None
    assert len(await list_backups(config_dir)) == 1
    await conn.close()


async def test_scheduler_start_stop_and_is_alive(tmp_path):
    config_dir = str(tmp_path)
    conn = await _fresh_db(config_dir)
    scheduler = BackupScheduler(db=conn, config_dir=config_dir)
    assert scheduler.is_alive is False
    await scheduler.start()
    assert scheduler.is_alive is True
    await scheduler.stop()
    assert scheduler.is_alive is False
    await conn.close()
