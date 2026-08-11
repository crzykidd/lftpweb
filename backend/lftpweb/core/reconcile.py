"""Merge a remote tree and a local tree into the unified model (DESIGN.md §3.2).

Phase 2 implements rules 1, 2, and 4 — the ones that don't need a job engine or lifecycle
history (rules 3, 5, 6, 7 need job/queue state; rule 8 needs the pattern evaluator). Both land
in phase 3/4. **The seam is left here on purpose**: `counts_predicate` decides whether a given
remote file counts toward its parent directory's completeness. Phase 2 always counts
everything (`default_counts_predicate`); phase 4 swaps in a predicate that returns `False` for
an `EXCLUDED` file, without this module changing shape.

States produced this phase: `REMOTE_ONLY`, `LOCAL_ONLY`, `PARTIAL`, `DOWNLOADED` — the phase 2
prompt's explicit scope, since `QUEUED`/`DOWNLOADING`/`STOPPED`/`FAILED` need a job engine that
doesn't exist yet, and `REMOVED_LOCAL`/`REMOVED_BOTH` (§3.2 rule 3) need persisted lifecycle
history that phase 3/4 add.

**Never latch remote size (rule 4).** This module is a pure function of its two input trees —
whatever `remote_tree`/`local_tree` a caller passes in *is* "the current scan"; there is no
cached size read from a previous call. The engine (`core/engine.py`) is what must call this
fresh on every scan rather than diffing against a stored total, and that discipline lives
there, not here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.remote import RemoteEntry

STATE_REMOTE_ONLY = "REMOTE_ONLY"
STATE_LOCAL_ONLY = "LOCAL_ONLY"
STATE_PARTIAL = "PARTIAL"
STATE_DOWNLOADED = "DOWNLOADED"


class _SizedEntry(Protocol):
    is_dir: bool
    size: int


CountsPredicate = Callable[[str, RemoteEntry], bool]


def default_counts_predicate(rel_path: str, entry: RemoteEntry) -> bool:  # noqa: ARG001
    """Phase 2: every remote file counts toward its directory's completeness. Phase 4
    replaces this with one that excludes `EXCLUDED` files (DESIGN.md §4.7, §3.2 rule 8) —
    callers should depend on the signature, not on this always returning `True`.
    """
    return True


@dataclass(frozen=True)
class ReconciledNode:
    """One row of the unified model — what `core/engine.py` persists to `item` and what the
    Files page renders. `remote_size`/`local_size` are `None` when the side is absent.
    """

    rel_path: str
    is_dir: bool
    state: str
    remote_size: int | None
    local_size: int | None
    remote_mtime: float | None


def _parent(rel_path: str) -> str | None:
    if "/" not in rel_path:
        return None
    return rel_path.rsplit("/", 1)[0]


def _rollup(entries: Mapping[str, _SizedEntry], contributions: dict[str, int]) -> dict[str, int]:
    """Sum each node's own contribution up into every ancestor. `contributions` holds each
    node's own value (its size for a file, 0 to start for a directory); processing deepest
    paths first means a directory's accumulated total is complete by the time it is added
    into *its* parent, without recursion.
    """
    totals = dict(contributions)
    for path in sorted(entries, key=lambda p: p.count("/"), reverse=True):
        parent = _parent(path)
        if parent is not None and parent in totals:
            totals[parent] += totals[path]
    return totals


def reconcile(
    remote_tree: Mapping[str, RemoteEntry],
    local_tree: Mapping[str, LocalEntry],
    counts_predicate: CountsPredicate | None = None,
) -> dict[str, ReconciledNode]:
    """Merge one queue's remote and local scans into the unified per-path state.

    Both trees are keyed by POSIX-style `rel_path` relative to the queue's roots — the
    caller (`core/engine.py`) is responsible for scanning each side and root-relativizing
    before calling this; `reconcile` itself does no I/O and no path resolution.
    """
    predicate = counts_predicate or default_counts_predicate

    all_paths: set[str] = set(remote_tree) | set(local_tree)
    is_dir_by_path: dict[str, bool] = {}
    for path in all_paths:
        remote_entry = remote_tree.get(path)
        local_entry = local_tree.get(path)
        # Prefer the remote side's type when both exist and (rarely) disagree — remote is
        # the side we can't rescan on demand mid-decision, local always will be.
        is_dir_by_path[path] = (remote_entry or local_entry).is_dir  # type: ignore[union-attr]

    # Display totals: every remote/local byte under a directory, irrespective of the
    # completeness predicate — this is "how much is there," not "how much counts."
    remote_size_totals = _rollup(
        remote_tree, {p: (0 if e.is_dir else e.size) for p, e in remote_tree.items()}
    )
    local_size_totals = _rollup(
        local_tree, {p: (0 if e.is_dir else e.size) for p, e in local_tree.items()}
    )

    # Completeness accounting for rule 1: for every remote *file* that counts (per
    # `predicate`), is it complete, and is there any local copy of it at all. Rolled up the
    # same way as sizes, so a directory's totals already reflect its whole subtree.
    relevant_own: dict[str, int] = {}
    complete_own: dict[str, int] = {}
    local_present_own: dict[str, int] = {}
    for path, entry in remote_tree.items():
        if entry.is_dir:
            relevant_own[path] = 0
            complete_own[path] = 0
            local_present_own[path] = 0
            continue
        if not predicate(path, entry):
            relevant_own[path] = 0
            complete_own[path] = 0
            local_present_own[path] = 0
            continue
        local_entry = local_tree.get(path)
        relevant_own[path] = 1
        local_present_own[path] = 1 if local_entry is not None else 0
        complete_own[path] = 1 if (local_entry is not None and local_entry.size >= entry.size) else 0

    relevant_totals = _rollup(remote_tree, relevant_own)
    complete_totals = _rollup(remote_tree, complete_own)
    local_present_totals = _rollup(remote_tree, local_present_own)

    nodes: dict[str, ReconciledNode] = {}
    for path in all_paths:
        is_dir = is_dir_by_path[path]
        remote_entry = remote_tree.get(path)
        local_entry = local_tree.get(path)

        if is_dir:
            if remote_entry is None:
                # Never seen remotely — regardless of what's beneath it, this directory (and
                # everything under it) is local-only. §3.2's LOCAL_ONLY, applied at the
                # directory level so an entirely-local subtree isn't vacuously "DOWNLOADED".
                state = STATE_LOCAL_ONLY
            else:
                relevant = relevant_totals.get(path, 0)
                complete = complete_totals.get(path, 0)
                local_present = local_present_totals.get(path, 0)
                if relevant == 0:
                    # Rule 1's vacuous case: nothing under this directory counts toward
                    # completeness (empty, or — once phase 4 wires the predicate — every
                    # child excluded). §4.7: "vacuously DOWNLOADED", not PARTIAL.
                    state = STATE_DOWNLOADED
                elif complete == relevant:
                    state = STATE_DOWNLOADED
                elif local_present == 0:
                    # DESIGN.md §3.2 rule 1 only says "otherwise PARTIAL" for an incomplete
                    # directory, without distinguishing zero progress from partial progress.
                    # A directory with *no* local presence at all reads far more usefully as
                    # REMOTE_ONLY than PARTIAL — see the phase 2 report's design-gap note.
                    state = STATE_REMOTE_ONLY
                else:
                    state = STATE_PARTIAL
        else:
            if remote_entry is not None and local_entry is not None:
                # Rule 2: local_size < remote_size ⇒ PARTIAL, never DOWNLOADED, even with no
                # active job to explain the gap.
                state = STATE_DOWNLOADED if local_entry.size >= remote_entry.size else STATE_PARTIAL
            elif remote_entry is not None:
                state = STATE_REMOTE_ONLY
            else:
                state = STATE_LOCAL_ONLY

        nodes[path] = ReconciledNode(
            rel_path=path,
            is_dir=is_dir,
            state=state,
            remote_size=remote_size_totals.get(path) if remote_entry is not None else None,
            local_size=local_size_totals.get(path) if local_entry is not None else None,
            remote_mtime=remote_entry.mtime if (remote_entry is not None and not is_dir) else None,
        )

    return nodes
