"""core/arrsync.py -- the poller (matching + import/removal detection) against a real fake
*arr (tests/fake_arr.py). Covers docs/arr-integration-spec.md's "Matching" and "The association
lifecycle" sections plus the handoff prompt's "at minimum" list.

Phase A scope reminder (see core/arrsync.py's own module docstring): only `(no status) ->
detected` and `detected/notified -> imported|gone` are reachable. Notify (`-> notified`) and
cleanup are phase B and are not exercised here.

2026-08-18 (`prompts/2026-08-18-arr-gone-grace-and-recheck.md`, production incident) adds the
amber `dropped` grace state between the two-pass guard and terminal `gone`, plus the retroactive
`gone`-row heal sweep -- see the "dropped" and "Retroactive heal" sections below.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_arr import run_fake_arr_server

from lftpweb.core.arrsync import (
    INITIAL_BACKOFF_S,
    MAX_BACKOFF_S,
    MAX_GONE_HEAL_ATTEMPTS,
    MAX_SCAN_COMMAND_CHECK_ATTEMPTS,
    MAX_SOURCE_DELETE_RETRY_ATTEMPTS,
    ArrSyncScheduler,
)
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
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
    local_path: str = "/l",
    arr_visible_path: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "arr_instance_id, arr_delete_completed, arr_visible_path) VALUES (?, 'q', '/r', ?, 1, ?, ?, ?)",
        (host_id, local_path, arr_instance_id, 1 if arr_delete_completed else 0, arr_visible_path),
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
    notify_on_complete: bool = False,
) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
        "notify_on_complete, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "Sonarr",
            kind,
            base_url,
            encrypt_secret(config_dir, api_key),
            1 if enabled else 0,
            1 if notify_on_complete else 0,
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
    arr_status_at: str | None = None,
    arr_download_id: str | None = None,
) -> int:
    # 2026-08-18 (`_dropped_grace_expired`'s own docstring): a `dropped` row's grace check reads
    # `arr_status_at` off the DB directly, so a test that seeds `arr_status='dropped'` straight
    # into the table (bypassing `_commit_dropped`, which always stamps it) must not leave it
    # NULL -- defaults to "now" whenever `arr_status` is set but the caller didn't pass one
    # explicitly, matching what any real row in that state actually looks like.
    if arr_status is not None and arr_status_at is None:
        arr_status_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, arr_status, arr_status_at, "
        "arr_download_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            queue_id,
            rel_path,
            1 if is_dir else 0,
            state,
            arr_status,
            arr_status_at,
            arr_download_id,
        ),
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
    return {
        "eventType": "downloadFolderImported",
        "downloadId": download_id,
        "sourceTitle": source_title,
    }


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


# --- dropped: the amber grace state (2026-08-18, production incident, support bundle
# lftpweb-support-0.2.3-20260818T013532Z) -- a queue record vanishing with no import evidence
# no longer commits terminal `gone` directly; it commits the amber `dropped` state, which is
# then re-checked every subsequent pass (not gated behind another two-pass observation) until it
# either reappears (-> detected), gets confirmed imported (-> imported), or its grace window
# expires (-> gone). -------------------------------------------------------------------------


async def test_dropped_when_record_vanishes_with_no_import_event_confirmed_over_two_passes(
    db, fake_arr_server, tmp_path
):
    """The old `gone`-after-two-passes test, updated for the new terminal semantics: the
    two-pass quiescence guard confirming "record gone, no import" now lands on `dropped`, not
    `gone`.
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
    assert row["arr_status"] == "dropped"
    assert await _event_kinds(db, item_id) == ["arr_queue_dropped"]


