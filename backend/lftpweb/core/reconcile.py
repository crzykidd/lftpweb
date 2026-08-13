"""Merge a remote tree and a local tree into the unified model (DESIGN.md §3.2).

Phase 2 implements rules 1, 2, and 4 — the ones that don't need a job engine or lifecycle
history (rules 3, 5, 6, 7 need job/queue state; rule 8 needs the pattern evaluator). Rule 8
lands in phase 4 (below); rules 3, 5, 6, 7 need persisted lifecycle history and live partly
in `core/engine.py._persist` (the "protected rows" and, from phase 4, the mount-gated
`REMOVED_LOCAL` grace period — see `core/mount_sentinel.py`) rather than in this pure
function. **The seam left here on purpose in phase 2**: `counts_predicate` decides whether a
given remote file counts toward its parent directory's completeness. Phase 2 always counted
everything (`default_counts_predicate`); phase 4 (`core/patterns.py.build_counts_predicate`)
swaps in a predicate that returns `False` for a file matching a `file_exclude` pattern, and
this module also now marks that file's own state `EXCLUDED` rather than `REMOTE_ONLY` — a
real state, not an absence (DESIGN.md §3.2 rule 8, §4.7): the file was never going to arrive,
on purpose, so it must not read as "missing."

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
# DESIGN.md §3.2 rule 8, §4.7: a file matched by a `file_exclude` pattern. Deliberately not
# "REMOTE_ONLY" — that would look like something waiting to be downloaded, when it is in
# fact never going to be, on purpose. Only ever produced for a non-directory node whose
# `counts_predicate` returns `False`; see `core/patterns.py.build_counts_predicate`.
STATE_EXCLUDED = "EXCLUDED"


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
    """One row of this module's output — a *candidate* reading of an item, which
    `core/engine.py._persist` arbitrates against the persisted one before anything is stored
    or published. `remote_size`/`local_size` are `None` when the side is absent.

    **`structural_state`, not `state`, and the name is load-bearing.** This field is what the
    remote-vs-local byte comparison says on its own, with no knowledge of a running job
    (`core/queue.py`), a post-processing outcome (`core/postprocess.py`) or §7.3's
    `REMOVED_LOCAL` grace period (`core/mount_sentinel.py`) — all three of which routinely
    win over it. Called `state`, it read as *the* state at every call site, and the engine
    duly published it to the WebSocket while writing something different to `item`; see
    `core/itemview.py` for what that cost. Publishing the structural reading now requires
    explicitly asking for it by name.
    """

    rel_path: str
    is_dir: bool
    structural_state: str
    remote_size: int | None
    local_size: int | None
    remote_mtime: float | None
    # The local-side counterpart to `remote_mtime` (2026-08-13,
    # prompts/2026-08-13-files-detail-inspector.md). Same convention, deliberately: files only,
    # `None` for a directory -- `remote_mtime` never had a directory reading either (see that
    # field's own line below), and inventing a different rule for the local side (newest child?
    # the directory inode's own mtime, which only moves on entry add/remove and says nothing
    # about *content*) would make the two sides answer different questions for no real gain.
    local_mtime: float | None


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
        # 2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md): `not local_entry.
        # is_temp` is load-bearing, not defensive. A local entry whose only on-disk form is
        # still a temp-suffixed name (`.lftp` or `.lftp~<timestamp>~`) has not been through
        # lftp's atomic rename yet, and its reported `size` can be wrong in ways a renamed
        # file's cannot (a missing/mismatched `.lftp-pget-status` sidecar falls back to a
        # sparse `st_size` that already reads as the full allocation; two processes racing the
        # same target -- the bug this task's root cause fixes -- can leave one mid-write). Size
        # alone used to be enough to call a file "complete" here; a large enough *orphaned* temp
        # file (no active job explains the gap) could satisfy `size >= entry.size` while the
        # real transfer was still incomplete, which on a `move`-mode queue is the path to
        # deleting the remote copy of a release that never actually finished. See
        # `core/local_scan.py.LocalEntry.is_temp`'s own docstring for the full reasoning.
        complete_own[path] = (
            1
            if (
                local_entry is not None
                and not local_entry.is_temp
                and local_entry.size >= entry.size
            )
            else 0
        )

    relevant_totals = _rollup(remote_tree, relevant_own)
    complete_totals = _rollup(remote_tree, complete_own)
    local_present_totals = _rollup(remote_tree, local_present_own)

    # Disambiguates rule 1's vacuous `relevant == 0` case below (§3.2 rule 8, §4.7 "Directories
    # with nothing left in them"). `relevant_totals` is 0 both when every remote file under a
    # directory was excluded by a `file_exclude` pattern *and* when the directory has no remote
    # files under it at all — the predicate is only ever asked about files that exist, so it
    # can't tell "excluded" from "nothing to exclude" apart. This rollup counts every remote
    # *file* under a directory before the predicate runs, so `remote_file_totals.get(path) == 0`
    # means genuinely nothing remote under here, full stop.
    remote_file_own: dict[str, int] = {p: (0 if e.is_dir else 1) for p, e in remote_tree.items()}
    remote_file_totals = _rollup(remote_tree, remote_file_own)

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
                    # Rule 1's vacuous case splits two ways that `relevant == 0` alone cannot
                    # tell apart (see `remote_file_totals` above):
                    if remote_file_totals.get(path, 0) == 0:
                        # No remote files anywhere under this directory — a genuinely empty
                        # remote directory (or one containing only other empty directories).
                        # DESIGN.md §3.2/§4.7 are silent on this exact case (rule 8's "vacuously
                        # DOWNLOADED" is about *excluded* children, not an absence of children);
                        # this task's decision follows the phase 2 precedent one branch up
                        # (LOCAL_ONLY) and the REMOTE_ONLY-over-PARTIAL call for zero local
                        # presence: not yet mirrored locally reads REMOTE_ONLY, so it is still
                        # eligible to be picked up (mirrored, then immediately DOWNLOADED on the
                        # next scan) rather than reading as permanently, vacuously done.
                        state = STATE_DOWNLOADED if local_entry is not None else STATE_REMOTE_ONLY
                    else:
                        # Remote files exist under here but every one of them was matched by a
                        # file_exclude pattern. §3.2 rule 8 / §4.7 "Directories with nothing
                        # left in them": vacuously DOWNLOADED regardless of local presence —
                        # lftp does not create a directory it has nothing to put in, so
                        # completeness must not require the local directory to exist. This is
                        # the load-bearing branch that stops a filtered release sitting
                        # incomplete and being re-queued on every auto-queue pass (phase 4,
                        # docs/decisions.md) — do not collapse it back into the branch above.
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
            if remote_entry is not None and not predicate(path, remote_entry):
                # DESIGN.md §3.2 rule 8: excluded, not missing. Applies regardless of
                # whatever local presence happens to exist (e.g. a file downloaded before an
                # exclude pattern was added) — the pattern is the current source of truth for
                # "does this file belong in the transfer," so the state reflects that rather
                # than a leftover local copy's size.
                state = STATE_EXCLUDED
            elif remote_entry is not None and local_entry is not None:
                # Rule 2: local_size < remote_size ⇒ PARTIAL, never DOWNLOADED, even with no
                # active job to explain the gap. `not local_entry.is_temp` (2026-08-13): a
                # still-temp-suffixed file is never DOWNLOADED regardless of its reported size
                # -- see `complete_own`'s comment above, and `LocalEntry.is_temp`'s own
                # docstring, for why size alone can lie for an unrenamed file.
                state = (
                    STATE_DOWNLOADED
                    if (local_entry.size >= remote_entry.size and not local_entry.is_temp)
                    else STATE_PARTIAL
                )
            elif remote_entry is not None:
                state = STATE_REMOTE_ONLY
            else:
                state = STATE_LOCAL_ONLY

        nodes[path] = ReconciledNode(
            rel_path=path,
            is_dir=is_dir,
            structural_state=state,
            remote_size=remote_size_totals.get(path) if remote_entry is not None else None,
            local_size=local_size_totals.get(path) if local_entry is not None else None,
            remote_mtime=remote_entry.mtime if (remote_entry is not None and not is_dir) else None,
            local_mtime=local_entry.mtime if (local_entry is not None and not is_dir) else None,
        )

    return nodes
