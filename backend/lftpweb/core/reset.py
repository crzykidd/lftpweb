"""Reset item tracking: forget an item's rows (`item`, `item_settle`, `deleted_archive`)
without touching a byte on disk, so a once-suppressed path becomes reusable. Not a delete.
Split out of `core/local_delete.py` (audit P3)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite

from lftpweb.core import audit
from lftpweb.core import patterns as patterns_core

from lftpweb.core.local_delete import DeleteInFlight, _subtree_rows

logger = logging.getLogger(__name__)


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
