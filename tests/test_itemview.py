"""`core/itemview.py`'s lifecycle facets (2026-08-13, prompts/2026-08-13-lifecycle-icons.md).

`item.state` carries at least five orthogonal facts in one slot -- remote presence, local
presence, verified, extracted, and current activity -- and that collapsing is what produced
several of the 2026-08-12/13 state bugs (a `LOCAL_ONLY` reading clobbering a `move`-mode item's
`EXTRACTED` outcome, a `DOWNLOADED` row claiming bytes that are not on disk during the grace
period). This file tests the four small pure predicates that derive **R**(emote)/**L**(ocal)/
**V**(erified)/**E**(xtracted) from a persisted row without touching how `state` itself is
computed -- every branch named in the handoff prompt's own "Tests" section, plus the headline
scenario: a completed `move`-mode item, run through the real `Engine` pipeline (not a
hand-built dict), reading R dim/L green/V green/E green -- and that this projection is the one
and only place `GET /api/files`, `queue_delta`/`snapshot()`, and `item_delta` all read it from.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest
from fastapi import Request

from lftpweb.api.files import get_files
from lftpweb.core.engine import Engine
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import (
    ITEM_VIEW_COLUMNS,
    ITEM_VIEW_COLUMNS_QUALIFIED,
    _extracted_facet,
    _lifecycle_facets,
    _local_facet,
    _optional,
    _remote_facet,
    _verified_facet,
    item_view,
)
from lftpweb.db import migrate

# Reuse the real engine harness `test_state_persistence.py` already built (a `move`-mode queue
# whose remote copy is already gone) rather than a second one -- the headline test below is
# this task's own worked example, run through the actual scan/persist pipeline so it can't drift
# from what production code actually does.
from test_state_persistence import REL_PATH, SIZE, _make_move_engine

# --- _remote_facet -------------------------------------------------------------------------


def test_remote_facet_present_is_green():
    assert _remote_facet(1000, None) == {"level": "green", "reason": "present"}


def test_remote_facet_present_wins_over_a_stray_remote_deleted_at():
    # Can't really happen together (a real remote_deleted_at only survives once remote_size
    # goes NULL), but presence must always win if it ever did.
    assert _remote_facet(1000, "2026-08-13T00:00:00Z") == {"level": "green", "reason": "present"}


def test_remote_facet_absent_and_deleted_by_us_is_dim_never_red():
    facet = _remote_facet(None, "2026-08-13T00:00:00Z")
    assert facet == {"level": "dim", "reason": "deleted_by_us"}
    assert facet["level"] != "red", "a successful move must not render as a failure"


def test_remote_facet_absent_and_never_deleted_by_us_is_dim_with_a_different_reason():
    facet = _remote_facet(None, None)
    assert facet["level"] == "dim"
    assert facet["reason"] == "no_remote"
    # Distinguishable from the deliberate-delete case by reason, not by color (per the task's
    # "never red" rule) -- this is the "merely vanished" case the prompt asks to keep
    # distinguishable from "we deleted it."
    assert facet["reason"] != _remote_facet(None, "2026-08-13T00:00:00Z")["reason"]


# --- _local_facet ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    ["DOWNLOADED", "VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED", "VERIFYING", "EXTRACTING"],
)
def test_local_facet_complete_states_read_green_regardless_of_bytes(state):
    # 4533617: an EXTRACTED item whose spent archive volumes were deleted after a successful
    # extraction has local_size < remote_size *by design*. None of these states may be derived
    # from the byte comparison, or that case (and the vacuous-exclude-directory case below)
    # misreads as partial/absent.
    facet = _local_facet(state, local_size=10, remote_size=1000, first_missing_at=None)
    assert facet == {"level": "green", "reason": "complete"}


@pytest.mark.parametrize(
    "state", ["DOWNLOADED", "VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED"]
)
def test_local_facet_missing_during_grace_period_reads_dark_not_complete(state):
    # The *arr-import case (item 8): held at a "complete" state by §7.3's grace period while the
    # bytes are actually gone. `first_missing_at` is the only signal that tells this apart from
    # the vacuous-exclude-directory case, which never sets it.
    facet = _local_facet(
        state, local_size=None, remote_size=1000, first_missing_at="2026-08-13T00:00:00Z"
    )
    assert facet == {"level": "dim", "reason": "missing"}


def test_local_facet_vacuously_downloaded_directory_with_zero_local_bytes_is_complete_not_missing():
    # §3.2 rule 8 / §4.7: a directory whose remote children were all EXCLUDED reads DOWNLOADED
    # with real remote bytes (the excluded files' own sizes) and zero local bytes, by design.
    # first_missing_at is never set for this case -- must not read as missing.
    facet = _local_facet("DOWNLOADED", local_size=0, remote_size=4096, first_missing_at=None)
    assert facet == {"level": "green", "reason": "complete"}


@pytest.mark.parametrize("state", ["REMOVED_LOCAL", "REMOVED_BOTH"])
def test_local_facet_removed_by_us_is_dark_regardless_of_stale_local_size(state):
    # core/local_delete.py._mark_subtree_removed never clears local_size -- it can still read
    # the pre-delete value in the item_delta published immediately after a manual delete, before
    # the next scan corrects it. The facet must not trust that column for these two states.
    facet = _local_facet(state, local_size=999_999, remote_size=1000, first_missing_at=None)
    assert facet == {"level": "dim", "reason": "removed_by_us"}


def test_local_facet_excluded_is_dim_but_not_the_missing_reason():
    facet = _local_facet("EXCLUDED", local_size=None, remote_size=500, first_missing_at=None)
    assert facet["level"] == "dim"
    assert facet["reason"] == "excluded"
    assert facet["reason"] != "missing"
    assert facet["reason"] != "absent"


def test_local_facet_local_only_is_complete_with_no_remote_denominator():
    facet = _local_facet("LOCAL_ONLY", local_size=500, remote_size=None, first_missing_at=None)
    assert facet == {"level": "green", "reason": "local_only"}


@pytest.mark.parametrize("state", ["REMOTE_ONLY", "QUEUED", "STOPPED"])
def test_local_facet_byte_rule_absent_when_nothing_local(state):
    assert _local_facet(state, local_size=None, remote_size=1000, first_missing_at=None) == {
        "level": "dim",
        "reason": "absent",
    }
    assert _local_facet(state, local_size=0, remote_size=1000, first_missing_at=None) == {
        "level": "dim",
        "reason": "absent",
    }


@pytest.mark.parametrize("state", ["PARTIAL", "DOWNLOADING", "FAILED"])
def test_local_facet_byte_rule_partial_between_zero_and_remote_size(state):
    # This is also the "partially-present directory" case: local_size/remote_size are already
    # rollup sums for a directory (core/reconcile.py), so the leaf byte rule applied to those
    # totals is the directory's own reading -- no separate directory rule needed.
    assert _local_facet(state, local_size=400, remote_size=1000, first_missing_at=None) == {
        "level": "amber",
        "reason": "partial",
    }


def test_local_facet_byte_rule_complete_when_local_reaches_remote():
    assert _local_facet(
        "DOWNLOADING", local_size=1000, remote_size=1000, first_missing_at=None
    ) == {
        "level": "green",
        "reason": "complete",
    }


def test_local_facet_byte_rule_partial_with_no_remote_denominator_at_all():
    # Something is there but nothing proves it's everything -- safer to read partial than to
    # guess complete.
    assert _local_facet("STOPPED", local_size=300, remote_size=None, first_missing_at=None) == {
        "level": "amber",
        "reason": "partial",
    }


# --- _verified_facet / _extracted_facet ------------------------------------------------------


def test_verified_facet_verified_at_wins_even_if_state_has_moved_on():
    # Milestone, not derived from `state` -- stays green after a rescan moves state on to
    # EXTRACTING/EXTRACTED.
    assert _verified_facet("EXTRACTED", "2026-08-13T00:00:00Z") == {
        "level": "green",
        "reason": "verified",
    }


def test_verified_facet_corrupt_is_red():
    facet = _verified_facet("CORRUPT", None)
    assert facet == {"level": "red", "reason": "corrupt"}


def test_verified_facet_verifying_is_amber():
    assert _verified_facet("VERIFYING", None) == {"level": "amber", "reason": "in_progress"}


def test_verified_facet_never_verified_is_dim():
    assert _verified_facet("DOWNLOADED", None) == {"level": "dim", "reason": "not_verified"}


def test_extracted_facet_extracted_at_wins():
    assert _extracted_facet("EXTRACTED", "2026-08-13T00:00:00Z") == {
        "level": "green",
        "reason": "extracted",
    }


def test_extracted_facet_extract_failed_is_red():
    assert _extracted_facet("EXTRACT_FAILED", None) == {"level": "red", "reason": "failed"}


def test_extracted_facet_extracting_is_amber():
    assert _extracted_facet("EXTRACTING", None) == {"level": "amber", "reason": "in_progress"}


def test_extracted_facet_not_extracted_is_dim():
    assert _extracted_facet("DOWNLOADED", None) == {"level": "dim", "reason": "not_extracted"}


# --- _lifecycle_facets / item_view -------------------------------------------------------------


def _row(**overrides):
    base = {
        "id": 1,
        "rel_path": "Release.One",
        "is_dir": 0,
        "state": "DOWNLOADED",
        "substate": None,
        "suppressed_reason": None,
        "remote_size": 1000,
        "local_size": 1000,
        "remote_mtime": None,
        "local_mtime": None,
        "state_changed_at": None,
        "first_seen_at": "2026-08-12T23:00:00Z",
        "downloaded_at": "2026-08-13T00:00:00Z",
        "verified_at": None,
        "extracted_at": None,
        "first_missing_at": None,
        "remote_deleted_at": None,
    }
    base.update(overrides)
    return base


def test_lifecycle_facets_bundles_all_four():
    facets = _lifecycle_facets(_row())
    assert set(facets) == {"remote", "local", "verified", "extracted"}
    assert facets["remote"]["level"] == "green"
    assert facets["local"]["level"] == "green"
    assert facets["verified"]["level"] == "dim"
    assert facets["extracted"]["level"] == "dim"


def test_item_view_carries_facets_and_the_raw_timestamps_verbatim():
    row = _row(verified_at="2026-08-13T01:00:00Z", extracted_at="2026-08-13T02:00:00Z")
    view = item_view(row)
    assert view["facets"] == _lifecycle_facets(row)
    assert view["verified_at"] == "2026-08-13T01:00:00Z"
    assert view["extracted_at"] == "2026-08-13T02:00:00Z"
    assert view["downloaded_at"] == "2026-08-13T00:00:00Z"
    assert view["first_missing_at"] is None
    assert view["remote_deleted_at"] is None


# --- local_mtime / first_seen_at (2026-08-13, prompts/2026-08-13-files-detail-inspector.md) --


def test_item_view_converts_local_mtime_like_remote_mtime():
    # Same TEXT-affinity round-trip as `remote_mtime` (a REAL bound value comes back out of a
    # TEXT column as a string) -- `item_view` must apply the same `float(...)` correction.
    row = _row(local_mtime="1700000000.0")
    assert item_view(row)["local_mtime"] == 1700000000.0


def test_item_view_local_mtime_none_when_column_is_null():
    assert item_view(_row(local_mtime=None))["local_mtime"] is None


def test_item_view_passes_first_seen_at_through_verbatim():
    row = _row(first_seen_at="2026-08-12T23:00:00Z")
    assert item_view(row)["first_seen_at"] == "2026-08-12T23:00:00Z"


# --- settle-progress fields (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3) ---------


def test_optional_returns_none_for_a_dict_row_missing_the_key():
    # `_row()`'s base dict never carries settle_matched_scans/settle_first_matched_at -- the
    # exact shape of a bare `SELECT * FROM item` row (core/postprocess.py, core/local_delete.py,
    # core/queue.py), which never joins item_settle at all.
    assert _optional(_row(), "settle_matched_scans") is None


def test_optional_returns_none_for_a_sqlite_row_missing_the_key():
    # sqlite3.Row (what aiosqlite.Row is) raises IndexError, not KeyError, for an absent column
    # -- both must be caught, not just the dict case above.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (a)")
    conn.execute("INSERT INTO t VALUES (1)")
    row = conn.execute("SELECT a FROM t").fetchone()
    assert _optional(row, "settle_matched_scans") is None
    conn.close()


def test_optional_returns_the_value_when_present():
    assert _optional({"settle_matched_scans": 2}, "settle_matched_scans") == 2


def test_item_view_settle_fields_default_none_when_row_lacks_them():
    # `_row()`'s base dict has no settle columns at all -- the projection must not KeyError,
    # and must publish None rather than fabricating a value.
    view = item_view(_row(substate="settling"))
    assert view["settle_matched_scans"] is None
    assert view["settle_first_matched_at"] is None


def test_item_view_passes_settle_fields_through_when_present_and_settling():
    row = _row(
        substate="settling",
        settle_matched_scans=1,
        settle_first_matched_at="2026-08-13T00:00:00Z",
    )
    view = item_view(row)
    assert view["settle_matched_scans"] == 1
    assert view["settle_first_matched_at"] == "2026-08-13T00:00:00Z"


def test_item_view_settle_fields_are_none_when_not_settling_even_if_the_row_has_them():
    # `core/engine.py._persist` keeps advancing `item_settle` for a top-level item on every scan
    # for as long as its fingerprint keeps matching -- including long after it finished
    # downloading and stopped being `settling`. An ungated read would make
    # `settle_matched_scans` climb forever on a row nothing else about is changing, which
    # defeats `diff_nodes`'s "only publish what actually changed" property
    # (`tests/test_ws_deltas.py`). A joined row with real settle data but `substate != None`
    # -- meaning `"settling"` -- must still publish `None` for both fields.
    row = _row(
        substate=None, settle_matched_scans=9, settle_first_matched_at="2026-08-13T00:00:00Z"
    )
    view = item_view(row)
    assert view["settle_matched_scans"] is None
    assert view["settle_first_matched_at"] is None


def test_item_view_columns_qualified_matches_item_view_columns_prefixed():
    # Derived, not hand-duplicated -- this is the one thing that could let the two drift.
    assert ITEM_VIEW_COLUMNS_QUALIFIED == ", ".join(
        f"item.{c.strip()}" for c in ITEM_VIEW_COLUMNS.split(",")
    )


# --- The headline test: a completed move-mode item, through the real Engine pipeline ---------


async def test_headline_completed_move_mode_item_r_dim_l_green_v_green_e_green(
    tmp_path, monkeypatch
):
    """The worked example the whole task is built around (56ec523 made it reproducible): a
    `move`-mode release, verified then extracted, whose remote copy this codebase deleted on
    purpose after verification. `item.state` alone reads this as `EXTRACTED` with no way to
    say the remote is gone *on purpose* -- the facets are what make "R dim, L filled, V green,
    E green" visible, and R must read as the neutral/success treatment, never the failure one.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path,
        monkeypatch,
        local_size=SIZE,
        state="EXTRACTED",
        remote_deleted_at="2026-08-13T00:00:00.000000Z",
    )
    try:
        await db.execute(
            "UPDATE item SET verified_at = ?, extracted_at = ? WHERE id = ?",
            ("2026-08-13T00:00:00.000000Z", "2026-08-13T00:05:00.000000Z", item_id),
        )
        await db.commit()

        await engine.scan_queue(q, host)
        # `_project` is the shared read-back both `queue_delta` (scan_queue, above) and
        # `snapshot()` publish through -- see core/itemview.py's module docstring.
        published = await engine._project(q.id, {REL_PATH})
        node = published[REL_PATH]

        assert node["facets"]["remote"]["level"] == "dim"
        assert node["facets"]["remote"]["level"] != "red"
        assert node["facets"]["remote"]["reason"] == "deleted_by_us"
        assert node["facets"]["local"] == {"level": "green", "reason": "complete"}
        assert node["facets"]["verified"] == {"level": "green", "reason": "verified"}
        assert node["facets"]["extracted"] == {"level": "green", "reason": "extracted"}
    finally:
        await db.close()


