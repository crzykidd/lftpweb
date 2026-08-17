"""Post-extraction archive cleanup: remove a successfully-extracted release's spent
`.rar`/`.r00`/... volumes (never the item itself), and record which paths were deleted on
purpose so the reconciler reads them as gone-deliberately, not missing. Split out of
`core/local_delete.py` (audit P3)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, extract, mount_sentinel
from lftpweb.core.util import to_safe_text

from lftpweb.core.local_delete import _physical_local_root

logger = logging.getLogger(__name__)


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


async def purge_deleted_archive_paths(
    db: aiosqlite.Connection, queue_id: int, rel_paths: Iterable[str]
) -> None:
    """Forget `rel_paths` from the `deleted_archive` registry -- the narrow delete this table
    was missing (2026-08-17, `prompts/2026-08-17-orphaned-spent-archive-rows.md`).

    `core/engine.py._persist`'s vanished-from-both-trees sweep resolves a spent archive
    volume's `EXCLUDED` exemption to `REMOVED_BOTH` once the row's own top-level ancestor has
    itself left both trees (the release finished its whole pipeline and departed) -- see that
    sweep's own comment for the ancestor check. This call is the other half: without it, the
    registry entry would survive forever, and a later release landing at the identical
    `rel_path` would read `EXCLUDED` from its very first scan for a file it never touched --
    the same stale-registry failure `core/reset.py`'s own module docstring names for
    `reset_item`/`reset_scope`, reached here by an ordinary vanish rather than a deliberate
    reset.

    `rel_paths` is taken as already the safe-text form the `item`/`deleted_archive` rows are
    both stored in -- `core/engine.py`'s `previous` dict keys are `item.rel_path` values, no
    different an assumption from `core/reset.py._reset_rows`'s identical one for the same
    table. No commit here, deliberately: the caller (`_persist`) folds this into the same
    transaction as the rest of the pass's writes, exactly like the `UPDATE item` a few lines
    away in that sweep.
    """
    rel_paths = list(rel_paths)
    if not rel_paths:
        return
    placeholders = ",".join("?" for _ in rel_paths)
    await db.execute(
        f"DELETE FROM deleted_archive WHERE queue_id = ? AND rel_path IN ({placeholders})",  # noqa: S608 - placeholders only, no interpolated values
        (queue_id, *rel_paths),
    )


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
        # No `event` row here, deliberately -- most items have no archives at all, and this is
        # the *only* caller-eligible path (`_do_extract` already returned earlier, before ever
        # reaching this call, whenever `find_archives` came back empty), so an event per scan
        # would be near-pure noise for the common case. But it used to be genuinely silent (no
        # log line either) -- the one withhold this function had that left no trace at all when
        # a user was diagnosing why cleanup hadn't run (2026-08-13,
        # prompts/2026-08-13-per-queue-archive-cleanup.md, item 4). A debug line at least gives
        # `LFTPWEB_LOG_LEVEL=DEBUG` something to find.
        logger.debug(
            "archive-cleanup: no archives to clean up for %r (queue %s)", rel_path, queue_id
        )
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

    # "Folder prefix during transfer" (`core/download_prefix.py`) can still have this item's
    # bytes physically sitting under a prefixed directory name when this runs -- extraction
    # (this function's only caller, `core/postprocess.py._do_extract`) now runs as part of the
    # pipeline *before* the rename off the prefix (2026-08-14,
    # prompts/done/2026-08-14-rename-after-postprocessing-not-before.md), so `find_archives` was
    # handed the physical, still-prefixed root. Resolved the same way `delete_local` already
    # does (reused, not re-derived, per this task's own instruction) so the two agree on "where
    # are this item's bytes actually."
    physical_root = await _physical_local_root(db, queue_id=queue_id, root=root, rel_path=rel_path)

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
        # Recorded under the item's *logical* `rel_path`, never the physical one -- `rel_path`
        # relative to `physical_root` (which may be the prefixed directory) reattached onto
        # `item["rel_path"]` (never prefixed, DESIGN.md's own invariant for that column). This
        # is compared against `item.rel_path` elsewhere (`core/engine.py.
        # build_scan_counts_predicate`), so a path recorded relative to the physical root here
        # would silently embed the prefix and never match, defeating the archive-cleanup
        # completeness accounting (DESIGN.md §6) the moment this ran on a still-prefixed item.
        rest = candidate.relative_to(physical_root)
        logical_rel = f"{rel_path}/{rest.as_posix()}" if str(rest) != "." else rel_path
        deleted.append(logical_rel)
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


# --- Reset item tracking (2026-08-13, prompts/2026-08-13-reset-item-tracking.md) ---------------
#
# **Not a delete.** `delete_local()` above removes bytes and marks the row so a rescan leaves it
# alone. This removes the *row* -- and every other row keyed to the same path -- so a rescan
# treats the path as never having been seen. Named "Reset item tracking" deliberately, not
# anything close to "Clear History" (`48ad72c`, `api/history.py`): that feature deletes `job`/
# `event` rows and explicitly never touches `item` -- "clearing records must not change
# behaviour" was the right call there. This is the opposite: the whole point is to change
# behaviour (a suppressed/failed path becomes fetchable again), and it belongs on the Files page
# where the items are, never on the History page.
#
# **All three tables, every time -- this is the whole point of the task.** `item_settle`
# (migration 007) and `deleted_archive` (migration 010) both cascade from `path_queue`, *not*
# from `item` (`PRIMARY KEY (queue_id, rel_path)` on both, no FK to `item.id` at all) -- so
# deleting only the `item` row leaves both behind, keyed to the same `(queue_id, rel_path)` a
# freshly re-downloaded item at that path would get. A stale `item_settle` row hands the new
# item someone else's fingerprint and scan count; a stale `deleted_archive` row is worse and
# silent -- `core/engine.py.build_scan_counts_predicate` folds it straight into the completeness
# seam, so a freshly re-downloaded archive reads `EXCLUDED` immediately, with no error and no
# obvious cause. Checked at the time this was written: no other table is keyed by
# `(queue_id, rel_path)` (`grep` across every migration) -- `job`/`event` key off `item.id`
# instead and are handled by the schema's own `ON DELETE CASCADE` / `ON DELETE SET NULL`, not by
# this module.
#
# **The `job`/`event` consequence is real and is not this module's to avoid.**
# `job.item_id REFERENCES item (id) ON DELETE CASCADE` (`001_initial_schema.sql:109`) -- every
# transfer-attempt record for a reset item is gone the instant its `item` row is, not merely
# hidden. `event.item_id ... ON DELETE SET NULL` (line 139) -- audit rows survive but lose the
# link back to what they were about. Both fire automatically (`PRAGMA foreign_keys = ON`,
# `core/db.py.connect`) the moment `_reset_targets` below deletes the row; there is no way to
# reset tracking *without* this, short of denormalising `job`/`event` first (open issue #1,
# `prompts/open-issues.md`). The right response is to say so plainly before it happens, not to
# build around it -- see the API layer (`api/jobs.py`) for the warning text this is paired with.
#
# **Refuse, don't race -- deliberately not `delete_item`'s stop-then-act ordering.** `delete_item`
# satisfies its own "no active job" guard by stopping the job first, because a delete has to run
# regardless (the user asked for the bytes gone). Reset has no such urgency -- forgetting a path
# is just as available a minute from now, once whatever is happening to it has finished on its
# own -- so `_guard_busy` below only ever refuses; it never calls `TransferQueue.stop_item()`.
# Same three guards `delete_local` already established: an active `job` row for the target's own
# id (not its descendants' -- a "mirror" job is tracked against the top-level item's id, exactly
# the same reading `delete_local`'s guard 2 uses), `PostprocessPipeline.in_flight_item_ids()`,
# and (2026-08-13's own addition) `DeleteInFlight` -- resetting the rows out from under a delete
# that is still `rmtree`-ing the same subtree would let `_mark_subtree_removed`'s writes target
# rows that raced back into existence mid-delete.
#
# **No new in-flight marker, and that is a considered omission, not an oversight.** Every write
# below is a handful of `DELETE ... WHERE queue_id = ? AND rel_path IN (...)` statements followed
# by one `commit()` -- no `asyncio.to_thread`, no filesystem work, nothing that holds the event
# loop or a transaction open long enough for `core/engine.py._protected_rel_paths` to need to
# shield it from a racing scan the way `delete_local`'s (potentially large, potentially slow)
# `rmtree` needs shielding. If this module ever grows a reset path with real latency in the
# middle, that omission is the first thing to revisit.
