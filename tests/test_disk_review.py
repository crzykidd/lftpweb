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
    _load_in_use_paths,  # testing Set C's own I/O helper directly, see the section below
    freed_bytes,
    reconcile,
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


def _claim(client_id, client_name, content_path, *, transfer_id="t1", transfer_name="Release"):
    return ClientClaim(
        client_id=client_id,
        client_name=client_name,
        transfer_id=transfer_id,
        transfer_name=transfer_name,
        content_path=content_path,
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
    assert result.broken_seeds == ()


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
# A - B: broken seeds
# ================================================================================================


def test_broken_seed_surfaces_as_its_own_pile():
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
    assert len(result.broken_seeds) == 1
    broken = result.broken_seeds[0]
    assert broken.content_path == f"{root}/Vanished.Release"
    assert broken.transfer_id == "nzo1"
    # And a claim that *is* found on disk is not reported as broken.
    present = _file(root, "Present.Release/file.mkv")
    result2 = reconcile(
        **_base(
            base_paths=[root],
            contributors=[BasePathContributor(root, client_id=1)],
            reachable_client_ids=[1],
            disk_entries=[present],
            claims=[_claim(1, "SAB", f"{root}/Present.Release")],
        )
    )
    assert result2.broken_seeds == ()


def test_broken_seed_not_reported_when_its_root_was_never_walked():
    # A claim whose content_path isn't under any configured base path at all -- absent
    # information, never a verdict either way.
    result = reconcile(
        **_base(
            base_paths=["/complete/tv"],
            claims=[_claim(1, "Client", "/somewhere/else/entirely")],
        )
    )
    assert result.broken_seeds == ()


def test_broken_seed_not_reported_when_the_walk_itself_failed():
    root = "/complete/tv"
    result = reconcile(
        **_base(
            base_paths=[root],
            unavailable_roots={root: "remote scan failed: connection reset"},
            claims=[_claim(1, "Client", f"{root}/Some.Release")],
        )
    )
    assert result.broken_seeds == ()


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