# --- Facets reach all three projection consumers ----------------------------------------------
#
# `GET /api/files`, `queue_delta`/`snapshot()` (`core/engine.py._project`, exercised above), and
# `item_delta` (`core/queue.py`/`core/postprocess.py`) are all `item_view(row)` calls, but two
# different SQL shapes feed them: an explicit `ITEM_VIEW_COLUMNS` list (engine.py, api/files.py,
# core/queue.py) and a bare `SELECT *` (core/postprocess.py, core/local_delete.py). The risk
# this task's projection change could actually introduce is those two shapes disagreeing --
# `SELECT *` picking up a column `ITEM_VIEW_COLUMNS` forgot, or vice versa -- so both are
# exercised here directly, against the same row.


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _seed_item(db, **item_overrides) -> tuple[int, int]:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    queue_id = cursor.lastrowid
    fields = {
        "queue_id": queue_id,
        "rel_path": "Release.One",
        "is_dir": 0,
        "remote_size": None,
        "local_size": 1000,
        "state": "EXTRACTED",
        "verified_at": "2026-08-13T01:00:00Z",
        "extracted_at": "2026-08-13T02:00:00Z",
        "remote_deleted_at": "2026-08-13T00:00:00Z",
    }
    fields.update(item_overrides)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    cursor = await db.execute(
        f"INSERT INTO item ({columns}) VALUES ({placeholders})",  # noqa: S608 - fixed key set above
        tuple(fields.values()),
    )
    item_id = cursor.lastrowid
    await db.commit()
    return queue_id, item_id


