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

**A third caller, added 2026-08-13, deletes *parts* of an item rather than the whole thing.**
`delete_extracted_archives()` below removes a successfully-extracted release's spent `.rar`/
`.r00`/... volumes, never the item itself -- so it is deliberately **not** a third code path
built on `delete_local()`'s whole-item shape (which ends in `item.state = 'REMOVED_BOTH'`, wrong
for "some files under this item are gone, the rest stays"). What it does reuse is the same
guard vocabulary: `extract.resolve_within_root`'s containment check and the mount-sentinel gate.
See its own docstring for the naive-implementation trap
(`prompts/2026-08-13-delete-archives-after-extract.md`) this exists to avoid, and
`load_deleted_archive_paths`/`save_deleted_archive_paths` for how the reconciler is told these
files are gone on purpose rather than missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, extract, mount_sentinel
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import item_view
from lftpweb.core.util import to_safe_text

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


# --- Delete archives after extract (prompts/2026-08-13-delete-archives-after-extract.md) ------
#
# **The trap this section exists to avoid.** Deleting a release's archive volumes after a
# successful extraction drops the item's local byte total below its remote total. The next scan
# (`core/reconcile.py`) would read that as `local < remote` -> `PARTIAL` (DESIGN.md §3.2 rule
# 2), and rule 9/`core/postprocess.py.outcome_survives_rescan` says `PARTIAL` beats any
# post-processing outcome -- so the `EXTRACTED` state this codebase just wrote would not
# protect the item, and auto-queue would re-fetch the archives, extract them again, delete them
# again, every scan interval, forever. This is the same shape as the `REMOVED_LOCAL` bug shipped
# and reverted the same night in `6d3bd95` (prompts/open-issues.md "4").
#
# The fix is **not** a new completeness rule and **not** `auto_queue_suppressed` (that flag is
# for user decisions and permanent errors, DESIGN.md §4.6, and suppressing an item here would
# also stop it being legitimately re-fetched if the user ever wanted it again). It reuses the
# mechanism `core/patterns.py.build_counts_predicate` already built for the identical problem
# with a different cause (a `file_exclude` pattern instead of a deletion this codebase
# performed): a file the counts_predicate rejects is marked `EXCLUDED` -- a real state, not an
# absence -- and stops counting toward its parent directory's completeness (DESIGN.md §3.2 rule
# 8). `load_deleted_archive_paths`/`save_deleted_archive_paths` below persist exactly the set
# `core/engine.py.scan_queue` needs to fold into that same seam; see that function for how the
# two sources (patterns, deletions) combine into one predicate.


# No site setting lives in this module for this feature -- see
# `core/postprocess.py.PostprocessSettings.delete_archives_after_extract`, alongside the other
# post-processing toggles. This module only holds the deletion primitive and its bookkeeping
# table, the same split `retention_settings` above doesn't need because retention's own toggle
# has no other natural home.


@dataclass(frozen=True)
class ArchiveCleanupResult:
    """One `delete_extracted_archives()` call's outcome. `deleted_rel_paths` is exactly what
    got persisted to the `deleted_archive` table -- what `core/engine.py` needs to have
    happened for the reconciler to stop counting these files, not just what was attempted.
    """

    deleted_rel_paths: tuple[str, ...]
    bytes_freed: int
    withheld_reason: str | None = None


async def load_deleted_archive_paths(db: aiosqlite.Connection, queue_id: int) -> frozenset[str]:
    """Every `rel_path` (queue-root-relative, the same raw scanning-domain string
    `core/reconcile.py`'s trees are keyed by) this codebase has deleted as a spent archive
    volume for this queue -- read by `core/engine.py.scan_queue` on every pass and folded into
    the counts_predicate it hands to `reconcile()`.

    **Not re-encoded through `to_safe_text` on the way out**, deliberately: that boundary
    conversion exists for SQLite/JSON storage, never for the scanning/matching path
    (`core/util.py`'s own docstring), and this frozenset is compared directly against raw
    `remote_tree`/`local_tree` keys inside `reconcile()`. For the overwhelming common case
    (any filename that round-trips through UTF-8 cleanly) the stored and raw forms are
    identical anyway; a filename containing a genuine lone surrogate is the same known,
    accepted edge case `core/postprocess.py._find_item_id_for_failed_dir` already has -- see
    docs/decisions.md.
    """
    cursor = await db.execute(
        "SELECT rel_path FROM deleted_archive WHERE queue_id = ?", (queue_id,)
    )
    rows = await cursor.fetchall()
    return frozenset(row["rel_path"] for row in rows)


async def save_deleted_archive_paths(
    db: aiosqlite.Connection, queue_id: int, rel_paths: Iterable[str]
) -> None:
    """Persist `rel_paths` (already resolved, queue-root-relative) as deleted for `queue_id`.
    `to_safe_text` is applied here, at the storage boundary -- see `load_deleted_archive_paths`
    for why it is deliberately *not* undone on the way back out. `INSERT OR IGNORE`: a path
    already recorded (a second extraction of files that reappeared, or a re-run after a partial
    failure) is not an error, just a no-op for that row.
    """
    if not rel_paths:
        return
    await db.executemany(
        "INSERT INTO deleted_archive (queue_id, rel_path) VALUES (?, ?) "
        "ON CONFLICT (queue_id, rel_path) DO NOTHING",
        [(queue_id, to_safe_text(p)) for p in rel_paths],
    )
    await db.commit()


