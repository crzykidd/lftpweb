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


async def test_migration_015_sets_existing_queues_to_inherit_without_losing_children(
    tmp_path, monkeypatch
):
    """Migration 015 (`prompts/2026-08-13-postprocess-inherit-or-override.md`) makes the four
    `path_queue` post-processing columns nullable and sets every existing row's value to
    `NULL` (inherit) -- the user explicitly chose not to preserve pre-migration *effective*
    values once nothing had shipped yet (docs/decisions.md), so this is deliberately not a
    behaviour-preserving migration.

    It's also a table rebuild, and `path_queue` is the parent of `item`/`pattern` via
    `ON DELETE CASCADE` -- the regression test for the cascade-delete bug `db.py.migrate()`'s
    `PRAGMA foreign_keys` handling exists to prevent (see that function's own comment): without
    it, this migration's `DROP TABLE path_queue` would silently wipe every item and pattern in
    the database the moment it ran, on any install that already had real data.
    """
    import lftpweb.db as db_module

    real_migrations_dir = db_module.MIGRATIONS_DIR

    # A copy of the real migrations directory, minus 015 -- an actual pre-upgrade database,
    # not a synthetic one, so this exercises the real migration file.
    staged = tmp_path / "migrations"
    staged.mkdir()
    for path in sorted(real_migrations_dir.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 14:
            (staged / path.name).write_text(path.read_text())
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", staged)

    conn = await connect(str(tmp_path))
    try:
        await migrate(conn)

        await conn.execute(
            "INSERT INTO host (id, name, address, username, auth_method) "
            "VALUES (1, 'h', 'a', 'u', 'agent')"
        )
        await conn.execute(
            "INSERT INTO path_queue (id, host_id, name, remote_path, local_path, "
            "auto_verify, auto_extract, auto_move, auto_delete_archives) "
            "VALUES (1, 1, 'q', '/r', '/l', 1, 0, 1, 0)"
        )
        await conn.execute(
            "INSERT INTO item (id, queue_id, rel_path, is_dir, state) "
            "VALUES (1, 1, 'foo', 0, 'DOWNLOADED')"
        )
        await conn.execute(
            "INSERT INTO pattern (id, queue_id, kind, expr) VALUES (1, 1, 'select', '*.mkv')"
        )
        await conn.commit()

        # Now point at the real migrations directory (015 included) and migrate again.
        monkeypatch.setattr(db_module, "MIGRATIONS_DIR", real_migrations_dir)
        await migrate(conn)

        cursor = await conn.execute(
            "SELECT auto_verify, auto_extract, auto_move, auto_delete_archives "
            "FROM path_queue WHERE id = 1"
        )
        row = await cursor.fetchone()
        assert (
            row["auto_verify"],
            row["auto_extract"],
            row["auto_move"],
            row["auto_delete_archives"],
        ) == (
            None,
            None,
            None,
            None,
        )

        # The children of `path_queue` survived the rebuild -- not cascade-deleted.
        cursor = await conn.execute("SELECT COUNT(*) FROM item")
        assert (await cursor.fetchone())[0] == 1
        cursor = await conn.execute("SELECT COUNT(*) FROM pattern")
        assert (await cursor.fetchone())[0] == 1

        # The column genuinely accepts NULL / 0 / 1 now, and still rejects anything else.
        await conn.execute("UPDATE path_queue SET auto_verify = 1 WHERE id = 1")
        await conn.execute("UPDATE path_queue SET auto_verify = 0 WHERE id = 1")
        await conn.execute("UPDATE path_queue SET auto_verify = NULL WHERE id = 1")
        await conn.commit()
        with pytest.raises(Exception):
            await conn.execute("UPDATE path_queue SET auto_verify = 2 WHERE id = 1")
            await conn.commit()

        # `connect()`'s own invariant holds again once migrate() has fully returned.
        cursor = await conn.execute("PRAGMA foreign_keys")
        assert (await cursor.fetchone())[0] == 1
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
