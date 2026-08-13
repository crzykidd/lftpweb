"""Unit tests for `core/settle.py` (prompts/open-issues.md "2 -- the settle gate"). Pure
fingerprint/counter arithmetic plus a small round-trip through migration 007's `item_settle`
table -- no lftp, no fake seedbox. The real reproduction (a growing remote file and a growing
remote directory, both against the real fake seedbox) lives in `tests/test_settle_gate_e2e.py`.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core import settle
from lftpweb.core.remote import RemoteEntry
from lftpweb.db import migrate


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


# --- compute_fingerprints -----------------------------------------------------------------


def test_fingerprint_groups_by_top_level_item():
    tree = {
        "Release": RemoteEntry(rel_path="Release", is_dir=True),
        "Release/a.mkv": RemoteEntry(rel_path="Release/a.mkv", is_dir=False, size=100, mtime=10.0),
        "Release/Subs": RemoteEntry(rel_path="Release/Subs", is_dir=True),
        "Release/Subs/a.srt": RemoteEntry(
            rel_path="Release/Subs/a.srt", is_dir=False, size=5, mtime=20.0
        ),
        "loose.txt": RemoteEntry(rel_path="loose.txt", is_dir=False, size=50, mtime=15.0),
    }
    fps = settle.compute_fingerprints(tree)
    assert fps["Release"] == (2, 105, 20.0)  # 2 files, 100+5 bytes, newest mtime wins
    assert fps["loose.txt"] == (1, 50, 15.0)


def test_fingerprint_bare_directory_with_no_files_yet():
    tree = {"Release": RemoteEntry(rel_path="Release", is_dir=True)}
    assert settle.compute_fingerprints(tree)["Release"] == (0, 0, None)


def test_fingerprint_ignores_a_nested_directorys_own_mtime():
    # A subdirectory's own mtime must never leak into max_mtime -- only file mtimes count
    # (the module docstring's reasoning: a directory's mtime only moves on entry add/remove).
    tree = {
        "Release": RemoteEntry(rel_path="Release", is_dir=True, mtime=999.0),
        "Release/Subs": RemoteEntry(rel_path="Release/Subs", is_dir=True, mtime=888.0),
        "Release/a.mkv": RemoteEntry(rel_path="Release/a.mkv", is_dir=False, size=1, mtime=5.0),
    }
    assert settle.compute_fingerprints(tree)["Release"] == (1, 1, 5.0)


# --- advance_settle / is_settled -----------------------------------------------------------

# An arbitrary fixed epoch so every test below is deterministic -- never `time.time()`.
_T0 = 1_700_000_000.0


def test_first_sighting_starts_at_one_and_is_not_settled():
    record = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    assert record.matched_scans == 1
    assert record.first_matched_at == _T0
    assert not settle.is_settled(record, now=_T0)


def test_atomic_arrival_settles_after_exactly_two_scans_and_the_age_floor():
    """prompts/2026-08-12-settle-gate-followups.md item 2: settled requires **both**
    `REQUIRED_SETTLE_SCANS` matches **and** `SETTLE_MIN_AGE_S` of wall-clock time since the
    streak began -- neither alone is enough.
    """
    fp = (3, 300, 5.0)
    first = settle.advance_settle(None, fp, partial_scan=False, now=_T0)
    assert not settle.is_settled(first, now=_T0)
    second = settle.advance_settle(first, fp, partial_scan=False, now=_T0 + 1.0)
    assert second.matched_scans == 2 == settle.REQUIRED_SETTLE_SCANS
    # Count alone is satisfied, but almost no time has passed since the streak began --
    # must not read settled yet.
    assert not settle.is_settled(second, now=_T0 + 1.0)
    assert not settle.is_settled(second, now=_T0 + settle.SETTLE_MIN_AGE_S - 1.0)
    # Once both the count and the age floor are met, and no later.
    assert settle.is_settled(second, now=_T0 + settle.SETTLE_MIN_AGE_S)


def test_first_matched_at_is_carried_forward_across_matching_scans_not_reset():
    """The streak's start time must not creep forward on every confirming match -- only a
    fresh sighting or a changed fingerprint may move it, or the age floor above would never
    actually bind (it would always measure "since the last scan," not "since first observed").
    """
    fp = (3, 300, 5.0)
    first = settle.advance_settle(None, fp, partial_scan=False, now=_T0)
    second = settle.advance_settle(first, fp, partial_scan=False, now=_T0 + 30.0)
    third = settle.advance_settle(second, fp, partial_scan=False, now=_T0 + 90.0)
    assert third.matched_scans == 3
    assert third.first_matched_at == _T0


def test_a_changed_fingerprint_resets_the_counter_and_the_age_floor():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    second = settle.advance_settle(
        first, (1, 100, 1.0), partial_scan=False, now=_T0 + settle.SETTLE_MIN_AGE_S
    )
    assert settle.is_settled(second, now=_T0 + settle.SETTLE_MIN_AGE_S)
    grown = settle.advance_settle(
        second, (2, 200, 2.0), partial_scan=False, now=_T0 + settle.SETTLE_MIN_AGE_S + 500.0
    )
    assert grown.matched_scans == 1
    assert grown.first_matched_at == _T0 + settle.SETTLE_MIN_AGE_S + 500.0
    assert not settle.is_settled(grown, now=_T0 + settle.SETTLE_MIN_AGE_S + 500.0)


def test_partial_scan_holds_rather_than_resets_or_advances():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    # A partial scan mid-arrival: even though the (truncated) reading happens to be identical,
    # this must not count as a confirming match.
    held = settle.advance_settle(first, (1, 100, 1.0), partial_scan=True, now=_T0 + 500.0)
    assert held == first
    # Nor may a *different* truncated reading reset the counter.
    held_again = settle.advance_settle(first, (9, 9, 9.0), partial_scan=True, now=_T0 + 500.0)
    assert held_again == first


def test_partial_scan_holds_first_matched_at_too():
    """A hold must not let the streak's start time creep forward either -- only the actual
    matched-scan count has a documented "held, not reset" rule; this proves the age floor
    inherits the same conservative treatment rather than accidentally resetting on every
    partial-scan hiccup.
    """
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    held = settle.advance_settle(first, (1, 100, 1.0), partial_scan=True, now=_T0 + 500.0)
    assert held.first_matched_at == _T0


def test_partial_scan_with_no_previous_record_still_starts_at_one():
    record = settle.advance_settle(None, (1, 100, 1.0), partial_scan=True, now=_T0)
    assert record.matched_scans == 1
    assert record.first_matched_at == _T0
    assert not settle.is_settled(record, now=_T0)


def test_none_record_is_never_settled():
    assert not settle.is_settled(None)
    assert not settle.is_settled(None, now=_T0 + 1_000_000.0)


# --- persistence ----------------------------------------------------------------------------


async def _make_queue(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'example.invalid', 'u', 'key', 'insecure')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    queue_id = cursor.lastrowid
    await db.commit()
    return queue_id


async def test_settle_records_round_trip_through_the_database(db):
    """`first_matched_at` round-trips through `item_settle.updated_at` (the repurposed column,
    prompts/2026-08-12-settle-gate-followups.md item 2) exactly, not just `matched_scans`.
    """
    queue_id = await _make_queue(db)

    assert await settle.load_settle_records(db, queue_id) == {}
    assert not await settle.is_settled_in_db(db, queue_id, "Release", now=_T0)

    records = {
        "Release": settle.SettleRecord(
            fingerprint=(2, 250, 5.0), matched_scans=1, first_matched_at=_T0
        )
    }
    await settle.save_settle_records(db, queue_id, records)
    await db.commit()

    loaded = await settle.load_settle_records(db, queue_id)
    assert loaded == records
    # matched_scans below REQUIRED_SETTLE_SCANS -- not settled regardless of age.
    assert not await settle.is_settled_in_db(db, queue_id, "Release", now=_T0 + 1_000_000.0)

    records["Release"] = settle.SettleRecord(
        fingerprint=(2, 250, 5.0), matched_scans=2, first_matched_at=_T0
    )
    await settle.save_settle_records(db, queue_id, records)
    await db.commit()

    assert await settle.load_settle_records(db, queue_id) == records
    # Count now met, but not yet old enough.
    assert not await settle.is_settled_in_db(db, queue_id, "Release", now=_T0 + 1.0)
    # Both met.
    assert await settle.is_settled_in_db(db, queue_id, "Release", now=_T0 + settle.SETTLE_MIN_AGE_S)


async def test_is_settled_in_db_requires_the_age_floor_too(db):
    """The DB-row-shape check (`core/autoqueue.py`, `core/queue.py._reap_one`) must apply the
    same two-condition rule `is_settled` does for an in-memory `SettleRecord` -- a matched
    count alone is not enough.
    """
    queue_id = await _make_queue(db)
    await settle.save_settle_records(
        db,
        queue_id,
        {
            "Release": settle.SettleRecord(
                fingerprint=(2, 250, 5.0),
                matched_scans=settle.REQUIRED_SETTLE_SCANS,
                first_matched_at=_T0,
            )
        },
    )
    await db.commit()

    assert not await settle.is_settled_in_db(db, queue_id, "Release", now=_T0)
    assert not await settle.is_settled_in_db(
        db, queue_id, "Release", now=_T0 + settle.SETTLE_MIN_AGE_S - 1.0
    )
    assert await settle.is_settled_in_db(db, queue_id, "Release", now=_T0 + settle.SETTLE_MIN_AGE_S)


async def test_settle_settings_default_on_and_round_trip(db):
    """prompts/2026-08-12-settle-gate-followups.md item 3: the default flipped from off to on
    -- a real behavior change, asserted here rather than left to drift back unnoticed.
    """
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is True

    await settle.save_settle_settings(db, settle.SettleSettings(enabled=False))
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is False

    await settle.save_settle_settings(db, settle.SettleSettings(enabled=True))
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is True
