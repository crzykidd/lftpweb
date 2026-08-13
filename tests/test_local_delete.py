"""Unit tests for `core/local_delete.py` -- the shared `delete_local()` primitive, its guards,
and the retention scheduler built on top of it (`prompts/open-issues.md` "7 + 8 -- the deletion
cluster"; `prompts/2026-08-12-local-deletion-and-retention.md`).

No fake seedbox needed: every guard here is filesystem + SQLite only. The `downloaded_at`
backfill (`core/engine.py._persist`) is exercised through a real `Engine.scan_queue` pass,
same harness shape `tests/test_state_persistence.py` uses, so the retention-selection test is
proven against the real write path rather than a hand-inserted timestamp standing in for it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import aiosqlite

import lftpweb.core.engine as engine_module
from lftpweb.core import local_delete
from lftpweb.core.autoqueue import AutoQueue, ELIGIBLE_STATES, QueueAutoConfig
from lftpweb.core.engine import Engine, QueueConfig
from lftpweb.core.events import EventBus
from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    # This file is about local_delete/retention, not the settle gate -- which now defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3) and would otherwise hold a fresh
    # `Engine.scan_queue` pass's items at REMOTE_ONLY/settling instead of DOWNLOADED. Disabled
    # to isolate what this file actually tests; the gate itself is covered by
    # tests/test_settle.py and tests/test_settle_gate_e2e.py.
    await save_settle_settings(db, SettleSettings(enabled=False))
    return db


async def _make_queue(db, local_path, *, enabled=1) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'seedbox.invalid', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, ?, 'copy')",
        (host_id, str(local_path), enabled),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(
    db, queue_id, rel_path, *, is_dir=False, state="DOWNLOADED", local_size=100, downloaded_at=None
):
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (queue_id, rel_path, 1 if is_dir else 0, local_size, local_size, state, downloaded_at),
    )
    await db.commit()
    return cursor.lastrowid


async def _queue_row(db, queue_id):
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
    return await cursor.fetchone()


async def _item_row(db, item_id):
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    return await cursor.fetchone()


async def _events_for(db, item_id):
    cursor = await db.execute(
        "SELECT kind, message FROM event WHERE item_id = ? ORDER BY id", (item_id,)
    )
    return await cursor.fetchall()


# --- The non-negotiable one: path containment against a real symlink escape ------------------


async def test_delete_local_refuses_a_symlink_escaping_the_local_root(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_bytes(b"do not touch")

    escape = local_root / "Release"
    escape.symlink_to(outside, target_is_directory=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is False
        assert "outside" in outcome.reason
        assert canary.exists(), "the symlink escape must never be followed"
        assert escape.is_symlink(), "the escaping symlink itself must be left alone too"

        events = await _events_for(db, item_id)
        assert [e["kind"] for e in events] == ["local_delete_withheld"]

        item = await _item_row(db, item_id)
        assert item["state"] == "DOWNLOADED", "a withheld delete must not touch the item row"
    finally:
        await db.close()


# --- Every other guard, in isolation, asserting the withhold event ---------------------------


async def test_active_job_guard_withholds(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "Release").mkdir()
    (local_root / "Release" / "a.mkv").write_bytes(b"x" * 10)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)
        await db.execute(
            "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (item_id,)
        )
        await db.commit()

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is False
        assert "active job" in outcome.reason
        assert (local_root / "Release").exists()
        events = await _events_for(db, item_id)
        assert events[0]["kind"] == "local_delete_withheld"
    finally:
        await db.close()


async def test_in_flight_postprocess_guard_withholds(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "Release").mkdir()
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset({item_id}),
        )

        assert outcome.deleted is False
        assert "post-processing" in outcome.reason
        assert (local_root / "Release").exists()
    finally:
        await db.close()


async def test_mount_sentinel_guard_withholds_when_sentinel_missing(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "Release").mkdir()
    # Deliberately no write_if_needed(): the sentinel is missing, as if the volume never
    # completed a scan (or isn't really mounted).

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is False
        assert "mount sentinel" in outcome.reason or "sentinel" in outcome.reason
        assert (local_root / "Release").exists()
    finally:
        await db.close()


async def test_nonexistent_item_is_withheld_not_an_error(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Gone", is_dir=True)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is False
        assert "does not exist" in outcome.reason
    finally:
        await db.close()


# --- The nlink guard, both ways ----------------------------------------------------------------


async def test_nlink_guard_blocks_a_single_link_file_when_required(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "loose.txt"
    target.write_bytes(b"only copy")
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "loose.txt")

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="retention",
            require_nlink_guard=True,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is False
        assert "hard link" in outcome.reason
        assert target.exists()
    finally:
        await db.close()


async def test_nlink_guard_allows_a_hardlinked_file_when_required(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "loose.txt"
    target.write_bytes(b"has another link")
    other = tmp_path / "arr-library" / "loose.txt"
    other.parent.mkdir()
    os.link(target, other)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "loose.txt")

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="retention",
            require_nlink_guard=True,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert not target.exists()
        assert other.exists(), "the other hardlink must survive -- this is the whole point"
    finally:
        await db.close()


async def test_nlink_guard_off_deletes_a_single_link_file(tmp_path):
    """Manual delete: the guard is OFF, so exactly the `LOCAL_ONLY` junk-with-one-copy case the
    user is trying to remove is not blocked.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"only copy, and that's fine")
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "junk.txt")

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert not target.exists()
    finally:
        await db.close()