async def test_dropped_blip_spanning_both_quiescence_passes_never_reaches_gone(
    db, fake_arr_server, tmp_path
):
    """The production incident itself, reproduced via `fake_arr.py`'s own blip fixture: a
    download client (SABnzbd) returns a blank queue for both of the quiescence guard's
    observation passes, exactly the shape that used to flip 8 real items straight to `gone` in a
    single pass. The row must land on `dropped`, never `gone` -- and once the blip clears, the
    identical `downloadId` reappearing sends it straight back to `detected`.
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
        state="DOWNLOADING",  # still actively downloading -- the incident's own evidence
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]
    fake_arr_server.state.queue_empty_for_requests = 2  # the blip spans both observation passes

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "detected", "pass 1 -- pending only"

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "dropped", "confirmed after two blank passes -- never gone"

    # The blip has now cleared (`queue_empty_for_requests` exhausted) -- the *same* downloadId
    # reappearing is direct evidence this was transient, so it goes straight back to `detected`.
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_queue_dropped", "arr_matched"]
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_matched'", (item_id,)
    )
    message = (await cursor.fetchone())["message"]
    assert "dropping out" in message or "dropped" in message


async def test_dropped_reappearance_with_same_download_id_returns_to_detected(
    db, fake_arr_server, tmp_path
):
    """The narrower, direct-DB-seeded version of the scenario above: a row seeded straight at
    `dropped` (bypassing the two-pass mechanism) rematches on the identical `downloadId` alone,
    unlike `gone`/`cleaned` which refuse exactly that match.
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
        arr_status="dropped",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert row["arr_download_id"] == "abc123"


async def test_dropped_promotes_to_imported_when_history_shows_an_import(
    db, fake_arr_server, tmp_path
):
    """While `dropped`, an import history event promotes straight to `imported` -- rechecked
    every pass, no further two-pass wait (the row already spent a pass or more absent).
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
        arr_status="dropped",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []  # still absent from the queue
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_imported"]


async def test_dropped_promotion_to_imported_fires_rung_4_delete(db, fake_arr_server, tmp_path):
    """The `dropped` -> `imported` promotion runs through the exact same `_commit_terminal`
    path as any other import -- a move-mode item's deferred rung-4 source delete fires from it,
    not just the plain `arr_status` flip.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="dropped",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert len(pool.calls) == 1
    assert row["remote_deleted_at"] is not None
    assert row["remote_delete_pending"] is None


async def test_dropped_grace_window_expiry_commits_gone(db, fake_arr_server, tmp_path, monkeypatch):
    """Neither reappearance nor import within `DROPPED_GONE_GRACE_S` -- the row finally commits
    `gone`, today's terminal semantics unchanged (icon dims, nothing deleted). The grace constant
    is monkeypatched down to 0 so the very next check pass reads as expired -- this module's own
    `_dropped_grace_expired` compares against the real wall clock (a persisted `arr_status_at`),
    not `time.monotonic()`, so there's no clock to fake here, only the threshold.
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
        state="PARTIAL",
        arr_status="dropped",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    # Not yet expired at the real 6h default -- stays dropped.
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "dropped"

    monkeypatch.setattr("lftpweb.core.arrsync.DROPPED_GONE_GRACE_S", 0.0)
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_gone"]
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_gone'", (item_id,)
    )
    message = (await cursor.fetchone())["message"]
    assert "unconfirmed" in message
    assert "grace window" in message


# --- Ladder/cleanup gates: `dropped` behaves exactly like any other non-imported tracked status
# (2026-08-18) -------------------------------------------------------------------------------


async def test_dropped_never_fires_rung_4_delete_while_still_dropped(db, fake_arr_server, tmp_path):
    """A `dropped` row must not trigger the rung-4 deferred delete on its own -- only an actual
    promotion to `imported` (reappearance -> detected -> ... -> imported, or a history event
    found while dropped) may do that. Several passes of "still absent, no import" must leave the
    deferred delete exactly as it was.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="dropped",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )
    await scheduler.run_once()
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "dropped"
    assert pool.calls == []
    assert row["remote_deleted_at"] is None
    assert row["remote_delete_pending"] == "VERIFIED"


# --- Rung 4 of the move-mode delete ladder (2026-08-16,
# prompts/done/2026-08-16-move-delete-gate-ladder.md, resolving open issue #2 /
# docs/audit-v0.1.0.md G1) -------------------------------------------------------------------


