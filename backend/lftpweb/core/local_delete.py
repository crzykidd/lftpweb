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

from lftpweb.core import audit, extract, local_scan, mount_sentinel
from lftpweb.core import patterns as patterns_core
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


# --- Orphaned temp-file cleanup (2026-08-13, prompts/2026-08-13-lftp-timestamped-temp-files.md) -

ORPHAN_TEMP_CLEANUP_SETTING_KEY = "orphan_temp_cleanup_settings"


@dataclass(frozen=True)
class OrphanTempCleanupSettings:
    """Site-level toggle for `local_scan.sweep_orphan_temp_files`. **Default off**, this
    project's rule for anything that deletes (`RetentionSettings`'s own docstring), even though
    the thing being removed here is accidental byte waste with no diagnostic value: an operator
    who has never hit the duplicate-job bug this task fixes (`core/queue.py.enqueue_item`) has
    no orphaned temp files to clean up, and the first time this runs should still be a decision,
    not a surprise sweep of a directory someone is mid-transfer into with an unusually long
    stall.
    """

    enabled: bool = False
    max_age_days: float = local_scan.ORPHAN_TEMP_FILE_DEFAULT_MAX_AGE_DAYS


async def load_orphan_temp_cleanup_settings(db: aiosqlite.Connection) -> OrphanTempCleanupSettings:
    cursor = await db.execute(
        "SELECT value FROM setting WHERE key = ?", (ORPHAN_TEMP_CLEANUP_SETTING_KEY,)
    )
    row = await cursor.fetchone()
    if row is None:
        return OrphanTempCleanupSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return OrphanTempCleanupSettings()
    return OrphanTempCleanupSettings(
        enabled=bool(data.get("enabled", False)),
        max_age_days=float(
            data.get("max_age_days", local_scan.ORPHAN_TEMP_FILE_DEFAULT_MAX_AGE_DAYS)
        ),
    )


async def save_orphan_temp_cleanup_settings(
    db: aiosqlite.Connection, settings: OrphanTempCleanupSettings
) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            ORPHAN_TEMP_CLEANUP_SETTING_KEY,
            json.dumps({"enabled": settings.enabled, "max_age_days": settings.max_age_days}),
        ),
    )
    await db.commit()


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
        # 2026-08-13: orphan temp-file cleanup rides the same hourly pass but is gated by its
        # own, independent setting -- an operator who wants one without the other (most will:
        # this is disk hygiene from a bug, not a data-lifecycle policy) must not have to accept
        # both or neither.
        await self._sweep_orphan_temp_files()

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

    async def _sweep_orphan_temp_files(self) -> None:
        """Disk-hygiene half of prompts/2026-08-13-lftp-timestamped-temp-files.md's task 4 --
        removes stale `.lftp`/`.lftp~<timestamp>~` leftovers (`local_scan.
        sweep_orphan_temp_files`'s own docstring covers why age alone is a safe guard) across
        every enabled queue, gated by `OrphanTempCleanupSettings.enabled` (default off, this
        module's own rule for anything that deletes).

        One queue at a time, off the event loop (`asyncio.to_thread`, matching
        `postprocess.py._sweep_failed_dirs`'s reasoning for `extract.sweep_failed_dirs`: a
        large tree walk must not block request handling or the WS delivery of anything else
        happening at the same time). No `item_delta` publish here -- unlike `delete_local`,
        an orphaned temp file was never its own visible node (`core/local_scan.py.scan_local`
        already folds it into its final name's entry, dead or alive), so there is no row on the
        Files page whose removal needs announcing; the `event` row is the whole audit trail.
        """
        settings = await load_orphan_temp_cleanup_settings(self.db)
        if not settings.enabled:
            return
        cursor = await self.db.execute("SELECT id, local_path FROM path_queue WHERE enabled = 1")
        queues = await cursor.fetchall()
        for queue in queues:
            removed = await asyncio.to_thread(
                local_scan.sweep_orphan_temp_files,
                Path(queue["local_path"].rstrip("/")),
                max_age_days=settings.max_age_days,
            )
            for path, age_days in removed:
                await audit.record_event(
                    self.db,
                    level="info",
                    kind="orphan_temp_file_removed",
                    message=(
                        f"queue {queue['id']}: removed stale lftp temp file {path} "
                        f"(age {age_days:.1f}d >= {settings.max_age_days}d)"
                    ),
                )
            if removed:
                logger.info(
                    "orphan-temp-cleanup: removed %d stale temp file(s) in queue %d",
                    len(removed),
                    queue["id"],
                )


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


