"""core/arrsync.py -- the poller (matching + import/removal detection) against a real fake
*arr (tests/fake_arr.py). Covers docs/arr-integration-spec.md's "Matching" and "The association
lifecycle" sections plus the handoff prompt's "at minimum" list.

Phase A scope reminder (see core/arrsync.py's own module docstring): only `(no status) ->
detected` and `detected/notified -> imported|gone` are reachable. Notify (`-> notified`) and
cleanup are phase B and are not exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_arr import run_fake_arr_server

from lftpweb.core.arrsync import ArrSyncScheduler
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.db import migrate

# --- Fixtures / helpers ----------------------------------------------------------------------


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
        "INSERT INTO host (name, address, username, auth_method) VALUES ('h', 'a', 'u', 'agent')"
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_queue(
    db: aiosqlite.Connection,
    host_id: int,
    *,
    arr_instance_id: int | None = None,
    arr_delete_completed: bool = False,
) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "arr_instance_id, arr_delete_completed) VALUES (?, 'q', '/r', '/l', 1, ?, ?)",
        (host_id, arr_instance_id, 1 if arr_delete_completed else 0),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_instance(
    db: aiosqlite.Connection,
    config_dir: str,
    *,
    kind: str = "sonarr",
    base_url: str,
    api_key: str,
    enabled: bool = True,
) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
        "notify_on_complete, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (
            "Sonarr",
            kind,
            base_url,
            encrypt_secret(config_dir, api_key),
            1 if enabled else 0,
            now,
            now,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_item(
    db: aiosqlite.Connection,
    queue_id: int,
    rel_path: str,
    *,
    is_dir: bool = True,
    state: str = "REMOTE_ONLY",
    arr_status: str | None = None,
    arr_download_id: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, arr_status, arr_download_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (queue_id, rel_path, 1 if is_dir else 0, state, arr_status, arr_download_id),
    )
    await db.commit()
    return cursor.lastrowid


async def _item_row(db: aiosqlite.Connection, item_id: int) -> aiosqlite.Row:
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row is not None
    return row


async def _event_kinds(db: aiosqlite.Connection, item_id: int | None = None) -> list[str]:
    if item_id is None:
        cursor = await db.execute("SELECT kind FROM event ORDER BY id")
    else:
        cursor = await db.execute(
            "SELECT kind FROM event WHERE item_id = ? ORDER BY id", (item_id,)
        )
    return [r["kind"] for r in await cursor.fetchall()]


def _queue_record(
    *, download_id: str, title: str, output_path: str, tracked_download_state: str = "downloading"
):
    return {
        "downloadId": download_id,
        "title": title,
        "outputPath": output_path,
        "trackedDownloadState": tracked_download_state,
    }


def _import_event(*, download_id: str, source_title: str | None = None):
    # `"downloadFolderImported"` -- the camelCase **string** a real Sonarr v3 response body
    # actually serializes `eventType` as (verified against a live instance, 2026-08-15). The
    # numeric form (`3`) is a fixture bug this codebase shipped with: modeling the wire format
    # wrongly is exactly why two real Sonarr imports were misclassified `gone` on the first
    # live run before this fix -- see `test_imported_also_confirmed_via_legacy_numeric_event_type`
    # below for the (separate, tolerance-only) numeric case.
    return {"eventType": "downloadFolderImported", "downloadId": download_id, "sourceTitle": source_title}


# --- Matching ----------------------------------------------------------------------------


async def test_matches_by_output_path_basename(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",  # deliberately not matching the item name
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP/",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert row["arr_download_id"] == "abc123"
    assert row["arr_status_at"] is not None
    assert await _event_kinds(db, item_id) == ["arr_matched"]


async def test_matches_by_normalized_title_fallback(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(db, queue_id, "My.Movie.2024.mkv", is_dir=False)

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="mv1",
            title="My Movie 2024.mkv",  # normalizes equal to the item name, basename does not
            output_path="/data/torrents/complete/some-other-folder-name",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert row["arr_download_id"] == "mv1"


async def test_non_top_level_item_is_never_matched(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    # A nested file -- '/' in rel_path -- must never become a matching candidate even if a
    # record's outputPath basename or title happens to equal its rel_path verbatim.
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP/episode.mkv", is_dir=False)

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show.S01E05.1080p-GRP/episode.mkv",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP/episode.mkv",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] is None
    assert await _event_kinds(db, item_id) == []


async def test_item_in_unbound_queue_is_never_matched(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    # Note: no arr_instance_id -- this queue has no integration at all.
    unbound_queue_id = await _seed_queue(db, host_id, arr_instance_id=None)
    item_id = await _seed_item(db, unbound_queue_id, "Show.S01E05.1080p-GRP")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP/",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] is None
    # The instance itself is never even queried -- no bound queue means no attempt at all
    # (spec: "For each enabled instance with >=1 bound queue").
    assert instance_id is not None  # instance exists, just irrelevant here


async def test_disabled_instance_never_polled(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        enabled=False,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="whatever",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP/",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] is None


# --- The slow multi-file import scenario (spec "Failure modes") --------------------------


async def test_slow_multi_file_import_does_not_confirm_while_still_importing(
    db, fake_arr_server, tmp_path
):
    """A record present in `importing` with per-file history events accreting must NOT
    produce `imported`, no matter how many passes run or how much history accumulates.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01.Season.Pack",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="season1",
    )

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="season1",
            title="Show S01 Season Pack",
            output_path="/data/torrents/complete/Show.S01.Season.Pack",
            tracked_download_state="importing",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    for file_num in range(1, 4):
        fake_arr_server.state.history_events.append(
            _import_event(download_id="season1", source_title="Show S01 Season Pack")
        )
        await scheduler.run_once()
        row = await _item_row(db, item_id)
        assert row["arr_status"] == "detected", f"flipped early after file {file_num}"

    assert await _event_kinds(db, item_id) == []  # no arr_imported, ever, while importing