class _FakeRemotePool:
    """Stands in for `core/remote.py`'s `RemoteConnectionPool` -- same shape as
    `tests/test_postprocess.py`'s own fake, kept local to this file rather than imported so
    this module's tests don't reach across test files for a fixture.

    `fail_times` (2026-08-17, the retry-sweep task) makes the first N calls raise instead of
    succeeding -- a transient SSH failure, the exact shape of the production incident
    (`SSH connection closed`) this task's retry sweep exists to survive. `0` (the default)
    preserves every existing rung-4 test's always-succeeds behavior unmodified.
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[object, str]] = []
        self._fail_times = fail_times

    async def delete_path(self, host, remote_path: str) -> None:
        self.calls.append((host, remote_path))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("SSH connection closed")


async def _async_host():
    from lftpweb.core.remote import HostConfig

    return HostConfig(
        id=1, address="seedbox.invalid", port=22, username="u", auth_method="key", key_path="/k"
    )


async def test_move_mode_delete_survives_detected_and_notified_fires_only_on_imported(
    db, fake_arr_server, tmp_path
):
    """An *arr-tracked, `move`-mode item that `core/postprocess.py` already deferred
    (`item.remote_delete_pending` set, rungs 1-3 already cleared) must keep its source through
    `detected`/`notified` and delete only on the confirmed `imported` transition -- never a
    poll interval earlier, and not before the second confirming pass.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )

    # Still sitting in the *arr's own queue -- source must stay untouched through `detected`.
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="downloading",
        )
    ]
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert pool.calls == []
    assert row["remote_deleted_at"] is None

    # Now confirm import: record gone, history has the import event -- two consecutive passes.
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected", "first observing pass -- not yet confirmed"
    assert pool.calls == []

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert len(pool.calls) == 1
    _, remote_path = pool.calls[0]
    assert remote_path == "/r/Show.S01E05.1080p-GRP"
    assert row["remote_deleted_at"] is not None
    assert row["remote_delete_pending"] is None
    kinds = await _event_kinds(db, item_id)
    assert "remote_delete" in kinds
    assert kinds.index("arr_imported") < kinds.index("remote_delete")


async def test_move_mode_delete_never_fires_while_dropped_or_after_gone(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The *arr's queue record disappearing with no import history event -- through both the
    amber `dropped` grace state and the eventual terminal `gone` once the window expires -- must
    never delete the source. A deferred item stays deferred exactly as the ladder promises: no
    timeout, no automatic fallback (2026-08-18: this used to reach `gone` directly after the
    two-pass guard; it now passes through `dropped` first -- see `docs/decisions.md`).
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="detected",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )

    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []  # no import event at all -- removed, not imported
    await scheduler.run_once()
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "dropped"
    assert pool.calls == []
    assert row["remote_deleted_at"] is None
    assert row["remote_delete_pending"] == "VERIFIED", "left exactly as it was -- no fallback"
    kinds = await _event_kinds(db, item_id)
    assert "remote_delete" not in kinds

    # Grace window expires -- still no fallback delete, even once terminal `gone` commits.
    monkeypatch.setattr("lftpweb.core.arrsync.DROPPED_GONE_GRACE_S", 0.0)
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert pool.calls == []
    assert row["remote_deleted_at"] is None
    assert row["remote_delete_pending"] == "VERIFIED"
    assert "remote_delete" not in await _event_kinds(db, item_id)


