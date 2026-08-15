from __future__ import annotations

import pytest

from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.patterns import CompiledPatterns, Pattern, build_counts_predicate
from lftpweb.core.reconcile import (
    STATE_DOWNLOADED,
    STATE_EXCLUDED,
    STATE_LOCAL_ONLY,
    STATE_PARTIAL,
    STATE_REMOTE_ONLY,
    reconcile,
)
from lftpweb.core.remote import RemoteEntry


# --- File-level state table (DESIGN.md §3.2 rules 1, 2, 4) -----------------------------
# (remote present/size, local present/size) -> expected state
@pytest.mark.parametrize(
    "remote_size,local_size,expected",
    [
        (100, None, STATE_REMOTE_ONLY),  # remote only
        (100, 100, STATE_DOWNLOADED),  # exact match
        (100, 50, STATE_PARTIAL),  # rule 2: local < remote -> PARTIAL, never DOWNLOADED
        (100, 0, STATE_PARTIAL),  # zero bytes so far, still remote and still incomplete
        (100, 150, STATE_DOWNLOADED),  # local > remote (moving target settled higher) -> complete
        (0, 0, STATE_DOWNLOADED),  # a genuinely empty remote file, fully "downloaded"
    ],
)
def test_file_state_table(remote_size, local_size, expected):
    remote_tree = {
        "f.bin": RemoteEntry(rel_path="f.bin", is_dir=False, size=remote_size, mtime=1.0)
    }
    local_tree = (
        {"f.bin": LocalEntry(rel_path="f.bin", is_dir=False, size=local_size)}
        if local_size is not None
        else {}
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["f.bin"].structural_state == expected


def test_file_local_only_when_absent_remotely():
    remote_tree: dict = {}
    local_tree = {"orphan.txt": LocalEntry(rel_path="orphan.txt", is_dir=False, size=10)}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["orphan.txt"].structural_state == STATE_LOCAL_ONLY


# --- `is_temp` (2026-08-13, prompts/2026-08-13-lftp-timestamped-temp-files.md): a still-temp
# entry can never satisfy completeness, no matter what its `size` claims -- this is what
# protects a `move`-mode queue from deleting the remote copy of a release that never actually
# finished, just because an orphaned `.lftp`/`.lftp~<timestamp>~` file's reported size (a
# sparse `st_size` with no sidecar to correct it, or a race between two writers) happened to
# reach or exceed the remote size. ------------------------------------------------------------


def test_orphaned_temp_file_at_or_above_remote_size_reads_partial_not_downloaded():
    remote_tree = {"f.bin": RemoteEntry(rel_path="f.bin", is_dir=False, size=100, mtime=1.0)}
    local_tree = {
        "f.bin": LocalEntry(rel_path="f.bin", is_dir=False, size=150, is_temp=True),
    }
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["f.bin"].structural_state == STATE_PARTIAL


def test_a_directory_with_only_an_orphaned_temp_file_never_reads_downloaded():
    """The exact danger this task's fix protects against: an abandoned temp file whose
    reported size reaches the remote size must not make the *directory* read DOWNLOADED --
    on a `move`-mode queue, that state is what triggers verify -> delete-remote -> extract for
    a release that was never actually complete.
    """
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/movie.mkv", False, 1000, is_temp=True),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release/movie.mkv"].structural_state == STATE_PARTIAL
    assert nodes["Release"].structural_state != STATE_DOWNLOADED


def test_a_directory_reads_downloaded_once_the_temp_file_has_actually_been_renamed():
    """Same bytes, `is_temp=False` this time (the real, post-rename final file) -- confirms the
    fix is scoped to "still a temp file," not an unconditional regression that would make a
    completed download permanently unable to reach DOWNLOADED.
    """
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/movie.mkv", False, 1000, is_temp=False),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release/movie.mkv"].structural_state == STATE_DOWNLOADED
    assert nodes["Release"].structural_state == STATE_DOWNLOADED


# --- Directory-level state (rule 1: every non-dir descendant with a remote size must be
# complete, else PARTIAL; rule 4: remote size is never latched, recomputed each call) -----


def _tree(*entries: RemoteEntry) -> dict[str, RemoteEntry]:
    return {e.rel_path: e for e in entries}


def _local(*entries: LocalEntry) -> dict[str, LocalEntry]:
    return {e.rel_path: e for e in entries}


