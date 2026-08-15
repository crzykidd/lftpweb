"""Sonarr/Radarr integration, phase B (docs/arr-integration-spec.md "Notify") -- the primary
notify attempt fired from `core/postprocess.py.PostprocessPipeline`'s own tail, against a real
fake *arr (`tests/fake_arr.py`, the same fixture phase A's `test_arrsync.py` uses).

Covers the handoff prompt's "at minimum" list for notify: fires only after full postprocess
success, never on a failed pipeline; path translation (NULL passthrough, prefix replacement,
the post-move case); failure is non-fatal and writes its own event.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from lftpweb.core import postprocess
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.events import EventBus
from lftpweb.db import migrate


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await migrate(db)
    return db


async def _seed_instance(
    db: aiosqlite.Connection,
    config_dir: str,
    *,
    kind: str = "sonarr",
    base_url: str,
    api_key: str,
    enabled: bool = True,
    notify_on_complete: bool = True,
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


async def _seed_host_and_queue(
    db: aiosqlite.Connection,
    *,
    local_path: str,
    staging_path: str | None = None,
    auto_move: int = 0,
    arr_instance_id: int | None = None,
    arr_visible_path: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', 'seedbox.invalid', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode, auto_move, arr_instance_id, arr_visible_path) "
        "VALUES (?, 'q', '/data/pickup', ?, ?, 1, 'copy', ?, ?, ?)",
        (host_id, local_path, staging_path, auto_move, arr_instance_id, arr_visible_path),
    )
    queue_id = cursor.lastrowid
    await db.commit()
    return queue_id


async def _seed_item(
    db: aiosqlite.Connection,
    queue_id: int,
    rel_path: str,
    *,
    remote_size: int = 100,
    arr_status: str | None = "detected",
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, "
        "arr_status) VALUES (?, ?, 0, ?, ?, 'DOWNLOADED', ?)",
        (queue_id, rel_path, remote_size, remote_size, arr_status),
    )
    await db.commit()
    return cursor.lastrowid


async def _item_row(db: aiosqlite.Connection, item_id: int) -> aiosqlite.Row:
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    assert row is not None
    return row


async def _event_kinds(db: aiosqlite.Connection) -> list[str]:
    cursor = await db.execute("SELECT kind FROM event ORDER BY id")
    return [r["kind"] for r in await cursor.fetchall()]


class _NullRemotePool:
    async def delete_path(self, host, remote_path) -> None:  # pragma: no cover - unused (copy mode)
        raise AssertionError("copy-mode queue must never call delete_path")


async def test_notify_fires_after_full_success_and_sends_unchanged_path_when_visible_path_is_null(
    tmp_path, fake_arr_server
):
    """No `arr_visible_path` -> NULL passthrough (spec "Path namespaces"). No move, no
    download-prefix in play -- the simplest "already at rest" success branch.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db, local_path=str(local_root), arr_instance_id=instance_id
        )
        item_id = await _seed_item(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert len(fake_arr_server.state.command_calls) == 1
        call = fake_arr_server.state.command_calls[0]
        assert call["name"] == "DownloadedEpisodesScan"
        assert call["importMode"] == "Copy"
        assert call["path"] == str(local_root / rel_path)

        row = await _item_row(db, item_id)
        assert row["arr_status"] == "notified"
        assert row["arr_status_at"] is not None
        assert "arr_notified" in await _event_kinds(db)
    finally:
        await db.close()


async def test_notify_translates_path_via_arr_visible_path(tmp_path, fake_arr_server):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db,
            local_path=str(local_root),
            arr_instance_id=instance_id,
            arr_visible_path="/data/torrents/complete",
        )
        item_id = await _seed_item(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert len(fake_arr_server.state.command_calls) == 1
        assert (
            fake_arr_server.state.command_calls[0]["path"] == f"/data/torrents/complete/{rel_path}"
        )
    finally:
        await db.close()


async def test_notify_post_move_sends_the_relocated_path_translated(tmp_path, fake_arr_server):
    """The Move step relocates the item to `staging_path` before notify computes its final
    path -- the translated path sent to the *arr must be the *post-move* location (spec "Path
    namespaces": "arr_visible_path describes where that lands in the *arr's view").
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        staging_root = tmp_path / "staging"
        staging_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db,
            local_path=str(local_root),
            staging_path=str(staging_root),
            auto_move=1,
            arr_instance_id=instance_id,
            arr_visible_path="/library/staged",
        )
        item_id = await _seed_item(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings(move_enabled=True))

        # The bytes actually moved.
        assert not (local_root / rel_path).exists()
        assert (staging_root / rel_path).exists()

        assert len(fake_arr_server.state.command_calls) == 1
        assert fake_arr_server.state.command_calls[0]["path"] == f"/library/staged/{rel_path}"
    finally:
        await db.close()


async def test_notify_never_fires_on_a_failed_pipeline(tmp_path, fake_arr_server):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        release_dir = local_root / "Release"
        release_dir.mkdir()
        (release_dir / "a.txt").write_bytes(b"hello world")
        # Wrong checksum -> CORRUPT.
        (release_dir / "checksums.sfv").write_text("a.txt deadbeef\n")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db, local_path=str(local_root), arr_instance_id=instance_id
        )
        item_id = await _seed_item(db, queue_id, "Release")
        await db.execute("UPDATE path_queue SET auto_verify = 1 WHERE id = ?", (queue_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        row = await _item_row(db, item_id)
        assert row["state"] == "CORRUPT"
        assert row["arr_status"] == "detected", "must not have been touched"
        assert fake_arr_server.state.command_calls == []
        assert "arr_notified" not in await _event_kinds(db)
        assert "arr_notify_failed" not in await _event_kinds(db)
    finally:
        await db.close()


async def test_notify_skipped_when_item_was_never_matched(tmp_path, fake_arr_server):
    """`arr_status` still `None` (the *arr poller hasn't matched this item yet) -- notify must
    not fire even though the pipeline itself succeeds and the queue is fully configured.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db, local_path=str(local_root), arr_instance_id=instance_id
        )
        item_id = await _seed_item(db, queue_id, rel_path, arr_status=None)

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert fake_arr_server.state.command_calls == []
        row = await _item_row(db, item_id)
        assert row["arr_status"] is None
    finally:
        await db.close()


async def test_notify_skipped_when_notify_on_complete_is_off(tmp_path, fake_arr_server):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
            notify_on_complete=False,
        )
        queue_id = await _seed_host_and_queue(
            db, local_path=str(local_root), arr_instance_id=instance_id
        )
        item_id = await _seed_item(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert fake_arr_server.state.command_calls == []
        row = await _item_row(db, item_id)
        assert row["arr_status"] == "detected", "left alone -- not attempted, not failed"
    finally:
        await db.close()


async def test_notify_failure_is_non_fatal_writes_its_own_event_and_leaves_status_detected(
    tmp_path, fake_arr_server
):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release.File.mkv"
        (local_root / rel_path).write_bytes(b"the release")

        instance_id = await _seed_instance(
            db,
            str(tmp_path),
            base_url=fake_arr_server.base_url,
            api_key=fake_arr_server.state.api_key,
        )
        queue_id = await _seed_host_and_queue(
            db, local_path=str(local_root), arr_instance_id=instance_id
        )
        item_id = await _seed_item(db, queue_id, rel_path)
        fake_arr_server.state.fail_all = True

        pipeline = postprocess.PostprocessPipeline(
            db=db,
            events=EventBus(),
            remote_pool=_NullRemotePool(),
            config_dir=str(tmp_path),
        )
        # Must not raise -- notify failure is non-fatal to the pipeline.
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        row = await _item_row(db, item_id)
        assert row["arr_status"] == "detected"
        assert "arr_notify_failed" in await _event_kinds(db)
        assert "arr_notified" not in await _event_kinds(db)
    finally:
        await db.close()