async def test_facets_agree_between_select_star_and_item_view_columns(db):
    """`core/postprocess.py`/`core/local_delete.py`'s `item_delta` reads `SELECT *`;
    `core/engine.py`/`api/files.py`/`core/queue.py` read the explicit `ITEM_VIEW_COLUMNS` list.
    Both must produce byte-identical facets for the same row -- this is the one place the two
    shapes could silently drift (a column added to the table but not to `ITEM_VIEW_COLUMNS`).
    """
    _queue_id, item_id = await _seed_item(db)

    star_cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    star_row = await star_cursor.fetchone()

    columns_cursor = await db.execute(
        f"SELECT {ITEM_VIEW_COLUMNS} FROM item WHERE id = ?",  # noqa: S608 - module constant
        (item_id,),
    )
    columns_row = await columns_cursor.fetchone()

    assert item_view(star_row) == item_view(columns_row)
    assert item_view(star_row)["facets"]["remote"] == {"level": "dim", "reason": "deleted_by_us"}
    assert item_view(star_row)["facets"]["verified"] == {"level": "green", "reason": "verified"}


class _FakeEngineState:
    """Just enough of `Engine`'s public surface for `api/files.get_files` -- the real dicts a
    scan would populate, without running one.
    """

    def __init__(self, queue_id: int) -> None:
        self.queue_meta = {queue_id: type("Q", (), {"name": "q"})()}
        self.last_scan_at = {queue_id: "2026-08-13T03:00:00Z"}
        self.scan_errors = {queue_id: None}
        self.scan_warnings = {queue_id: None}
        self.mount_ok = {queue_id: True}