# --- A successful delete: state, suppression, audit trail ------------------------------------


async def test_successful_delete_sets_removed_both_and_suppresses_the_item(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 50)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=50)

        events_bus = EventBus()
        subscriber = events_bus.subscribe()

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
            events=events_bus,
        )

        assert outcome.deleted is True
        assert outcome.bytes_freed == 50
        assert not release.exists()

        item = await _item_row(db, item_id)
        assert item["state"] == "REMOVED_BOTH"
        assert item["auto_queue_suppressed"] == 1
        assert item["suppressed_reason"] == "deleted_local"

        events = await _events_for(db, item_id)
        assert [e["kind"] for e in events] == ["local_delete"]

        published = subscriber.get_nowait()
        assert published["type"] == "item_delta"
        assert published["nodes"][0]["id"] == item_id
    finally:
        await db.close()


async def test_delete_of_a_symlinked_item_removes_only_the_link(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    inside_target = local_root / "_real_data"
    inside_target.mkdir()
    (inside_target / "a.txt").write_bytes(b"data")
    link = local_root / "Release"
    link.symlink_to(inside_target, target_is_directory=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert not link.exists()
        assert not link.is_symlink()
        assert inside_target.exists(), "only the link is removed, never its target's contents"
        assert (inside_target / "a.txt").exists()
    finally:
        await db.close()


# --- Dry run mirrors a real run exactly --------------------------------------------------------


async def test_dry_run_returns_exactly_what_a_real_run_would_delete(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    pickup = tmp_path / "arr-library"
    pickup.mkdir()
    (local_root / "Old.Release").mkdir()
    (local_root / "Old.Release" / "a.mkv").write_bytes(b"x" * 200)
    (local_root / "Blocked.Release").mkdir()
    (local_root / "Blocked.Release" / "a.mkv").write_bytes(b"x" * 300)
    # Retention always runs with the nlink guard on (module docstring) -- give both files a
    # second hardlink so the guard isn't what withholds "Blocked.Release" here; the active job
    # is the guard this test means to exercise.
    os.link(local_root / "Old.Release" / "a.mkv", pickup / "old.mkv")
    os.link(local_root / "Blocked.Release" / "a.mkv", pickup / "blocked.mkv")
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        free_id = await _make_item(
            db, queue_id, "Old.Release", is_dir=True, local_size=200, downloaded_at=old_ts
        )
        blocked_id = await _make_item(
            db, queue_id, "Blocked.Release", is_dir=True, local_size=300, downloaded_at=old_ts
        )
        await db.execute(
            "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')",
            (blocked_id,),
        )
        await db.commit()

        preview = await local_delete.preview_retention(
            db, retention_days=30.0, in_flight_item_ids=frozenset()
        )
        preview_ids = {p["item_id"] for p in preview}
        assert preview_ids == {free_id}
        assert next(p for p in preview if p["item_id"] == free_id)["local_size"] == 200

        # Nothing on disk moved -- a dry run must not touch the filesystem.
        assert (local_root / "Old.Release").exists()
        assert (local_root / "Blocked.Release").exists()
        assert await _events_for(db, free_id) == []

        # Now a real run: exactly the previewed set gets deleted, the blocked one doesn't.
        settings = local_delete.RetentionSettings(enabled=True, retention_days=30.0)
        await local_delete.save_retention_settings(db, settings)
        scheduler = local_delete.RetentionScheduler(db, EventBus())
        result = await scheduler.run_once()

        assert result.deleted == 1
        assert result.withheld == 1
        assert not (local_root / "Old.Release").exists()
        assert (local_root / "Blocked.Release").exists()
    finally:
        await db.close()


# --- Retention selects on downloaded_at, not state_changed_at --------------------------------


async def test_select_expired_ignores_recently_downloaded_items(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "Fresh.Release").mkdir()
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        recent = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await _make_item(db, queue_id, "Fresh.Release", is_dir=True, downloaded_at=recent)

        candidates = await local_delete._select_expired(db, retention_days=30.0)
        assert candidates == []
    finally:
        await db.close()


async def test_select_expired_ignores_items_never_stamped_downloaded_at():
    db = await _make_db()
    try:
        queue_id = await _make_queue(db, "/nonexistent")
        # downloaded_at left NULL, as a reconcile-only DOWNLOADED item would be before the
        # engine.py backfill runs at all.
        await _make_item(db, queue_id, "Untouched.Release", is_dir=True, downloaded_at=None)

        candidates = await local_delete._select_expired(db, retention_days=0.0)
        assert candidates == []
    finally:
        await db.close()


# --- The reconcile-path downloaded_at backfill (core/engine.py._persist) ---------------------


class _FakePool:
    def __init__(self, tree):
        self._tree = tree

    async def scan(self, host, remote_path):  # noqa: ARG002
        return self._tree, None


async def test_reconcile_only_completion_backfills_downloaded_at_for_retention(
    tmp_path, monkeypatch
):
    """An item that reaches DOWNLOADED purely via a scan (no job ever ran for it -- e.g.
    pre-existing local files matching the remote on first scan) must still get a
    `downloaded_at` stamp, or retention silently never sees it (prompts/open-issues.md "7 + 8").
    """
    rel_path = "Preexisting.Release"
    local_root = tmp_path
    (local_root / rel_path).mkdir()
    (local_root / rel_path / "a.mkv").write_bytes(b"x" * 10)

    db = await _make_db()
    try:
        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
            "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
        )
        host_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
            "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
            (host_id, str(local_root)),
        )
        queue_id = cursor.lastrowid
        await db.commit()

        remote_tree = {
            rel_path: RemoteEntry(rel_path=rel_path, is_dir=True),
            f"{rel_path}/a.mkv": RemoteEntry(
                rel_path=f"{rel_path}/a.mkv", is_dir=False, size=10, mtime=1.0
            ),
        }
        monkeypatch.setattr(
            engine_module.local_scan,
            "scan_local",
            lambda root: {
                rel_path: LocalEntry(rel_path=rel_path, is_dir=True),
                f"{rel_path}/a.mkv": LocalEntry(
                    rel_path=f"{rel_path}/a.mkv", is_dir=False, size=10
                ),
            },
        )

        engine = Engine(db, str(tmp_path), EventBus())
        engine.pool = _FakePool(remote_tree)
        q = QueueConfig(
            id=queue_id,
            host_id=host_id,
            name="q",
            remote_path="/remote",
            local_path=str(local_root),
            staging_path=None,
            enabled=True,
            sync_mode="copy",
        )
        host = HostConfig(
            id=host_id,
            address="127.0.0.1",
            port=22,
            username="u",
            auth_method="key",
            key_path="/k",
            known_hosts_policy="strict",
        )

        await engine.scan_queue(q, host)

        cursor = await db.execute(
            "SELECT id, state, downloaded_at FROM item WHERE queue_id = ? AND rel_path = ?",
            (queue_id, rel_path),
        )
        row = await cursor.fetchone()
        assert row["state"] == "DOWNLOADED"
        assert row["downloaded_at"] is not None

        # Backdate it (as if this had happened 60 days ago) and confirm retention now sees it.
        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await db.execute("UPDATE item SET downloaded_at = ? WHERE id = ?", (old_ts, row["id"]))
        await db.commit()

        candidates = await local_delete._select_expired(db, retention_days=30.0)
        assert [c[0]["id"] for c in candidates] == [row["id"]]

        # A second scan must never overwrite the real timestamp with "now" (COALESCE).
        await engine.scan_queue(q, host)
        cursor = await db.execute("SELECT downloaded_at FROM item WHERE id = ?", (row["id"],))
        assert (await cursor.fetchone())["downloaded_at"] == old_ts
    finally:
        await db.close()


# --- An item deleted by retention is not re-queued on the next scan --------------------------


async def test_retention_deleted_item_is_not_requeued_by_autoqueue(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    pickup = tmp_path / "arr-library"
    pickup.mkdir()
    (local_root / "Old.Release").mkdir()
    (local_root / "Old.Release" / "a.mkv").write_bytes(b"x" * 10)
    os.link(local_root / "Old.Release" / "a.mkv", pickup / "old.mkv")
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        item_id = await _make_item(
            db, queue_id, "Old.Release", is_dir=True, local_size=10, downloaded_at=old_ts
        )

        await local_delete.save_retention_settings(
            db, local_delete.RetentionSettings(enabled=True, retention_days=30.0)
        )
        scheduler = local_delete.RetentionScheduler(db, EventBus())
        result = await scheduler.run_once()
        assert result.deleted == 1

        item = await _item_row(db, item_id)
        assert item["state"] == "REMOVED_BOTH"
        assert item["auto_queue_suppressed"] == 1

        # `REMOVED_LOCAL` is excluded from `ELIGIBLE_STATES` by default (reverted, 2026-08-12,
        # docs/decisions.md), so this item would stay excluded by state name alone even without
        # the assertion below -- but `delete_local` never writes bare `REMOVED_LOCAL` anyway
        # (it goes straight to `REMOVED_BOTH`, already asserted above), so the real safety net
        # this test is pinning is suppression, which holds regardless of the eligible-states
        # tuple or the `re_download_externally_removed` setting -- lftpweb never re-fetches
        # what it deleted itself.
        assert "REMOVED_LOCAL" not in ELIGIBLE_STATES

        enqueued: list[int] = []

        async def _enqueue(item_id: int) -> int:
            enqueued.append(item_id)
            return item_id

        aq = AutoQueue(db, _enqueue)
        queued = await aq.on_scan(
            QueueAutoConfig(
                id=queue_id,
                local_path=str(local_root),
                auto_queue_enabled=True,
                patterns_only=False,
            )
        )
        assert queued == 0
        assert enqueued == []
    finally:
        await db.close()


# --- Settings round trip, default off ---------------------------------------------------------


async def test_retention_settings_default_off():
    db = await _make_db()
    try:
        settings = await local_delete.load_retention_settings(db)
        assert settings.enabled is False
        assert settings.retention_days == 30.0
    finally:
        await db.close()


async def test_retention_settings_round_trip():
    db = await _make_db()
    try:
        saved = local_delete.RetentionSettings(enabled=True, retention_days=14.5)
        await local_delete.save_retention_settings(db, saved)
        loaded = await local_delete.load_retention_settings(db)
        assert loaded == saved
    finally:
        await db.close()


async def test_retention_scheduler_is_a_no_op_while_disabled(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "Old.Release").mkdir()
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await _make_item(db, queue_id, "Old.Release", is_dir=True, downloaded_at=old_ts)

        # Settings never saved -- default is enabled=False.
        scheduler = local_delete.RetentionScheduler(db, EventBus())
        result = await scheduler.run_once()

        assert result == local_delete.RetentionRunResult(considered=0, deleted=0, withheld=0)
        assert (local_root / "Old.Release").exists()
    finally:
        await db.close()
