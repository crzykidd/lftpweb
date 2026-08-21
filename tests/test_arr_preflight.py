"""Preflight (docs/transfers-redesign-spec.md §4, prefigured; this task's own handoff prompt,
prompts/done/2026-08-20-preflight-box.md) -- `core/arrsync.py.ArrSyncScheduler.preflight_rows`,
the pure projection of the poller's own latest pass. Same fixtures/idioms as
`tests/test_arrsync.py` (a fresh in-memory db + `fake_arr_server`); a matched-item test and the
flap-tolerance test are what's genuinely new here, everything else exercises
`_preflight_candidates`'s attribution rules end to end through a real poll pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from lftpweb.core.arrclient import QueueRecord
from lftpweb.core.arrsync import ArrSyncScheduler, _parse_timeleft, _record_matches_any_item
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.preflight import PREFLIGHT_HOLD_S
from lftpweb.db import migrate

# --- Fixtures / helpers (duplicated from tests/test_arrsync.py -- this project's own convention
# of small per-file helpers rather than cross-file imports, see that file's own top-level ones)
# ---------------------------------------------------------------------------------------------


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
    local_path: str = "/l",
    arr_visible_path: str | None = None,
    name: str = "q",
    short_name: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "arr_instance_id, arr_visible_path, short_name) VALUES (?, ?, '/r', ?, 1, ?, ?, ?)",
        (host_id, name, local_path, arr_instance_id, arr_visible_path, short_name),
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


async def _seed_item(db: aiosqlite.Connection, queue_id: int, rel_path: str) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state) VALUES (?, ?, 1, 'REMOTE_ONLY')",
        (queue_id, rel_path),
    )
    await db.commit()
    return cursor.lastrowid


def _queue_record(
    *,
    download_id: str,
    title: str,
    output_path: str | None,
    tracked_download_state: str = "downloading",
    size: int | None = None,
    sizeleft: int | None = None,
    timeleft: str | None = None,
    download_client: str | None = None,
):
    record: dict = {
        "downloadId": download_id,
        "title": title,
        "outputPath": output_path,
        "trackedDownloadState": tracked_download_state,
    }
    if size is not None:
        record["size"] = size
    if sizeleft is not None:
        record["sizeleft"] = sizeleft
    if timeleft is not None:
        record["timeleft"] = timeleft
    if download_client is not None:
        record["downloadClient"] = download_client
    return record


# --- Attribution ------------------------------------------------------------------------------


async def test_attributes_by_arr_visible_path_prefix_and_omits_no_match(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    tv_queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv"
    )
    movies_queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/movies"
    )

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="tv1", title="Show.S01E01", output_path="/data/tv/Show.S01E01"),
        _queue_record(download_id="mv1", title="Movie.2024", output_path="/data/movies/Movie.2024"),
        # `/data/tvshows/...` must NOT match `/data/tv` -- component-boundary, not bare prefix.
        _queue_record(
            download_id="tvshows1",
            title="Not.Really.Tv",
            output_path="/data/tvshows/Not.Really.Tv",
        ),
        # Matches nothing configured at all -- silence is correct.
        _queue_record(
            download_id="other1", title="Something.Else", output_path="/data/other/Something.Else"
        ),
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = {r.title: r for r in await scheduler.preflight_rows({instance_id})}
    assert set(rows) == {"Show.S01E01", "Movie.2024"}
    assert rows["Show.S01E01"].queue_id == tv_queue_id
    assert rows["Movie.2024"].queue_id == movies_queue_id
    # The box's row shape is source-agnostic (docs/transfers-redesign-spec.md §4's settle-gate
    # follow-up, prefigured) -- `source`/`source_label`/`source_kind` are how an *arr row
    # identifies itself, not special-cased fields of their own.
    assert rows["Show.S01E01"].source == "arr"
    assert rows["Show.S01E01"].source_kind == "sonarr"
    assert rows["Show.S01E01"].source_label == "Sonarr"


async def test_no_output_path_attributed_only_when_instance_has_one_queue(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(db, host_id, arr_instance_id=instance_id)

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="one", title="Single.Queue.Release", output_path=None)
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = await scheduler.preflight_rows({instance_id})
    assert len(rows) == 1
    assert rows[0].title == "Single.Queue.Release"
    assert rows[0].queue_id == queue_id


async def test_no_output_path_omitted_when_instance_has_several_queues(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/movies")

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="ambiguous", title="Ambiguous.Release", output_path=None)
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    assert await scheduler.preflight_rows({instance_id}) == []


async def test_size_and_sizeleft_carried_through_when_present(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="withsize",
            title="Has.Size",
            output_path="/data/tv/Has.Size",
            size=1_000_000,
            sizeleft=250_000,
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = await scheduler.preflight_rows({instance_id})
    assert len(rows) == 1
    assert rows[0].size_bytes == 1_000_000
    assert rows[0].size_remaining_bytes == 250_000


async def test_already_imported_at_the_arr_level_is_never_projected(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="done",
            title="Already.Imported",
            output_path="/data/tv/Already.Imported",
            tracked_download_state="imported",
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    assert await scheduler.preflight_rows({instance_id}) == []


# --- No duplicate at handover ------------------------------------------------------------------


async def test_a_matched_record_is_not_projected(db, fake_arr_server, tmp_path):
    """The core "no duplicate at handover" guarantee: the moment a record matches a real
    lftpweb item (`_match_items` -> `arr_matched`, exercised here via the same real poll pass),
    it must never also appear in the Preflight projection -- a release visible twice in one view
    is exactly the failure mode this task names as the one to avoid.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv"
    )
    await _seed_item(db, queue_id, "Show.S01E01")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="matched1", title="Show.S01E01", output_path="/data/tv/Show.S01E01"
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    # The real matcher did its job (sanity check this test actually exercises the matched path,
    # not an attribution miss that would trivially also produce zero preflight rows).
    cursor = await db.execute("SELECT arr_status FROM item WHERE queue_id = ?", (queue_id,))
    row = await cursor.fetchone()
    assert row["arr_status"] == "detected"

    assert await scheduler.preflight_rows({instance_id}) == []


