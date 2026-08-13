"""Local deletion -- manual (Files-page button) and scheduled (retention). The second
irreversible-delete feature in this codebase and the first that touches the user's own data
(DESIGN.md §7.1/§7.3/§7.4; `prompts/open-issues.md` "7 + 8 -- the deletion cluster", the
specification this module implements). Built to the bar `move` mode's remote-delete gate
already set (`core/postprocess.py._maybe_delete_remote`): two-layer opt-in, an `event` row on
every delete *and* every withhold, no silent path.

**One primitive, two callers.** `delete_local()` below is called by `api/jobs.py`'s per-item
delete endpoint (manual, `require_nlink_guard=False` -- `LOCAL_ONLY` junk with exactly one link
is precisely what a human deleting by hand is trying to remove) and by `RetentionScheduler`
(scheduled, `require_nlink_guard=True` -- a robot deleting unattended must refuse when it
cannot prove another copy exists via a hardlink out of the download directory). Whether the
user's `*arr` actually hardlinks, copies, or moves out of the local downloads directory is
unknown at the time this was written; it shapes how often the retention guard fires, not
whether either caller is correct.

**Every guard, every call:** path containment (`core/extract.py.resolve_within_root`, the same
helper `sweep_failed_dirs` uses -- see that module for why there is exactly one of these), no
active job for the item, not in `PostprocessPipeline.in_flight_item_ids()` (the live-worker
check, never a state string -- `prompts/startnewsession.md`: "a state that is merely protected
is a state that can never be un-stuck"), and the mount-sentinel gate `core/autoqueue.py`
already uses. The `nlink > 1` guard is the one place the two callers differ, so it is a
parameter of the caller (`require_nlink_guard`), never baked into the primitive.

**Row lifetime.** Deletion never removes the `item` row -- nothing in this codebase does
(`core/engine.py._project`'s own docstring; row lifetime stays an explicitly open question,
per `prompts/startnewsession.md` item 6, that this module must not answer by accident). A
successful delete sets `state = 'REMOVED_BOTH'` and `auto_queue_suppressed = 1` with
`suppressed_reason = 'deleted_local'` (migration 008) in the same write. That pairing is what
keeps the row frozen there across every later rescan: `core/engine.py._persist`'s
`_protected_rel_paths` already treats any `auto_queue_suppressed = 1` row as one whose `state`
a scan pass must not touch -- the identical mechanism that already freezes `STOPPED`/`FAILED`
rows -- so nothing new had to be taught to `resolve_absence`/`outcome_survives_rescan` for this
to stick. `REMOVED_BOTH`'s documented meaning (DESIGN.md §3.2) is "remote deleted by us," which
is not literally true for a `copy`-mode queue's local-only delete -- a deliberate, minor
overload recorded in docs/decisions.md rather than left implicit, because it is the only
terminal "we're done with this row" state already excluded from
`core/autoqueue.py.ELIGIBLE_STATES` and it keeps History honest that *something* deliberate
happened, which is the property this module actually needs.

**Explicitly out of scope: "delete remote."** The only remote deletion in this codebase is
`move` mode's verification-gated pipeline (§7.4). A manual remote-delete button is a much
larger safety conversation and was deliberately left out of this task -- see
`prompts/2026-08-12-local-deletion-and-retention.md`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, extract, mount_sentinel
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class DeleteOutcome:
    """The result of one `delete_local()` call. `reason` is always populated -- "deleted" (or
    "would delete" for a dry run) on success, the withheld/failed precondition otherwise -- so
    a caller never has to re-derive why from a bare boolean.
    """

    deleted: bool
    reason: str
    bytes_freed: int | None = None


def _all_hardlinked(local_root: Path, resolved: Path) -> bool:
    """The `nlink > 1` guard (prompts/open-issues.md "7 + 8"): every regular file under the
    item has more than one hard link, so removing this copy provably cannot destroy the only
    one -- the other link (an `*arr`'s hardlink-out-of-the-download-directory pickup) still
    holds the data. A file with exactly one link is the only copy and fails this guard.

    An item that is itself a symlink is vacuously safe -- unlinking a symlink never touches
    the data it points to, so there is nothing this guard needs to protect. Symlinks
    encountered *inside* a directory item are likewise skipped, for the same reason (and
    `shutil.rmtree` never follows them during the real delete either, so counting their
    target's links would be answering a question about data this call would never touch). A
    directory with no regular files under it at all is vacuously safe too -- there is no
    single copy anywhere for this guard to be protecting.
    """
    if local_root.is_symlink():
        return True
    if resolved.is_file():
        return os.stat(resolved).st_nlink > 1
    for entry in resolved.rglob("*"):
        if entry.is_symlink():
            continue
        if entry.is_file() and os.stat(entry).st_nlink <= 1:
            return False
    return True


async def delete_local(
    db: aiosqlite.Connection,
    *,
    item: Mapping[str, Any],
    queue: Mapping[str, Any],
    caller: str,
    require_nlink_guard: bool,
    in_flight_item_ids: frozenset[int],
    events: EventBus | None = None,
    dry_run: bool = False,
) -> DeleteOutcome:
    """Delete one item's local copy, or explain why it was withheld. `item` is a `SELECT *`
    (or equivalent) row from `item`; `queue` needs at least `id`, `name`, `local_path`.

    `caller` is a short label ("manual" / "retention") folded into the `event` message so
    History reads "who did this," not just "this happened." `dry_run=True` runs every guard
    exactly as a real call would and reports what *would* happen, without touching the
    filesystem, the `item` row, or the audit trail (`RetentionScheduler`'s preview endpoint
    uses this so "here is exactly what would be deleted" can never drift from what a real run
    actually does -- there is no second implementation of the guard chain to keep in sync).
    """
    item_id = item["id"]
    queue_id = queue["id"]
    rel_path = item["rel_path"]

    async def withhold(reason: str) -> DeleteOutcome:
        if not dry_run:
            await audit.record_event(
                db,
                level="warning",
                item_id=item_id,
                kind="local_delete_withheld",
                message=(
                    f"{caller}: delete of {rel_path!r} (queue {queue_id} '{queue['name']}') "
                    f"withheld -- {reason}"
                ),
            )
        return DeleteOutcome(deleted=False, reason=reason)

    if not rel_path:
        return await withhold("item has an empty rel_path, refusing to act")

    local_path = queue["local_path"].rstrip("/")
    root = Path(local_path)
    local_root = root / rel_path

    # 1. Path containment (non-negotiable -- prompts/2026-08-12-local-deletion-and-retention.md
    # calls this out by name). A `LOCAL_ONLY` item can be a symlink; refuse to follow one that
    # would place the real target outside the queue's local root.
    resolved = extract.resolve_within_root(local_root, root)
    if resolved is None:
        return await withhold(
            f"{local_root} resolves outside the queue's local root {root} -- refusing "
            "(symlink escape or similar)"
        )

    # 2. No active job for the item.
    cursor = await db.execute(
        "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
        (item_id,),
    )
    if await cursor.fetchone() is not None:
        return await withhold("an active job exists for this item")

    # 3. Not in the live-worker check -- never the state string
    # (`PostprocessPipeline.in_flight_item_ids()`).
    if item_id in in_flight_item_ids:
        return await withhold("a post-processing worker is currently running for this item")

    # 4. Mount-sentinel gated, like auto-queue (`core/autoqueue.py.on_scan`).
    if not mount_sentinel.check(local_path):
        return await withhold(
            f"local root {local_path!r} is missing, unreadable, or has not yet completed a "
            "scan with the mount sentinel present"
        )

    if not local_root.exists() and not local_root.is_symlink():
        return await withhold(f"{local_root} does not exist -- nothing to delete")

    # 5. The nlink guard -- caller's choice (module docstring).
    if require_nlink_guard and not _all_hardlinked(local_root, resolved):
        return await withhold(
            "at least one file under this item has only one hard link (no proof another copy "
            "exists) and the caller requires the nlink guard"
        )

    bytes_freed = item["local_size"]

    if dry_run:
        return DeleteOutcome(deleted=True, reason="would delete", bytes_freed=bytes_freed)

    try:
        if local_root.is_symlink():
            local_root.unlink()
        elif resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
    except OSError as exc:
        await audit.record_event(
            db,
            level="error",
            item_id=item_id,
            kind="local_delete_failed",
            message=f"{caller}: delete of {local_root} failed: {exc}",
        )
        return DeleteOutcome(deleted=False, reason=f"delete failed: {exc}")

    await db.execute(
        "UPDATE item SET state = 'REMOVED_BOTH', auto_queue_suppressed = 1, "
        "suppressed_reason = 'deleted_local' WHERE id = ?",
        (item_id,),
    )
    await db.commit()
    await audit.record_event(
        db,
        level="info",
        item_id=item_id,
        kind="local_delete",
        message=(
            f"{caller}: deleted local copy of {rel_path!r} (queue {queue_id} '{queue['name']}'), "
            f"{bytes_freed if bytes_freed is not None else 'unknown'} bytes"
        ),
    )
    if events is not None:
        row_cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        row = await row_cursor.fetchone()
        if row is not None:
            events.publish({"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(row)]})

    return DeleteOutcome(deleted=True, reason="deleted", bytes_freed=bytes_freed)


# --- Retention (prompts/open-issues.md issue 7) -----------------------------------------------

SETTING_KEY = "retention_settings"


@dataclass(frozen=True)
class RetentionSettings:
    """Site-level toggle. **Default off, non-negotiable** -- this project ships new
    capabilities off, and deletion of the user's own data is not where to make the one
    reasoned exception (scheduled backups) this project has made: backups add files, this
    removes them.
    """

    enabled: bool = False
    retention_days: float = 30.0


async def load_retention_settings(db: aiosqlite.Connection) -> RetentionSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return RetentionSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return RetentionSettings()
    return RetentionSettings(
        enabled=bool(data.get("enabled", False)),
        retention_days=float(data.get("retention_days", 30.0)),
    )


async def save_retention_settings(db: aiosqlite.Connection, settings: RetentionSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps({"enabled": settings.enabled, "retention_days": settings.retention_days}),
        ),
    )
    await db.commit()


async def _select_expired(
    db: aiosqlite.Connection, retention_days: float
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every top-level item (DESIGN.md §4.7's granularity, the same one
    `core/autoqueue.py`/`core/settle.py` use) whose `downloaded_at` is older than
    `retention_days` -- **not `state_changed_at`**: "when did this complete" and "when did it
    last move" are different questions, and an item that dips `DOWNLOADED -> PARTIAL ->
    DOWNLOADED` must not earn a fresh lease it hasn't actually earned
    (`prompts/open-issues.md`). `downloaded_at` is backfilled for the reconcile-only path by
    `core/engine.py._persist` (2026-08-12) precisely so this query doesn't silently skip a
    large share of the library.

    Restricted to `mount_sentinel.COMPLETE_STATES` -- the item must currently be in a state
    that asserts its bytes are all here; a `PARTIAL` or already-`REMOVED_LOCAL` item has
    nothing retention should be acting on -- and to `auto_queue_suppressed = 0`, so an item a
    previous retention pass (or a manual delete) already removed is never selected again.
    Only enabled queues are considered, mirroring `core/engine.py.scan_all`'s own `if not
    q.enabled: continue`.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    states_sql = ",".join("?" for _ in mount_sentinel.COMPLETE_STATES)
    cursor = await db.execute(
        "SELECT item.id AS item_id, item.rel_path AS item_rel_path, item.local_size AS item_local_size, "
        "item.downloaded_at AS item_downloaded_at, item.state AS item_state, "
        "path_queue.id AS queue_id, path_queue.name AS queue_name, "
        "path_queue.local_path AS queue_local_path "
        "FROM item JOIN path_queue ON path_queue.id = item.queue_id "
        "WHERE item.downloaded_at IS NOT NULL AND item.downloaded_at < ? "
        "AND item.auto_queue_suppressed = 0 AND instr(item.rel_path, '/') = 0 "
        f"AND item.state IN ({states_sql}) AND path_queue.enabled = 1 "  # noqa: S608 - fixed set, no user input
        "ORDER BY item.downloaded_at ASC",
        (cutoff, *mount_sentinel.COMPLETE_STATES),
    )
    rows = await cursor.fetchall()
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        item = {
            "id": row["item_id"],
            "rel_path": row["item_rel_path"],
            "local_size": row["item_local_size"],
            "downloaded_at": row["item_downloaded_at"],
            "state": row["item_state"],
        }
        queue = {
            "id": row["queue_id"],
            "name": row["queue_name"],
            "local_path": row["queue_local_path"],
        }
        out.append((item, queue))
    return out


async def preview_retention(
    db: aiosqlite.Connection,
    *,
    retention_days: float,
    in_flight_item_ids: frozenset[int],
) -> list[dict[str, Any]]:
    """ "Here is exactly what would be deleted, and the total bytes" -- the dry-run endpoint
    (prompts/open-issues.md "7 + 8"), mirroring `api/settings.py.pattern_preview`'s idiom
    rather than inventing a new one. Runs `delete_local(dry_run=True)` for every candidate with
    the guard shape a real retention pass uses (`require_nlink_guard=True`), so this is
    provably "what a real run would delete," not a separately-maintained approximation of it.
    """
    candidates = await _select_expired(db, retention_days)
    out: list[dict[str, Any]] = []
    for item, queue in candidates:
        outcome = await delete_local(
            db,
            item=item,
            queue=queue,
            caller="retention",
            require_nlink_guard=True,
            in_flight_item_ids=in_flight_item_ids,
            dry_run=True,
        )
        if outcome.deleted:
            out.append(
                {
                    "item_id": item["id"],
                    "queue_id": queue["id"],
                    "queue_name": queue["name"],
                    "rel_path": item["rel_path"],
                    "local_size": item["local_size"],
                    "downloaded_at": item["downloaded_at"],
                }
            )
    return out


@dataclass(frozen=True)
class RetentionRunResult:
    considered: int
    deleted: int
    withheld: int


class RetentionScheduler:
    """Background loop, same `_task`/`start()`/`stop()` shape as `core/backup.py.
    BackupScheduler`, `core/engine.py.Engine`, and `core/metrics.py.MetricsRetentionScheduler`
    -- one bad cycle must not kill the loop, and `stop()` cancels cleanly on shutdown.

    `in_flight_provider` is a zero-arg callable returning `PostprocessPipeline.
    in_flight_item_ids()` at call time, the same plain-attribute-after-construction seam
    `core/engine.py.Engine.postprocess` uses -- `main.py`'s lifespan can't hand this scheduler
    a `PostprocessPipeline` instance until one exists, and this module must not import
    `core/postprocess.py` just to type a callable.
    """

    CHECK_INTERVAL_S = 3600.0  # hourly, same cadence as MetricsRetentionScheduler/BackupScheduler

    def __init__(
        self,
        db: aiosqlite.Connection,
        events: EventBus,
        *,
        in_flight_provider: Callable[[], frozenset[int]] | None = None,
    ) -> None:
        self.db = db
        self.events = events
        self._in_flight_provider = in_flight_provider or (lambda: frozenset())
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-retention-loop")

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
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("retention cycle failed")
            await asyncio.sleep(self.CHECK_INTERVAL_S)

    async def run_once(self) -> RetentionRunResult:
        settings = await load_retention_settings(self.db)
        if not settings.enabled:
            return RetentionRunResult(considered=0, deleted=0, withheld=0)

        candidates = await _select_expired(self.db, settings.retention_days)
        deleted = withheld = 0
        for item, queue in candidates:
            outcome = await delete_local(
                self.db,
                item=item,
                queue=queue,
                caller="retention",
                require_nlink_guard=True,
                in_flight_item_ids=self._in_flight_provider(),
                events=self.events,
            )
            if outcome.deleted:
                deleted += 1
            else:
                withheld += 1
        if candidates:
            logger.info(
                "retention: considered %d item(s), deleted %d, withheld %d",
                len(candidates),
                deleted,
                withheld,
            )
        return RetentionRunResult(considered=len(candidates), deleted=deleted, withheld=withheld)
