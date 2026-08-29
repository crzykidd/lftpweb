"""`core/disk_review.py` -- the disk review scan (docs/download-client-framework-spec.md §11,
stage 4 of #18). `reconcile()`/`freed_bytes()` are pure set math over three inputs, tested here
exhaustively without SSH or a live client, per this task's own handoff prompt. The two
scenarios that make this feature dangerous if done the obvious way (spec §11.1a, §11.1b) are
asserted directly, by name, rather than only implied by a passing suite.

`_load_in_use_paths`/`_load_in_flight_ids` (the I/O shell's Set C) get one real-database test
each, against `core/pipeline_flight.py`'s actual SQL -- proving this module reuses that
predicate rather than re-deriving "busy" a second time, the one thing this task's own prompt
called out as a defect class (v0.2.6's `REMOTE_GONE`) if gotten wrong.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core.disk_review import (
    BasePathContributor,
    ClientClaim,
    DiskEntry,
    InUsePath,
    SkippedBasePath,
    UnclaimedItem,
    _load_in_use_paths,  # testing Set C's own I/O helper directly, see the section below
    _resolve_client_exclusions,  # the pure derivation step behind `run_scan`'s own gathering
    freed_bytes,
    is_authorized_delete_target,
    reconcile,
    resolve_category_exclusion_paths,
)
from lftpweb.db import migrate

NOW = 2_000_000.0
OLD_MTIME = NOW - 3600.0  # well past any reasonable age floor
FRESH_MTIME = NOW - 1.0  # one second old -- must never be proposed


def _file(root, rel_path, *, size=100, mtime=OLD_MTIME, inode=None, nlink=1, is_dir=False):
    return DiskEntry(
        root=root,
        rel_path=rel_path,
        abs_path=f"{root}/{rel_path}",
        is_dir=is_dir,
        size=size,
        mtime=mtime,
        inode=inode,
        nlink=nlink,
    )


def _claim(
    client_id,
    client_name,
    content_path,
    *,
    transfer_id="t1",
    transfer_name="Release",
    category=None,
):
    return ClientClaim(
        client_id=client_id,
        client_name=client_name,
        transfer_id=transfer_id,
        transfer_name=transfer_name,
        content_path=content_path,
        category=category,
    )


def _base(**overrides):
    """Every `reconcile()` kwarg defaulted to "nothing," so each test only states what it's
    actually exercising.
    """
    base = dict(
        base_paths=[],
        contributors=[],
        reachable_client_ids=[],
        disk_entries=[],
        unavailable_roots={},
        claims=[],
        in_use=[],
        now_s=NOW,
    )
    base.update(overrides)
    return base


# ================================================================================================
# §11.1a -- Set A is a union across clients, and an unreachable contributor blocks debris
# ================================================================================================


def test_union_across_clients_protects_the_others_estate():
    """The catastrophe, named directly: SAB and rTorrent share a completed folder. SAB's claim
    covers its own release; rTorrent's release is only claimed by its own seeding-directory
    content_path (a different root entirely). A scan that only consulted one client's claims
    would see the other client's release as unclaimed -- both must survive.
    """
    shared = "/complete/tv"
    working = "/rtorrent/data"

    sab_release = _file(shared, "SAB.Release/episode.mkv", inode=1)
    rtorrent_hardlink = _file(shared, "RT.Release/episode.mkv", inode=2, nlink=2)
    rtorrent_seed_copy = _file(working, "RT.Release/episode.mkv", inode=2, nlink=2)

    result = reconcile(
        **_base(
            base_paths=[shared, working],
            contributors=[
                BasePathContributor(shared, client_id=1),
                BasePathContributor(shared, client_id=2),
                BasePathContributor(working, client_id=2),
            ],
            reachable_client_ids=[1, 2],
            disk_entries=[sab_release, rtorrent_hardlink, rtorrent_seed_copy],
            claims=[
                _claim(1, "SAB", f"{shared}/SAB.Release"),
                _claim(2, "rTorrent", f"{working}/RT.Release"),
            ],
        )
    )

    debris_paths = {d.abs_path for d in result.debris}
    assert sab_release.abs_path not in debris_paths
    assert rtorrent_hardlink.abs_path not in debris_paths
    assert rtorrent_seed_copy.abs_path not in debris_paths
    # Both are shown, distinctly, as claimed -- not silently dropped.
    seeding_paths = {s.abs_path for s in result.seeding_estate}
    assert sab_release.abs_path in seeding_paths
    assert rtorrent_hardlink.abs_path in seeding_paths


def test_unreachable_contributing_client_blocks_all_debris_on_the_shared_path():
    """The same shared folder, but rTorrent (client 2) did not report this pass. Even a file
    under that path with *no* claim at all must not be proposed -- absence of a claim from a
    client that didn't answer carries no information (spec §4.2/§11.1a), and the whole root is
    withheld rather than just the specific releases that might belong to the silent client.
    """
    shared = "/complete/tv"
    genuinely_unclaimed = _file(shared, "Orphan.Release/file.mkv", inode=9)

    result = reconcile(
        **_base(
            base_paths=[shared],
            contributors=[
                BasePathContributor(shared, client_id=1),
                BasePathContributor(shared, client_id=2),
            ],
            reachable_client_ids=[1],  # client 2 never reported
            disk_entries=[genuinely_unclaimed],
            claims=[],
        )
    )

    assert result.debris == ()
    assert len(result.skipped_base_paths) == 1
    skipped = result.skipped_base_paths[0]
    assert skipped.root == shared
    assert "2" in skipped.reason


def test_disabled_client_declared_on_a_path_blocks_debris_the_same_as_unreachable():
    # `run_scan` never puts a disabled client's id into `reachable_client_ids` at all -- from
    # `reconcile`'s point of view this is indistinguishable from "didn't answer," which is the
    # point (spec §11.1a names "unreachable, disabled, or has not reported" as one condition).
    root = "/complete/movies"
    entry = _file(root, "Some.Movie/movie.mkv", inode=5)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[],  # client 1 is disabled -- never reachable
            disk_entries=[entry],
        )
    )
    assert result.debris == ()
    assert result.skipped_base_paths[0].root == root


def test_root_with_every_contributor_reporting_proposes_genuine_debris():
    # The positive case: every declared contributor answered, nothing claims this file, and
    # it's old enough -- it must actually show up, or the guards above would be indistinguishable
    # from "the scan never proposes anything."
    root = "/complete/tv"
    entry = _file(root, "Leftover/broken.part", inode=42)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
        )
    )
    assert [d.abs_path for d in result.debris] == [entry.abs_path]
    assert result.skipped_base_paths == ()


# ================================================================================================
# §11.1b -- claiming is by inode, not path
# ================================================================================================


def test_inode_claim_protects_a_hardlink_never_named_by_any_claim():
    """rTorrent's `content_path` names only its seeding directory. The completed-folder
    hardlink shares its inode but is never itself the subject of any claim's path -- it must
    still read as claimed, purely through the shared inode.
    """
    completed = "/complete/tv"
    working = "/rtorrent/data"
    seed_copy = _file(working, "Release/file.mkv", inode=77, nlink=2)
    hardlink = _file(completed, "Release/file.mkv", inode=77, nlink=2)

    result = reconcile(
        **_base(
            base_paths=[completed, working],
            contributors=[
                BasePathContributor(completed, client_id=1),
                BasePathContributor(working, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[seed_copy, hardlink],
            claims=[_claim(1, "rTorrent", f"{working}/Release")],
        )
    )
    assert result.debris == ()
    seeding_paths = {s.abs_path for s in result.seeding_estate}
    assert hardlink.abs_path in seeding_paths
    assert seed_copy.abs_path in seeding_paths


def test_seeding_estate_entries_carry_their_claims_torrent_identity():
    """2026-08-23, finding #7: "it would be better to show Torrents and expand each torrent."
    The rollup is a display-layer concern (`lib/diskReview.ts.groupSeedingEstateByTorrent`), but
    it needs each file's own claim identity to group by -- both the seeding-directory copy and
    its completed-folder hardlink must carry the *same* `claimed_transfer_id`/
    `claimed_transfer_name`/`claimed_content_path`, proving one torrent's two on-disk files roll
    up together rather than needing a second lookup.
    """
    completed = "/complete/tv"
    working = "/rtorrent/data"
    seed_copy = _file(working, "Release/file.mkv", inode=77, nlink=2)
    hardlink = _file(completed, "Release/file.mkv", inode=77, nlink=2)

    result = reconcile(
        **_base(
            base_paths=[completed, working],
            contributors=[
                BasePathContributor(completed, client_id=1),
                BasePathContributor(working, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[seed_copy, hardlink],
            claims=[
                _claim(
                    1,
                    "rTorrent",
                    f"{working}/Release",
                    transfer_id="t-release-1",
                    transfer_name="Release.S01E01",
                )
            ],
        )
    )
    by_path = {s.abs_path: s for s in result.seeding_estate}
    assert by_path[seed_copy.abs_path].claimed_transfer_id == "t-release-1"
    assert by_path[hardlink.abs_path].claimed_transfer_id == "t-release-1"
    assert by_path[seed_copy.abs_path].claimed_transfer_name == "Release.S01E01"
    assert by_path[hardlink.abs_path].claimed_transfer_name == "Release.S01E01"
    assert by_path[seed_copy.abs_path].claimed_content_path == f"{working}/Release"
    assert by_path[hardlink.abs_path].claimed_content_path == f"{working}/Release"


def test_nlink_greater_than_found_links_is_never_proposed():
    """`nlink=2` but this scan only ever found one on-disk entry for that inode -- the other
    link lives somewhere outside every scanned base path. The conservative default: never
    propose it, since the unseen link's status is unknown.
    """
    root = "/complete/tv"
    entry = _file(root, "Mystery/file.mkv", inode=100, nlink=2)  # only one found
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
        )
    )
    assert result.debris == ()
    assert result.seeding_estate == ()


def test_both_links_of_a_genuinely_orphaned_hardlink_pair_are_both_proposed():
    # The mirror image of the catastrophe: two links, both found, both unclaimed -- a real
    # orphaned hardlink pair (e.g. two base paths both holding a stale copy). Both are proposed,
    # each carrying the other in `link_paths` so `freed_bytes` can be link-aware about them.
    root_a, root_b = "/complete/tv", "/complete/tv-alt"
    a = _file(root_a, "Stale/file.mkv", inode=55, nlink=2, size=1000)
    b = _file(root_b, "Stale/file.mkv", inode=55, nlink=2, size=1000)
    result = reconcile(
        **_base(
            base_paths=[root_a, root_b],
            contributors=[
                BasePathContributor(root_a, client_id=1),
                BasePathContributor(root_b, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[a, b],
        )
    )
    assert {d.abs_path for d in result.debris} == {a.abs_path, b.abs_path}
    for d in result.debris:
        assert set(d.link_paths) == {a.abs_path, b.abs_path}


def test_one_claimed_link_protects_its_unclaimed_sibling():
    # If even one of the found links is itself claimed, neither link is proposed -- "a file is
    # claimed if *any* link to its inode is claimed" applied to the group-candidacy check too.
    root = "/complete/tv"
    working = "/client/working"
    claimed_link = _file(working, "Release/file.mkv", inode=9, nlink=2)
    other_link = _file(root, "Release/file.mkv", inode=9, nlink=2)
    result = reconcile(
        **_base(
            base_paths=[root, working],
            contributors=[
                BasePathContributor(root, client_id=1),
                BasePathContributor(working, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[claimed_link, other_link],
            claims=[_claim(1, "Client", f"{working}/Release")],
        )
    )
    assert result.debris == ()


# ================================================================================================
# Link-aware freed bytes (spec §10.5, §11.1b)
# ================================================================================================


def test_freed_bytes_zero_for_partial_selection_of_a_linked_pair():
    root_a, root_b = "/complete/tv", "/complete/tv-alt"
    a = _file(root_a, "Stale/file.mkv", inode=55, nlink=2, size=40_000_000_000)
    b = _file(root_b, "Stale/file.mkv", inode=55, nlink=2, size=40_000_000_000)
    result = reconcile(
        **_base(
            base_paths=[root_a, root_b],
            contributors=[
                BasePathContributor(root_a, client_id=1),
                BasePathContributor(root_b, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[a, b],
        )
    )
    assert freed_bytes(result.debris, [a.abs_path]) == 0
    assert freed_bytes(result.debris, [a.abs_path, b.abs_path]) == 40_000_000_000


def test_freed_bytes_sums_unlinked_candidates_normally():
    root = "/complete/tv"
    x = _file(root, "a.mkv", size=100)
    y = _file(root, "b.mkv", size=250)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[x, y],
        )
    )
    assert freed_bytes(result.debris, [x.abs_path]) == 100
    assert freed_bytes(result.debris, [x.abs_path, y.abs_path]) == 350


# ================================================================================================
# Other guards: age floor, containment, shared save paths, mount-sentinel / walk failures
# ================================================================================================


def test_age_floor_excludes_a_freshly_written_release():
    root = "/complete/tv"
    fresh = _file(root, "Just.Landed/file.mkv", mtime=FRESH_MTIME)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[fresh],
            age_floor_s=600.0,
        )
    )
    assert result.debris == ()


def test_containment_ignores_an_entry_outside_every_configured_root():
    # Defensive: `run_scan` should never hand `reconcile` an entry outside `base_paths`, but if
    # it did (a bug, a stale root removed from config mid-scan), it must never be proposed.
    entry = _file("/not/configured", "leftover.bin")
    result = reconcile(
        **_base(
            base_paths=["/complete/tv"],
            disk_entries=[entry],
        )
    )
    assert result.debris == ()
    assert result.seeding_estate == ()
    assert result.torrents == ()


def test_shared_save_path_stays_protected_by_either_claim():
    # Cross-seed (spec §10.4): two different transfers (different client ids, or the same
    # client with two torrents) both claim the same save path. The files there stay protected
    # as long as *any* claim exists -- claiming is a union, not a majority vote.
    root = "/complete/tv"
    shared_content = f"{root}/Cross.Seeded"
    entry = _file(root, "Cross.Seeded/file.mkv", inode=3)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[
                BasePathContributor(root, client_id=1),
                BasePathContributor(root, client_id=2),
            ],
            reachable_client_ids=[1, 2],
            disk_entries=[entry],
            claims=[
                _claim(1, "Tracker A", shared_content, transfer_id="hashA"),
                _claim(2, "Tracker B", shared_content, transfer_id="hashB"),
            ],
        )
    )
    assert entry.abs_path not in {d.abs_path for d in result.debris}

    # Remove both claims: now genuinely unclaimed, and becomes debris.
    result_unclaimed = reconcile(
        **_base(
            base_paths=[root],
            contributors=[
                BasePathContributor(root, client_id=1),
                BasePathContributor(root, client_id=2),
            ],
            reachable_client_ids=[1, 2],
            disk_entries=[entry],
            claims=[],
        )
    )
    assert entry.abs_path in {d.abs_path for d in result_unclaimed.debris}


def test_unavailable_root_excludes_debris_and_is_reported():
    # Stands in for both an SSH walk failure and a failed mount-sentinel check (`run_scan`
    # populates `unavailable_roots` for either reason identically) -- `reconcile` itself
    # doesn't need to know which.
    root = "/complete/tv"
    entry = _file(root, "Whatever/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            unavailable_roots={root: "queue 'TV' local mount is not healthy (mount sentinel)"},
        )
    )
    assert result.debris == ()
    assert result.skipped_base_paths == (
        SkippedBasePath(root=root, reason="queue 'TV' local mount is not healthy (mount sentinel)"),
    )


def test_in_use_path_is_never_debris():
    # Set C (`core/pipeline_flight.py`'s predicate, applied by `run_scan`) -- an item lftpweb
    # itself still considers in flight must never be proposed, even if no client claims it and
    # it's old enough.
    root = "/complete/tv"
    entry = _file(root, "InProgress/file.part")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            in_use=[InUsePath(item_id=1, abs_path=f"{root}/InProgress")],
        )
    )
    assert result.debris == ()


# ================================================================================================
# `torrents` -- one row per claim (2026-08-24, spec §11.4). Supersedes the retired `broken_seeds`:
# a broken seed is exactly `missing_on_disk=True` here.
# ================================================================================================


def test_torrent_reports_missing_on_disk_when_its_root_was_walked_and_found_empty():
    root = "/complete/tv"
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[],  # nothing on disk at all
            claims=[_claim(1, "SAB", f"{root}/Vanished.Release", transfer_id="nzo1")],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    assert torrent.content_path == f"{root}/Vanished.Release"
    assert torrent.transfer_id == "nzo1"
    assert torrent.file_count == 0
    assert torrent.missing_on_disk is True
    # And a claim that *is* found on disk is not reported as missing.
    present = _file(root, "Present.Release/file.mkv", size=123)
    result2 = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[present],
            claims=[_claim(1, "SAB", f"{root}/Present.Release")],
        )
    )
    assert len(result2.torrents) == 1
    assert result2.torrents[0].missing_on_disk is False
    assert result2.torrents[0].file_count == 1
    assert result2.torrents[0].size_on_disk == 123


def test_torrent_file_count_is_null_not_zero_when_its_root_was_never_walked():
    # A claim whose content_path isn't under any configured base path at all -- absent
    # information, never a verdict either way (spec §4.2's instinct). `file_count`/`size_on_disk`
    # must be `None`, and `missing_on_disk` must stay `False` -- "never walked" is not "found
    # empty."
    result = reconcile(
        **_base(
            base_paths=["/complete/tv"],
            claims=[_claim(1, "Client", "/somewhere/else/entirely")],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    assert torrent.file_count is None
    assert torrent.size_on_disk is None
    assert torrent.missing_on_disk is False


def test_torrent_file_count_is_null_not_zero_when_the_walk_itself_failed():
    root = "/complete/tv"
    result = reconcile(
        **_base(
            base_paths=[root],
            unavailable_roots={root: "remote scan failed: connection reset"},
            claims=[_claim(1, "Client", f"{root}/Some.Release")],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    assert torrent.file_count is None
    assert torrent.size_on_disk is None
    assert torrent.missing_on_disk is False


def test_torrent_with_no_content_path_reports_null_files_and_claims_nothing():
    """Spec §11.4's own "surface the two things currently dropped silently": a transfer the
    client reports with no path at all still gets a `torrents` row (`file_count`/`size_on_disk`
    genuinely unknown, `None`, never `0`), and it must never participate in claiming -- a disk
    entry that would otherwise be genuine debris stays debris, unaffected by the path-less claim.
    """
    root = "/complete/tv"
    entry = _file(root, "Unrelated.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            claims=[
                ClientClaim(
                    client_id=1,
                    client_name="SAB",
                    transfer_id="nzo-no-path",
                    transfer_name="Pathless",
                    content_path=None,
                )
            ],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    assert torrent.content_path is None
    assert torrent.file_count is None
    assert torrent.size_on_disk is None
    assert torrent.missing_on_disk is False
    # The unrelated on-disk entry is untouched -- still genuine debris.
    assert entry.abs_path in {d.abs_path for d in result.debris}
    assert result.seeding_estate == ()


def test_sabnzbd_shaped_claim_reports_none_never_zero_for_torrent_only_figures():
    """`USENET_BASELINE` declares `RATIO`/`UPLOADED_BYTES`/`SEED_TIME_S` as `Support.NONE` --
    every SABnzbd-sourced claim must carry `None` for all three, never a fabricated `0`/`0.0`
    (this task's own `ClientClaim` docstring: "a fabricated 0.00 ratio sitting beside a real one
    is exactly the guess dressed up as a fact `SpaceInfo`'s own docstring warns against").
    """
    root = "/complete/tv"
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            claims=[
                ClientClaim(
                    client_id=1,
                    client_name="SAB",
                    transfer_id="nzo1",
                    transfer_name="Release",
                    content_path=f"{root}/Release",
                    size_bytes=5_000,
                    uploaded_bytes=None,
                    ratio=None,
                    seed_time_s=None,
                    added_at="2026-08-24T00:00:00Z",
                    raw_status="Completed",
                    phase="completed",
                )
            ],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    assert torrent.size_bytes == 5_000
    assert torrent.uploaded_bytes is None
    assert torrent.ratio is None
    assert torrent.seed_time_s is None


def test_size_on_disk_counts_a_hardlinked_torrent_file_once():
    """`size_on_disk` answers "how big is this torrent," not "what would deleting it reclaim" --
    it must count an inode once even though only one of its two links sits under this claim's own
    seeding-directory tree, the ordinary rTorrent shape (the other link is the completed-folder
    hardlink, a different claim's tree entirely).
    """
    working = "/rtorrent/data"
    completed = "/complete/tv"
    seed_copy = _file(working, "Release/file.mkv", inode=9, nlink=2, size=40_000_000_000)
    hardlink = _file(completed, "Release/file.mkv", inode=9, nlink=2, size=40_000_000_000)
    result = reconcile(
        **_base(
            base_paths=[working, completed],
            contributors=[
                BasePathContributor(working, client_id=1),
                BasePathContributor(completed, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[seed_copy, hardlink],
            claims=[_claim(1, "rTorrent", f"{working}/Release", transfer_id="rt1")],
        )
    )
    assert len(result.torrents) == 1
    torrent = result.torrents[0]
    # Only `seed_copy` is under `working/Release` -- `hardlink` belongs to the completed root,
    # outside this claim's own tree, so file_count counts the claim's own tree only.
    assert torrent.file_count == 1
    assert torrent.size_on_disk == 40_000_000_000


# ================================================================================================
# Excluded paths -- "not used by this instance" as a hard safety boundary, not a preference
# (migration 031, findings #15/#16, 2026-08-23,
# prompts/2026-08-23-category-tristate-and-exclusion.md). The deployment shape: two lftpweb
# instances share one seedbox, one SABnzbd, one rTorrent -- each instance permanently sees the
# other's work. An excluded path must be never scanned, never proposed as debris, and never
# inside the future delete-containment boundary.
# ================================================================================================


def test_excluded_path_is_never_proposed_as_debris():
    """An excluded sub-path (the enforceable primitive, `download_client_excluded_path`) with no
    claim covering it must never appear in the debris pile even though every other guard
    (contributor reachable, old enough, unclaimed) would otherwise let it through -- instead it
    lands in `excluded_content` (2026-08-24, this task, spec §11.4), visible but never selectable
    and never counted toward a reclaim total.
    """
    root = "/complete/tv"
    others_release = _file(root, "OtherInstance.Release/file.mkv", inode=9)
    ours = _file(root, "Our.Release/file.mkv", inode=10)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[others_release, ours],
            excluded_paths=[f"{root}/OtherInstance.Release"],
        )
    )
    debris_paths = {d.abs_path for d in result.debris}
    assert others_release.abs_path not in debris_paths
    assert ours.abs_path in debris_paths  # unaffected -- a different tree entirely
    assert result.unclaimed == ()
    assert len(result.excluded_content) == 1
    excluded_entry = result.excluded_content[0]
    assert excluded_entry.abs_path == others_release.abs_path
    assert excluded_entry.excluded_path == f"{root}/OtherInstance.Release"


def test_excluded_path_content_still_covered_by_a_claim_shows_in_the_seeding_estate():
    """2026-08-24 correction, this task's own governing principle (exclusion is a delete-safety
    boundary, not a visibility boundary): a claim under an excluded path is the *ordinary* steady
    state of §17.7's shared seedbox (the other instance's client still lists its own active
    transfers) -- it is shown in the seeding estate like any other claimed file, tagged with
    whatever attribution its own category has, and never in `debris` or `excluded_content` (that
    pile is reserved for excluded content **no current claim** covers -- see the sibling test
    above and `test_excluded_content_disappears_from_the_seeding_estate_once_its_claim_is_gone`).
    """
    root = "/complete/tv"
    entry = _file(root, "OtherInstance.Release/file.mkv", inode=9)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            claims=[_claim(1, "SAB", f"{root}/OtherInstance.Release")],
            excluded_paths=[f"{root}/OtherInstance.Release"],
        )
    )
    assert result.debris == ()
    assert result.excluded_content == ()
    assert len(result.seeding_estate) == 1
    assert result.seeding_estate[0].abs_path == entry.abs_path


def test_excluded_content_disappears_from_the_seeding_estate_once_its_claim_is_gone():
    """§17.7's own "latent data-loss path," made visible instead of silent (this task's whole
    point): the same file, once its claim vanishes (the other instance's client dropped its
    history entry, or the torrent was removed), moves from the seeding estate into
    `excluded_content` -- never into `debris`.
    """
    root = "/complete/tv"
    entry = _file(root, "OtherInstance.Release/file.mkv", inode=9)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            claims=[],  # the claim is gone
            excluded_paths=[f"{root}/OtherInstance.Release"],
        )
    )
    assert result.debris == ()
    assert result.seeding_estate == ()
    assert len(result.excluded_content) == 1
    assert result.excluded_content[0].abs_path == entry.abs_path


def test_excluded_path_claim_can_now_be_reported_missing_too():
    """2026-08-24 correction: a claim under an excluded path with nothing found on disk **is now
    reported missing** in `torrents` -- this used to be suppressed entirely, which was correct
    for delete safety but wrong for visibility (this module's own governing principle). "An
    excluded claim can now be reported missing too. That is correct -- it is visibility, which is
    the point" (this task's own spec §11.4 note).
    """
    root = "/complete/tv"
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[],
            claims=[_claim(1, "SAB", f"{root}/OtherInstance.Release", transfer_id="nzo9")],
            excluded_paths=[f"{root}/OtherInstance.Release"],
        )
    )
    assert len(result.torrents) == 1
    assert result.torrents[0].missing_on_disk is True
    assert result.torrents[0].file_count == 0


def test_excluding_a_whole_base_path_protects_everything_under_it():
    """A generic property of `excluded_paths`, not (as of 2026-08-23's narrowing, see the
    "per-file fail-closed" section below) what `run_scan` does for a category that can't resolve
    to a path anymore -- that case no longer excludes the whole root at all, only the genuinely
    ambiguous unclaimed remainder (`debris_ambiguous_roots`). This still holds as a direct
    property of `excluded_paths` itself: naming an entire root there (e.g. a manually-configured
    exclusion covering a whole tree) protects everything under it, same as any sub-path.
    """
    root = "/rtorrent/data"
    entry = _file(root, "Some.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[entry],
            excluded_paths=[root],
        )
    )
    assert result.debris == ()


# ================================================================================================
# Per-file fail-closed (live use, 2026-08-23, follow-up to finding #16): a category that cannot
# resolve to a path (rTorrent) no longer suppresses its owning client's entire base path. A
# CLAIMED file is resolved directly off its own transfer's category; only a genuinely UNCLAIMED
# file under such a root is fail-closed, and even then the root's seeding estate stays populated.
# ================================================================================================


def test_claimed_content_in_an_excluded_category_is_hard_excluded_without_touching_the_root():
    """A base path with both a bound category's claim and an excluded category's claim: the
    bound content survives normally (seeding estate, `attribution="bound"`), and the excluded
    content is *also* shown in the seeding estate (2026-08-24 correction -- see this module's own
    governing principle) but tagged `attribution="excluded"` and never in `debris` -- neither
    pile is suppressed for the *whole* root, unlike the old whole-base-path fail closed
    behaviour.
    """
    root = "/rtorrent/data"
    bound_file = _file(root, "Bound.Release/file.mkv", inode=1)
    excluded_file = _file(root, "Excluded.Release/file.mkv", inode=2)
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=2)],
            reachable_client_ids=[2],
            disk_entries=[bound_file, excluded_file],
            claims=[
                _claim(2, "rTorrent", f"{root}/Bound.Release", transfer_id="rt1", category="ar-tv"),
                _claim(
                    2,
                    "rTorrent",
                    f"{root}/Excluded.Release",
                    transfer_id="rt2",
                    category="other-site-movies",
                ),
            ],
            excluded_categories_by_client={2: frozenset({"other-site-movies"})},
            debris_ambiguous_roots={root: "other-site-movies cannot be resolved to a path"},
            category_attribution_by_client={2: {"ar-tv": "bound", "other-site-movies": "excluded"}},
        )
    )
    seeding_by_path = {e.abs_path: e for e in result.seeding_estate}
    debris_paths = {d.abs_path for d in result.debris}
    assert bound_file.abs_path in seeding_by_path
    assert seeding_by_path[bound_file.abs_path].attribution == "bound"
    assert excluded_file.abs_path in seeding_by_path
    assert seeding_by_path[excluded_file.abs_path].attribution == "excluded"
    assert excluded_file.abs_path not in debris_paths


def test_excluded_category_content_appears_in_seeding_estate_tagged_excluded_not_debris():
    """This task's own headline behaviour (2026-08-24, spec §11.4): a file claimed by an EXCLUDED
    category is retained as a claim, so it lands in the seeding estate -- shown, tagged
    `attribution="excluded"` -- and it must never appear in `debris`. This is the corrected
    reading of finding #17's own line ("a file claimed by an excluded category is known to belong
    to the other lftpweb instance"): before this task that meant *dropped entirely*; now it means
    *shown, but never actionable*. Same fixture finding #17's own test used, so this is a direct
    before/after of the one behaviour this task changes.
    """
    root = "/rtorrent/data"
    excluded_file = _file(root, "Excluded.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=2)],
            reachable_client_ids=[2],
            disk_entries=[excluded_file],
            claims=[
                _claim(
                    2,
                    "rTorrent",
                    f"{root}/Excluded.Release",
                    transfer_id="rt2",
                    category="other-site-movies",
                )
            ],
            excluded_categories_by_client={2: frozenset({"other-site-movies"})},
            debris_ambiguous_roots={root: "other-site-movies cannot be resolved to a path"},
            category_attribution_by_client={2: {"other-site-movies": "excluded"}},
        )
    )
    assert excluded_file.abs_path not in {d.abs_path for d in result.debris}
    assert excluded_file.abs_path not in {u.abs_path for u in result.unclaimed}
    assert excluded_file.abs_path not in {e.abs_path for e in result.excluded_content}
    seeding_by_path = {e.abs_path: e for e in result.seeding_estate}
    assert excluded_file.abs_path in seeding_by_path
    assert seeding_by_path[excluded_file.abs_path].attribution == "excluded"


def test_a_genuinely_unclaimed_file_under_an_ambiguous_root_is_shown_in_the_unclaimed_pile():
    """Finding #17's correction: the one truly irreducible case (nobody claims this file at all,
    so there is no category to read off a claim -- it might be the leftover of a since-vanished
    excluded-category transfer, or genuine debris, and `reconcile` cannot tell which) is no
    longer a silent count. It is shown as its own pile item, with its reason, never selectable
    through the debris flow (it is not in `result.debris`).
    """
    root = "/rtorrent/data"
    orphan = _file(root, "Orphan.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=2)],
            reachable_client_ids=[2],
            disk_entries=[orphan],
            debris_ambiguous_roots={root: "other-site-movies cannot be resolved to a path"},
        )
    )
    assert result.debris == ()
    assert result.unclaimed == (
        UnclaimedItem(
            root=root,
            rel_path=orphan.rel_path,
            abs_path=orphan.abs_path,
            size=orphan.size,
            mtime=orphan.mtime,
            inode=orphan.inode,
            nlink=orphan.nlink,
            link_paths=(),
            reason="other-site-movies cannot be resolved to a path",
        ),
    )
    # The "not selectable through the debris flow" property, at the level this module can assert
    # it (the frontend enforces it further by never rendering a checkbox for this pile at all,
    # see `DiskReviewPage.tsx`): an unclaimed item is never also a debris candidate.
    assert orphan.abs_path not in {d.abs_path for d in result.debris}


def test_single_instance_shaped_fixture_produces_an_empty_unclaimed_pile():
    """The normal case must look normal (finding #17's own "in a single-instance setup it should
    be empty" requirement): no exclusions, every category resolvable, nothing ambiguous -- the
    unclaimed pile stays empty even though there is genuine, ordinary debris to find.
    """
    root = "/complete/tv"
    debris_file = _file(root, "Orphan.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[debris_file],
        )
    )
    assert result.unclaimed == ()
    assert len(result.debris) == 1


def test_unclaimed_pile_reclaim_total_is_link_aware():
    """§10.5's own requirement extended to the third pile (finding #17): a naive sum would
    reintroduce the lie that section exists to prevent. Selecting only one of two hardlinked
    unclaimed items must report zero bytes freed; selecting both reports the real total.
    """
    root = "/rtorrent/data"
    link_a = _file(root, "Orphan.Release/file.mkv", size=5_000, inode=7, nlink=2)
    link_b = _file("/complete/tv", "Orphan.Release/file.mkv", size=5_000, inode=7, nlink=2)
    result = reconcile(
        **_base(
            base_paths=[root, "/complete/tv"],
            contributors=[
                BasePathContributor(root, client_id=2),
                BasePathContributor("/complete/tv", client_id=2),
            ],
            reachable_client_ids=[2],
            disk_entries=[link_a, link_b],
            debris_ambiguous_roots={
                root: "reason A",
                "/complete/tv": "reason A",
            },
        )
    )
    assert len(result.unclaimed) == 2
    all_paths = {u.abs_path for u in result.unclaimed}
    assert freed_bytes(result.unclaimed, {link_a.abs_path}) == 0
    assert freed_bytes(result.unclaimed, all_paths) == 5_000


def test_seeding_estate_is_populated_even_when_debris_is_suppressed_for_that_path():
    """The whole point of the narrowing: a root in `debris_ambiguous_roots` is still walked, and
    its claimed content is still shown -- the old design's actual defect (live use: "there are
    things in there in ar-tv that it doesn't show now") was suppressing this too.
    """
    root = "/rtorrent/data"
    claimed = _file(root, "Bound.Release/file.mkv")
    result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=2)],
            reachable_client_ids=[2],
            disk_entries=[claimed],
            claims=[_claim(2, "rTorrent", f"{root}/Bound.Release", category="ar-tv")],
            debris_ambiguous_roots={root: "some-other-category cannot be resolved to a path"},
        )
    )
    assert len(result.seeding_estate) == 1
    assert result.seeding_estate[0].abs_path == claimed.abs_path


def test_resolve_category_exclusion_paths_joins_base_and_category():
    """Finding #16's own resolution rule: spec §1.1's `<base>/<category>` layout, for every
    combination of a client's content-kind base paths and its excluded categories.
    """
    paths = resolve_category_exclusion_paths(
        ["/downloads/complete"], ["other-site-tv", "other-site-movies"]
    )
    assert paths == ["/downloads/complete/other-site-movies", "/downloads/complete/other-site-tv"]


def test_resolve_category_exclusion_paths_handles_multiple_base_paths():
    paths = resolve_category_exclusion_paths(["/a", "/b"], ["cat"])
    assert set(paths) == {"/a/cat", "/b/cat"}


def test_is_authorized_delete_target_refuses_an_excluded_path():
    """The seed of §10.2's future containment check -- an excluded path must be refused even
    though it sits inside a declared base path, which is exactly the scenario finding #16 exists
    to guard against.
    """
    assert (
        is_authorized_delete_target(
            "/complete/tv/OtherInstance.Release",
            base_paths=["/complete/tv"],
            excluded_paths=["/complete/tv/OtherInstance.Release"],
        )
        is False
    )


def test_is_authorized_delete_target_refuses_a_sub_path_of_an_excluded_path():
    assert (
        is_authorized_delete_target(
            "/complete/tv/OtherInstance.Release/episode.mkv",
            base_paths=["/complete/tv"],
            excluded_paths=["/complete/tv/OtherInstance.Release"],
        )
        is False
    )


def test_is_authorized_delete_target_allows_a_base_path_target_outside_any_exclusion():
    assert (
        is_authorized_delete_target(
            "/complete/tv/Our.Release",
            base_paths=["/complete/tv"],
            excluded_paths=["/complete/tv/OtherInstance.Release"],
        )
        is True
    )


def test_is_authorized_delete_target_refuses_anything_outside_every_base_path():
    assert (
        is_authorized_delete_target(
            "/somewhere/else", base_paths=["/complete/tv"], excluded_paths=[]
        )
        is False
    )


def test_hard_invariant_excluded_content_stays_unauthorized_for_delete_after_becoming_visible():
    """**The one non-negotiable this task exists to protect** (this task's own handoff prompt,
    "the hard invariant"): making excluded content *visible* must never make it *authorized*.
    Both routes this task adds visibility through -- a manually-excluded path with no claim
    (`excluded_content`) and an excluded-category claim (`seeding_estate`, `attribution
    ="excluded"`) -- must still fail `is_authorized_delete_target` for the exact same path,
    unconditionally. `is_authorized_delete_target`'s own signature, behaviour, and the excluded-
    path set `_resolve_client_exclusions` builds are untouched by this task; this test is the
    direct assertion that they still compose correctly with the now-visible piles above them.
    """
    root = "/complete/tv"
    manually_excluded = f"{root}/OtherInstance.Release"
    excluded_category_release = f"{root}/Excluded.Release"

    # Route 1: a manually excluded path with no claim -- shows up in `excluded_content`.
    unclaimed_result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[_file(root, "OtherInstance.Release/file.mkv", inode=9)],
            excluded_paths=[manually_excluded],
        )
    )
    assert len(unclaimed_result.excluded_content) == 1
    assert (
        is_authorized_delete_target(
            f"{manually_excluded}/file.mkv", base_paths=[root], excluded_paths=[manually_excluded]
        )
        is False
    )

    # Route 2: an excluded-category claim -- shows up in `seeding_estate`, `attribution="excluded"`.
    claimed_result = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=2)],
            reachable_client_ids=[2],
            disk_entries=[_file(root, "Excluded.Release/file.mkv", inode=5)],
            claims=[
                _claim(2, "rTorrent", excluded_category_release, transfer_id="rt9", category="x")
            ],
            excluded_categories_by_client={2: frozenset({"x"})},
            category_attribution_by_client={2: {"x": "excluded"}},
        )
    )
    assert len(claimed_result.seeding_estate) == 1
    assert claimed_result.seeding_estate[0].attribution == "excluded"
    assert (
        is_authorized_delete_target(
            f"{excluded_category_release}/file.mkv",
            base_paths=[root],
            excluded_paths=[excluded_category_release],
        )
        is False
    )


def test_excluded_sibling_cannot_ride_a_hardlink_group_into_debris():
    """Regression guard for the subtle failure mode `_entry_eligible`'s own 2026-08-24 docstring
    names: since excluded entries no longer leave `disk_entries` before `by_inode` is built, a
    debris-eligible file's hardlink sibling landing under an excluded path must fail the whole
    group closed, not let the excluded sibling read as "eligible" and wave the pair through.
    """
    debris_root = "/complete/tv"
    excluded_root = "/complete/other-instance"
    candidate = _file(debris_root, "Maybe.Debris/file.mkv", inode=42, nlink=2, size=999)
    excluded_sibling = _file(excluded_root, "Maybe.Debris/file.mkv", inode=42, nlink=2, size=999)
    result = reconcile(
        **_base(
            base_paths=[debris_root, excluded_root],
            contributors=[
                BasePathContributor(debris_root, client_id=1),
                BasePathContributor(excluded_root, client_id=1),
            ],
            reachable_client_ids=[1],
            disk_entries=[candidate, excluded_sibling],
            excluded_paths=[excluded_root],
        )
    )
    assert result.debris == ()
    assert candidate.abs_path not in {d.abs_path for d in result.debris}


# --- `run_scan`'s own derivation step, isolated from the database/SSH (finding #16) ------------


def test_resolve_client_exclusions_derives_a_path_from_a_content_kind_base_path():
    """The SAB-shaped case: a `content`-kind base path exists, so the excluded category resolves
    precisely -- no fail-closed root at all.
    """
    excluded_paths, fail_closed = _resolve_client_exclusions(
        manual_excluded_paths=[],
        base_paths_by_client={1: [("/downloads/complete", "content")]},
        excluded_categories_by_client={1: ["other-site-tv"]},
    )
    assert excluded_paths == {"/downloads/complete/other-site-tv"}
    assert fail_closed == {}


def test_resolve_client_exclusions_fails_closed_with_no_content_base_path():
    """The rTorrent-shaped "hard part" (finding #16): the client's only declared base path is
    `working` (its seeding directory), unrelated to any category folder -- the exclusion cannot
    be resolved to a path, so the client's entire declared base path is suppressed instead, with
    a stated reason, rather than silently proposing nothing is wrong.
    """
    excluded_paths, fail_closed = _resolve_client_exclusions(
        manual_excluded_paths=[],
        base_paths_by_client={2: [("/rtorrent/data", "working")]},
        excluded_categories_by_client={2: ["other-site-movies"]},
    )
    assert excluded_paths == set()
    assert list(fail_closed) == ["/rtorrent/data"]
    assert "other-site-movies" in fail_closed["/rtorrent/data"]
    assert "cannot be resolved" in fail_closed["/rtorrent/data"]


def test_resolve_client_exclusions_only_fails_closed_the_client_with_no_content_base_path():
    """One client's unresolvable exclusion must not spill over onto a different client's own,
    perfectly resolvable one.
    """
    excluded_paths, fail_closed = _resolve_client_exclusions(
        manual_excluded_paths=[],
        base_paths_by_client={
            1: [("/downloads/complete", "content")],  # SAB -- resolves fine
            2: [("/rtorrent/data", "working")],  # rTorrent -- cannot resolve
        },
        excluded_categories_by_client={1: ["other-site-tv"], 2: ["other-site-movies"]},
    )
    assert excluded_paths == {"/downloads/complete/other-site-tv"}
    assert list(fail_closed) == ["/rtorrent/data"]


def test_resolve_client_exclusions_combines_manual_and_derived_paths():
    excluded_paths, fail_closed = _resolve_client_exclusions(
        manual_excluded_paths=["/complete/manually-excluded"],
        base_paths_by_client={1: [("/downloads/complete", "content")]},
        excluded_categories_by_client={1: ["other-site-tv"]},
    )
    assert excluded_paths == {
        "/complete/manually-excluded",
        "/downloads/complete/other-site-tv",
    }
    assert fail_closed == {}


# ================================================================================================
# Set C's own I/O helper, against `core/pipeline_flight.py`'s real predicate (a real database,
# not a mock) -- proving `run_scan` never re-derives "busy" on its own.
# ================================================================================================


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _seed_host(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method) "
        "VALUES ('seedbox', 'example.com', 22, 'user', 'agent')"
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_queue(db: aiosqlite.Connection, host_id: int) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path) "
        "VALUES (?, 'TV', '/complete/tv', '/local/tv')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_item(db: aiosqlite.Connection, queue_id: int, rel_path: str, state: str) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, ?, 0, ?)",
        (queue_id, rel_path, state),
    )
    await db.commit()
    return cursor.lastrowid


async def test_load_in_use_paths_includes_item_with_an_active_job(db: aiosqlite.Connection):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    item_id = await _seed_item(db, queue_id, "Downloading.Release", "DOWNLOADING")
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (item_id,)
    )
    await db.commit()

    in_use = await _load_in_use_paths(db, frozenset())
    assert [u.abs_path for u in in_use] == ["/complete/tv/Downloading.Release"]


async def test_load_in_use_paths_includes_item_pipeline_flight_says_is_busy(
    db: aiosqlite.Connection,
):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    # No job at all -- busy purely because it's in the postprocess pipeline's own live
    # `in_flight_item_ids()` set, `core/pipeline_flight.py`'s second blocking condition.
    item_id = await _seed_item(db, queue_id, "Extracting.Release", "EXTRACTING")

    in_use = await _load_in_use_paths(db, frozenset({item_id}))
    assert [u.abs_path for u in in_use] == ["/complete/tv/Extracting.Release"]


async def test_load_in_use_paths_excludes_a_terminal_item_with_no_active_job(
    db: aiosqlite.Connection,
):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    await _seed_item(db, queue_id, "Done.Release", "DOWNLOADED")

    in_use = await _load_in_use_paths(db, frozenset())
    assert in_use == []
