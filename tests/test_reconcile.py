from __future__ import annotations

import pytest

from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.reconcile import (
    STATE_DOWNLOADED,
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
    assert nodes["f.bin"].state == expected


def test_file_local_only_when_absent_remotely():
    remote_tree: dict = {}
    local_tree = {"orphan.txt": LocalEntry(rel_path="orphan.txt", is_dir=False, size=10)}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["orphan.txt"].state == STATE_LOCAL_ONLY


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
    assert nodes["Release"].state == STATE_DOWNLOADED


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
    assert nodes["Release"].state == STATE_PARTIAL
    # local < remote -> the child itself is PARTIAL too, never DOWNLOADED (rule 2)
    assert nodes["Release/b.txt"].state == STATE_PARTIAL


def test_directory_remote_only_with_zero_local_presence():
    remote_tree = _tree(
        RemoteEntry("Release", True, 0, 1.0),
        RemoteEntry("Release/a.txt", False, 100, 1.0),
    )
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["Release"].state == STATE_REMOTE_ONLY


def test_directory_vacuously_downloaded_when_empty():
    # A directory with no descendant files at all (only sub-dirs, or truly empty) is
    # vacuously complete — nothing outstanding to fetch.
    remote_tree = _tree(RemoteEntry("EmptyRelease", True, 0, 1.0))
    local_tree: dict = {}
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["EmptyRelease"].state == STATE_DOWNLOADED


def test_directory_local_only_when_absent_remotely_entirely():
    remote_tree: dict = {}
    local_tree = _local(
        LocalEntry("MyStuff", True),
        LocalEntry("MyStuff/notes.txt", False, 10),
    )
    nodes = reconcile(remote_tree, local_tree)
    assert nodes["MyStuff"].state == STATE_LOCAL_ONLY
    assert nodes["MyStuff/notes.txt"].state == STATE_LOCAL_ONLY


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
    assert nodes["Release/Subs"].state == STATE_PARTIAL
    assert nodes["Release"].state == STATE_PARTIAL  # incompleteness propagates to the root


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


def test_remote_size_never_latched_recomputed_each_call():
    # DESIGN.md §3.2 rule 4: a torrent may still be growing on the seedbox. Two separate
    # reconcile() calls over different remote sizes must each reflect the size passed in,
    # never a value cached from an earlier call.
    local_tree = _local(LocalEntry("f.bin", False, 100))

    first = reconcile(_tree(RemoteEntry("f.bin", False, 100, 1.0)), local_tree)
    assert first["f.bin"].state == STATE_DOWNLOADED

    grown = reconcile(_tree(RemoteEntry("f.bin", False, 500, 1.0)), local_tree)
    assert grown["f.bin"].state == STATE_PARTIAL
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
    assert without_predicate["Release"].state == STATE_PARTIAL  # the naive/pre-phase-4 result

    with_predicate = reconcile(remote_tree, local_tree, counts_predicate=exclude_nfo)
    assert with_predicate["Release"].state == STATE_DOWNLOADED