# --- imported: two-pass confirmation ------------------------------------------------------


async def test_imported_requires_record_gone_and_history_and_two_passes(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )

    # The record has already left the queue, and history has the import event -- both
    # requirements hold, but only one pass has observed them so far.
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [
        _import_event(download_id="abc123", source_title="Show S01E05 1080p GRP")
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected", "must not commit on the first observing pass"
    assert await _event_kinds(db, item_id) == []

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert await _event_kinds(db, item_id) == ["arr_imported"]


async def test_imported_confirmed_via_string_event_type_end_to_end(db, fake_arr_server, tmp_path):
    """Pins the actual fix (2026-08-15): `eventType` arrives as the camelCase **string**
    `"downloadFolderImported"` in a real response body, spelled out literally here (not via the
    `_import_event` helper, which every other test in this file already uses) so this test
    fails loudly if a future change ever reverts the wire-format assumption back to the numeric
    code that shipped wrong the first time and misclassified two real Sonarr imports as `gone`.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [
        {
            "eventType": "downloadFolderImported",
            "downloadId": "abc123",
            "sourceTitle": "Show S01E05 1080p GRP",
        }
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert await _event_kinds(db, item_id) == ["arr_imported"]


async def test_imported_also_confirmed_via_legacy_numeric_event_type(db, fake_arr_server, tmp_path):
    """The tolerance fallback (`HistoryEvent.is_import_event`'s numeric branch), proven
    end-to-end through the same poller path as the string case above, not just at the
    `HistoryEvent` unit level -- an *arr version or serializer setting that still emits the
    pre-fix numeric `eventType` must not regress to `gone`.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [
        {"eventType": 3, "downloadId": "abc123", "sourceTitle": "Show S01E05 1080p GRP"}
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert await _event_kinds(db, item_id) == ["arr_imported"]


async def test_imported_also_confirmed_when_record_explicitly_reports_imported_state(
    db, fake_arr_server, tmp_path
):
    """Requirement 1's other satisfying condition: the record is still present but reports
    `trackedDownloadState: imported` rather than having vanished outright.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="imported",
        )
    ]
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"


async def test_pending_candidacy_resets_if_signals_stop_holding(db, fake_arr_server, tmp_path):
    """A candidacy that doesn't hold on the very next pass must not silently carry over --
    two passes means *consecutive*.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    # Pass 1: record gone, history present -- "imported" candidate registered.
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "detected"

    # Pass 2: the record reappears (e.g. a transient API blip) -- requirement 1 no longer
    # holds, so the guard must reset, not just pause.
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "detected"

    # Pass 3: gone again -- this must be pass *one* of a fresh two-pass count, not pass three
    # of the original count.
    fake_arr_server.state.queue_records = []
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "detected"
    assert await _event_kinds(db, item_id) == []

    # Pass 4: confirmed.
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "imported"


# --- gone: two-pass confirmation, no history ----------------------------------------------


async def test_gone_when_record_vanishes_with_no_import_event(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="PARTIAL",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []  # no import event at all -- removed, not imported

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "detected"

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert await _event_kinds(db, item_id) == ["arr_gone"]


# --- Upgrade-regrab: a fresh association on a different downloadId ------------------------


async def test_regrab_on_a_gone_item_starts_a_fresh_association(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="REMOTE_ONLY",
        arr_status="gone",
        arr_download_id="old-download-id",
    )

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="new-download-id",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert row["arr_download_id"] == "new-download-id"
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_matched"]


async def test_identical_download_id_does_not_resurrect_a_gone_item(db, fake_arr_server, tmp_path):
    """The exact opposite of a regrab: the *same* downloadId reappearing (e.g. a slow-to-clear
    queue listing) must not flip a settled `gone` row back to `detected`.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="REMOTE_ONLY",
        arr_status="gone",
        arr_download_id="same-id",
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="same-id",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert await _event_kinds(db, item_id) == []


async def test_cleaned_item_can_also_be_regrabbed(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="REMOVED_LOCAL",
        arr_status="cleaned",
        arr_download_id="old-id",
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="upgrade-id",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert row["arr_download_id"] == "upgrade-id"


# --- Per-instance failure isolation ---------------------------------------------------------


async def test_unreachable_instance_backs_off_and_does_not_retry_immediately(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id)
    fake_arr_server.state.fail_all = True

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert await _event_kinds(db) == ["arr_unreachable"]
    assert instance_id in scheduler._backoff

    # A second pass immediately after must not re-attempt (still backing off) -- no second
    # event row.
    await scheduler.run_once()
    assert await _event_kinds(db) == ["arr_unreachable"]


async def test_one_unreachable_instance_does_not_block_another(db, tmp_path):
    """The spec's own requirement: an unreachable instance "must never block or slow the loop
    for other instances." Two real, independent fake *arr instances -- one made unreachable,
    the other left healthy -- in the same `run_once()` pass.
    """
    async with run_fake_arr_server() as broken, run_fake_arr_server() as healthy:
        broken.state.fail_all = True

        host_id = await _seed_host(db)
        broken_instance_id = await _seed_instance(
            db, str(tmp_path), base_url=broken.base_url, api_key=broken.state.api_key
        )
        healthy_instance_id = await _seed_instance(
            db, str(tmp_path), base_url=healthy.base_url, api_key=healthy.state.api_key
        )
        await _seed_queue(db, host_id, arr_instance_id=broken_instance_id)
        healthy_queue_id = await _seed_queue(db, host_id, arr_instance_id=healthy_instance_id)
        item_id = await _seed_item(db, healthy_queue_id, "Show.S01E05.1080p-GRP")

        healthy.state.queue_records = [
            _queue_record(
                download_id="abc123",
                title="whatever",
                output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            )
        ]

        scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
        await scheduler.run_once()

        row = await _item_row(db, item_id)
        assert row["arr_status"] == "detected"
        assert "arr_unreachable" in await _event_kinds(db)


# --- item_delta publish (DESIGN.md §2.2: persist -> read back -> publish) ------------------


async def test_match_publishes_an_item_delta(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="whatever",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]

    events = EventBus()
    subscriber = events.subscribe()
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path), events=events)
    await scheduler.run_once()

    message = subscriber.get_nowait()
    assert message["type"] == "item_delta"
    assert message["queue_id"] == queue_id
    nodes = {n["id"]: n for n in message["nodes"]}
    assert nodes[item_id]["arr_status"] == "detected"
    # arr_download_id is deliberately not published (spec "Data model": "Not published in the
    # item projection").
    assert "arr_download_id" not in nodes[item_id]


# --- Settings ------------------------------------------------------------------------------


async def test_arr_settings_round_trip(db):
    from lftpweb.core.arrsync import ArrSettings, load_arr_settings, save_arr_settings

    default = await load_arr_settings(db)
    assert default.poll_interval_s == 60.0

    await save_arr_settings(db, ArrSettings(poll_interval_s=15.0))
    reloaded = await load_arr_settings(db)
    assert reloaded.poll_interval_s == 15.0


# --- Migration -------------------------------------------------------------------------------


async def test_migration_018_applies_to_a_seeded_pre_018_database(tmp_path):
    import lftpweb.db as db_module

    real_migrations_dir = db_module.MIGRATIONS_DIR
    staged = tmp_path / "migrations"
    staged.mkdir()
    for path in sorted(real_migrations_dir.glob("*.sql")):
        if int(path.stem.split("_")[0]) <= 17:
            (staged / path.name).write_text(path.read_text())

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db_module, "MIGRATIONS_DIR", staged)
            await migrate(conn)

            await conn.execute(
                "INSERT INTO host (id, name, address, username, auth_method) "
                "VALUES (1, 'h', 'a', 'u', 'agent')"
            )
            await conn.execute(
                "INSERT INTO path_queue (id, host_id, name, remote_path, local_path) "
                "VALUES (1, 1, 'q', '/r', '/l')"
            )
            await conn.execute(
                "INSERT INTO item (id, queue_id, rel_path, is_dir, state) "
                "VALUES (1, 1, 'foo', 0, 'DOWNLOADED')"
            )
            await conn.commit()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db_module, "MIGRATIONS_DIR", real_migrations_dir)
            await migrate(conn)  # migration 018 now applies on top of real, seeded data

        cursor = await conn.execute(
            "SELECT arr_instance_id, arr_delete_completed, arr_visible_path FROM path_queue "
            "WHERE id = 1"
        )
        row = await cursor.fetchone()
        assert (row["arr_instance_id"], row["arr_delete_completed"], row["arr_visible_path"]) == (
            None,
            0,
            None,
        )

        cursor = await conn.execute(
            "SELECT arr_status, arr_status_at, arr_download_id FROM item WHERE id = 1"
        )
        row = await cursor.fetchone()
        assert (row["arr_status"], row["arr_status_at"], row["arr_download_id"]) == (
            None,
            None,
            None,
        )

        cursor = await conn.execute("SELECT COUNT(*) FROM arr_instance")
        assert (await cursor.fetchone())[0] == 0  # no rows inserted by the migration itself

        # The prior data survived the migration batch (no cascade-delete regression).
        cursor = await conn.execute("SELECT COUNT(*) FROM item")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await conn.close()
