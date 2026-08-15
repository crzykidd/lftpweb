"""Sonarr/Radarr integration, phase B (docs/arr-integration-spec.md "Cleanup") -- the poller's
cleanup pass and its notify-retry, against a real fake *arr (`tests/fake_arr.py`).

Covers the handoff prompt's "at minimum" list for cleanup: the slow multi-file import headline
test never triggers cleanup; withheld cases (failed verify, active job) each write their event
and never delete; suppression lands before deletion; `gone` items are never cleaned, ever;
cleanup rides the existing absence-grace machinery rather than writing `REMOVED_LOCAL` itself.
Plus notify's bounded retry, which lives in this module (`ArrSyncScheduler._maybe_retry_notify`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from lftpweb.core.arrsync import MAX_NOTIFY_RETRY_ATTEMPTS, ArrSyncScheduler
from lftpweb.core.crypto import encrypt_secret
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
    local_path: str,
    staging_path: str | None = None,
    arr_instance_id: int | None = None,
    arr_delete_completed: bool = False,
) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, arr_instance_id, arr_delete_completed) VALUES (?, 'q', '/r', ?, ?, 1, ?, ?)",
        (
            host_id,
            local_path,
            staging_path,
            arr_instance_id,
            1 if arr_delete_completed else 0,
        ),
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
    is_dir: bool = False,
    state: str = "DOWNLOADED",
    arr_status: str | None = None,
    arr_download_id: str | None = None,
    pending_download_prefix: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, arr_status, arr_download_id, "
        "pending_download_prefix) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            queue_id,
            rel_path,
            1 if is_dir else 0,
            state,
            arr_status,
            arr_download_id,
            pending_download_prefix,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_job(db: aiosqlite.Connection, item_id: int, *, state: str = "running") -> int:
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'pget', ?)", (item_id, state)
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
    return {"eventType": 3, "downloadId": download_id, "sourceTitle": source_title}


# --- The headline test: cleanup must never fire during a slow multi-file import -------------


async def test_cleanup_never_fires_while_still_importing(db, fake_arr_server, tmp_path):
    """The 40 GB-season-pack-over-slow-network scenario: a queue record present in `importing`
    with per-file history events accreting across several poller passes must never trigger
    cleanup, no matter how many passes run -- even with `arr_delete_completed` on.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    release_dir = local_root / "Show.S01.Season.Pack"
    release_dir.mkdir()
    (release_dir / "e01.mkv").write_bytes(b"episode one")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        local_path=str(local_root),
        arr_instance_id=instance_id,
        arr_delete_completed=True,
    )
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01.Season.Pack",
        is_dir=True,
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

    for file_num in range(1, 5):
        fake_arr_server.state.history_events.append(
            _import_event(download_id="season1", source_title="Show S01 Season Pack")
        )
        await scheduler.run_once()
        row = await _item_row(db, item_id)
        assert row["arr_status"] == "detected", f"flipped early after file {file_num}"
        assert release_dir.exists(), f"bytes removed early after file {file_num}"

    assert "arr_cleanup" not in await _event_kinds(db, item_id)
    assert "arr_cleanup_withheld" not in await _event_kinds(db, item_id)


# --- Cleanup fires once imported is confirmed, in the same pass as confirmation -------------


async def test_cleanup_fires_once_imported_is_confirmed(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        local_path=str(local_root),
        arr_instance_id=instance_id,
        arr_delete_completed=True,
    )
    item_id = await _seed_item(
        db,
        queue_id,
        rel_path,
        state="DOWNLOADED",
        arr_status="detected",
        arr_download_id="abc123",
    )

    write_if_needed(str(local_root))
    fake_arr_server.state.queue_records = []
    fake_arr_server.state.history_events = [_import_event(download_id="abc123")]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected", "first observing pass -- not yet confirmed"
    assert (local_root / rel_path).exists()

    await scheduler.run_once()
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "cleaned"
    assert not (local_root / rel_path).exists()
    assert row["auto_queue_suppressed"] == 1
    # Cleanup must never write item.state directly -- the normal absence-grace machinery is
    # left to discover the disappearance and carry it to REMOVED_LOCAL on its own clock.
    assert row["state"] == "DOWNLOADED"
    kinds = await _event_kinds(db, item_id)
    assert "arr_imported" in kinds
    assert "arr_cleanup" in kinds
    assert "arr_cleanup_withheld" not in kinds


# --- Withheld: verification failed -----------------------------------------------------------


async def test_cleanup_withheld_when_verification_failed(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        local_path=str(local_root),
        arr_instance_id=instance_id,
        arr_delete_completed=True,
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="CORRUPT", arr_status="imported", arr_download_id="abc123"
    )

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported", "withheld, not terminal"
    assert row["auto_queue_suppressed"] == 0, "never suppressed on a withheld cleanup"
    assert (local_root / rel_path).exists()
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_cleanup_withheld"]
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_cleanup_withheld'",
        (item_id,),
    )
    message = (await cursor.fetchone())["message"]
    assert "CORRUPT" in message