# --- Flap tolerance -----------------------------------------------------------------------------


async def test_flap_tolerance_holds_across_one_missed_poll_then_expires(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The SABnzbd-blank-queue-blip discipline (`core/arrsync.py`'s own module docstring,
    2026-08-18 production incident), applied to this projection: a record missing for one pass
    must not blink the row out and back; missing for longer than `PREFLIGHT_HOLD_S` must drop it
    for good -- proof this can never itself accumulate.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="flap1", title="Flaky.Release", output_path="/data/tv/Flaky.Release"
        )
    ]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Flaky.Release"]

    # The *arr's own queue blanks out for a beat (the real production shape) -- one missed pass,
    # well inside the hold window.
    fake_arr_server.state.queue_records = []
    clock["t"] += 60.0
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Flaky.Release"]

    # Still missing, now past `PREFLIGHT_HOLD_S` total -- the row must finally clear, and must
    # not have been resurrected by the intervening held pass.
    clock["t"] += PREFLIGHT_HOLD_S
    await scheduler.run_once()
    assert await scheduler.preflight_rows({instance_id}) == []


async def test_reappearance_within_the_hold_window_refreshes_rather_than_duplicates(
    db, fake_arr_server, tmp_path, monkeypatch
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    record = _queue_record(
        download_id="flap2", title="Flaky.Release.2", output_path="/data/tv/Flaky.Release.2"
    )
    fake_arr_server.state.queue_records = [record]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    fake_arr_server.state.queue_records = []
    clock["t"] += 60.0
    await scheduler.run_once()

    fake_arr_server.state.queue_records = [record]
    clock["t"] += 5.0
    await scheduler.run_once()

    rows = await scheduler.preflight_rows({instance_id})
    assert [r.title for r in rows] == ["Flaky.Release.2"]  # exactly one row, never two


# --- preflight_rows' own instance filter -------------------------------------------------------


async def test_preflight_rows_filters_to_the_caller_supplied_instance_set(
    db, fake_arr_server, tmp_path
):
    """`preflight_rows`' own `enabled_instance_ids` filter -- the caller's live "is this
    instance still enabled and bound" check (`api/jobs.py.get_preflight`) -- rather than
    `ArrSyncScheduler` re-deriving that from its own cache, so a disabled instance's rows stop
    being returned the moment the caller says so, not after `PREFLIGHT_HOLD_S`.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="x1", title="X.Release", output_path="/data/tv/X.Release")
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    assert len(await scheduler.preflight_rows({instance_id})) == 1
    assert await scheduler.preflight_rows(set()) == []
    assert await scheduler.preflight_rows({instance_id + 999}) == []


# --- Queue tag (2026-08-21, "we moved the columns around") -------------------------------------


async def test_queue_name_and_short_name_are_carried_on_the_row(db, fake_arr_server, tmp_path):
    """`PreflightRow` used to carry `queue_id` with no name at all, so an *arr row could not
    show the queue tag every other Transfers row shows (`lib/queueDisplayName.ts.
    queueDisplayName`). Both the full name and the short-name fallback must survive onto the row.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(
        db,
        host_id,
        arr_instance_id=instance_id,
        arr_visible_path="/data/tv",
        name="DC-TV",
        short_name="TV",
    )

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="q1", title="Show.S01E01", output_path="/data/tv/Show.S01E01")
    ]
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = await scheduler.preflight_rows({instance_id})
    assert len(rows) == 1
    assert rows[0].queue_name == "DC-TV"
    assert rows[0].queue_short_name == "TV"