@dataclass(frozen=True)
class ResetOutcome:
    """One reset call's outcome, covering all three scopes (a selected item, a whole queue, a
    pattern purge) with one shape -- deliberately never all-or-nothing for a multi-target scope:
    `withheld` is the parallel list of what a busy target's own guard refused, each with its own
    reason, while every other target in the same request still goes through. This mirrors
    `delete_local`'s per-target guard shape (and the Files page's existing `Promise.allSettled`
    bulk reporting) rather than failing an entire whole-queue reset because one item happened to
    be mid-transfer.

    `reset_top_level` counts the *root* targets actually reset (a selected item, or one matched
    top-level item for a queue/pattern scope) -- `len(affected_rel_paths)` is the real row count
    across every level, always `>= reset_top_level` once subtrees are expanded.
    """

    reset_top_level: int
    withheld: tuple[dict[str, str], ...]
    affected_rel_paths: tuple[str, ...]


async def _guard_busy(
    db: aiosqlite.Connection,
    item_id: int,
    *,
    in_flight_item_ids: frozenset[int],
    delete_in_flight: DeleteInFlight | None,
) -> str | None:
    """A withhold reason, or `None` if this target is clear to reset -- the same three checks
    `delete_local`'s guards 2/3 run, reused rather than re-derived (this module's own "refuse,
    don't race" paragraph above for why there is no fourth check calling `stop_item()`).
    """
    cursor = await db.execute(
        "SELECT 1 FROM job WHERE item_id = ? AND state IN ('queued', 'running') LIMIT 1",
        (item_id,),
    )
    if await cursor.fetchone() is not None:
        return "an active job exists for this item"
    if item_id in in_flight_item_ids:
        return "a post-processing worker is currently running for this item"
    if delete_in_flight is not None and item_id in delete_in_flight.in_flight_item_ids():
        return "a delete is currently removing this item's local files"
    return None


async def _subtree_deleted_archive_paths(
    db: aiosqlite.Connection, *, queue_id: int, rel_path: str
) -> list[str]:
    """`deleted_archive`'s own subtree membership, computed independently of `item` -- this is
    the fix for the trap this task named by name. Under normal operation a spent archive volume
    still carries its own `item` row (state `EXCLUDED`, DESIGN.md §3.2 rule 8 -- `reconcile()`
    marks a file `EXCLUDED` rather than dropping its node), so `_subtree_rows` alone would
    usually already catch it. But `deleted_archive` has no foreign key to `item.id` at all
    (migration 010's own docstring -- it cascades from `path_queue`, not `item`), so nothing
    guarantees that row still exists at reset time, and a reset that only ever looked at what
    `_subtree_rows` happened to return would silently miss a `deleted_archive` row for a path
    with no matching `item` row. Matched the identical way `_subtree_rows` matches (`rel_path ==
    target` or a genuine `target/`-prefixed child), for the identical reason (a raw SQL `LIKE`
    both over-matches a `target-extra` sibling and treats `_` as a wildcard).
    """
    cursor = await db.execute(
        "SELECT rel_path FROM deleted_archive WHERE queue_id = ?", (queue_id,)
    )
    rows = await cursor.fetchall()
    prefix = rel_path + "/"
    return [
        row["rel_path"]
        for row in rows
        if row["rel_path"] == rel_path or row["rel_path"].startswith(prefix)
    ]


