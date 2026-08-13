from __future__ import annotations

import pytest

import lftpweb.db as db_module
from lftpweb.db import connect, is_healthy, migrate

EXPECTED_TABLES = {
    "setting",
    "host",
    "path_queue",
    "pattern",
    "item",
    "job",
    "event",
    "schema_version",
}


async def test_migrate_empty_db_to_head_creates_all_tables(tmp_path):
    conn = await connect(str(tmp_path))
    try:
        await migrate(conn)
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in await cursor.fetchall()}
        assert EXPECTED_TABLES.issubset(tables)
        assert await is_healthy(conn)
    finally:
        await conn.close()


async def test_migrate_is_idempotent(tmp_path):
    conn = await connect(str(tmp_path))
    try:
        await migrate(conn)
        await migrate(conn)  # must not raise (e.g. "table already exists")

        cursor = await conn.execute("SELECT COUNT(*) FROM schema_version")
        (count,) = await cursor.fetchone()
        # Every migration file recorded exactly once -- not hardcoded to "1", since that
        # made this test fail the instant migration 002 (phase 4) was added even though
        # idempotency itself was never in question. See db_module.MIGRATIONS_DIR.
        expected = len(list(db_module.MIGRATIONS_DIR.glob("*.sql")))
        assert count == expected
    finally:
        await conn.close()


async def test_failed_migration_is_rolled_back_entirely(tmp_path, monkeypatch):
    """A migration that fails partway must leave the database at the previous version.

    Without an explicit transaction around each migration, sqlite3's executescript()
    commits statement-by-statement: the statements before the failure would persist while
    schema_version stayed un-updated, so the next start would re-run the migration from the
    top, hit "table already exists", and wedge the install with no way forward but manual
    SQL. This asserts the all-or-nothing behaviour that prevents that.
    """
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "001_ok.sql").write_text("CREATE TABLE alpha (x INTEGER);")
    (bad / "002_broken.sql").write_text(
        "CREATE TABLE beta (y INTEGER);\n"
        "CREATE TABLE alpha (z INTEGER);\n"  # fails: alpha already exists
    )
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", bad)

    conn = await connect(str(tmp_path))
    try:
        with pytest.raises(Exception):
            await migrate(conn)

        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in await cursor.fetchall()}
        assert "alpha" in tables, "migration 001 committed, as it should have"
        assert "beta" not in tables, "migration 002 partially applied — it must roll back"

        cursor = await conn.execute("SELECT version FROM schema_version")
        assert {row[0] for row in await cursor.fetchall()} == {1}

        # The real point: after the failure, re-running still works once 002 is fixed,
        # rather than being permanently stuck on "table beta already exists".
        (bad / "002_broken.sql").write_text("CREATE TABLE beta (y INTEGER);")
        await migrate(conn)
        cursor = await conn.execute("SELECT version FROM schema_version")
        assert {row[0] for row in await cursor.fetchall()} == {1, 2}
    finally:
        await conn.close()


async def test_migrate_takes_a_pre_migration_backup_containing_the_prior_schema(
    tmp_path, monkeypatch
):
    """The pre-migration backup, exercised for real (not mocked): create a database at
    migration N, add a migration N+1, run migrate() again with config_dir wired in, and
    confirm the backup file both exists and opens -- with the schema as it stood *before*
    the new migration, proving it was taken before, not after.
    """
    import sqlite3

    from lftpweb.core.backup import backup_dir

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_initial.sql").write_text("CREATE TABLE widget (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", migrations)

    config_dir = str(tmp_path)
    conn = await connect(config_dir)
    try:
        await migrate(
            conn, config_dir
        )  # database created at migration 1 (its own backup, ignored below)
        backups_after_first = set(backup_dir(config_dir).glob("*.db"))

        (migrations / "002_add_gadget.sql").write_text(
            "CREATE TABLE gadget (id INTEGER PRIMARY KEY);"
        )
        await migrate(conn, config_dir)  # database at migration 1 -> 2, backup fires first

        backups = set(backup_dir(config_dir).glob("*.db"))
        new_backups = backups - backups_after_first
        assert len(new_backups) == 1

        raw = sqlite3.connect(str(next(iter(new_backups))))
        try:
            tables = {
                r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "widget" in tables
            assert "gadget" not in tables  # the pre-migration state, not the post-migration one

            versions = {r[0] for r in raw.execute("SELECT version FROM schema_version")}
            assert versions == {1}
        finally:
            raw.close()

        # And the live database did actually move on to migration 2.
        cursor = await conn.execute("SELECT version FROM schema_version")
        assert {row[0] for row in await cursor.fetchall()} == {1, 2}
    finally:
        await conn.close()


async def test_migrate_without_config_dir_takes_no_backup(tmp_path, monkeypatch):
    """Every pre-phase-7 caller (and this module's own tests above) calls migrate(conn) with
    no config_dir -- must keep working exactly as before, with no backup attempted.
    """
    from lftpweb.core.backup import backup_dir

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_initial.sql").write_text("CREATE TABLE widget (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", migrations)

    conn = await connect(str(tmp_path))
    try:
        await migrate(conn)  # no config_dir
        assert not backup_dir(str(tmp_path)).exists()
    finally:
        await conn.close()


async def test_wal_and_foreign_keys_enabled(tmp_path):
    conn = await connect(str(tmp_path))
    try:
        cursor = await conn.execute("PRAGMA journal_mode")
        (mode,) = await cursor.fetchone()
        assert mode.lower() == "wal"

        cursor = await conn.execute("PRAGMA foreign_keys")
        (fk,) = await cursor.fetchone()
        assert fk == 1
    finally:
        await conn.close()


async def test_busy_timeout_set_on_shared_connection(tmp_path):
    """`connect()` must actually set `busy_timeout` on the connection it hands back -- not
    merely execute the pragma line and lose it. 30000ms, matching
    `core/backup.py.create_backup`'s dedicated VACUUM connection (docs/decisions.md).
    """
    conn = await connect(str(tmp_path))
    try:
        cursor = await conn.execute("PRAGMA busy_timeout")
        (timeout_ms,) = await cursor.fetchone()
        await cursor.close()
        assert timeout_ms == 30000
    finally:
        await conn.close()