async def test_queue_short_name_is_none_when_not_set(db, fake_arr_server, tmp_path):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(download_id="q1", title="Show.S01E01", output_path="/data/tv/Show.S01E01")
    ]
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = await scheduler.preflight_rows({instance_id})
    assert rows[0].queue_name == "q"  # `_seed_queue`'s own default
    assert rows[0].queue_short_name is None


# --- Remaining time (2026-08-21, "we missed the remaining time") -------------------------------


def test_parse_timeleft_present_absent_and_unparseable():
    """`_parse_timeleft` is the pure boundary between the *arr's own `timeleft` wire string and
    `PreflightRow.remaining_s` -- unit-tested directly, per this codebase's own convention for
    pure helpers, alongside the end-to-end coverage below.
    """
    assert _parse_timeleft("00:03:00") == 180.0
    assert _parse_timeleft("1.02:00:00") == 1 * 86400 + 2 * 3600  # the "[d.]hh:mm:ss" form
    assert _parse_timeleft("00:00:05.500000") == 5.0  # fractional seconds truncate, not round
    assert _parse_timeleft(None) is None  # absent
    assert _parse_timeleft("") is None  # absent
    assert _parse_timeleft("not-a-duration") is None  # unparseable
    assert _parse_timeleft("00:00:00") is None  # paused/stalled -- never a fabricated "0s left"


async def test_remaining_time_present_absent_and_unparseable_end_to_end(
    db, fake_arr_server, tmp_path
):
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="present",
            title="Has.Timeleft",
            output_path="/data/tv/Has.Timeleft",
            timeleft="00:03:00",
        ),
        _queue_record(
            download_id="absent", title="No.Timeleft", output_path="/data/tv/No.Timeleft"
        ),
        _queue_record(
            download_id="garbage",
            title="Bad.Timeleft",
            output_path="/data/tv/Bad.Timeleft",
            timeleft="not-a-duration",
        ),
    ]
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = {r.title: r for r in await scheduler.preflight_rows({instance_id})}
    assert rows["Has.Timeleft"].remaining_s == 180.0
    assert rows["No.Timeleft"].remaining_s is None
    assert rows["Bad.Timeleft"].remaining_s is None


