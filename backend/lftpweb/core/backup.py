"""Database backup (DESIGN.md §10.2): `VACUUM INTO`, never a file copy.

`VACUUM INTO` is atomic and WAL-safe — SQLite builds the target file from a consistent
snapshot inside its own transaction machinery, so a backup taken mid-transfer (WAL actively
being written) cannot capture a torn database the way copying `lftpweb.db` off the
filesystem while it's open could. This is the entire reason DESIGN.md specifies it instead
of the "obvious" `shutil.copy`.

Three things use this module:

- **Settings → Backup**'s manual "Backup now" (`api/backup.py`).
- **`BackupScheduler`** below — a background loop, same `_task`/`start()`/`stop()` shape as
  `core/engine.py.Engine` and `core/queue.py.TransferQueue` — that takes a backup roughly
  every `BackupSettings.interval_days` and prunes to `keep_count` (default: daily, keep 7,
  both configurable — DESIGN.md's own literal default).
- **`db.py`'s `migrate()`** — a backup immediately before any pending migration runs. This is
  the one that actually matters (docs/decisions.md): migrations are the failure mode that
  loses everything, not a random Tuesday. It is unconditional and not user-configurable —
  there is no toggle that can turn off the one backup this module exists for.

**The encryption secret (`core/crypto.py`'s `secret.key`) is never included.** It lives in
its own file outside the SQLite database, `VACUUM INTO` only ever touches the database file's
own pages, and this module never writes anything to the backups directory except what
`VACUUM INTO` produces — so there is no code path that could copy it in.
`tests/test_backup.py::test_backup_never_contains_the_encryption_secret` asserts this
directly (byte-search the backup file for the raw secret, and confirm `secret.key` itself
never appears under `<config>/backups/`) rather than relying on that argument alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"

# lftpweb-YYYYMMDD-HHMMSS.db, plus an optional -N suffix for the (practically never hit)
# case of two backups requested within the same wall-clock second.
_FILENAME_RE = re.compile(r"^lftpweb-(\d{8}-\d{6})(?:-\d+)?\.db$")

SETTING_KEY = "backup_settings"


def backup_dir(config_dir: str) -> Path:
    return Path(config_dir) / BACKUP_DIRNAME


# --- Settings (JSON in `setting`, the same pattern core/queue.py.TransferSettings and
# core/postprocess.py.PostprocessSettings use) ------------------------------------------


@dataclass(frozen=True)
class BackupSettings:
    """Settings → Backup (DESIGN.md §10.2): "daily by default, keep 7, both configurable" —
    used verbatim as the default rather than shipped disabled. See docs/decisions.md for why
    this phase's scheduled backup is the one exception this overnight run makes to "every new
    capability defaults off": it is non-destructive, bounded by `keep_count`, and is the
    literal spec'd default, not an invented extra.
    """

    interval_days: float = 1.0
    keep_count: int = 7


async def load_backup_settings(db: aiosqlite.Connection) -> BackupSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return BackupSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return BackupSettings()
    return BackupSettings(
        interval_days=float(data.get("interval_days", 1.0)),
        keep_count=int(data.get("keep_count", 7)),
    )


async def save_backup_settings(db: aiosqlite.Connection, settings: BackupSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES "
        "(?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps(
                {"interval_days": settings.interval_days, "keep_count": settings.keep_count}
            ),
        ),
    )
    await db.commit()


# --- Taking, listing, and pruning backups -----------------------------------------------


@dataclass(frozen=True)
class BackupInfo:
    filename: str
    size_bytes: int
    created_at: str  # ISO-8601 UTC, parsed from the filename's own timestamp


def _parse_created_at(filename: str) -> str:
    match = _FILENAME_RE.match(filename)
    if not match:
        raise ValueError(f"not a backup filename: {filename}")
    dt = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _list_backups_sync(config_dir: str) -> list[BackupInfo]:
    d = backup_dir(config_dir)
    if not d.is_dir():
        return []
    # Sort by real mtime (sub-second resolution on any filesystem this app targets), not the
    # filename's own second-resolution timestamp -- a burst of "Backup now" clicks inside the
    # same wall-clock second collides at the filename's own granularity (the -N suffix keeps
    # names unique, but "-1.db" does not sort after ".db" lexicographically), so filename
    # order alone cannot be trusted as chronological order for retention/listing.
    entries = [(p, p.stat()) for p in d.iterdir() if p.is_file() and _FILENAME_RE.match(p.name)]
    entries.sort(key=lambda e: e[1].st_mtime, reverse=True)  # newest first
    return [
        BackupInfo(filename=p.name, size_bytes=stat.st_size, created_at=_parse_created_at(p.name))
        for p, stat in entries
    ]


async def list_backups(config_dir: str) -> list[BackupInfo]:
    return await asyncio.to_thread(_list_backups_sync, config_dir)


def backup_file_path(config_dir: str, filename: str) -> Path:
    """Resolve a backup filename to its path, validating it against the exact naming
    convention this module writes -- never trust a filename from a request directly, since
    it's about to be handed to a download endpoint.
    """
    if not _FILENAME_RE.match(filename):
        raise ValueError(f"not a valid backup filename: {filename}")
    return backup_dir(config_dir) / filename


async def create_backup(
    db: aiosqlite.Connection, config_dir: str, *, reason: str = "manual"
) -> BackupInfo:
    """Take a `VACUUM INTO` backup. `reason` (e.g. "manual", "scheduled",
    "pre-migration:003") is logged only, for operators reading the app log -- there is no
    separate provenance column.
    """
    d = backup_dir(config_dir)
    await asyncio.to_thread(d.mkdir, parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = d / f"lftpweb-{timestamp}.db"
    suffix = 1
    while await asyncio.to_thread(path.exists):
        path = d / f"lftpweb-{timestamp}-{suffix}.db"
        suffix += 1

    # VACUUM INTO takes its target as a bound parameter like any other SQL expression --
    # verified against real sqlite3 (>= 3.27) before relying on it here, not assumed.
    await db.execute("VACUUM INTO ?", (str(path),))

    size = (await asyncio.to_thread(path.stat)).st_size
    logger.info("database backup created (%s): %s (%d bytes)", reason, path.name, size)
    return BackupInfo(filename=path.name, size_bytes=size, created_at=_parse_created_at(path.name))


def _prune_sync(config_dir: str, keep: int) -> list[str]:
    infos = _list_backups_sync(config_dir)  # newest first
    doomed = list(reversed(infos[max(keep, 0) :]))  # oldest first, matching the docstring below
    removed: list[str] = []
    for info in doomed:
        try:
            (backup_dir(config_dir) / info.filename).unlink()
            removed.append(info.filename)
        except FileNotFoundError:
            pass
    return removed


async def prune_backups(config_dir: str, keep: int) -> list[str]:
    """Delete the oldest backups beyond `keep`, oldest first. Returns the filenames removed."""
    removed = await asyncio.to_thread(_prune_sync, config_dir, keep)
    if removed:
        logger.info(
            "pruned %d backup(s) beyond the keep count of %d: %s",
            len(removed),
            keep,
            ", ".join(removed),
        )
    return removed


# --- The background schedule --------------------------------------------------------------


class BackupScheduler:
    """Background loop, same shape as `core/engine.py.Engine`/`core/queue.py.TransferQueue`:
    a cancellable `asyncio.Task` checked on a fixed cadence, not a `while True: sleep(interval)`
    that only reacts to a settings change once the old sleep finally wakes up.
    """

    # Checked hourly rather than sleeping for the full configured interval, so a change to
    # `BackupSettings.interval_days` in Settings → Backup takes effect within the hour
    # instead of waiting out whatever the previous interval had already been slept.
    CHECK_INTERVAL_S = 3600.0

    def __init__(self, db: aiosqlite.Connection, config_dir: str) -> None:
        self.db = db
        self.config_dir = config_dir
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-backup-scheduler-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("scheduled backup cycle failed")
            await asyncio.sleep(self.CHECK_INTERVAL_S)

    async def run_if_due(self) -> BackupInfo | None:
        """Take (and prune after) a scheduled backup if the configured interval has elapsed
        since the newest existing one. Exposed as its own method so a test can call it
        directly rather than waiting out a real `CHECK_INTERVAL_S`.
        """
        settings = await load_backup_settings(self.db)
        existing = await list_backups(self.config_dir)  # newest first
        if existing:
            newest = datetime.strptime(existing[0].created_at, "%Y-%m-%dT%H:%M:%S.000000Z").replace(
                tzinfo=UTC
            )
            elapsed_days = (datetime.now(UTC) - newest).total_seconds() / 86400.0
            if elapsed_days < settings.interval_days:
                return None
        info = await create_backup(self.db, self.config_dir, reason="scheduled")
        await prune_backups(self.config_dir, settings.keep_count)
        return info
