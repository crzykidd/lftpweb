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

**The active-job guard is an ordering requirement, not a dead end** (2026-08-13,
`prompts/2026-08-13-delete-during-transfer.md`). The user asked to be able to delete an item
that's actively transferring, and the wrong fix is dropping this guard: `rmtree`-ing a
directory lftp is still writing into races the writer (files can reappear mid-delete, or a
mirror job can recreate directories it's midway through), so the guard staying in `delete_local`
itself is correct. What changed is who satisfies it and when -- `api/jobs.py.delete_item` now
stops the item's active job through `core/queue.py.TransferQueue.stop_item()` (the exact same
SIGTERM -> grace -> SIGKILL path the Stop button uses, DESIGN.md §4.6; no second stop
implementation exists anywhere) and only calls `delete_local()` once that stop has been
confirmed complete -- the process reaped, its job row terminal, never merely "signal sent." A
stop that can't be confirmed within a bounded window withholds the delete instead of proceeding
blind. See that endpoint's own docstring for the exact sequencing and what "confirmed" means.

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

**A fourth caller, added 2026-08-13, is not a delete at all.** `reset_item()`/`reset_scope()`
below forget an item's tracking outright -- removing its row from `item`, `item_settle`, and
`deleted_archive` -- without touching a single byte on disk (`prompts/2026-08-13-reset-item-
tracking.md`, user report: hitting the same suppressed path three times with no way to make it
reusable again). This exists because nothing else in the codebase can do what it does: `item`
rows are never deleted by anything above (this module's own "Row lifetime" paragraph), which is
exactly right for every *delete* in this file -- the suppression has to survive a rescan -- but
it also means a path that was once `STOPPED`, `deleted_local`'d, or permanently `FAILED`
carries that verdict forever, and a *new* release arriving at the identical name inherits it.
Reset is the deliberate escape hatch: forget the row entirely, so the next scan sees a genuinely
unknown path and starts clean. Three scopes share one primitive (`_reset_targets` below) --
selected items (any depth, expanded to their own subtree via `_subtree_rows`, identical to
`delete_local`'s own shape), a whole queue, and a purge by filename pattern (matched with
`core/patterns.py.pattern_matches`, the same single evaluator `select`/`skip` patterns use --
DESIGN.md §12 requires there be only one). See `reset_item`/`reset_scope`'s own docstrings for
the guard reuse (active job, in-flight post-processing, `DeleteInFlight`) and why this is
"refuse, don't race" rather than the stop-then-act ordering `delete_item` uses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, extract, local_scan, mount_sentinel
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

    **The loose-file branch also cleans up lftp's own in-flight leftovers** (2026-08-13,
    `prompts/2026-08-13-delete-during-transfer.md`). A delete of a top-level item is now only
    reachable after its active job has been stopped and reaped (see `delete_local`'s own
    docstring for the ordering), but "stopped" is not "finished" -- a `pget` job killed
    mid-transfer leaves its bytes under `<name>.lftp` (`xfer:use-temp-file`,
    `core/local_scan.py.TEMP_FILE_SUFFIX`), not under the item's own final name, plus that temp
    file's own `.lftp-pget-status` sidecar. For a **directory** item `shutil.rmtree` below
    already sweeps both regardless of what's inside it -- only a **loose top-level file** item
    (whose `local_root`/`resolved` *is* the final name) can have its actual bytes sitting under
    a name this function was never told about. Removing only `resolved` in that case would
    leave exactly the bytes the delete was asked to remove, orphaned under a different name --
    the failure mode `prompts/2026-08-13-delete-during-transfer.md` calls out by name. Both
    removals are `missing_ok=True`: the common case (a fully-finished file, or one that was
    never in flight at all) has neither, and that's not an error here.

    **Every temp-file *variant*, not just the plain one** (2026-08-13,
    `prompts/2026-08-13-lftp-timestamped-temp-files.md`): `core/local_scan.py.
    find_temp_variants` also finds any `<final-name>.lftp~<timestamp>~` lftp fell back to when
    a second process once raced this same target for the plain name -- see that function's own
    docstring. Each variant's own sidecar (built on *its* name, not the plain one) is removed
    alongside it.
    """
    if local_root.is_symlink():
        local_root.unlink()
    elif resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        if resolved.exists():
            resolved.unlink()
        # The sidecar's own name is built on top of the *temp* name, not the final one --
        # lftp's `pget:save-status` writes `<currently-in-use-name>.lftp-pget-status`, and the
        # currently-in-use name is `<final-name>.lftp` (or its `~<timestamp>~` variant) for as
        # long as `xfer:use-temp-file` (on, `core/lftp.py.build_rc_text`) hasn't yet renamed it.
        # Matches the same two-suffix stacking `core/local_scan.py.scan_local`'s own sidecar
        # lookup expects.
        for temp_path in local_scan.find_temp_variants(resolved.parent, resolved.name):
            temp_path.with_name(temp_path.name + local_scan.PGET_STATUS_SUFFIX).unlink(
                missing_ok=True
            )
            temp_path.unlink(missing_ok=True)


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


async def _physical_local_root(
    db: aiosqlite.Connection, *, queue_id: int, root: Path, rel_path: str
) -> Path:
    """Where this item's bytes **actually are** on disk, which is not `root / rel_path` whenever
    "folder prefix during transfer" is in play (2026-08-14, `core/download_prefix.py`).

    **The bug this exists for.** Stop a directory transfer mid-stream with the prefix enabled and
    its files stay at `<local_path>/<prefix><name>/` -- `core/queue.py._reap_one` renames a
    prefixed directory onto its logical name only on a *successful, complete* job, and clears
    `item.pending_download_prefix` in that same statement. Delete then looked for
    `<local_path>/<name>/`, found nothing, and refused with "does not exist -- nothing to
    delete", leaving the user no way to clean up through the UI.

    A set `pending_download_prefix` therefore means exactly "the bytes are still under the
    prefixed name"; `NULL` means there is nothing in flight to account for.

    **Resolved from the top-level ancestor, not from this row.** The column is only ever written
    on the item a job was spawned for -- the top-level one (`_spawn_decision`) -- so a *child*
    row inside a prefixed directory carries `NULL` while still physically living under the
    prefixed parent. Splitting the first path segment off and looking that up covers both shapes
    with one query; deleting a single file out of a still-prefixed release is a real thing the
    Files page can ask for.

    Falls back to the logical path whenever the prefixed one is not actually on disk. That covers
    a stale column (a prefix changed between the spawn and now) and makes this safe to call
    unconditionally: it can only ever redirect to a path that exists.
    """
    top = rel_path.split("/", 1)[0]
    cursor = await db.execute(
        "SELECT pending_download_prefix FROM item WHERE queue_id = ? AND rel_path = ?",
        (queue_id, top),
    )
    row = await cursor.fetchone()
    prefix = row["pending_download_prefix"] if row is not None else None
    if not prefix:
        return root / rel_path

    rest = rel_path[len(top) :].lstrip("/")
    prefixed = root / f"{prefix}{top}"
    candidate = prefixed / rest if rest else prefixed
    if candidate.exists() or candidate.is_symlink():
        return candidate
    return root / rel_path


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
    local_root = await _physical_local_root(db, queue_id=queue_id, root=root, rel_path=rel_path)

    # 1. Path containment (non-negotiable -- prompts/2026-08-12-local-deletion-and-retention.md
    # calls this out by name). A `LOCAL_ONLY` item can be a symlink; refuse to follow one that
    # would place the real target outside the queue's local root. Deliberately applied to the
    # *physical* root resolved above, not the logical one -- containment must be checked against
    # the path this function is actually going to remove.
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

    # A loose top-level file stopped mid-transfer (see this function's own docstring) can exist
    # on disk *only* as `<name>.lftp` or (2026-08-13) its `<name>.lftp~<timestamp>~` variant --
    # its own final name never got there. Checked unconditionally rather than gated on
    # `item["is_dir"]`: a directory's own name never carries this suffix (mirror writes temp
    # names to the *files inside* it, not to the directory itself), so this is safe to check
    # for every item without needing to know its kind, and `RetentionScheduler`'s own `item`
    # dict (`_select_expired`) doesn't carry `is_dir` at all.
    has_temp_variant = bool(local_scan.find_temp_variants(local_root.parent, local_root.name))
    if not local_root.exists() and not local_root.is_symlink() and not has_temp_variant:
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


# --- Re-exports (audit P3) -------------------------------------------------------------
# The retention / archive-cleanup / reset features moved to their own modules; these keep
# every existing `local_delete.<symbol>` reference resolving without touching a caller. The
# imports sit at the *bottom* so the primitive above is fully defined before a child module
# (which imports it) is first executed -- avoiding the import cycle.
from lftpweb.core.retention import (  # noqa: E402,F401
    OrphanTempCleanupSettings,
    RetentionRunResult,
    RetentionScheduler,
    RetentionSettings,
    _select_expired,
    load_orphan_temp_cleanup_settings,
    load_retention_settings,
    preview_retention,
    save_orphan_temp_cleanup_settings,
    save_retention_settings,
)
from lftpweb.core.archive_cleanup import (  # noqa: E402,F401
    ArchiveCleanupResult,
    delete_extracted_archives,
    load_deleted_archive_paths,
    purge_deleted_archive_paths,
    save_deleted_archive_paths,
)
from lftpweb.core.reset import (  # noqa: E402,F401
    ResetOutcome,
    reset_by_pattern,
    reset_item,
    reset_pattern_matches,
    reset_queue,
    reset_queue_targets,
)
