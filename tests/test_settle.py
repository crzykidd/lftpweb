"""Unit tests for `core/settle.py` (prompts/open-issues.md "2 -- the settle gate"). Pure
fingerprint/counter arithmetic plus a small round-trip through migration 007's `item_settle`
table -- no lftp, no fake seedbox. The real reproduction (a growing remote file and a growing
remote directory, both against the real fake seedbox) lives in `tests/test_settle_gate_e2e.py`.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core import settle
from lftpweb.core.clients.models import Transfer, TransferPhase
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


# --- first_observed_at / last_changed_at (migration 013, 2026-08-13,
# prompts/2026-08-13-settle-progress-visibility.md) -----------------------------------------


def test_first_observed_at_and_last_changed_at_set_together_on_first_sighting():
    record = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    assert record.first_observed_at == _T0
    assert record.last_changed_at == _T0


def test_a_growing_fingerprint_keeps_matched_scans_at_one_and_updates_last_changed_at():
    """The user's own report: a directory actively being copied onto the seedbox changes on
    every scan, so `matched_scans` keeps resetting to 1 -- unchanged behavior, see
    `docs/decisions.md` for why an earlier version of this task tried making it 0 instead and
    reverted -- for as long as it keeps growing. What's new is `last_changed_at`, which moves
    with every one of those changes even though `matched_scans` itself does not; that's the
    signal the Files page actually uses to tell "still changing" apart from "confirmed once,"
    both of which read `matched_scans == 1`. `first_observed_at` moves with neither.
    """
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    grown_once = settle.advance_settle(first, (1, 200, 2.0), partial_scan=False, now=_T0 + 30.0)
    assert grown_once.matched_scans == 1
    assert grown_once.last_changed_at == _T0 + 30.0
    assert grown_once.first_observed_at == _T0  # unmoved

    grown_again = settle.advance_settle(
        grown_once, (1, 300, 3.0), partial_scan=False, now=_T0 + 60.0
    )
    assert grown_again.matched_scans == 1
    assert grown_again.last_changed_at == _T0 + 60.0
    assert grown_again.first_observed_at == _T0  # still unmoved


def test_a_held_fingerprint_advances_the_counter_and_leaves_last_changed_at_alone():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    grown = settle.advance_settle(first, (1, 200, 2.0), partial_scan=False, now=_T0 + 30.0)
    held = settle.advance_settle(grown, (1, 200, 2.0), partial_scan=False, now=_T0 + 60.0)
    assert held.matched_scans == grown.matched_scans + 1 == 2
    # The fingerprint held (matched grown's), so nothing "changed" on this scan -- both stay
    # exactly where the previous change left them.
    assert held.last_changed_at == _T0 + 30.0
    assert held.first_observed_at == _T0


def test_first_observed_at_is_set_once_and_never_moved_by_later_scans():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    held = settle.advance_settle(first, (1, 100, 1.0), partial_scan=False, now=_T0 + 30.0)
    changed = settle.advance_settle(held, (2, 200, 2.0), partial_scan=False, now=_T0 + 90.0)
    held_again = settle.advance_settle(changed, (2, 200, 2.0), partial_scan=False, now=_T0 + 120.0)
    for record in (first, held, changed, held_again):
        assert record.first_observed_at == _T0


def test_partial_scan_holds_first_observed_at_and_last_changed_at_too():
    first = settle.advance_settle(None, (1, 100, 1.0), partial_scan=False, now=_T0)
    grown = settle.advance_settle(first, (1, 200, 2.0), partial_scan=False, now=_T0 + 30.0)
    held = settle.advance_settle(grown, (9, 9, 9.0), partial_scan=True, now=_T0 + 500.0)
    assert held == grown
    assert held.first_observed_at == _T0
    assert held.last_changed_at == _T0 + 30.0


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


async def test_first_observed_at_and_last_changed_at_round_trip_through_the_database(db):
    """Migration 013's two new columns round-trip exactly, alongside the pre-existing ones."""
    queue_id = await _make_queue(db)

    records = {
        "Release": settle.SettleRecord(
            fingerprint=(2, 250, 5.0),
            matched_scans=1,
            first_matched_at=_T0,
            first_observed_at=_T0 - 90.0,
            last_changed_at=_T0,
        )
    }
    await settle.save_settle_records(db, queue_id, records)
    await db.commit()

    loaded = await settle.load_settle_records(db, queue_id)
    assert loaded == records
    assert loaded["Release"].first_observed_at == _T0 - 90.0
    assert loaded["Release"].last_changed_at == _T0