async def _reset_rows(db: aiosqlite.Connection, queue_id: int, rel_paths: Sequence[str]) -> None:
    """The actual forgetting -- `item`, `item_settle`, `deleted_archive`, exactly the three
    tables this module's own section docstring says are keyed to `(queue_id, rel_path)`. One
    call per target's whole subtree (`rel_paths` is the *union* of `_subtree_rows`'s and
    `_subtree_deleted_archive_paths`'s output -- see `_reset_targets`); the caller batches every
    target's rows into one list before calling this so the whole scope's reset is one
    transaction.

    `item_settle` only ever has a row for a *top-level* `rel_path` (migration 007's own
    docstring), so this deletes a no-op for every nested path in `rel_paths` -- cheaper to let
    SQLite match nothing than to split the list by depth first. `deleted_archive` is the
    opposite: its rows are individual (often nested) file paths, so the same unsplit list is
    exactly what it needs.
    """
    if not rel_paths:
        return
    placeholders = ",".join("?" for _ in rel_paths)
    for table in ("item", "item_settle", "deleted_archive"):
        await db.execute(
            f"DELETE FROM {table} WHERE queue_id = ? AND rel_path IN ({placeholders})",  # noqa: S608 - table name is a fixed tuple, placeholders only
            (queue_id, *rel_paths),
        )


async def _reset_targets(
    db: aiosqlite.Connection,
    *,
    queue: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
    caller: str,
    in_flight_item_ids: frozenset[int],
    delete_in_flight: DeleteInFlight | None,
) -> ResetOutcome:
    """The one primitive behind `reset_item`/`reset_scope` -- `targets` is every root this call
    should attempt (`{"id": ..., "rel_path": ...}`, any depth), independently guarded so one busy
    item never blocks the rest of a whole-queue or pattern-purge request. Every resettable
    target's *whole subtree* is forgotten (`_subtree_rows`, the identical expansion
    `delete_local` uses for the identical reason: a directory's descendant rows must not survive
    with a stale identity once their parent's is forgotten).

    One transaction for the whole call: every target's rows are queued up first, then written
    and committed together, so a whole-queue reset is atomic from the caller's perspective even
    though it iterates many targets -- a crash partway through leaves either the pre-reset state
    or (once the single `commit()` below runs) the fully-reset one, never a queue half forgotten
    with no record of which half.
    """
    withheld: list[dict[str, str]] = []
    reset_root_ids: list[int] = []
    all_affected: list[str] = []

    for target in targets:
        item_id = target["id"]
        rel_path = target["rel_path"]
        reason = await _guard_busy(
            db, item_id, in_flight_item_ids=in_flight_item_ids, delete_in_flight=delete_in_flight
        )
        if reason is not None:
            withheld.append({"rel_path": rel_path, "reason": reason})
            await audit.record_event(
                db,
                level="warning",
                item_id=item_id,
                kind="item_reset_withheld",
                message=(
                    f"{caller}: reset of {rel_path!r} (queue {queue['id']} '{queue['name']}') "
                    f"withheld -- {reason}"
                ),
            )
            continue
        subtree = await _subtree_rows(db, queue_id=queue["id"], rel_path=rel_path)
        archive_paths = await _subtree_deleted_archive_paths(
            db, queue_id=queue["id"], rel_path=rel_path
        )
        # The union, not either alone -- this module's own section docstring ("the trap") and
        # `_subtree_deleted_archive_paths`'s docstring for why `deleted_archive` needs its own
        # independent subtree lookup rather than trusting whatever `_subtree_rows` happened to
        # find in `item`.
        affected = sorted({row["rel_path"] for row in subtree} | set(archive_paths))
        if not affected:
            continue
        await _reset_rows(db, queue["id"], affected)
        all_affected.extend(affected)
        reset_root_ids.append(item_id)

    if all_affected:
        await db.commit()
        await audit.record_event(
            db,
            level="info",
            item_id=None,  # the rows this refers to no longer exist -- see this module's own
            # section docstring on why the FK would set this NULL a moment later regardless.
            kind="item_reset",
            message=(
                f"{caller}: reset tracking for {len(all_affected)} row(s) across "
                f"{len(reset_root_ids)} item(s) in queue {queue['id']} '{queue['name']}' -- "
                "item/item_settle/deleted_archive rows forgotten, local files untouched"
            ),
        )
        # No WS publish here, deliberately: unlike `delete_local` (which updates rows that
        # still exist), a reset removes the row outright, and only `Engine` can evict its own
        # `self.models` cache -- publishing from here without that eviction would tell a
        # connected browser the row is gone while `Engine`'s next scan still thinks it's there.
        # `core/engine.py.Engine.forget_rel_paths` is the API layer's job to call
        # (`api/jobs.py`) with `affected_rel_paths` below, once this transaction has committed.

    return ResetOutcome(
        reset_top_level=len(reset_root_ids),
        withheld=tuple(withheld),
        affected_rel_paths=tuple(all_affected),
    )