def test_directory_downloaded_only_when_every_child_complete():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/a.txt", False, 100, 1.0),
        RemoteEntry("Release/b.txt", False, 100, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/a.txt", False, 100),
        LocalEntry("Release/b.txt", False, 100),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].structural_state == STATE_DOWNLOADED


def test_directory_partial_when_one_child_incomplete():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/a.txt", False, 100, 1.0),
        RemoteEntry("Release/b.txt", False, 100, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/a.txt", False, 100),
        LocalEntry("Release/b.txt", False, 40),  # still partial
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].structural_state == STATE_PARTIAL
    # local < remote -> the child itself is PARTIAL too, never DOWNLOADED (rule 2)
    assert nodes["Release/b.txt"].structural_state == STATE_PARTIAL


def test_directory_remote_only_with_zero_local_presence():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/a.txt", False, 100, 1.0),
    )
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].structural_state == STATE_REMOTE_ONLY


def test_directory_empty_remote_no_local_copy_is_remote_only():
    # A genuinely empty remote directory that has never been mirrored locally is not
    # vacuously DOWNLOADED -- that hid it from ever being created (the empty-remote-directory
    # bug this task fixes). Not-yet-mirrored reads REMOTE_ONLY, matching every other
    # zero-local-presence case in this module; once mirrored (there's nothing to put inside,
    # but the directory itself gets created), the next scan reconciles it to DOWNLOADED --
    # see the test below.
    remote_tree = _tree(RemoteEntry("EmptyRelease", True, 0, 1.0))
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["EmptyRelease"].structural_state == STATE_REMOTE_ONLY


def test_directory_empty_remote_already_mirrored_locally_is_downloaded():
    # Same empty remote directory, but it has already been mirrored (lftp created the empty
    # directory locally, or a user did) -- nothing outstanding, so it is DOWNLOADED.
    remote_tree = _tree(RemoteEntry("EmptyRelease", True, 0, 1.0))
    local_tree = _local(LocalEntry("EmptyRelease", True))
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["EmptyRelease"].structural_state == STATE_DOWNLOADED


def test_directory_all_children_excluded_still_downloaded_regression_guard_for_requeue_loop():
    # Regression guard: a directory whose children are ALL excluded by a file_exclude pattern
    # must stay DOWNLOADED even though it has zero local presence -- it must NOT be confused
    # with the genuinely-empty-directory case above just because both have `relevant == 0`.
    # Getting this wrong resurrects the infinite re-queue loop §4.7/§3.2 rule 8 exists to
    # prevent: a filtered release would sit incomplete and be re-queued on every auto-queue
    # pass (docs/decisions.md, phase 4). The two are told apart via `remote_file_totals`
    # (remote files exist here, just none of them count), never via local presence.
    remote_tree = _tree(
        RemoteEntry("AllSamples", True, 0, 1.0),
        RemoteEntry("AllSamples/a.sample.mkv", False, 100, 1.0),
    )
    local_tree: dict = {}  # lftp never creates a directory it has nothing to put in
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*sample*")])
    predicate = build_counts_predicate(compiled)

    nodes = reconcile(remote_tree, local_tree, counts_predicate=predicate)
    assert nodes["AllSamples"].structural_state == STATE_DOWNLOADED


def test_nested_empty_directories_each_follow_the_same_rule_independently():
    # A remote directory containing only other empty directories -- no special case needed,
    # the same predicate falls out at every depth. Neither has been mirrored locally, so both
    # read REMOTE_ONLY.
    remote_tree = _tree(
        RemoteEntry("Outer", True, 0, 1.0),
        RemoteEntry("Outer/Inner", True, 0, 1.0),
    )
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Outer"].structural_state == STATE_REMOTE_ONLY
    assert nodes["Outer/Inner"].structural_state == STATE_REMOTE_ONLY

    # Once the whole chain has been mirrored, both read DOWNLOADED -- an empty directory that
    # has been mirrored is complete, independent of depth.
    local_tree_mirrored = _local(
        LocalEntry("Outer", True),
        LocalEntry("Outer/Inner", True),
    )
    nodes_mirrored = reconcile(remote_tree, local_tree_mirrored)
    assert nodes_mirrored["Outer"].structural_state == STATE_DOWNLOADED
    assert nodes_mirrored["Outer/Inner"].structural_state == STATE_DOWNLOADED