async def test_null_first_observed_at_and_last_changed_at_round_trip_as_none(db):
    """A pre-migration-013 `item_settle` row (or any row this module itself wrote with these
    two columns left `NULL`) must come back as `None`, not raise and not fabricate an epoch --
    the display layer (`core/itemview.py`, `frontend/src/lib/format.ts`) renders that as
    "unknown," never as 1970 or a crash.
    """
    queue_id = await _make_queue(db)
    await settle.save_settle_records(
        db,
        queue_id,
        {
            "Release": settle.SettleRecord(
                fingerprint=(2, 250, 5.0), matched_scans=1, first_matched_at=_T0
            )
        },
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT first_observed_at, last_changed_at FROM item_settle "
        "WHERE queue_id = ? AND rel_path = 'Release'",
        (queue_id,),
    )
    row = await cursor.fetchone()
    assert row["first_observed_at"] is None
    assert row["last_changed_at"] is None

    loaded = await settle.load_settle_records(db, queue_id)
    assert loaded["Release"].first_observed_at is None
    assert loaded["Release"].last_changed_at is None


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


async def test_settle_progress_in_db_is_none_with_no_row(db):
    """The Preflight box's own read (`core/autoqueue.py.on_scan`) must degrade to `None`, not
    raise, for an item with no `item_settle` row yet -- the same "never scanned" case
    `is_settled_in_db` already treats as conservatively not settled.
    """
    queue_id = await _make_queue(db)
    assert await settle.settle_progress_in_db(db, queue_id, "Release") is None


async def test_settle_progress_in_db_reads_matched_scans_and_first_matched_at(db):
    """`matched_scans`/`first_matched_at` round-trip through `settle_progress_in_db` exactly as
    `is_settled_in_db` reads them internally -- this is the read `core/preflight.py.
    PreflightRow.wait_scans`/`wait_since` are populated from, so it must agree with the gate's
    own numbers rather than a second, independently-computed pair.
    """
    queue_id = await _make_queue(db)
    await settle.save_settle_records(
        db,
        queue_id,
        {
            "Release": settle.SettleRecord(
                fingerprint=(2, 250, 5.0), matched_scans=1, first_matched_at=_T0
            )
        },
    )
    await db.commit()

    progress = await settle.settle_progress_in_db(db, queue_id, "Release")
    assert progress is not None
    assert progress.matched_scans == 1
    # `first_matched_at` comes back as the raw ISO-8601 string the column stores, not a float --
    # `is_settled_from_progress` is the one place that parses it back.
    assert isinstance(progress.first_matched_at, str)
    assert settle.is_settled_from_progress(progress, now=_T0) is False
    assert (
        settle.is_settled_from_progress(progress, now=_T0 + 1_000_000.0) is False
    )  # count too low

    await settle.save_settle_records(
        db,
        queue_id,
        {
            "Release": settle.SettleRecord(
                fingerprint=(2, 250, 5.0), matched_scans=2, first_matched_at=_T0
            )
        },
    )
    await db.commit()
    progress = await settle.settle_progress_in_db(db, queue_id, "Release")
    assert progress is not None
    assert progress.matched_scans == 2
    assert settle.is_settled_from_progress(progress, now=_T0 + settle.SETTLE_MIN_AGE_S) is True


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


async def test_client_skip_enabled_defaults_off_and_round_trips(db):
    """Stage 2b of #18 (prompts/2026-08-23-settle-gate-skip.md): unlike `enabled` above,
    `client_skip_enabled` must default `False` on a fresh install -- it depends directly on an
    unverified guess (spec §13.4 #2), so "every new capability ships off" applies here without
    exception. Also asserts the two fields are independently persisted -- a save that touches
    only one via `SettleSettings(...)` (this module's own dataclass, not the API's merge logic)
    naturally carries whatever the caller passed for the other, exercised here by round-
    tripping both combinations.
    """
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is True
    assert settings.client_skip_enabled is False

    await settle.save_settle_settings(
        db, settle.SettleSettings(enabled=True, client_skip_enabled=True)
    )
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is True
    assert settings.client_skip_enabled is True

    await settle.save_settle_settings(
        db, settle.SettleSettings(enabled=False, client_skip_enabled=True)
    )
    settings = await settle.load_settle_settings(db)
    assert settings.enabled is False
    assert settings.client_skip_enabled is True