async def reset_item(
    db: aiosqlite.Connection,
    *,
    item: Mapping[str, Any],
    queue: Mapping[str, Any],
    caller: str,
    in_flight_item_ids: frozenset[int],
    delete_in_flight: DeleteInFlight | None = None,
) -> ResetOutcome:
    """Reset one item (the Files-page single-row and multi-select-bulk scopes -- a bulk reset is
    this same call once per selected item, the identical `Promise.allSettled` shape
    `FileTree.tsx` already uses for bulk Delete, not a second bulk endpoint). `item` can be any
    depth -- a top-level directory/loose file or a nested file the user selected directly -- its
    whole subtree beneath `rel_path` is what actually gets forgotten (`_reset_targets`).
    """
    return await _reset_targets(
        db,
        queue=queue,
        targets=[{"id": item["id"], "rel_path": item["rel_path"]}],
        caller=caller,
        in_flight_item_ids=in_flight_item_ids,
        delete_in_flight=delete_in_flight,
    )


async def reset_queue_targets(db: aiosqlite.Connection, *, queue_id: int) -> list[aiosqlite.Row]:
    """Every top-level item in `queue_id` -- the All scope's whole candidate set, and the exact
    enumeration `reset_queue` below executes against. The All-scope preview endpoint
    (`api/jobs.py.reset_all_preview`) calls this function directly, never a second `SELECT` that
    happens to match it today, so "what the preview showed" and "what got reset" can never drift
    apart -- the identical invariant `reset_pattern_matches`' own docstring states for the
    pattern scope, and copied here for the same reason.

    **The bug this closes** (2026-08-14, `prompts/2026-08-14-reset-all-preview-undercounts.md`):
    before this function existed, the All scope's preview was improvised client-side from the
    *published* Files tree (the `nodes` prop), which `core/engine.py` (`a4a626d`) deliberately
    stops publishing a row from once it resolves to a terminal removed state
    (`REMOVED_LOCAL`/`REMOVED_BOTH`) with nothing left in either tree -- correct for the Files
    page, which should not show ghosts, but wrong for "everything this queue tracks." A
    `REMOVED_BOTH` row already off the wire was invisible to that improvised preview while
    `reset_queue`'s own `item`-table query reset it regardless: the preview undercounted the
    reset's actual blast radius.

    Same columns `reset_pattern_matches` selects (`is_dir`/`remote_size`/`local_size`) so the
    identical `ResetPatternPreviewItem` wire shape serves both scopes with no second schema.
    """
    cursor = await db.execute(
        "SELECT id, rel_path, is_dir, remote_size, local_size FROM item "
        "WHERE queue_id = ? AND instr(rel_path, '/') = 0",
        (queue_id,),
    )
    return await cursor.fetchall()