def test_directory_local_only_when_absent_remotely_entirely():
    remote_tree: dict = {}
    local_tree = _local(
        LocalEntry("MyStuff", True),
        LocalEntry("MyStuff/notes.txt", False, 10),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["MyStuff"].structural_state == STATE_LOCAL_ONLY
    assert nodes["MyStuff/notes.txt"].structural_state == STATE_LOCAL_ONLY


def test_nested_directory_completeness_propagates_up():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/Subs", True, 0, 1.0),
        RemoteEntry("Release/Subs/eng.srt", False, 50, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
    )
    # Everything downloaded except the deeply nested subtitle file.
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/Subs", True),
        LocalEntry("Release/Subs/eng.srt", False, 10),
        LocalEntry("Release/movie.mkv", False, 1000),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release/Subs"].structural_state == STATE_PARTIAL
    assert (
        nodes["Release"].structural_state == STATE_PARTIAL
    )  # incompleteness propagates to the root


def test_directory_size_rollup_sums_children_not_own_reported_size():
    # Remote "own size" for a directory entry (e.g. find's %s for the dir inode itself) must
    # be ignored — the total is the sum of descendant file sizes.
    remote_tree = _tree(
        RemoteEntry("Release", True, 4096, 1.0),  # 4096 = a directory inode's own on-disk size
        RemoteEntry("Release/a.txt", False, 100, 1.0),
        RemoteEntry("Release/b.txt", False, 250, 1.0),
    )
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].remote_size == 350


# --- local_mtime (2026-08-13, prompts/2026-08-13-files-detail-inspector.md): the local-side --
# --- counterpart to `remote_mtime` -- same files-only convention, deliberately -----------------


def test_local_mtime_carried_through_for_a_file():
    remote_tree = _tree(RemoteEntry("f.bin", False, 100, 1.0))
    local_tree = _local(LocalEntry("f.bin", False, 100, 1_700_000_000.0))
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["f.bin"].local_mtime == 1_700_000_000.0


def test_local_mtime_none_when_no_local_copy():
    remote_tree = _tree(RemoteEntry("f.bin", False, 100, 1.0))
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["f.bin"].local_mtime is None


def test_local_mtime_none_for_a_directory_even_though_it_has_local_children():
    # Files only, mirroring `remote_mtime`'s own convention (`reconcile`'s own docstring on
    # `ReconciledNode.local_mtime`) -- a directory's row never carries a `local_mtime`,
    # regardless of what its children report.
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/a.txt", False, 100, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/a.txt", False, 100, 1_700_000_000.0),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].local_mtime is None
    assert nodes["Release/a.txt"].local_mtime == 1_700_000_000.0


def test_local_mtime_none_for_a_local_only_directory():
    remote_tree: dict = {}
    local_tree = _local(LocalEntry("MyStuff", True, 0, 1_700_000_000.0))
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["MyStuff"].local_mtime is None


def test_remote_size_never_latched_recomputed_each_call():
    # DESIGN.md §3.2 rule 4: a torrent may still be growing on the seedbox. Two separate
    # reconcile() calls over different remote sizes must each reflect the size passed in,
    # never a value cached from an earlier call.
    local_tree = _local(LocalEntry("f.bin", False, 100))

    first = reconcile(_tree(RemoteEntry("f.bin", False, 100, 1.0)), local_tree)
    assert first["f.bin"].structural_state == STATE_DOWNLOADED

    grown = reconcile(_tree(RemoteEntry("f.bin", False, 500, 1.0)), local_tree)
    assert grown["f.bin"].structural_state == STATE_PARTIAL
    assert grown["f.bin"].remote_size == 500


def test_counts_predicate_seam_excludes_a_file_from_completeness():
    # Simulates what phase 4's pattern-aware predicate will do: a file that doesn't count
    # toward completeness must not block its directory from reaching DOWNLOADED, and must
    # not appear as locally-missing evidence either.
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
        RemoteEntry("Release/notes.nfo", False, 5, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/movie.mkv", False, 1000),
        # notes.nfo deliberately never downloaded (as if excluded)
    )

    def exclude_nfo(rel_path: str, entry: RemoteEntry) -> bool:  # noqa: ARG001
        return not rel_path.endswith(".nfo")

    without_predicate = reconcile(remote_tree, local_tree)
    assert (
        without_predicate["Release"].structural_state == STATE_PARTIAL
    )  # the naive/pre-phase-4 result

    with_predicate = reconcile(remote_tree, local_tree, counts_predicate=exclude_nfo)
    assert with_predicate["Release"].structural_state == STATE_DOWNLOADED