async def test_gone_commit_on_pending_source_delete_names_it_in_the_event(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """Purely audit-trail visibility (2026-08-17,
    `prompts/done/2026-08-17-stranded-source-delete-retry.md`): rung 4 never fires on `gone` --
    unchanged, see the test above -- so a source delete that was still pending when the *arr's
    queue record vanished just sits stranded silently otherwise. Production evidence: 15 items
    went `notified` -> `gone` with `remote_delete_pending` still set. `_commit_terminal`'s own
    `arr_gone` event message now names the withheld delete so History can answer "why is this
    still on the seedbox" without a second lookup. 2026-08-18: the terminal `gone` commit this
    now names is the grace-window-expiry one (`dropped` -> `gone`), not the two-pass guard's own
    commit -- that one lands on `dropped`, not `gone`, per this same task.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="notified",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []
    await scheduler.run_once()
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "dropped"

    monkeypatch.setattr("lftpweb.core.arrsync.DROPPED_GONE_GRACE_S", 0.0)
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert row["remote_delete_pending"] == "VERIFIED", "still withheld -- no behavior change"
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_gone'", (item_id,)
    )
    message = (await cursor.fetchone())["message"]
    assert "remote_delete_pending" in message or "deferred source delete" in message
    assert "Files page" in message


async def test_gone_commit_with_no_pending_source_delete_leaves_the_event_unchanged(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The common case -- nothing was ever deferred (`remote_delete_pending` NULL) -- must not
    gain the new sentence; it would be misleading noise for an item that was never *arr-tracked
    with a `move` delete deferred in the first place.
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
        state="PARTIAL",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    await scheduler.run_once()
    assert (await _item_row(db, item_id))["arr_status"] == "dropped"

    monkeypatch.setattr("lftpweb.core.arrsync.DROPPED_GONE_GRACE_S", 0.0)
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_gone'", (item_id,)
    )
    message = (await cursor.fetchone())["message"]
    assert "remote_delete_pending" not in message
    assert "Files page" not in message


# --- Rung-4 retry sweep: a failed deferred delete must not strand the source forever
# (2026-08-17, prompts/done/2026-08-17-stranded-source-delete-retry.md, live on both the user's
# test and production systems) -----------------------------------------------------------------


async def test_stranded_source_delete_retries_and_succeeds_next_pass(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The production incident this task fixes, reproduced end to end: rung 4's deferred delete
    fails once (a transient SSH error, `_FakeRemotePool(fail_times=1)`), the debt survives, and
    cleanup is withheld so the local copy is not removed out from under it (§7.3/§7.4's ladder
    order, restored by `_maybe_cleanup`'s new gate) -- then the very next pass, with the seedbox
    healthy again, the retry sweep clears the debt and cleanup proceeds. Full ladder order
    preserved across the failure, matching the incident's own audit trail
    (`arr_imported` -> failed `remote_delete` -> `arr_cleanup` anyway) except the local copy no
    longer goes first.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Show.S01E05.1080p-GRP"
    (local_root / rel_path).write_bytes(b"the release")
    write_if_needed(str(local_root))

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        arr_instance_id=instance_id,
        arr_delete_completed=True,
        local_path=str(local_root),
    )
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        rel_path,
        state="VERIFIED",
        arr_status="imported",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool(fail_times=1)
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["remote_delete_pending"] == "VERIFIED", "debt survives a failed attempt"
    assert row["remote_deleted_at"] is None
    assert (local_root / rel_path).exists(), "cleanup must withhold while source is still owed"
    kinds = await _event_kinds(db, item_id)
    assert "remote_delete_failed" in kinds
    assert "arr_cleanup_withheld" in kinds
    assert "remote_delete" not in kinds
    assert "arr_cleanup" not in kinds

    clock["t"] += INITIAL_BACKOFF_S + 1  # past the first attempt's backoff window
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["remote_delete_pending"] is None
    assert row["remote_deleted_at"] is not None
    assert row["arr_status"] == "cleaned"
    assert not (local_root / rel_path).exists(), "local copy removed once source delete cleared"
    kinds = await _event_kinds(db, item_id)
    assert "remote_delete" in kinds
    assert "arr_cleanup" in kinds
    assert kinds.index("remote_delete") < kinds.index("arr_cleanup"), "ladder order preserved"


async def test_stranded_source_delete_retries_back_off_and_eventually_pause(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """Repeated failures must not spam a `remote_delete_failed` event every ~60s pass for as
    long as a seedbox stays down -- attempts space out (each `run_once` here is deliberately
    made far enough apart, via the monkeypatched clock, that every one of them lands past the
    current backoff window) and, once `MAX_SOURCE_DELETE_RETRY_ATTEMPTS` is exhausted, this
    process pauses and writes exactly one `remote_delete_retries_paused` event.
    `remote_delete_pending` stays set throughout -- never silently dropped -- and a later pass,
    even with the pool healthy again, does not resume on its own (paused is sticky until a
    restart's clean in-memory slate).
    """
    clock = {"t": 0.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="imported",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool(fail_times=MAX_SOURCE_DELETE_RETRY_ATTEMPTS)
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )

    for _attempt in range(MAX_SOURCE_DELETE_RETRY_ATTEMPTS):
        await scheduler.run_once()
        clock["t"] += MAX_BACKOFF_S + 1  # always past whatever the current backoff is

    row = await _item_row(db, item_id)
    assert row["remote_delete_pending"] == "VERIFIED", "debt survives -- never silently dropped"
    assert len(pool.calls) == MAX_SOURCE_DELETE_RETRY_ATTEMPTS
    kinds = await _event_kinds(db, item_id)
    assert kinds.count("remote_delete_failed") == MAX_SOURCE_DELETE_RETRY_ATTEMPTS
    assert kinds.count("remote_delete_retries_paused") == 1
    assert kinds[-1] == "remote_delete_retries_paused"

    # A later pass, even with the pool healthy again, must not resume on its own.
    pool._fail_times = 0
    clock["t"] += MAX_BACKOFF_S + 1
    await scheduler.run_once()
    assert len(pool.calls) == MAX_SOURCE_DELETE_RETRY_ATTEMPTS, "no further attempts once paused"


async def test_stranded_source_delete_self_heals_a_row_stranded_before_this_shipped(
    db, fake_arr_server, tmp_path
):
    """Retroactive self-heal, required by design: a row already stranded before this fix
    shipped -- `cleaned`, remote copy still alive, `remote_delete_pending` still set from the
    original one-shot attempt that failed -- matches the sweep's own query and gets its delete
    attempted on the very first pass after upgrade. No migration, no state massaging: the query
    alone is the self-heal.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="cleaned",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "cleaned", "arr_status untouched -- only the source debt clears"
    assert row["remote_delete_pending"] is None
    assert row["remote_deleted_at"] is not None
    assert len(pool.calls) == 1
    assert "remote_delete" in await _event_kinds(db, item_id)


async def test_stranded_source_delete_sweep_is_a_no_op_when_the_feature_is_not_wired(
    db, fake_arr_server, tmp_path
):
    """A process that never wired `remote_pool`/`host_provider` (most of this module's own
    tests, and the identical rule the one-shot rung-4 call already follows) must leave a
    stranded row exactly as it was -- no crash, no attempt, no bookkeeping accumulated for a
    feature this process can't act on anyway.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="cleaned",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))  # no remote_pool/host_provider
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["remote_delete_pending"] == "VERIFIED"
    assert row["remote_deleted_at"] is None


# --- Retroactive heal for a row already stranded `gone` before `dropped` existed (2026-08-18,
# production incident: the 8 items whose pipeline run finished -- and so set
# `remote_delete_pending` -- only *after* their `gone` verdict already committed) -----------


async def test_gone_heal_promotes_a_stranded_row_when_history_shows_an_import(
    db, fake_arr_server, tmp_path
):
    """A row already sitting at `arr_status='gone'` with a stranded rung-4 debt, exactly the
    production shape -- promoted to `imported` the moment `import_events` turns up a matching
    event, with the deferred source delete firing from the very same pass (through the normal
    `_commit_terminal` path, "proceed normally from here").
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="gone",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    pool = _FakeRemotePool()
    scheduler = ArrSyncScheduler(
        db=db, config_dir=str(tmp_path), remote_pool=pool, host_provider=_async_host
    )
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert len(pool.calls) == 1
    assert row["remote_deleted_at"] is not None
    assert row["remote_delete_pending"] is None
    kinds = await _event_kinds(db, item_id)
    assert "arr_imported" in kinds
    assert "remote_delete" in kinds


async def test_gone_heal_ignored_when_remote_delete_pending_is_not_set(
    db, fake_arr_server, tmp_path
):
    """The sweep's own query is gated on `remote_delete_pending IS NOT NULL` -- a `gone` row
    with nothing owed is not this sweep's business at all (and must not spuriously promote to
    `imported` just because history happens to show something for its downloadId).
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
        state="PARTIAL",
        arr_status="gone",
        arr_download_id="abc123",
    )
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert "arr_imported" not in await _event_kinds(db, item_id)


async def test_gone_heal_gives_up_after_bounded_attempts_with_no_import_ever_found(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """A genuinely-gone row -- no import ever shows up in history -- must not be queried
    forever. `MAX_GONE_HEAL_ATTEMPTS` attempts, spaced apart by the same growing-delay backoff
    the rung-4 retry sweep uses, then one `arr_gone_heal_giving_up` event and no more.
    `remote_delete_pending` stays set throughout -- never silently dropped, same discipline as
    the rung-4 sweep's own pause.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01E05.1080p-GRP",
        state="VERIFIED",
        arr_status="gone",
        arr_download_id="abc123",
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []  # never an import, ever

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    for _attempt in range(MAX_GONE_HEAL_ATTEMPTS):
        await scheduler.run_once()
        clock["t"] += MAX_BACKOFF_S + 1  # always past whatever the current backoff is

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert row["remote_delete_pending"] == "VERIFIED", "debt survives -- never silently dropped"
    kinds = await _event_kinds(db, item_id)
    assert kinds.count("arr_gone_heal_giving_up") == 1
    assert kinds[-1] == "arr_gone_heal_giving_up"

    # A later pass must not resume on its own, even once history would satisfy it.
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]
    clock["t"] += MAX_BACKOFF_S + 1
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone", "paused -- no further automatic attempts"


async def test_gone_heal_skips_and_counts_an_attempt_when_download_id_is_absent(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """No `arr_download_id` recorded means no exact history lookup is possible -- this counts as
    an attempt (per this sweep's own hard cap) rather than exempting the row from ever giving up.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)
    await db.execute("UPDATE path_queue SET sync_mode = 'move' WHERE id = ?", (queue_id,))
    await db.commit()
    item_id = await _seed_item(
        db, queue_id, "Show.S01E05.1080p-GRP", state="VERIFIED", arr_status="gone"
    )
    await db.execute("UPDATE item SET remote_delete_pending = 'VERIFIED' WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    for _attempt in range(MAX_GONE_HEAL_ATTEMPTS):
        await scheduler.run_once()
        clock["t"] += MAX_BACKOFF_S + 1

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert row["remote_delete_pending"] == "VERIFIED"
    assert (await _event_kinds(db, item_id)).count("arr_gone_heal_giving_up") == 1


# --- Namespace-mismatch detection (2026-08-17, production evidence:
# private_data/debug_logs/productionlftpweb.log -- the user's *arr instances mount the same
# storage at a different path than lftpweb does) ------------------------------------------------


async def test_path_mismatch_warns_once_when_visible_path_is_unset(db, fake_arr_server, tmp_path):
    """`arr_visible_path` unset means every notify would push lftpweb's own root -- detectable
    the moment a match commits, from the matched record's own `outputPath` alone, well before
    the first notify ever fires. Fires once per (queue, derived root) even across two items
    matched in the same pass that share the same wrong root, and stays at exactly one across a
    second pass that matches nothing new -- the debounce is per-root, not per-item or per-pass.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id, local_path="/l")
    item_a = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    item_b = await _seed_item(db, queue_id, "Show.S01E06.1080p-GRP")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="a1",
            title="Show S01E05 1080p GRP",
            output_path="/arrside/box-dc-tv/Show.S01E05.1080p-GRP",
        ),
        _queue_record(
            download_id="a2",
            title="Show S01E06 1080p GRP",
            output_path="/arrside/box-dc-tv/Show.S01E06.1080p-GRP",
        ),
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    kinds_a = await _event_kinds(db, item_a)
    kinds_b = await _event_kinds(db, item_b)
    assert kinds_a.count("arr_path_mismatch") + kinds_b.count("arr_path_mismatch") == 1
    cursor = await db.execute("SELECT message FROM event WHERE kind = 'arr_path_mismatch'")
    message = (await cursor.fetchone())["message"]
    assert "/arrside/box-dc-tv" in message
    assert "Path as seen by the *arr" in message

    # A second pass matches nothing new (both items already `detected`) -- still exactly one.
    await scheduler.run_once()
    cursor = await db.execute("SELECT COUNT(*) AS n FROM event WHERE kind = 'arr_path_mismatch'")
    assert (await cursor.fetchone())["n"] == 1