async def reset_queue(
    db: aiosqlite.Connection,
    *,
    queue: Mapping[str, Any],
    caller: str,
    in_flight_item_ids: frozenset[int],
    delete_in_flight: DeleteInFlight | None = None,
) -> ResetOutcome:
    """Reset every top-level item in `queue` -- the clean-slate scope. Every top-level `item` row
    (`reset_queue_targets`, the same "top-level only" idiom `_select_expired` above uses) is a
    target; each is independently guarded, so one item mid-transfer is withheld and reported
    while the rest of the queue still resets. Confirmation (typed queue name, since this is the
    most destructive action in the app) is the API layer's job (`api/jobs.py`), not this one's --
    a primitive that also had to know about a confirmation string would be harder to test and
    reuse than one that trusts its caller.
    """
    rows = await reset_queue_targets(db, queue_id=queue["id"])
    targets = [{"id": row["id"], "rel_path": row["rel_path"]} for row in rows]
    return await _reset_targets(
        db,
        queue=queue,
        targets=targets,
        caller=caller,
        in_flight_item_ids=in_flight_item_ids,
        delete_in_flight=delete_in_flight,
    )


async def reset_pattern_matches(
    db: aiosqlite.Connection, *, queue_id: int, pattern: str
) -> list[aiosqlite.Row]:
    """Every top-level item in `queue_id` whose own name matches `pattern` -- the preview *and*
    the execute path share this one query, so "what the preview showed" and "what got reset" can
    never drift apart (the same reason `delete_local`'s `dry_run` reuses every real guard rather
    than approximating them).

    `core/patterns.py.pattern_matches` -- the identical evaluator a `select` pattern uses against
    an item's own name (case-insensitive, glob when the string contains `*`/`?`/`[`, substring
    otherwise) -- is reused directly rather than building a `CompiledPatterns` for one ad-hoc
    string; DESIGN.md §12 requires there be exactly one matcher, and a purge that matched
    differently from auto-queue's own patterns would be genuinely dangerous, since a user typing
    a pattern here has every reason to assume it behaves the same way.

    Deliberately **single-queue, never cross-queue** (confirmed with the user rather than
    inferred): items are keyed `(queue_id, rel_path)`, and a pattern purge spanning every queue
    at once is a much bigger blast radius than "let me reuse this one release name on this one
    queue" ever asked for. There is no `queue_id: None` form of this function.
    """
    cursor = await db.execute(
        "SELECT id, rel_path, is_dir, remote_size, local_size FROM item "
        "WHERE queue_id = ? AND instr(rel_path, '/') = 0",
        (queue_id,),
    )
    rows = await cursor.fetchall()
    return [row for row in rows if patterns_core.pattern_matches(pattern, row["rel_path"])]


async def reset_by_pattern(
    db: aiosqlite.Connection,
    *,
    queue: Mapping[str, Any],
    pattern: str,
    caller: str,
    in_flight_item_ids: frozenset[int],
    delete_in_flight: DeleteInFlight | None = None,
) -> ResetOutcome:
    """Execute half of the purge-by-pattern scope -- `reset_pattern_matches` for the candidate
    set, then the same per-target guard-and-reset `reset_queue` uses. The live "what would this
    match" preview (`reset_pattern_matches` called directly, no reset performed) is this scope's
    own safety mechanism, per the task this shipped from: a typed pattern is far easier to get
    wrong than a checkbox selection, and matching everything by accident should be visible before
    it does anything, not after.
    """
    matches = await reset_pattern_matches(db, queue_id=queue["id"], pattern=pattern)
    targets = [{"id": row["id"], "rel_path": row["rel_path"]} for row in matches]
    return await _reset_targets(
        db,
        queue=queue,
        targets=targets,
        caller=caller,
        in_flight_item_ids=in_flight_item_ids,
        delete_in_flight=delete_in_flight,
    )