# --- Phase 4: core/patterns.py wired through the counts_predicate seam (DESIGN.md §4.7,
# §3.2 rule 8) -- the single highest-value test in the phase, per its own prompt: a
# `file_exclude` of `*.nfo` must leave its release DOWNLOADED, never permanently PARTIAL. ---


def test_file_exclude_pattern_leaves_release_downloaded_not_partial():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
        RemoteEntry("Release/notes.nfo", False, 5, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/movie.mkv", False, 1000),
        # notes.nfo never arrives -- lftp was given --exclude-glob '*.nfo' for it.
    )
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    predicate = build_counts_predicate(compiled)

    nodes = reconcile(remote_tree, local_tree, counts_predicate=predicate)

    assert nodes["Release"].structural_state == STATE_DOWNLOADED
    assert nodes["Release/movie.mkv"].structural_state == STATE_DOWNLOADED
    # EXCLUDED, not REMOTE_ONLY -- a real state, not an absence (§3.2 rule 8).
    assert nodes["Release/notes.nfo"].structural_state == STATE_EXCLUDED


def test_directory_whose_children_are_all_excluded_is_downloaded_without_a_local_directory():
    # lftp does not create a directory it has nothing to put in, so the local directory may
    # legitimately not exist at all -- completeness must not require it (§4.7, §3.2 rule 8).
    remote_tree = _tree(
        RemoteEntry("AllSamples", True, 0, 1.0),
        RemoteEntry("AllSamples/a.sample.mkv", False, 100, 1.0),
        RemoteEntry("AllSamples/b.sample.mkv", False, 200, 1.0),
    )
    local_tree: dict = {}  # nothing local at all -- not even the directory itself
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*sample*")])
    predicate = build_counts_predicate(compiled)

    nodes = reconcile(remote_tree, local_tree, counts_predicate=predicate)

    assert nodes["AllSamples"].structural_state == STATE_DOWNLOADED
    assert nodes["AllSamples"].local_size is None  # never scanned locally -- never existed
    assert nodes["AllSamples/a.sample.mkv"].structural_state == STATE_EXCLUDED
    assert nodes["AllSamples/b.sample.mkv"].structural_state == STATE_EXCLUDED


def test_a_loose_top_level_file_matching_file_exclude_is_also_excluded_not_remote_only():
    remote_tree = _tree(RemoteEntry("notes.nfo", False, 5, 1.0))
    local_tree: dict = {}
    compiled = CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    predicate = build_counts_predicate(compiled)

    nodes = reconcile(remote_tree, local_tree, counts_predicate=predicate)
    assert nodes["notes.nfo"].structural_state == STATE_EXCLUDED


def test_changing_file_exclude_patterns_retroactively_changes_completeness_both_ways():
    # DESIGN.md §4.7: "changing file_excludes retroactively changes completeness. Tightening
    # them can make a DOWNLOADED item incomplete; loosening them can complete a PARTIAL one."
    # Same fixture, same local disk contents throughout -- only the pattern set changes, and
    # a file that was never fetched (because it used to be excluded) stays not-fetched.
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/movie.mkv", False, 1000, 1.0),
        RemoteEntry("Release/notes.nfo", False, 5, 1.0),
    )
    local_tree = _local(
        LocalEntry("Release", True),
        LocalEntry("Release/movie.mkv", False, 1000),
        # notes.nfo was never fetched -- it was excluded when this was downloaded.
    )

    no_patterns = build_counts_predicate(CompiledPatterns.compile([]))
    with_nfo_excluded = build_counts_predicate(
        CompiledPatterns.compile([Pattern(kind="file_exclude", expr="*.nfo")])
    )

    # Loosening (adding the exclude): a PARTIAL directory (nfo counts, is missing) becomes
    # vacuously DOWNLOADED once nfo no longer counts at all.
    assert reconcile(remote_tree, local_tree, counts_predicate=no_patterns)[
        "Release"
    ].structural_state == (STATE_PARTIAL)
    assert (
        reconcile(remote_tree, local_tree, counts_predicate=with_nfo_excluded)[
            "Release"
        ].structural_state
        == STATE_DOWNLOADED
    )

    # Tightening (removing that exclude on the next scan, patterns changed back): the exact
    # same on-disk contents now read PARTIAL again, because notes.nfo counts once more and
    # was never actually fetched -- nothing miraculously appeared on disk just because a
    # pattern changed.
    assert reconcile(remote_tree, local_tree, counts_predicate=no_patterns)[
        "Release"
    ].structural_state == (STATE_PARTIAL)