# --- Withheld: active job ----------------------------------------------------------------------


async def test_cleanup_withheld_when_a_job_is_active(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        local_path=str(local_root),
        arr_instance_id=instance_id,
        arr_delete_completed=True,
    )
    item_id = await _seed_item(
        db,
        queue_id,
        rel_path,
        state="DOWNLOADED",
        arr_status="imported",
        arr_download_id="abc123",
    )
    await _seed_job(db, item_id, state="running")

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "imported"
    assert row["auto_queue_suppressed"] == 0
    assert (local_root / rel_path).exists()
    kinds = await _event_kinds(db, item_id)
    assert kinds == ["arr_cleanup_withheld"]
    cursor = await db.execute(
        "SELECT message FROM event WHERE item_id = ? AND kind = 'arr_cleanup_withheld'",
        (item_id,),
    )
    message = (await cursor.fetchone())["message"]
    assert "active job" in message


# --- `gone` items are never cleaned, ever -----------------------------------------------------


async def test_gone_items_are_never_cleaned(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db, str(tmp_path), base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
    )
    queue_id = await _seed_queue(
        db,
        host_id,
        local_path=str(local_root),
        arr_instance_id=instance_id,
        arr_delete_completed=True,
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="DOWNLOADED", arr_status="gone", arr_download_id="abc123"
    )

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "gone"
    assert (local_root / rel_path).exists()
    assert "arr_cleanup" not in await _event_kinds(db, item_id)
    assert "arr_cleanup_withheld" not in await _event_kinds(db, item_id)


# --- Notify retry (spec "Notify": "retry on the next poller tick (bounded retries)") --------


async def test_notify_retry_pushes_when_primary_attempt_never_ran(db, fake_arr_server, tmp_path):
    """An item that reached a stable, successful local outcome (`DOWNLOADED`, no pending
    download-prefix, no active job) but was never pushed by `PostprocessPipeline` -- the
    poller's own retry path pushes it.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db, host_id, local_path=str(local_root), arr_instance_id=instance_id
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="DOWNLOADED", arr_status="detected", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123", title="whatever", output_path="/data/torrents/complete/x"
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    assert len(fake_arr_server.state.command_calls) == 1
    assert fake_arr_server.state.command_calls[0]["path"] == str(local_root / rel_path)
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "notified"
    assert "arr_notified" in await _event_kinds(db, item_id)


async def test_notify_retry_does_not_fire_before_the_item_has_finished_downloading(
    db, fake_arr_server, tmp_path
):
    """The *arr's own queue can list a release before lftpweb has finished pulling it down --
    `arr_status == 'detected'` alone is not evidence the pipeline is done.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db, host_id, local_path=str(local_root), arr_instance_id=instance_id
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="PARTIAL", arr_status="detected", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123", title="whatever", output_path="/data/torrents/complete/x"
        )
    ]

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    assert fake_arr_server.state.command_calls == []
    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"


async def test_notify_retry_recovers_after_a_transient_failure(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db, host_id, local_path=str(local_root), arr_instance_id=instance_id
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="DOWNLOADED", arr_status="detected", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123", title="whatever", output_path="/data/torrents/complete/x"
        )
    ]
    fake_arr_server.state.fail_command = True

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    assert "arr_notify_failed" in await _event_kinds(db, item_id)

    fake_arr_server.state.fail_command = False
    await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "notified"
    assert "arr_notified" in await _event_kinds(db, item_id)


async def test_notify_retry_is_bounded(db, fake_arr_server, tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    rel_path = "Release.File.mkv"
    (local_root / rel_path).write_bytes(b"the release")

    host_id = await _seed_host(db)
    instance_id = await _seed_instance(
        db,
        str(tmp_path),
        base_url=fake_arr_server.base_url,
        api_key=fake_arr_server.state.api_key,
        notify_on_complete=True,
    )
    queue_id = await _seed_queue(
        db, host_id, local_path=str(local_root), arr_instance_id=instance_id
    )
    item_id = await _seed_item(
        db, queue_id, rel_path, state="DOWNLOADED", arr_status="detected", arr_download_id="abc123"
    )
    fake_arr_server.state.queue_records = [
        _queue_record(
            download_id="abc123", title="whatever", output_path="/data/torrents/complete/x"
        )
    ]
    fake_arr_server.state.fail_command = True

    scheduler = ArrSyncScheduler(db=db, config_dir=str(tmp_path))
    for _ in range(MAX_NOTIFY_RETRY_ATTEMPTS + 3):
        await scheduler.run_once()

    row = await _item_row(db, item_id)
    assert row["arr_status"] == "detected"
    failed_events = [k for k in await _event_kinds(db, item_id) if k == "arr_notify_failed"]
    assert len(failed_events) == MAX_NOTIFY_RETRY_ATTEMPTS