async def test_download_client_is_carried_through_when_present(db, fake_arr_server, tmp_path):
    """The chip tooltip's own provenance detail (2026-08-21, user's own words: "Downloading from
    '<download client name>' from arr") -- read from `raw`, no extra request.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="withclient",
            title="Has.Client",
            output_path="/data/tv/Has.Client",
            download_client="SABnzbd",
        ),
        _queue_record(download_id="noclient", title="No.Client", output_path="/data/tv/No.Client"),
    ]
    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    rows = {r.title: r for r in await scheduler.preflight_rows({instance_id})}
    assert rows["Has.Client"].download_client == "SABnzbd"
    assert rows["No.Client"].download_client is None
    # An *arr row's own wait isn't bound by scan count -- `wait_scans`/`wait_since`
    # (`core/preflight.py.PreflightRow`'s own docstring) stay unset for either row.
    assert rows["Has.Client"].wait_scans is None
    assert rows["Has.Client"].wait_since is None


# --- Evict on handover (2026-08-21, "a handed-over release lingers in Preflight for up to
# 150s") -- confirmed by reading the code, not observed in a browser --------------------------


async def test_retired_row_evicts_immediately_not_held(db, fake_arr_server, tmp_path, monkeypatch):
    """The defect: `_preflight_candidates` excludes a record the moment it matches a real
    `item`, but before the 2026-08-21 fix `_update_preflight` couldn't tell that apart from the
    record simply going missing for a beat -- so a just-handed-over release sat in the cache,
    duplicated alongside its own new Active/pending row, until `PREFLIGHT_HOLD_S` (150s) elapsed.
    This asserts the row is gone the very next pass, seconds later, not merely eventually.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv"
    )

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="handoff1", title="Show.S01E01", output_path="/data/tv/Show.S01E01"
        )
    ]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Show.S01E01"]

    # The release lands: a real `item` row now exists with the matching name, so this pass's own
    # `_preflight_candidates` excludes the record for matching it -- the ordinary "no duplicate
    # at handover" rule -- but unlike a genuine blip, this is a *known* reason, not silence.
    await _seed_item(db, queue_id, "Show.S01E01")
    clock["t"] += 1.0  # nowhere near PREFLIGHT_HOLD_S
    await scheduler.run_once()

    # Must be gone THIS pass, not held for PREFLIGHT_HOLD_S alongside a genuinely-missing row.
    assert await scheduler.preflight_rows({instance_id}) == []


async def test_a_merely_missing_row_still_holds_for_the_full_window(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The flap-tolerance side of the same fix must survive untouched: a record that simply
    disappears from the *arr's own report (no matching item, the SABnzbd blank-queue blip this
    hold exists for) is still held for the full `PREFLIGHT_HOLD_S`, not evicted early just
    because eviction now has a fast path. Guards against a fix that (wrongly) evicts anything
    absent from `seen`, not just what is actually `retired`.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="flap3", title="Flaky.Release.3", output_path="/data/tv/Flaky.Release.3"
        )
    ]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Flaky.Release.3"]

    # Blip -- no matching item exists anywhere, so this is "merely absent," not "retired."
    fake_arr_server.state.queue_records = []
    clock["t"] += 1.0
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Flaky.Release.3"]

    # Only past the full hold window does it finally clear.
    clock["t"] += PREFLIGHT_HOLD_S
    await scheduler.run_once()
    assert await scheduler.preflight_rows({instance_id}) == []


# --- Request-time retirement (2026-08-21, "it does take 20-30 seconds ... sometimes fast and
# sometimes slow") -- the poll-cadence term left behind by the evict-on-handover fix above.
# `test_retired_row_evicts_immediately_not_held` proves the per-*poll* fast path; this proves
# retirement no longer needs a poll pass to happen at all. ---------------------------------------


async def test_retirement_happens_at_request_time_with_no_poller_pass_in_between(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The central fix. Before it, `preflight_rows` was a pure read of the in-memory hold --
    whether a row was "retired" was decided exactly once per `_update_preflight` call, i.e. once
    per `ArrSettings.poll_interval_s` (60s default). A release that lands locally right after a
    poll sat here, duplicated against its own new Active/pending row, for up to that whole
    interval (plus the frontend's own poll on top). This test seeds the matching `item` directly
    -- never calling `run_once()` a second time -- so it can only pass if `preflight_rows` itself
    re-asks "does a matching item exist" on every call, against the live database, not just once
    per poll pass. Confirmed failing against pre-fix code before implementing the fix.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv"
    )

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="latency1", title="Show.S01E01", output_path="/data/tv/Show.S01E01"
        )
    ]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Show.S01E01"]

    # The release lands locally -- no second `run_once()`, no clock advance, no poll of any kind.
    await _seed_item(db, queue_id, "Show.S01E01")

    assert await scheduler.preflight_rows({instance_id}) == []