class _FakeAppState:
    def __init__(self, db, queue_id: int) -> None:
        self.db = db
        self.engine = _FakeEngineState(queue_id)


class _FakeApp:
    def __init__(self, db, queue_id: int) -> None:
        self.state = _FakeAppState(db, queue_id)


async def test_facets_reach_get_files_endpoint(db):
    """`GET /api/files` (`api/files.get_files`) is the third path -- called directly here, the
    same way `tests/test_delete_api.py` calls route functions directly rather than through a
    live TestClient, since the thing under test is the route's own wiring onto `item_view`.
    """
    queue_id, _item_id = await _seed_item(db)
    # Starlette's `Request.app` reads `self.scope["app"]` -- set it the way the real ASGI
    # server does, rather than reaching for a private attribute.
    request = Request(scope={"type": "http", "app": _FakeApp(db, queue_id)})

    response = await get_files(request)
    assert len(response.queues) == 1
    node = response.queues[0].nodes[0]
    assert node.facets.remote.level == "dim"
    assert node.facets.remote.reason == "deleted_by_us"
    assert node.facets.verified.level == "green"
    assert node.facets.extracted.level == "green"


# --- The item_settle join reaches both callers (2026-08-13, prompts/2026-08-13-files-ux-pass.md
# item 3) -----------------------------------------------------------------------------------
#
# `core/engine.py._project` and `api/files.py.get_files` each add their own `LEFT JOIN
# item_settle` -- the risk is those two drifting (one joins, the other doesn't; one aliases a
# column differently). Both are exercised directly against the same seeded row below, the same
# "don't just trust the two SQL shapes agree" spirit as `test_facets_agree_between_select_star_
# and_item_view_columns` above.


