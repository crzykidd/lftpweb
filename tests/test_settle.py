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


def test_first_sighting_starts_at_one_and_is_not_settled():
    record = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False)
    assert record.matched_scans == 1
    assert not settle.is_settled(record)


def test_atomic_arrival_settles_after_exactly_two_scans_and_no_more():
    fp = (3, 300, 5.0)
    first = settle.advance_settle(None, fp, partial_scan=False)
    assert not settle.is_settled(first)
    second = settle.advance_settle(first, fp, partial_scan=False)
    assert second.matched_scans == 2 == settle.REQUIRED_SETTLE_SCANS
    assert settle.is_settled(second)


def test_a_changed_fingerprint_resets_the_counter():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False)
    second = settle.advance_settle(first, (1, 100, 1.0), partial_scan=False)
    assert settle.is_settled(second)
    grown = settle.advance_settle(second, (2, 200, 2.0), partial_scan=False)
    assert grown.matched_scans == 1
    assert not settle.is_settled(grown)


def test_partial_scan_holds_rather_than_resets_or_advances():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False)
    # A partial scan mid-arrival: even though the (truncated) reading happens to be identical,
    # this must not count as a confirming match.
    held = settle.advance_settle(first, (1, 100, 1.0), partial_scan=True)
    assert held == first
    # Nor may a *different* truncated reading reset the counter.
    held_again = settle.advance_settle(first, (9, 9, 9.0), partial_scan=True)
    assert held_again == first


def test_partial_scan_with_no_previous_record_still_starts_at_one():
    record = settle.advance_settle(None, (1, 100, 1.0), partial_scan=True)
    assert record.matched_scans == 1
    assert not settle.is_settled(record)


def test_none_record_is_never_settled():
    assert not settle.is_settled(None)


# --- persistence ----------------------------------------------------------------------------


async def test_settle_records_round_trip_through_the_database(db):
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

    assert await settle.load_settle_records(db, queue_id) == {}
    assert not await settle.is_settled_in_db(db, queue_id, "Release")

    records = {"Release": settle.SettleRecord(fingerprint=(2, 250, 5.0), matched_scans=1)}
    await settle.save_settle_records(db, queue_id, records)
    await db.commit()

    loaded = await settle.load_settle_records(db, queue_id)
    assert loaded == records
    assert not await settle.is_settled_in_db(db, queue_id, "Release")

    records["Release"] = settle.SettleRecord(fingerprint=(2, 250, 5.0), matched_scans=2)
    await settle.save_settle_records(db, queue_id, records)
    await db.commit()

    assert await settle.is_settled_in_db(db, queue_id, "Release")


async def test_settle_settings_default_off_and_round_trip(db):
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is False

    await settle.save_settle_settings(db, settle.SettleSettings(enabled=True))
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is True