async def test_a_row_with_no_last_seen_record_is_unaffected_by_the_request_time_check(
    db, fake_arr_server, tmp_path, monkeypatch
):
    """The request-time check must not regress flap tolerance: a row currently held blind (its
    identity went missing from the *arr's own last-fetched records, the SABnzbd-blip case) has
    nothing to re-test the predicate against and must keep showing, exactly as
    `test_a_merely_missing_row_still_holds_for_the_full_window` already proves for the poll-driven
    path -- this proves the *new* per-request filter doesn't accidentally drop it either.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    await _seed_queue(db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv")

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="blind1", title="Blind.Release", output_path="/data/tv/Blind.Release"
        )
    ]

    clock = {"t": 1_000.0}
    monkeypatch.setattr("lftpweb.core.arrsync.time.monotonic", lambda: clock["t"])

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    # Blip -- the record vanishes from the *arr's own report, so this instance's last-seen
    # records no longer carry this identity at all.
    fake_arr_server.state.queue_records = []
    clock["t"] += 1.0
    await scheduler.run_once()

    assert [r.title for r in await scheduler.preflight_rows({instance_id})] == ["Blind.Release"]


# --- The shared predicate (this task: "extract and share the match predicate; do not
# reimplement it") -- `_record_matches_any_item` is the one function both `_preflight_candidates`
# (the per-poll path) and `preflight_rows` (the request-time path, above) call; this exercises it
# directly with a table of cases so both paths are provably answering the identical question. ----


@pytest.mark.parametrize(
    ("output_path", "title", "item_names", "expected"),
    [
        # No items at all -- never a match, regardless of the record's own fields.
        ("/data/tv/Show.S01E01", "Show.S01E01", frozenset(), False),
        # Exact basename-of-output_path match.
        ("/data/tv/Show.S01E01", "Some Other Title", frozenset({"Show.S01E01"}), True),
        # Normalized-title fallback (case-fold, `.`/`_`/space equivalence) when there's no
        # output_path at all.
        (None, "Show.S01E01", frozenset({"show s01e01"}), True),
        # Neither the basename nor the normalized title matches anything in the set.
        ("/data/tv/Show.S01E01", "Show.S01E01", frozenset({"Unrelated.Release"}), False),
        # Matches one of several candidate item names, not just the first.
        (
            "/data/tv/Show.S01E01",
            "Show.S01E01",
            frozenset({"Unrelated.Release", "Show.S01E01"}),
            True,
        ),
    ],
)
def test_record_matches_any_item_table(output_path, title, item_names, expected):
    record = QueueRecord(
        download_id="d1",
        title=title,
        output_path=output_path,
        tracked_download_state="downloading",
        raw={},
    )
    assert _record_matches_any_item(record, item_names) is expected


async def test_shared_predicate_agrees_at_both_call_sites(db, fake_arr_server, tmp_path):
    """Same record, same item-name fact, checked through both real call sites --
    `_preflight_candidates`' per-poll retirement (via a real `run_once()`) and `preflight_rows`'
    own request-time re-check (via a second call with the item seeded in between, no second
    poll) -- both must agree it is a match, because both call the identical
    `_record_matches_any_item`, never two independent definitions.
    """
    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db, host_id, arr_instance_id=instance_id, arr_visible_path="/data/tv"
    )

    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="shared1", title="Show.S01E01", output_path="/data/tv/Show.S01E01"
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))

    # Path 1: the item already exists before the poll ever runs -- `_preflight_candidates`'
    # own per-pass retirement never even lets the row appear.
    await _seed_item(db, queue_id, "Show.S01E01")
    await scheduler.run_once()
    assert await scheduler.preflight_rows({instance_id}) == []

    # Path 2: the identical record/item-name fact, but discovered by `preflight_rows`' own
    # request-time re-check instead -- a fresh scheduler with no item yet, one poll, then the
    # item lands, no second poll.
    scheduler2 = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    host_id2 = await _seed_host(db)
    instance_id2 = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
    )
    queue_id2 = await _seed_queue(
        db, host_id2, arr_instance_id=instance_id2, arr_visible_path="/data/tv2"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="shared2", title="Show.S02E02", output_path="/data/tv2/Show.S02E02"
        )
    ]
    await scheduler2.run_once()
    assert [r.title for r in await scheduler2.preflight_rows({instance_id2})] == ["Show.S02E02"]
    await _seed_item(db, queue_id2, "Show.S02E02")
    assert await scheduler2.preflight_rows({instance_id2}) == []