# --- The client-verdict skip's own matching (stage 2b of #18) -----------------------------
#
# Pure functions, no database, no scheduler -- `core/clientsync.py.ClientSyncScheduler.
# completed_transfers`'s own candidate shape (`(instance_id, instance_name, Transfer)`)
# constructed directly here rather than through the real poller/fake SABnzbd, the same "unit
# test the pure half separately from the wired-up half" split this module's own docstring
# describes for `compute_fingerprints`/`advance_settle`/`is_settled` above.
# `tests/test_autoqueue.py` and `tests/test_clientsync.py` cover the wired-up half.


def _transfer(
    *, phase: TransferPhase, content_path: str | None, completed_at: str | None = None
) -> Transfer:
    return Transfer(
        client_id="nzo-1",
        name="Show.S01.1080p-GRP",
        phase=phase,
        raw_status=phase.value,
        raw={},
        content_path=content_path,
        completed_at=completed_at,
    )


def test_find_client_completion_matches_on_exact_path_equality():
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(phase=TransferPhase.COMPLETED, content_path="/complete/ar-tv/Show.S01"),
        )
    ]
    result = settle.find_client_completion("/complete/ar-tv/Show.S01", candidates)
    assert result is not None
    instance_id, instance_name, transfer = result
    assert instance_id == 1
    assert instance_name == "SABnzbd"
    assert transfer.content_path == "/complete/ar-tv/Show.S01"


def test_find_client_completion_matches_a_component_boundary_child():
    # A client reporting a specific file inside a multi-file release directory.
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(
                phase=TransferPhase.COMPLETED,
                content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv",
            ),
        )
    ]
    result = settle.find_client_completion("/complete/ar-tv/Show.S01", candidates)
    assert result is not None


def test_find_client_completion_rejects_a_bare_prefix_that_straddles_a_name_boundary():
    """The exact anti-example this task's own handoff prompt names: a sibling directory that
    merely shares a name prefix must never read as a match -- the bug a bare `str.startswith`
    would produce.
    """
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(
                phase=TransferPhase.COMPLETED,
                content_path="/complete/ar-tv/Show.S01.EXTRA",
            ),
        )
    ]
    result = settle.find_client_completion("/complete/ar-tv/Show.S01", candidates)
    assert result is None


def test_find_client_completion_ignores_a_queue_side_non_terminal_status():
    """A queue-side status (still downloading/verifying/etc) must never satisfy the gate, even
    with an exact path match -- only a terminal, history-derived `COMPLETED` counts.
    """
    for phase in (
        TransferPhase.QUEUED,
        TransferPhase.DOWNLOADING,
        TransferPhase.PAUSED,
        TransferPhase.VERIFYING,
        TransferPhase.EXTRACTING,
        TransferPhase.SEEDING,
        TransferPhase.FAILED,
        TransferPhase.UNKNOWN,
    ):
        candidates = [
            (1, "SABnzbd", _transfer(phase=phase, content_path="/complete/ar-tv/Show.S01"))
        ]
        assert settle.find_client_completion("/complete/ar-tv/Show.S01", candidates) is None


def test_find_client_completion_ignores_a_completed_transfer_with_no_content_path():
    """Absent is not a verdict (spec §4.2): a `COMPLETED` transfer that never reports
    `content_path` at all (declared-unsupported, or the connector simply didn't populate it)
    must never match -- there is nothing to compare against, so this must not fall back to
    matching on name.
    """
    candidates = [(1, "SABnzbd", _transfer(phase=TransferPhase.COMPLETED, content_path=None))]
    assert settle.find_client_completion("/complete/ar-tv/Show.S01", candidates) is None


def test_find_client_completion_empty_candidates_is_not_a_match():
    """An unreachable client / blank queue-and-history response / `client_skip_enabled` off
    upstream all converge on an empty candidate list -- must read as "no information," never as
    an error.
    """
    assert settle.find_client_completion("/complete/ar-tv/Show.S01", []) is None


def test_find_client_completion_empty_item_path_never_matches():
    candidates = [(1, "SABnzbd", _transfer(phase=TransferPhase.COMPLETED, content_path="/x"))]
    assert settle.find_client_completion("", candidates) is None


