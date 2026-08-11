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
    """Open the database with the pragmas this app requires on every connection."""
    path = db_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
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


async def migrate(conn: aiosqlite.Connection) -> None:
    """Apply every migration not yet recorded in schema_version, in order.

    Idempotent: re-running against a database already at head applies nothing, because
    each migration's version is checked against schema_version before it runs.
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

    for version, name, path in _discover_migrations():
        if version in applied:
            continue
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


async def is_healthy(conn: aiosqlite.Connection) -> bool:
    """A real query against the database, not a constant — backs /api/health's `db` field."""
    try:
        await conn.execute("SELECT 1")
        return True
    except aiosqlite.Error:
        return False