async def test_path_mismatch_fires_nothing_when_visible_path_is_correctly_set(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        arr_instance_id=instance_id,
        local_path="/l",
        arr_visible_path="/arrside/box-dc-tv",
    )
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="a1",
            title="Show S01E05 1080p GRP",
            output_path="/arrside/box-dc-tv/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert "arr_path_mismatch" not in await _event_kinds(db, item_id)


async def test_path_mismatch_fires_when_visible_path_is_set_but_wrong(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, local_path="/l", arr_visible_path="/wrong/path"
    )
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="a1",
            title="Show S01E05 1080p GRP",
            output_path="/arrside/box-dc-tv/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    kinds = await _event_kinds(db, item_id)
    assert "arr_path_mismatch" in kinds
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_path_mismatch'", (item_id,)
    )
    message = (await cursor.fetchone())["message"]
    assert "/arrside/box-dc-tv" in message


async def test_path_mismatch_fires_nothing_when_output_path_is_none(db, fake_arr_server, tmp_path):
    """A title-fallback match has no *arr-side path to compare against at all."""
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id, local_path="/l")
    item_id = await _seed_item(db, queue_id, "Show S01E05 1080p GRP")
    fake_arr_server.state.queue_records = [
        {
            "downloadId": "a1",
            "title": "Show S01E05 1080p GRP",
            "outputPath": None,
            "trackedDownloadState": "downloading",
        }
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert "arr_path_mismatch" not in await _event_kinds(db, item_id)


async def test_path_mismatch_fires_nothing_when_notify_on_complete_is_off(
    db, fake_arr_server, tmp_path
):
    """Nothing will ever be pushed, so a namespace mismatch here is moot -- matches this
    module's "everything defaults off produces zero events, not noise" convention.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=False,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id, local_path="/l")
    item_id = await _seed_item(db, queue_id, "Show.S01E05.1080p-GRP")
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="a1",
            title="Show S01E05 1080p GRP",
            output_path="/arrside/box-dc-tv/Show.S01E05.1080p-GRP",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert "arr_path_mismatch" not in await _event_kinds(db, item_id)


# --- Scan-command outcome verification (2026-08-17, production evidence:
# private_data/debug_logs/productionlftpweb.log) -- `notify_arr`'s push was otherwise
# fire-and-forget: a 201 only means "command queued," never "the *arr could act on this path."
# Every test below pins a matching, still-`downloading` queue record for the item's own
# `downloadId` so `_check_import` resets its pending-verdict guard every pass and never commits
# `imported`/`gone` mid-test -- keeping these tests focused on the scan-command column alone. ---


async def test_scan_command_completed_clears_the_column_with_no_event(
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
        db, queue_id, "Show.S01E05.1080p-GRP", arr_status="notified", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="downloading",
        )
    ]
    fake_arr_server.state.command_statuses[7] = {"id": 7, "status": "completed"}
    await db.execute("UPDATE item SET arr_scan_command_id = 7 WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_scan_command_id"] is None
    assert "arr_scan_command_failed" not in await _event_kinds(db, item_id)


async def test_scan_command_failed_writes_one_warning_event_naming_the_path(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id, local_path="/l")
    item_id = await _seed_item(
        db, queue_id, "Show.S01E05.1080p-GRP", arr_status="notified", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="downloading",
        )
    ]
    fake_arr_server.state.command_statuses[9] = {"id": 9, "status": "failed"}
    await db.execute("UPDATE item SET arr_scan_command_id = 9 WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_scan_command_id"] is None
    kinds = await _event_kinds(db, item_id)
    assert kinds.count("arr_scan_command_failed") == 1
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_scan_command_failed'",
        (item_id,),
    )
    message = (await cursor.fetchone())["message"]
    assert "/l/Show.S01E05.1080p-GRP" in message
    assert "Path as seen by the *arr" in message


async def test_scan_command_404_clears_silently(db, fake_arr_server, tmp_path):
    """An unknown command id -- pruned by the *arr, or lost across a restart -- is "no evidence
    either way," not a failure: cleared with no event, same as a resolved `completed`.
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
        db, queue_id, "Show.S01E05.1080p-GRP", arr_status="notified", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="downloading",
        )
    ]
    # No entry seeded in `command_statuses` for id=42 -- simulates a pruned/unknown command.
    await db.execute("UPDATE item SET arr_scan_command_id = 42 WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_scan_command_id"] is None
    assert "arr_scan_command_failed" not in await _event_kinds(db, item_id)