def test_find_client_completion_returns_the_first_match_among_several_candidates():
    unrelated = _transfer(phase=TransferPhase.COMPLETED, content_path="/complete/ar-tv/Other")
    matching = _transfer(phase=TransferPhase.COMPLETED, content_path="/complete/ar-tv/Show.S01")
    candidates = [(1, "SABnzbd", unrelated), (2, "rTorrent", matching)]
    result = settle.find_client_completion("/complete/ar-tv/Show.S01", candidates)
    assert result is not None
    assert result[0] == 2
    assert result[1] == "rTorrent"


# --- The settle-gate skip's own completion delay (2026-08-23,
# prompts/2026-08-23-client-completion-delay.md, finding #9) -- `client_completion_ready`'s own
# timing question, checked only once `find_client_completion` above has already found a match.


def test_client_completion_ready_holds_a_young_completion():
    """A completion reported an instant ago must not satisfy the gate yet -- the whole point of
    the delay this task adds.
    """
    transfer = _transfer(
        phase=TransferPhase.COMPLETED,
        content_path="/complete/ar-tv/Show.S01",
        completed_at="2026-08-23T12:00:00+00:00",
    )
    now = settle._parse_iso("2026-08-23T12:00:00+00:00") + 3.0  # 3s after completion
    assert settle.client_completion_ready(transfer, first_observed_at=now, now=now) is False


def test_client_completion_ready_satisfies_once_the_hold_elapses():
    """The identical item as above, on a later pass once `CLIENT_COMPLETION_HOLD_S` has actually
    elapsed since the client's own completion time.
    """
    completed_epoch = settle._parse_iso("2026-08-23T12:00:00+00:00")
    transfer = _transfer(
        phase=TransferPhase.COMPLETED,
        content_path="/complete/ar-tv/Show.S01",
        completed_at="2026-08-23T12:00:00+00:00",
    )
    now = completed_epoch + settle.CLIENT_COMPLETION_HOLD_S
    assert settle.client_completion_ready(transfer, first_observed_at=now, now=now) is True


def test_client_completion_ready_is_immediate_for_a_completion_already_older_than_the_hold():
    """The ordinary case for anything the poller only picks up on a later pass: a release that
    finished five minutes ago must not wait a further `CLIENT_COMPLETION_HOLD_S` -- there must be
    no added latency at all here.
    """
    completed_epoch = settle._parse_iso("2026-08-23T12:00:00+00:00")
    transfer = _transfer(
        phase=TransferPhase.COMPLETED,
        content_path="/complete/ar-tv/Show.S01",
        completed_at="2026-08-23T12:00:00+00:00",
    )
    now = completed_epoch + 300.0  # 5 minutes later
    assert settle.client_completion_ready(transfer, first_observed_at=now, now=now) is True


def test_client_completion_ready_falls_back_to_first_observed_at_with_no_completed_at():
    """A connector that reports no `completed_at` at all (an undeclared `Field.COMPLETED_AT`, or
    a value its own mapping couldn't parse) must fall back to `first_observed_at` -- asserted
    explicitly here, not merely incidentally by some other test happening to leave
    `completed_at` unset.
    """
    transfer = _transfer(
        phase=TransferPhase.COMPLETED, content_path="/complete/ar-tv/Show.S01", completed_at=None
    )
    first_observed_at = 1_000_000.0
    # Younger than the hold, measured from `first_observed_at` -- must not be ready yet. If this
    # were (wrongly) measured from some other clock, this assertion could pass for the wrong
    # reason, so the "ready once the hold elapses" case right below is what actually proves the
    # fallback clock is the one being used.
    assert (
        settle.client_completion_ready(
            transfer, first_observed_at=first_observed_at, now=first_observed_at + 3.0
        )
        is False
    )
    assert (
        settle.client_completion_ready(
            transfer,
            first_observed_at=first_observed_at,
            now=first_observed_at + settle.CLIENT_COMPLETION_HOLD_S,
        )
        is True
    )