async def test_engine_project_joins_settle_progress_for_a_top_level_row(db, tmp_path):
    queue_id, item_id = await _seed_item(
        db, state="REMOTE_ONLY", substate="settling", remote_size=1000, local_size=None
    )
    await db.execute(
        "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, max_mtime, "
        "matched_scans, updated_at) VALUES (?, 'Release.One', 3, 1000, 123.0, 1, "
        "'2026-08-13T00:00:00.000000Z')",
        (queue_id,),
    )
    await db.commit()

    engine = Engine(db=db, config_dir=str(tmp_path), events=EventBus())
    published = await engine._project(queue_id, {"Release.One"})
    node = published["Release.One"]

    assert node["settle_matched_scans"] == 1
    assert node["settle_first_matched_at"] == "2026-08-13T00:00:00.000000Z"
    assert node["id"] == item_id  # the join must not change which row this still is


async def test_engine_project_settle_fields_are_none_without_an_item_settle_row(db, tmp_path):
    # No INSERT into item_settle at all -- a row not yet scanned, or not top-level, has no
    # match, and the LEFT JOIN must degrade to NULL rather than dropping the item row entirely.
    queue_id, _item_id = await _seed_item(db, state="REMOTE_ONLY", substate=None)

    engine = Engine(db=db, config_dir=str(tmp_path), events=EventBus())
    published = await engine._project(queue_id, {"Release.One"})
    node = published["Release.One"]

    assert node["settle_matched_scans"] is None
    assert node["settle_first_matched_at"] is None


async def test_get_files_joins_settle_progress(db):
    queue_id, _item_id = await _seed_item(
        db, state="REMOTE_ONLY", substate="settling", remote_size=1000, local_size=None
    )
    await db.execute(
        "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, max_mtime, "
        "matched_scans, updated_at) VALUES (?, 'Release.One', 1, 500, 100.0, 2, "
        "'2026-08-13T00:01:00.000000Z')",
        (queue_id,),
    )
    await db.commit()
    request = Request(scope={"type": "http", "app": _FakeApp(db, queue_id)})

    response = await get_files(request)
    node = response.queues[0].nodes[0]
    assert node.settle_matched_scans == 2
    assert node.settle_first_matched_at == "2026-08-13T00:01:00.000000Z"