async def delete_extracted_archives(
    db: aiosqlite.Connection,
    *,
    item: Mapping[str, Any],
    queue: Mapping[str, Any],
    archive_heads: Sequence[Path],
) -> ArchiveCleanupResult:
    """Remove every file belonging to a set of archives that just extracted successfully
    (`core/postprocess.py._do_extract`, gated on `PostprocessSettings.
    delete_archives_after_extract`, default off, and only ever called with `result.state ==
    'EXTRACTED'` -- never on `EXTRACT_FAILED`, never on a precondition failure, both of which
    leave `archive_heads` moot because the caller never reaches this function for them).

    `archive_heads` is `core/extract.py.find_archives`'s own output for the item that was just
    extracted -- the *first* volume of each archive only. This function expands every head to
    its full volume set (`extract.archive_volume_paths`) before touching anything, so a
    multi-volume rar's `.r00`/`.r01`/...`.partNN.rar` continuation volumes are removed too, not
    just the head left holding the whole set's apparent size. Nothing outside that expanded set
    is ever a candidate -- a release directory's `.nfo`/`.sfv`/samples/subtitles are not
    archives and `find_archives` never returned them in the first place, so they are never
    touched here either; sidecars (`.sfv`/`.md5`) survive deliberately, since a future re-verify
    (`core/verify.py`) still wants them.

    **Directories only.** An item that is itself a loose top-level archive file (DESIGN.md §4.7
    -- no containing directory) is withheld outright: deleting its own single file *is*
    deleting the whole item, `delete_local()`'s job, never this one's -- and the sharper reason,
    `core/reconcile.py`'s vacuous-`DOWNLOADED` branch for "every child excluded" only exists at
    the *directory* level (`relevant == 0` is computed per-directory); excluding a loose file's
    own node instead reads `EXCLUDED`, which does not satisfy `outcome_survives_rescan`'s
    `structural_state == 'DOWNLOADED'` requirement and would drop the very `EXTRACTED` state
    this feature exists to protect.

    **No nlink guard**, unlike `delete_local()`'s retention path. That guard proves an `*arr`'s
    hardlink-out-of-the-download-directory pickup already holds a second copy of content this
    call is about to remove -- but nothing hardlinks a compressed archive volume itself; an
    importer picks up the *extracted* output, which this function never touches. There is no
    second copy to prove because the raw archive bytes were never the artifact anything
    downstream wanted, in `copy`, `move`, or `sync` mode alike (see docs/decisions.md for why
    `move` mode -- where the remote copy is already gone by the time extraction runs -- is not
    additionally gated here either).

    Every outcome writes an `event` row before returning -- a withheld batch (one row, the
    whole batch shares one reason), or a completed one (one row naming every file removed and
    the bytes freed). A per-file `OSError` withholds only that file, appended to the same
    success event's message rather than failing the batch -- an unrelated permissions problem
    on volume 5 of 12 must not leave the other 11 behind.
    """
    item_id = item["id"]
    queue_id = queue["id"]
    rel_path = item["rel_path"]

    async def withhold(reason: str) -> ArchiveCleanupResult:
        await audit.record_event(
            db,
            level="warning",
            item_id=item_id,
            kind="archive_cleanup_withheld",
            message=(
                f"delete-archives-after-extract: cleanup of {rel_path!r} (queue {queue_id} "
                f"'{queue['name']}') withheld -- {reason}"
            ),
        )
        return ArchiveCleanupResult(deleted_rel_paths=(), bytes_freed=0, withheld_reason=reason)

    if not archive_heads:
        return ArchiveCleanupResult(deleted_rel_paths=(), bytes_freed=0)

    if not item["is_dir"]:
        return await withhold(
            "item is a loose top-level file, not a directory -- removing its own archive "
            "would remove the whole item"
        )

    local_path = queue["local_path"].rstrip("/")
    root = Path(local_path)

    if not mount_sentinel.check(local_path):
        return await withhold(
            f"local root {local_path!r} is missing, unreadable, or has not yet completed a "
            "scan with the mount sentinel present"
        )

    candidates: list[Path] = []
    for head in archive_heads:
        candidates.extend(extract.archive_volume_paths(head))

    resolved: list[tuple[Path, Path]] = []
    for candidate in candidates:
        one_resolved = extract.resolve_within_root(candidate, root)
        if one_resolved is None:
            return await withhold(
                f"{candidate} resolves outside the queue's local root {root} -- refusing the "
                "whole batch (symlink escape or similar)"
            )
        resolved.append((candidate, one_resolved))

    deleted: list[str] = []
    failed: list[str] = []
    bytes_freed = 0
    for candidate, target in resolved:
        try:
            size = target.stat().st_size
            target.unlink()
        except OSError as exc:
            failed.append(f"{candidate.name} ({exc})")
            continue
        deleted.append(candidate.relative_to(root).as_posix())
        bytes_freed += size

    if not deleted:
        reason = (
            "every candidate archive file failed to delete: " + "; ".join(failed)
            if failed
            else "no archive files were found on disk to delete"
        )
        return await withhold(reason)

    await save_deleted_archive_paths(db, queue_id, deleted)

    message = (
        f"delete-archives-after-extract: removed {len(deleted)} archive file(s) for "
        f"{rel_path!r} (queue {queue_id} '{queue['name']}'), {bytes_freed} bytes freed: "
        + ", ".join(deleted)
    )
    if failed:
        message += f" -- {len(failed)} file(s) could not be removed: " + "; ".join(failed)
    await audit.record_event(
        db,
        level="info",
        item_id=item_id,
        kind="archive_cleanup",
        message=message,
    )
    return ArchiveCleanupResult(deleted_rel_paths=tuple(deleted), bytes_freed=bytes_freed)