def test_client_completion_ready_falls_back_on_an_unparseable_completed_at():
    """An unparseable `completed_at` must read exactly like an absent one -- fall back to
    `first_observed_at`, never "already old enough."
    """
    transfer = _transfer(
        phase=TransferPhase.COMPLETED,
        content_path="/complete/ar-tv/Show.S01",
        completed_at="not-a-timestamp",
    )
    first_observed_at = 1_000_000.0
    assert (
        settle.client_completion_ready(
            transfer, first_observed_at=first_observed_at, now=first_observed_at + 3.0
        )
        is False
    )


# --- The withhold gate's own matching (stage 3 of #18) -- `find_client_failure`'s mirror-image
# coverage of `find_client_completion` above, over the identical pure `_transfer` helper.


def test_find_client_failure_matches_on_exact_path_equality():
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(phase=TransferPhase.FAILED, content_path="/complete/ar-tv/Show.S01"),
        )
    ]
    result = settle.find_client_failure("/complete/ar-tv/Show.S01", candidates)
    assert result is not None
    instance_id, instance_name, transfer = result
    assert instance_id == 1
    assert instance_name == "SABnzbd"
    assert transfer.content_path == "/complete/ar-tv/Show.S01"


def test_find_client_failure_matches_a_component_boundary_child():
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(
                phase=TransferPhase.FAILED,
                content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv",
            ),
        )
    ]
    result = settle.find_client_failure("/complete/ar-tv/Show.S01", candidates)
    assert result is not None


def test_find_client_failure_rejects_a_bare_prefix_that_straddles_a_name_boundary():
    """The identical anti-example `find_client_completion`'s own test names: a sibling directory
    that merely shares a name prefix must never read as a match.
    """
    candidates = [
        (
            1,
            "SABnzbd",
            _transfer(phase=TransferPhase.FAILED, content_path="/complete/ar-tv/Show.S01.EXTRA"),
        )
    ]
    result = settle.find_client_failure("/complete/ar-tv/Show.S01", candidates)
    assert result is None


def test_find_client_failure_ignores_a_non_terminal_status():
    """Only an explicit, terminal `FAILED` verdict may ever withhold anything (spec §4.2) -- a
    queue-side status, `UNKNOWN`, and even `COMPLETED` (a different question entirely, answered
    by `find_client_completion`) must never satisfy this gate.
    """
    for phase in (
        TransferPhase.QUEUED,
        TransferPhase.DOWNLOADING,
        TransferPhase.PAUSED,
        TransferPhase.VERIFYING,
        TransferPhase.EXTRACTING,
        TransferPhase.SEEDING,
        TransferPhase.COMPLETED,
        TransferPhase.UNKNOWN,
    ):
        candidates = [
            (1, "SABnzbd", _transfer(phase=phase, content_path="/complete/ar-tv/Show.S01"))
        ]
        assert settle.find_client_failure("/complete/ar-tv/Show.S01", candidates) is None


def test_find_client_failure_ignores_an_outright_failure_with_no_content_path():
    """`docs/transfers-redesign-spec.md` §4.3: "a client failing outright needs no code" -- an
    outright failure never lands any bytes, so it has no on-disk path to report at all. Absent is
    not a verdict (spec §4.2): no `content_path`, no match, ever, never a name-based fallback.
    """
    candidates = [(1, "SABnzbd", _transfer(phase=TransferPhase.FAILED, content_path=None))]
    assert settle.find_client_failure("/complete/ar-tv/Show.S01", candidates) is None


def test_find_client_failure_empty_candidates_is_not_a_match():
    """An unreachable client / blank queue-and-history response / `WithholdSettings.enabled` off
    upstream all converge on an empty candidate list -- must read as "no information," never as a
    withhold.
    """
    assert settle.find_client_failure("/complete/ar-tv/Show.S01", []) is None


def test_find_client_failure_empty_item_path_never_matches():
    candidates = [(1, "SABnzbd", _transfer(phase=TransferPhase.FAILED, content_path="/x"))]
    assert settle.find_client_failure("", candidates) is None


def test_find_client_failure_returns_the_first_match_among_several_candidates():
    unrelated = _transfer(phase=TransferPhase.FAILED, content_path="/complete/ar-tv/Other")
    matching = _transfer(phase=TransferPhase.FAILED, content_path="/complete/ar-tv/Show.S01")
    candidates = [(1, "SABnzbd", unrelated), (2, "rTorrent", matching)]
    result = settle.find_client_failure("/complete/ar-tv/Show.S01", candidates)
    assert result is not None
    assert result[0] == 2
    assert result[1] == "rTorrent"
