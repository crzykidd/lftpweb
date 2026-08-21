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

from lftpweb.core.arrsync import ArrSyncScheduler
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
) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "arr_instance_id, arr_visible_path) VALUES (?, 'q', '/r', ?, 1, ?, ?)",
        (host_id, local_path, arr_instance_id, arr_visible_path),
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

    rows = {r.title: r for r in scheduler.preflight_rows({instance_id})}
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

    rows = scheduler.preflight_rows({instance_id})
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

    assert scheduler.preflight_rows({instance_id}) == []


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

    rows = scheduler.preflight_rows({instance_id})
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

    assert scheduler.preflight_rows({instance_id}) == []


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

    assert scheduler.preflight_rows({instance_id}) == []


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
    assert [r.title for r in scheduler.preflight_rows({instance_id})] == ["Flaky.Release"]

    # The *arr's own queue blanks out for a beat (the real production shape) -- one missed pass,
    # well inside the hold window.
    fake_arr_server.state.queue_records = []
    clock["t"] += 60.0
    await scheduler.run_once()
    assert [r.title for r in scheduler.preflight_rows({instance_id})] == ["Flaky.Release"]

    # Still missing, now past `PREFLIGHT_HOLD_S` total -- the row must finally clear, and must
    # not have been resurrected by the intervening held pass.
    clock["t"] += PREFLIGHT_HOLD_S
    await scheduler.run_once()
    assert scheduler.preflight_rows({instance_id}) == []


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

    rows = scheduler.preflight_rows({instance_id})
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

    assert len(scheduler.preflight_rows({instance_id})) == 1
    assert scheduler.preflight_rows(set()) == []
    assert scheduler.preflight_rows({instance_id + 999}) == []
