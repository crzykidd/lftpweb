"""aiosqlite connection management and the hand-rolled migration runner.

Migrations are numbered SQL files in migrations/NNN_description.sql, applied in order and
tracked in a `schema_version` table. See docs/decisions.md (hand-rolled migrations, not
Alembic) for why: the schema is raw SQL with no ORM, and a future pre-migration backup
(DESIGN.md §10.2) hooks trivially into `migrate()` below once core/backup.py exists.

Rules for migration files:

- **No transaction control.** `migrate()` wraps each file in BEGIN/COMMIT itself; a `BEGIN`
  inside a file would nest and fail.
- **No `PRAGMA journal_mode`** or other pragmas that cannot run inside a transaction.
  Connection-level pragmas belong in `connect()`.
- **Foreign keys are OFF for the whole batch of pending migrations**, restored to ON once
  they've all applied (or the batch has failed) — see `migrate()`'s own comment. A
  table-rebuild migration (create the new shape, copy rows, `DROP TABLE` the old one, rename
  — SQLite's only way to widen a `CHECK` or drop `NOT NULL`) would otherwise cascade-delete
  every row of any child table referencing it `ON DELETE CASCADE`, because `DROP TABLE` with
  foreign keys enabled performs an implicit delete of the parent's rows first.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def db_path(config_dir: str) -> Path:
    return Path(config_dir) / "lftpweb.db"


async def connect(config_dir: str) -> aiosqlite.Connection:
    """Open the database with the pragmas this app requires on every connection.

    `busy_timeout` matters as much as `journal_mode`/`foreign_keys` now: this connection is
    shared by the engine's scan persist, the transfer queue's ~1 Hz tick, the metrics
    sampler's 30s heartbeat, and the post-processing pipeline, all writing concurrently. At
    SQLite's default `busy_timeout` of 0, any lock contention between them fails instantly
    with `SQLITE_BUSY` instead of waiting a bounded time for the other writer to finish. Set
    to 30000ms to match `core/backup.py.create_backup`'s dedicated VACUUM connection --
    one number, not two conventions.

    Every pragma cursor is closed explicitly rather than left for GC, per
    `core/backup.py`'s own documented trap: a PRAGMA returning a row whose cursor is never
    finalized leaves an unfinalized statement on the connection, which is enough to make
    `VACUUM` refuse with "cannot VACUUM - SQL statements in progress" -- and the
    pre-migration backup (`migrate()` below) runs against this very connection's database
    file from a second connection, so a lingering statement here is exactly the kind of bug
    that would only show up the next time someone adds a migration.
    """
    path = db_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    for pragma in (
        "PRAGMA journal_mode = WAL",
        "PRAGMA foreign_keys = ON",
        "PRAGMA busy_timeout = 30000",
    ):
        cursor = await conn.execute(pragma)
        await cursor.close()
    await conn.commit()
    return conn


def _discover_migrations() -> list[tuple[int, str, Path]]:
    """Parse migrations/NNN_description.sql into (version, name, path), sorted by version."""
    migrations = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version_str, _, rest = path.stem.partition("_")
        migrations.append((int(version_str), rest, path))
    migrations.sort(key=lambda m: m[0])
    return migrations


async def migrate(conn: aiosqlite.Connection, config_dir: str | None = None) -> None:
    """Apply every migration not yet recorded in schema_version, in order.

    Idempotent: re-running against a database already at head applies nothing, because
    each migration's version is checked against schema_version before it runs.

    `config_dir`, when given, is where a backup (DESIGN.md §10.2, `core/backup.py`) is taken
    *before* the first pending migration runs — the second net, not a replacement for the
    per-migration transaction/rollback above. `None` (every pre-phase-7 caller, and this
    module's own tests that pass a bare connection) means "don't back up" rather than
    guessing a path; `main.py` always passes the real one.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    await conn.commit()

    cursor = await conn.execute("SELECT version FROM schema_version")
    applied = {row[0] for row in await cursor.fetchall()}

    pending = [m for m in _discover_migrations() if m[0] not in applied]

    # Table-rebuild migrations (`ALTER TABLE ... ADD COLUMN` is SQLite's only in-place
    # schema change; widening a `CHECK` or dropping `NOT NULL` -- migration 008, and now 015
    # -- means create the new shape, copy every row across, `DROP TABLE` the old one, rename)
    # need `PRAGMA foreign_keys = OFF` for real, not merely attempted. `connect()` leaves this
    # connection with it ON, and SQLite's `DROP TABLE` with foreign keys enabled performs an
    # implicit `DELETE FROM` of every row first -- which fires `ON DELETE CASCADE` on any
    # child table, silently. A migration file's own script can't fix this: SQLite documents
    # `PRAGMA foreign_keys` as a no-op once a transaction is open, and every migration below
    # is wrapped in one. (Migration 008's own comment claims this was "confirmed empirically"
    # to be safe without disabling it -- that claim does not hold up: reproduced here while
    # building migration 015, which rebuilds `path_queue`, itself a parent of `item` and
    # `pattern` via `ON DELETE CASCADE` -- see docs/decisions.md, 2026-08-13.) So it's toggled
    # off here, before any pending migration opens a transaction, and back on afterward,
    # success or failure, so this connection's normal FK-enforced state is never left off.
    if pending:
        await conn.execute("PRAGMA foreign_keys = OFF")

    if pending and config_dir is not None:
        # Imported here, not at module level, so a bare `db.py` import (e.g. this module's
        # own tests, which construct migrations directories by hand) never needs
        # core/backup.py's dependencies just to call migrate() without a config_dir.
        from lftpweb.core.backup import create_backup

        try:
            info = await create_backup(
                conn, config_dir, reason=f"pre-migration:{pending[0][0]:03d}"
            )
            logger.info("pre-migration backup created: %s", info.filename)
        except Exception:
            # A failed backup must not block startup -- the migration below still has its
            # own transaction/rollback safety net (this is the *second* net, not the only
            # one), and refusing to start over a full disk or a permissions problem in the
            # backups directory would be worse than proceeding without one.
            logger.exception(
                "pre-migration backup failed; proceeding with migration %03d_%s anyway",
                pending[0][0],
                pending[0][1],
            )

    try:
        for version, name, path in pending:
            logger.info("applying migration %03d_%s", version, name)
            # Each migration is atomic: its statements AND the schema_version row that records
            # it commit together, or nothing does.
            #
            # This has to be done by wrapping the script text rather than by calling BEGIN
            # around executescript(), because executescript() commits any open transaction
            # before it runs — an outer BEGIN would be discarded. Without the wrapper, a
            # migration that fails on its Nth statement leaves statements 1..N-1 committed and
            # schema_version un-updated, so the next start re-runs it from the top, hits
            # "table already exists", and the install is stuck needing manual SQL repair.
            script = (
                "BEGIN;\n"
                f"{path.read_text()}\n"
                "INSERT INTO schema_version (version, name) VALUES "
                f"({version:d}, '{name.replace(chr(39), chr(39) * 2)}');\n"
                "COMMIT;"
            )
            try:
                await conn.executescript(script)
            except Exception:
                # The failed statement left the transaction open; discard the partial migration
                # so the database is still at the previous version rather than between two.
                await conn.rollback()
                logger.error("migration %03d_%s failed and was rolled back", version, name)
                raise
    finally:
        # Restore this connection's normal FK-enforced state (`connect()`'s own invariant)
        # whether the batch above succeeded or raised -- this must never be the reason a
        # caller ends up running the rest of the app's lifetime with FK checks silently off.
        if pending:
            await conn.execute("PRAGMA foreign_keys = ON")


async def is_healthy(conn: aiosqlite.Connection) -> bool:
    """A real query against the database, not a constant — backs /api/health's `db` field."""
    try:
        await conn.execute("SELECT 1")
        return True
    except aiosqlite.Error:
        return False
