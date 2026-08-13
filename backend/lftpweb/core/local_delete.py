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
successful delete sets `auto_queue_suppressed = 1` with `suppressed_reason = 'deleted_local'`
(migration 008) on every row it touches -- that pairing is what keeps the row frozen there
across every later rescan: `core/engine.py._persist`'s `_protected_rel_paths` already treats
any `auto_queue_suppressed = 1` row as one whose `state` a scan pass must not touch -- the
identical mechanism that already freezes `STOPPED`/`FAILED` rows -- so nothing new had to be
taught to `resolve_absence`/`outcome_survives_rescan` for this to stick.

**The state written is chosen per row, not hardcoded** (fixed 2026-08-13,
`prompts/2026-08-13-delete-must-mark-the-whole-subtree.md` --
`prompts/done/2026-08-12-local-deletion-and-retention.md`, the feature that shipped hours
earlier as `dfb74c2`, wrote an unconditional `REMOVED_BOTH` instead; see docs/decisions.md for
why that was wrong). `REMOVED_BOTH`'s
documented meaning (DESIGN.md §3.2) is "both copies are gone," which is only true when the item
never had a remote copy (`LOCAL_ONLY`) or its remote copy is already gone (a `move` queue past
the remote-delete step). A `copy`-mode delete's normal case leaves a remote copy behind --
`REMOVED_LOCAL` is what's actually true there, and strictly more informative than `REMOTE_ONLY`
("this was downloaded, and is now locally gone" vs. "this was never here"). `_removed_state_for`
below makes the call from `item.remote_size` (`None` only for `LOCAL_ONLY` -- the same reading
`FileTree.tsx`'s delete dialog already uses for the identical distinction), never a live scan.
Suppression is what stops the re-fetch either way -- the state does not have to lie to achieve
that, and never naming `REMOVED_LOCAL` in `core/autoqueue.py.ELIGIBLE_STATES` by default is a
second, independent reason it wouldn't even if suppression were somehow cleared.

**The whole subtree, not just the row that was clicked.** A directory delete removes every
descendant's files too, so every `item` row in the same queue whose `rel_path` is the deleted
path or lies beneath it (`_subtree_rows`) gets the same suppression -- and its own
`_removed_state_for` verdict, since a directory can hold a mix (an `EXCLUDED` child that never
had a remote counterpart alongside siblings that did). All of it lands in the one transaction
that also removes the files, via `_mark_subtree_removed` below -- a crash between "files gone"
and "rows updated" is not a state this module leaves reachable.

**Explicitly out of scope: "delete remote."** The only remote deletion in this codebase is
`move` mode's verification-gated pipeline (§7.4). A manual remote-delete button is a much
larger safety conversation and was deliberately left out of this task -- see
`prompts/2026-08-12-local-deletion-and-retention.md`.

**A third caller, added 2026-08-13, deletes *parts* of an item rather than the whole thing.**
`delete_extracted_archives()` below removes a successfully-extracted release's spent `.rar`/
`.r00`/... volumes, never the item itself -- so it is deliberately **not** a third code path
built on `delete_local()`'s whole-item shape (which ends every affected row at `REMOVED_LOCAL`
or `REMOVED_BOTH`, both wrong for "some files under this item are gone, the rest stays"). What
it does reuse is the same
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

    `affected_rel_paths` (2026-08-13,
    `prompts/2026-08-13-delete-must-mark-the-whole-subtree.md`) is the target's whole subtree --
    itself plus every descendant row in the same queue -- computed identically whether or not
    `dry_run` is set, so a preview can never claim a smaller (or larger) set than a real run
    would actually mark. Empty on every withheld/failed outcome; a call that never got past the
    guards affected nothing.
    """

    deleted: bool
    reason: str
    bytes_freed: int | None = None
    affected_rel_paths: tuple[str, ...] = ()


def _removed_state_for(remote_size: int | None) -> str:
    """DESIGN.md §3.2's two "gone" states, chosen from what is actually true rather than
    hardcoded (this was `REMOVED_BOTH` unconditionally for a few hours -- see docs/decisions.md
    and this module's own docstring). `remote_size` is `None` only for `LOCAL_ONLY` (never
    tracked remotely) or a `move` queue whose remote copy is already gone by the time a delete
    reaches it -- the same reading `FileTree.tsx`'s delete dialog already uses for the identical
    distinction (`hasRemoteCopy`), read from the persisted column rather than a live scan.
    """
    return "REMOVED_LOCAL" if remote_size is not None else "REMOVED_BOTH"


def reconsider_removed_state(
    prev_state: str,
    *,
    remote_present: bool,
    local_present: bool,
    structural_state: str,
) -> str | None:
    """Whether a row this codebase already marked `REMOVED_LOCAL`/`REMOVED_BOTH` (and
    suppressed, `suppressed_reason = 'deleted_local'` -- the only writer of either state) should
    be *corrected* on a later scan because content has demonstrably come back on one side or the
    other. `auto_queue_suppressed` is never touched here or by any caller of this function --
    that flag alone is what keeps the row out of auto-queue (DESIGN.md §3.2 rule 3); `state`
    should still describe what is actually on disk and on the seedbox, because a state that is
    wrong is worse than an ugly one.

    2026-08-13 (`prompts/2026-08-13-delete-state-truthfulness.md`, defect 2), found two ways:
    a deleted release re-uploaded to the seedbox (remote came back, local still absent -- the
    row kept reading `REMOVED_BOTH`, asserting "both gone," while `R` had already gone green a
    scan earlier because `core/engine.py._persist`'s protected branch always refreshes
    `remote_size`, just never `state`); and a deleted release's child file recreated by a fresh
    extraction after a manual re-download of the parent (local came back, remote still absent on
    a `move` queue -- same stale `REMOVED_BOTH`, this time on a row whose parent had already
    moved on to `EXTRACTED`).

    `None` means "no correction due" -- the row is left exactly as `core/engine.py._persist`'s
    protected branch has always left a suppressed row's `state`, unwidened. This fires only for
    `prev_state` in `{"REMOVED_LOCAL", "REMOVED_BOTH"}` -- **never** for `STOPPED`/`FAILED`,
    which are suppressed for entirely different reasons (a retry policy exhausted, a user
    choice) and must stay untouched regardless of what a fresh scan sees, or this would
    reintroduce the exact bug `_protected_rel_paths` exists to prevent (a periodic rescan
    silently reverting a job-lifecycle state).

    - Neither side present: nothing has changed: still `None`.
    - Remote present, local absent: `REMOVED_LOCAL` -- "was downloaded [historically true of
      every row this function fires for], now absent locally, remote still present," exactly
      DESIGN.md §3.2 rule 3's own wording.
    - Local present, remote absent: `LOCAL_ONLY` -- there is no remote copy to compare against
      (the same reading `core/reconcile.py` gives any never/no-longer-remotely-tracked file
      with local content), and it is the honest reading for a `move`-mode child whose remote
      copy is gone for good but whose bytes are back on disk.
    - Both present: the removal claim is false on both fronts at once, so the honest answer is
      simply this pass's own byte comparison -- `structural_state`, the identical reading any
      ordinary (non-suppressed) node with the same remote/local sizes would get. This is not
      "suppressed rows get recomputed generally" (`auto_queue_suppressed` stays set, and the
      caller only ever reaches this function for a row already carrying a delete-produced
      state) -- it is this one row's own removal assertion being corrected once, in place.

    Deliberately narrow in one further way: a `REMOVED_LOCAL` row whose surviving remote copy
    *later* disappears too (remote_present flips to `False` while local stays absent) is left
    exactly as `REMOVED_LOCAL`, not promoted to `REMOVED_BOTH` -- this function only ever
    corrects a removal claim when content *returns*, matching the delete-state-truthfulness
    prompt's own scope (`content returning on either side`) rather than the (also real, but
    unasked-for) opposite direction.
    """
    if prev_state not in ("REMOVED_LOCAL", "REMOVED_BOTH"):
        return None
    if not remote_present and not local_present:
        return None
    if remote_present and not local_present:
        return "REMOVED_LOCAL"
    if local_present and not remote_present:
        return "LOCAL_ONLY"
    return structural_state


class DeleteInFlight:
    """In-memory record of `item.id`s a `delete_local()` call is currently, actually removing
    from disk -- the identical shape and purpose `core/postprocess.py.PostprocessPipeline.
    in_flight_item_ids()` has, and for the same reason (this project's rule, restated in
    `prompts/startnewsession.md`: "a state that is merely protected is a state that can never
    be un-stuck"). Two consumers:

    - `core/engine.py._protected_rel_paths` folds this in alongside the postprocess set, so a
      periodic scan cannot recompute a row's structural state (or its `substate = 'removing'`
      marker, see `delete_local`) while its files are still actually disappearing from under
      it -- without this, a large delete racing a 30s scan could read `PARTIAL`/`REMOTE_ONLY`
      mid-removal and hand auto-queue a target it should never have seen.
    - `delete_local()` itself folds this into the same in-flight check a live postprocess
      worker already gates on (guard 3), so a second delete request for an item already being
      removed is withheld rather than starting a second concurrent `rmtree` of the same tree.

    **Deliberately in-memory, and that is the whole recovery mechanism**, exactly like
    `PostprocessPipeline`'s own set: a crashed or killed process forgets every mark it ever
    made, so a restart's very next scan finds nothing protected and recomputes every affected
    row structurally -- there is no durable "removing" flag anywhere for a crash to strand. A
    counting `dict`, not a `set`, for the same reason `PostprocessPipeline._in_flight` is one:
    a manual delete and a scheduled retention pass could (in principle) both be told to remove
    overlapping subtree rows, and the first to finish must not un-protect an item the second is
    still working on.
    """

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}

    def mark(self, item_ids: Iterable[int]) -> None:
        for item_id in item_ids:
            self._counts[item_id] = self._counts.get(item_id, 0) + 1

    def unmark(self, item_ids: Iterable[int]) -> None:
        for item_id in item_ids:
            remaining = self._counts.get(item_id, 1) - 1
            if remaining > 0:
                self._counts[item_id] = remaining
            else:
                self._counts.pop(item_id, None)

    def in_flight_item_ids(self) -> frozenset[int]:
        return frozenset(self._counts)


async def _subtree_rows(
    db: aiosqlite.Connection, *, queue_id: int, rel_path: str
) -> list[aiosqlite.Row]:
    """Every `item` row in `queue_id` that is `rel_path` itself or lies beneath it -- the
    target's whole subtree, not just the row a caller clicked.

    Matched in Python, deliberately not SQL `LIKE`: `LIKE 'target%'` also matches a sibling
    named `target-extra`, and `_` is a `LIKE` wildcard (any single character) that shows up
    constantly in scene release names, so an unescaped `LIKE` silently over-matches and an
    escaped one is a second thing to get right for no benefit here. `rel_path == target` or
    `rel_path.startswith(target + "/")` is the exact membership test a path-tree subtree
    actually needs, and it is unambiguous by construction: the `"/"` separator can only start a
    genuine child's own relative path, never a sibling's.

    One query for the whole queue rather than one per candidate -- a queue's `item` table is a
    release library, not something this scales badly against, and `queue_id` keeps two queues
    that happen to share a `rel_path` from ever seeing each other's rows.
    """
    cursor = await db.execute(
        "SELECT id, rel_path, remote_size FROM item WHERE queue_id = ?", (queue_id,)
    )
    rows = await cursor.fetchall()
    prefix = rel_path + "/"
    return [
        row for row in rows if row["rel_path"] == rel_path or row["rel_path"].startswith(prefix)
    ]


async def _mark_subtree_removed(db: aiosqlite.Connection, subtree: Sequence[aiosqlite.Row]) -> None:
    """Write every row in `subtree` (`_subtree_rows`'s output) to its own `_removed_state_for`
    verdict, suppressed with `deleted_local` -- the whole batch, so a caller commits it as one
    transaction alongside the filesystem change it follows. A directory can hold a mix (an
    `EXCLUDED` child that never had a remote counterpart next to siblings that did), which is
    exactly why this re-derives the state per row from that row's own `remote_size` rather than
    reusing one verdict for the whole batch.

    `substate = NULL` in the same write (2026-08-13,
    `prompts/2026-08-13-delete-state-truthfulness.md`): this is the one place the transient
    `substate = 'removing'` `delete_local` writes before doing the actual filesystem work gets
    cleared on success -- see that function for the full lifecycle.
    """
    for row in subtree:
        await db.execute(
            "UPDATE item SET state = ?, substate = NULL, auto_queue_suppressed = 1, "
            "suppressed_reason = 'deleted_local' WHERE id = ?",
            (_removed_state_for(row["remote_size"]), row["id"]),
        )


async def _mark_subtree_removing(db: aiosqlite.Connection, item_ids: Sequence[int]) -> None:
    """The transient half of `delete_local`'s two-phase write: `substate = 'removing'` for
    every row in the subtree, `state` left untouched. Issued, committed, and published *before*
    the actual filesystem work starts -- see `delete_local`'s own docstring for why this has to
    happen before the (potentially long) `rmtree`, not after it.
    """
    for item_id in item_ids:
        await db.execute("UPDATE item SET substate = 'removing' WHERE id = ?", (item_id,))


async def _publish_rows(
    db: aiosqlite.Connection, events: EventBus | None, queue_id: int, item_ids: Sequence[int]
) -> None:
    """Read `item_ids` back and publish one `item_delta` for the batch -- the same
    persist-then-read-back-then-publish invariant every other writer in this codebase follows
    (`core/engine.py.scan_queue`'s own comment). Shared by both of `delete_local`'s publishes
    (the transient `substate = 'removing'` write and the final removed-state write) so there is
    exactly one place that turns a set of ids into a WS message.
    """
    if events is None or not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    cursor = await db.execute(
        f"SELECT * FROM item WHERE id IN ({placeholders})",  # noqa: S608 - placeholders only
        tuple(item_ids),
    )
    rows = await cursor.fetchall()
    if rows:
        events.publish(
            {"type": "item_delta", "queue_id": queue_id, "nodes": [item_view(r) for r in rows]}
        )


def _do_remove_from_disk(local_root: Path, resolved: Path) -> None:
    """The actual, potentially slow, filesystem work -- split out so `delete_local` can run it
    via `asyncio.to_thread` (see that function's docstring for why that matters: a large
    `shutil.rmtree` run inline would block the *entire* event loop, including the WS delivery
    of the transient state this same change adds, for as long as the delete takes).
    """
    if local_root.is_symlink():
        local_root.unlink()
    elif resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()


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
    delete_in_flight: DeleteInFlight | None = None,
) -> DeleteOutcome:
    """Delete one item's local copy, or explain why it was withheld. `item` is a `SELECT *`
    (or equivalent) row from `item`; `queue` needs at least `id`, `name`, `local_path`.

    `caller` is a short label ("manual" / "retention") folded into the `event` message so
    History reads "who did this," not just "this happened." `dry_run=True` runs every guard
    exactly as a real call would and reports what *would* happen, without touching the
    filesystem, the `item` row, or the audit trail (`RetentionScheduler`'s preview endpoint
    uses this so "here is exactly what would be deleted" can never drift from what a real run
    actually does -- there is no second implementation of the guard chain to keep in sync).

    **Feedback for a slow delete** (2026-08-13, `prompts/2026-08-13-delete-state-truthfulness.md`,
    defect 1). A directory delete's actual filesystem work (`shutil.rmtree`) can take a while,
    and this function used to run it inline on the event loop, then write the final removed
    state in the same breath -- so a large delete both froze the whole process for its duration
    (no other request, no scan, no WS traffic could get through) *and* gave the row nothing
    honest to say while it happened. Fixed two ways together, because either alone is
    incomplete: (1) `item.substate = 'removing'` is written, committed, and published for the
    whole subtree *before* the filesystem work starts (the same vehicle `core/settle.py` uses
    for `'settling'` -- no `state` CHECK-constraint migration, no new §9.2 vocabulary word), and
    (2) the actual removal runs via `asyncio.to_thread`, so the event loop stays free to
    actually deliver that WS message (and everything else) while a large `rmtree` runs in a
    worker thread. `delete_in_flight` (a `DeleteInFlight`, `None` by default so every existing
    caller/test is unaffected) is marked for the whole subtree around that window and read by
    `core/engine.py._protected_rel_paths`, so a scan racing a large delete can't recompute
    (and republish) a row's structural state out from under files that are still disappearing.

    **Impossible to wedge, the same way `PostprocessPipeline.in_flight_item_ids()` is**: the
    `'removing'` marker is protected only by `delete_in_flight`'s live, in-memory entry, never
    by the substate string itself. A crashed or killed process forgets the mark instantly (see
    `DeleteInFlight`'s own docstring), and `substate` is a column the *general* (unprotected)
    write path in `core/engine.py._persist` always writes an explicit value for on every scan
    -- so the very next scan after a crash, restart, or any raised exception here corrects both
    `state` and `substate` from a fresh structural reading, within one scan interval at worst.
    The `finally` block below additionally clears both eagerly on any outcome (success or a
    caught `OSError`), so the common case never even waits that long.
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

    # 3. Not in a live-worker check -- never the state string
    # (`PostprocessPipeline.in_flight_item_ids()`, and -- 2026-08-13 -- this same item's own
    # `DeleteInFlight`, so a second delete request for an item already mid-removal is withheld
    # instead of racing a second `rmtree` of the same tree).
    if item_id in in_flight_item_ids:
        return await withhold("a post-processing worker is currently running for this item")
    if delete_in_flight is not None and item_id in delete_in_flight.in_flight_item_ids():
        return await withhold("a delete is already in progress for this item")

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

    # The whole subtree this delete is about to affect -- computed once, identically for the
    # dry-run and real-run paths below, so `preview_retention` can never claim a set a real run
    # wouldn't also mark (`DeleteOutcome.affected_rel_paths`'s own docstring).
    subtree = await _subtree_rows(db, queue_id=queue_id, rel_path=rel_path)
    affected_rel_paths = tuple(row["rel_path"] for row in subtree)

    if dry_run:
        return DeleteOutcome(
            deleted=True,
            reason="would delete",
            bytes_freed=bytes_freed,
            affected_rel_paths=affected_rel_paths,
        )

    subtree_ids = [row["id"] for row in subtree]

    # The transient half (module/function docstring): mark the whole subtree in-flight *before*
    # touching the filesystem, so `core/engine.py._protected_rel_paths` shields it from a
    # racing scan for the whole window below, then write/commit/publish `substate = 'removing'`
    # while `state` is left exactly as it was -- a row says something honest about what is
    # happening to it before the (possibly long) removal even starts.
    if delete_in_flight is not None:
        delete_in_flight.mark(subtree_ids)
    try:
        await _mark_subtree_removing(db, subtree_ids)
        await db.commit()
        await _publish_rows(db, events, queue_id, subtree_ids)

        try:
            # Off the event loop (2026-08-13, this function's own docstring): a large
            # `shutil.rmtree` run inline would block every other request, scan, and WS message
            # -- including the one this function just sent -- for as long as the delete takes.
            await asyncio.to_thread(_do_remove_from_disk, local_root, resolved)
        except OSError as exc:
            # Clear the transient marker eagerly rather than waiting for the next scan to
            # notice this item is no longer in-flight -- see the function docstring's
            # "impossible to wedge" paragraph.
            for row in subtree:
                await db.execute("UPDATE item SET substate = NULL WHERE id = ?", (row["id"],))
            await db.commit()
            await _publish_rows(db, events, queue_id, subtree_ids)
            await audit.record_event(
                db,
                level="error",
                item_id=item_id,
                kind="local_delete_failed",
                message=f"{caller}: delete of {local_root} failed: {exc}",
            )
            return DeleteOutcome(deleted=False, reason=f"delete failed: {exc}")

        # One transaction: the files are already gone on disk by this point, so every row the
        # subtree touches is marked before the single `commit()` below -- a crash between
        # "files gone" and "rows updated" must not be a state this module leaves reachable
        # (task item 2). This is also what clears `substate = 'removing'` back to `NULL` --
        # see `_mark_subtree_removed`'s own docstring.
        await _mark_subtree_removed(db, subtree)
        await db.commit()
    finally:
        if delete_in_flight is not None:
            delete_in_flight.unmark(subtree_ids)

    subtree_note = (
        f", {len(subtree)} item(s) in its subtree marked removed" if len(subtree) > 1 else ""
    )
    await audit.record_event(
        db,
        level="info",
        item_id=item_id,
        kind="local_delete",
        message=(
            f"{caller}: deleted local copy of {rel_path!r} (queue {queue_id} '{queue['name']}'), "
            f"{bytes_freed if bytes_freed is not None else 'unknown'} bytes{subtree_note}"
        ),
    )
    await _publish_rows(db, events, queue_id, subtree_ids)

    return DeleteOutcome(
        deleted=True,
        reason="deleted",
        bytes_freed=bytes_freed,
        affected_rel_paths=affected_rel_paths,
    )


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
        delete_in_flight: DeleteInFlight | None = None,
    ) -> None:
        self.db = db
        self.events = events
        self._in_flight_provider = in_flight_provider or (lambda: frozenset())
        # 2026-08-13: the same `DeleteInFlight` instance the manual delete endpoint uses
        # (`api/jobs.py`), threaded through so a scheduled retention delete gets the identical
        # transient-state/scan-protection treatment as a manual one -- see `delete_local`'s own
        # docstring. `None` (every existing caller/test) simply means retention deletes get no
        # extra protection, exactly the pre-existing behavior.
        self._delete_in_flight = delete_in_flight
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
                delete_in_flight=self._delete_in_flight,
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