async def test_scan_command_check_gives_up_silently_after_bounded_attempts(
    db, fake_arr_server, tmp_path
):
    """A command that never resolves (still `queued` every pass) must not accumulate a
    per-pass API call forever -- `MAX_SCAN_COMMAND_CHECK_ATTEMPTS` passes, then this process
    gives up silently: the column clears, no event, same as a 404.
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
        db, queue_id, "Show.S01E05.1080p-GRP", arr_status="notified", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123",
            title="Show S01E05 1080p GRP",
            output_path="/data/torrents/complete/Show.S01E05.1080p-GRP",
            tracked_download_state="downloading",
        )
    ]
    fake_arr_server.state.command_statuses[3] = {"id": 3, "status": "queued"}
    await db.execute("UPDATE item SET arr_scan_command_id = 3 WHERE id = ?", (item_id,))
    await db.commit()

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    for attempt in range(1, MAX_SCAN_COMMAND_CHECK_ATTEMPTS):
        await scheduler.run_once()
        row = await _item_row(db, item_id)
        assert row["arr_scan_command_id"] == 3, f"cleared too early, after attempt {attempt}"

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_scan_command_id"] is None
    assert "arr_scan_command_failed" not in await _event_kinds(db, item_id)


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


async def test_gone_commit_on_a_removed_both_row_does_not_publish_an_item_delta(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """Regression (2026-08-16, live): files deleted by hand carried the row to `REMOVED_BOTH`
    -- out of the published projection (`core/engine.py._project`) and off the Files page --
    then removing the *arr queue record made the `gone` commit publish an `item_delta` that
    resurrected the dead node in every connected client: visible, un-actionable (nothing local
    to delete, nothing remote to queue), and only cleared by the next connect-time snapshot.
    The state write and the audit event must still happen; only the WS publish is skipped.
    2026-08-18: the two-pass guard's own commit now lands on `dropped`, not `gone` -- both it and
    the eventual grace-window-expiry `gone` commit must skip the publish, since `_publish_item`'s
    `REMOVED_BOTH` guard is generic (keyed off the row's own `state`, unaffected by which
    `arr_status` transition triggered the call).
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
        state="REMOVED_BOTH",
        arr_status="detected",
        arr_download_id="abc123",
    )
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = []  # removed from the *arr queue, never imported

    events = EventBus()
    subscriber = events.subscribe()
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path), events=events)
    await scheduler.run_once()
    await scheduler.run_once()  # two-pass quiescence guard

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "dropped"
    assert await _event_kinds(db, item_id) == ["arr_queue_dropped"]
    assert subscriber.empty()

    monkeypatch.setattr("lftpweb.core.arrsync.DROPPED_GONE_GRACE_S", 0.0)
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert await _event_kinds(db, item_id) == ["arr_queue_dropped", "arr_gone"]
    assert subscriber.empty()


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
