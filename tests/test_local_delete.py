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
from lftpweb.core.autoqueue import (
    AutoQueue,
    AutoQueueSettings,
    ELIGIBLE_STATES,
    QueueAutoConfig,
    save_autoqueue_settings,
)
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


# Sentinel default for `_make_item`'s `remote_size` -- "same as `local_size`" (a normal
# already-downloaded `copy`-mode item, which is what most of this file's fixtures represent
# and, after 2026-08-13's state fix, is exactly the case that must read `REMOVED_LOCAL` after a
# delete). Pass `remote_size=None` explicitly for a `LOCAL_ONLY`-shaped item instead.
_SAME_AS_LOCAL = object()


async def _make_item(
    db,
    queue_id,
    rel_path,
    *,
    is_dir=False,
    state="DOWNLOADED",
    local_size=100,
    remote_size=_SAME_AS_LOCAL,
    downloaded_at=None,
):
    if remote_size is _SAME_AS_LOCAL:
        remote_size = local_size
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, downloaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (queue_id, rel_path, 1 if is_dir else 0, remote_size, local_size, state, downloaded_at),
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


async def test_successful_delete_sets_removed_local_when_remote_copy_exists(tmp_path):
    """The normal `copy`-mode case (task: prompts/2026-08-13-delete-must-mark-the-whole-
    subtree.md, item 0). `_make_item`'s default `remote_size` mirrors `local_size` -- an
    already-downloaded item with a surviving remote copy -- so this must land on `REMOVED_LOCAL`,
    not the unconditional `REMOVED_BOTH` this used to write.
    """
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
        assert outcome.affected_rel_paths == ("Release",)
        assert not release.exists()

        item = await _item_row(db, item_id)
        assert item["state"] == "REMOVED_LOCAL"
        assert item["auto_queue_suppressed"] == 1
        assert item["suppressed_reason"] == "deleted_local"

        events = await _events_for(db, item_id)
        assert [e["kind"] for e in events] == ["local_delete"]

        published = subscriber.get_nowait()
        assert published["type"] == "item_delta"
        assert published["nodes"][0]["id"] == item_id
        assert published["nodes"][0]["state"] == "REMOVED_LOCAL"
    finally:
        await db.close()


async def test_successful_delete_sets_removed_both_when_no_remote_copy(tmp_path):
    """A `LOCAL_ONLY` item (or a `move` queue whose remote copy is already gone) has
    `remote_size IS NULL` -- both copies really are gone once this delete runs, so `REMOVED_BOTH`
    is correct here, unlike the `REMOVED_LOCAL` case above.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target = local_root / "junk.txt"
    target.write_bytes(b"x" * 30)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(
            db, queue_id, "junk.txt", state="LOCAL_ONLY", local_size=30, remote_size=None
        )

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

        item = await _item_row(db, item_id)
        assert item["state"] == "REMOVED_BOTH"
        assert item["auto_queue_suppressed"] == 1
        assert item["suppressed_reason"] == "deleted_local"
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


# --- Subtree marking (2026-08-13, prompts/2026-08-13-delete-must-mark-the-whole-subtree.md) --


async def test_delete_marks_every_descendant_immediately_no_scan_needed(tmp_path):
    """The bug this task fixes: `WHERE id = ?` only ever touched the clicked row, leaving every
    descendant `item` row untouched until a scan's grace period (up to ten minutes) elapsed.
    Every descendant must be suppressed with `deleted_local` the instant `delete_local` returns
    -- no scan in between, no grace period elapsed.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 10)
    (release / "b.mkv").write_bytes(b"x" * 15)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        top_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=25)
        a_id = await _make_item(db, queue_id, "Release/a.mkv", local_size=10)
        b_id = await _make_item(db, queue_id, "Release/b.mkv", local_size=15)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, top_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert set(outcome.affected_rel_paths) == {"Release", "Release/a.mkv", "Release/b.mkv"}

        for touched_id in (top_id, a_id, b_id):
            item = await _item_row(db, touched_id)
            assert item["state"] == "REMOVED_LOCAL"
            assert item["auto_queue_suppressed"] == 1
            assert item["suppressed_reason"] == "deleted_local"
            assert item["first_missing_at"] is None, "no grace-period clock was ever started"
    finally:
        await db.close()


async def test_delete_publishes_the_whole_subtree_in_one_ws_message(tmp_path):
    """The WebSocket side of the same fix: a single-row `item_delta` left every descendant's
    last-published node claiming `DOWNLOADED` until the next full scan resent the tree --
    exactly the stale-subtree symptom item 6 of the task calls out. This asserts at the message
    level (no browser here) that one `item_delta` carries every affected node, each already
    showing its correct post-delete state.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 10)
    (release / "b.mkv").write_bytes(b"x" * 15)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        top_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=25)
        await _make_item(db, queue_id, "Release/a.mkv", local_size=10)
        await _make_item(db, queue_id, "Release/b.mkv", local_size=15)

        events_bus = EventBus()
        subscriber = events_bus.subscribe()

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, top_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
            events=events_bus,
        )
        assert outcome.deleted is True

        published = subscriber.get_nowait()
        assert published["type"] == "item_delta"
        assert published["queue_id"] == queue_id
        published_by_path = {n["rel_path"]: n for n in published["nodes"]}
        assert set(published_by_path) == {"Release", "Release/a.mkv", "Release/b.mkv"}
        for node in published_by_path.values():
            assert node["state"] == "REMOVED_LOCAL"

        # A directory delete is one coherent message, not one per row -- nothing else was
        # published.
        assert subscriber.empty()
    finally:
        await db.close()


async def test_subtree_state_is_chosen_per_row_not_per_batch(tmp_path):
    """A directory can hold a mix -- an `EXCLUDED` child that never had a remote counterpart
    alongside siblings that did (task item 1's own example) -- so each row's state comes from
    its own `remote_size`, never one verdict reused for the whole subtree.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 10)
    (release / "local-only.nfo").write_bytes(b"x" * 5)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        top_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=15)
        with_remote_id = await _make_item(db, queue_id, "Release/a.mkv", local_size=10)
        local_only_id = await _make_item(
            db,
            queue_id,
            "Release/local-only.nfo",
            state="EXCLUDED",
            local_size=5,
            remote_size=None,
        )

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, top_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert outcome.deleted is True

        assert (await _item_row(db, top_id))["state"] == "REMOVED_LOCAL"
        assert (await _item_row(db, with_remote_id))["state"] == "REMOVED_LOCAL"
        assert (await _item_row(db, local_only_id))["state"] == "REMOVED_BOTH"

        for touched_id in (top_id, with_remote_id, local_only_id):
            item = await _item_row(db, touched_id)
            assert item["auto_queue_suppressed"] == 1
            assert item["suppressed_reason"] == "deleted_local"
    finally:
        await db.close()


async def test_delete_does_not_touch_a_sibling_sharing_a_name_prefix(tmp_path):
    """`LIKE 'target%'` would also match `target-extra` -- this module never uses `LIKE`
    (`_subtree_rows`'s own docstring), so a sibling sharing a name prefix must survive intact.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target_dir = local_root / "Release"
    target_dir.mkdir()
    (target_dir / "a.mkv").write_bytes(b"x" * 10)
    sibling_dir = local_root / "Release-Extra"
    sibling_dir.mkdir()
    (sibling_dir / "b.mkv").write_bytes(b"x" * 15)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        target_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=10)
        child_id = await _make_item(db, queue_id, "Release/a.mkv", local_size=10)
        sibling_id = await _make_item(db, queue_id, "Release-Extra", is_dir=True, local_size=15)
        sibling_child_id = await _make_item(db, queue_id, "Release-Extra/b.mkv", local_size=15)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, target_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert set(outcome.affected_rel_paths) == {"Release", "Release/a.mkv"}
        assert not target_dir.exists()
        assert sibling_dir.exists(), "a sibling sharing a name prefix must survive"

        for touched_id in (target_id, child_id):
            item = await _item_row(db, touched_id)
            assert item["auto_queue_suppressed"] == 1

        for untouched_id in (sibling_id, sibling_child_id):
            item = await _item_row(db, untouched_id)
            assert item["state"] == "DOWNLOADED"
            assert item["auto_queue_suppressed"] == 0
    finally:
        await db.close()


async def test_delete_handles_sql_like_metacharacters_in_rel_path(tmp_path):
    """`_` and `%` are SQL `LIKE` wildcards and turn up constantly in real scene release names
    (`_` especially). This module matches subtree membership in Python
    (`_subtree_rows`), never SQL `LIKE`, so neither character can cause an unrelated sibling to
    be swept in -- unlike a naive `LIKE 'target%'` pattern would (`_` substitutes for any single
    character; a literal `%` in the target acts as its own wildcard).
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    target_dir = local_root / "My_Release%2024"
    target_dir.mkdir()
    (target_dir / "file_1.mkv").write_bytes(b"x" * 20)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        target_id = await _make_item(db, queue_id, "My_Release%2024", is_dir=True, local_size=20)
        child_id = await _make_item(db, queue_id, "My_Release%2024/file_1.mkv", local_size=20)

        # Would incorrectly match a naive `LIKE 'My_Release%2024%'` pattern: `_` wildcards to
        # any single character, so "MyXRelease%2024" satisfies "My" + (any char) + "Release%2024".
        underscore_trap_id = await _make_item(db, queue_id, "MyXRelease%2024", local_size=5)
        # Would also incorrectly match: the literal `%` in the target acts as a `LIKE` wildcard
        # for "anything", so "My_ReleaseABCD2024-unrelated" satisfies "My_Release" + (anything)
        # + "2024" + (anything).
        percent_trap_id = await _make_item(
            db, queue_id, "My_ReleaseABCD2024-unrelated", local_size=5
        )

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, target_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )

        assert outcome.deleted is True
        assert set(outcome.affected_rel_paths) == {
            "My_Release%2024",
            "My_Release%2024/file_1.mkv",
        }

        for touched_id in (target_id, child_id):
            item = await _item_row(db, touched_id)
            assert item["auto_queue_suppressed"] == 1

        for untouched_id in (underscore_trap_id, percent_trap_id):
            item = await _item_row(db, untouched_id)
            assert item["state"] == "DOWNLOADED"
            assert item["auto_queue_suppressed"] == 0
    finally:
        await db.close()


async def test_delete_does_not_touch_the_same_rel_path_in_a_different_queue(tmp_path):
    """Two queues can hold the same `rel_path` -- `_subtree_rows` scopes to `queue_id`, so a
    delete in one queue must never reach into another.
    """
    local_root_a = tmp_path / "local-a"
    local_root_a.mkdir()
    (local_root_a / "Release").mkdir()
    (local_root_a / "Release" / "a.mkv").write_bytes(b"x" * 10)
    local_root_b = tmp_path / "local-b"
    local_root_b.mkdir()
    (local_root_b / "Release").mkdir()
    (local_root_b / "Release" / "a.mkv").write_bytes(b"x" * 10)
    write_if_needed(str(local_root_a))
    write_if_needed(str(local_root_b))

    db = await _make_db()
    try:
        queue_a = await _make_queue(db, local_root_a)
        queue_b = await _make_queue(db, local_root_b)
        item_a = await _make_item(db, queue_a, "Release", is_dir=True, local_size=10)
        await _make_item(db, queue_a, "Release/a.mkv", local_size=10)
        item_b = await _make_item(db, queue_b, "Release", is_dir=True, local_size=10)
        child_b = await _make_item(db, queue_b, "Release/a.mkv", local_size=10)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_a),
            queue=await _queue_row(db, queue_a),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert outcome.deleted is True

        for untouched_id in (item_b, child_b):
            item = await _item_row(db, untouched_id)
            assert item["state"] == "DOWNLOADED"
            assert item["auto_queue_suppressed"] == 0
        assert (local_root_b / "Release" / "a.mkv").exists()
    finally:
        await db.close()


async def test_dry_run_reports_the_same_subtree_a_real_run_marks(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 10)
    (release / "b.mkv").write_bytes(b"x" * 15)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True, local_size=25)
        await _make_item(db, queue_id, "Release/a.mkv", local_size=10)
        await _make_item(db, queue_id, "Release/b.mkv", local_size=15)

        dry = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
            dry_run=True,
        )
        assert dry.deleted is True
        assert release.exists(), "a dry run must not touch the filesystem"

        real = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert real.deleted is True

        assert set(dry.affected_rel_paths) == set(real.affected_rel_paths)
        assert set(dry.affected_rel_paths) == {"Release", "Release/a.mkv", "Release/b.mkv"}
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


# --- The Files list reflects disk, through a real Engine.scan_queue pass ---------------------


async def test_delete_survives_a_scan_without_reverting_to_downloaded(tmp_path):
    """The user-visible symptom that started this task: a directory row correctly showed
    `REMOVED_BOTH`/`REMOVED_LOCAL`, but every file inside it kept reading `DOWNLOADED` because
    only the clicked row was ever updated -- and once a scan ran, the descendants would have
    entered §7.3's ten-minute grace period rather than reflecting the delete immediately. This
    goes through a real `Engine.scan_queue` pass (the Files list's actual read path), not just
    `delete_local` in isolation.
    """
    rel_path = "Release"
    local_root = tmp_path / "local"
    release = local_root / rel_path
    release.mkdir(parents=True)
    (release / "a.mkv").write_bytes(b"x" * 10)
    (release / "b.mkv").write_bytes(b"x" * 15)

    remote_tree = {
        rel_path: RemoteEntry(rel_path=rel_path, is_dir=True),
        f"{rel_path}/a.mkv": RemoteEntry(
            rel_path=f"{rel_path}/a.mkv", is_dir=False, size=10, mtime=1.0
        ),
        f"{rel_path}/b.mkv": RemoteEntry(
            rel_path=f"{rel_path}/b.mkv", is_dir=False, size=15, mtime=1.0
        ),
    }

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

        # A real first scan finds the pre-existing local files and reaches DOWNLOADED --
        # everything is genuinely present, on both sides, exactly like an item lftpweb itself
        # downloaded earlier.
        await engine.scan_queue(q, host)

        async def _state(path: str) -> str | None:
            cursor = await db.execute(
                "SELECT state FROM item WHERE queue_id = ? AND rel_path = ?", (queue_id, path)
            )
            row = await cursor.fetchone()
            return row["state"] if row else None

        assert await _state(rel_path) == "DOWNLOADED"
        assert await _state(f"{rel_path}/a.mkv") == "DOWNLOADED"
        assert await _state(f"{rel_path}/b.mkv") == "DOWNLOADED"

        cursor = await db.execute(
            "SELECT * FROM item WHERE queue_id = ? AND rel_path = ?", (queue_id, rel_path)
        )
        release_row = await cursor.fetchone()
        cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
        queue_row = await cursor.fetchone()

        outcome = await local_delete.delete_local(
            db,
            item=release_row,
            queue=queue_row,
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert outcome.deleted is True
        assert not release.exists()

        # Immediately -- no scan run yet -- every descendant already reflects the delete. The
        # remote copy still exists (`remote_tree` above is unchanged), so REMOVED_LOCAL is the
        # correct reading, not REMOVED_BOTH.
        assert await _state(rel_path) == "REMOVED_LOCAL"
        assert await _state(f"{rel_path}/a.mkv") == "REMOVED_LOCAL"
        assert await _state(f"{rel_path}/b.mkv") == "REMOVED_LOCAL"

        # A further scan pass -- remote unchanged, local genuinely empty now -- must not
        # resurrect DOWNLOADED for any of them, nor start a fresh grace-period clock: they're
        # `auto_queue_suppressed`, so `_protected_rel_paths` holds `state` untouched, never
        # `resolve_absence`.
        await engine.scan_queue(q, host)
        assert await _state(rel_path) == "REMOVED_LOCAL"
        assert await _state(f"{rel_path}/a.mkv") == "REMOVED_LOCAL"
        assert await _state(f"{rel_path}/b.mkv") == "REMOVED_LOCAL"
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
        # The hardlink to `pickup` above proves a second copy exists, but the item's own
        # `remote_size` (defaulted to `local_size` by `_make_item`) is what `delete_local`
        # actually reads -- and it is set here, so this lands on `REMOVED_LOCAL`, not
        # `REMOVED_BOTH` (fixed 2026-08-13,
        # prompts/2026-08-13-delete-must-mark-the-whole-subtree.md; it used to be an
        # unconditional `REMOVED_BOTH`).
        assert item["state"] == "REMOVED_LOCAL"
        assert item["auto_queue_suppressed"] == 1

        # `REMOVED_LOCAL` is excluded from `ELIGIBLE_STATES` by default (reverted, 2026-08-12,
        # docs/decisions.md), so with the default (off) `re_download_externally_removed`
        # setting this item is excluded by state name alone even before suppression is
        # considered. `test_removed_local_after_delete_is_never_requeued_even_with_the_setting_on`
        # below is the sharper test: with the setting *on*, `REMOVED_LOCAL` *does* become
        # state-name-eligible, and suppression is the only thing left standing between this
        # item and a re-fetch.
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


async def test_removed_local_after_delete_is_never_requeued_even_with_the_setting_on(tmp_path):
    """The suppression flag, not the state name, is what actually stops the re-fetch. With
    `re_download_externally_removed` **on**, `ELIGIBLE_STATES_WITH_EXTERNALLY_REMOVED` names
    `REMOVED_LOCAL` explicitly -- so a delete-produced `REMOVED_LOCAL` row would be picked right
    back up here if `auto_queue_suppressed` weren't also set in the same write. This is the
    scenario `core/autoqueue.py`'s own module docstring names as the thing that must never
    happen.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    release = local_root / "Release.mkv"
    release.write_bytes(b"x" * 10)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release.mkv", local_size=10)

        outcome = await local_delete.delete_local(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            require_nlink_guard=False,
            in_flight_item_ids=frozenset(),
        )
        assert outcome.deleted is True

        item = await _item_row(db, item_id)
        assert item["state"] == "REMOVED_LOCAL"
        assert item["auto_queue_suppressed"] == 1

        await save_autoqueue_settings(db, AutoQueueSettings(re_download_externally_removed=True))

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


async def test_retention_marks_the_deleted_items_subtree_too(tmp_path):
    """Retention shares `delete_local()` with the manual endpoint, so it inherits the same
    subtree-marking fix -- `_select_expired` only ever selects top-level items (DESIGN.md
    §4.7), but a directory's descendant rows must still come out of a retention delete
    suppressed and correctly stated.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    pickup = tmp_path / "arr-library"
    pickup.mkdir()
    release = local_root / "Old.Release"
    release.mkdir()
    (release / "a.mkv").write_bytes(b"x" * 10)
    os.link(release / "a.mkv", pickup / "old-a.mkv")
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        old_ts = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        top_id = await _make_item(
            db, queue_id, "Old.Release", is_dir=True, local_size=10, downloaded_at=old_ts
        )
        child_id = await _make_item(db, queue_id, "Old.Release/a.mkv", local_size=10)

        await local_delete.save_retention_settings(
            db, local_delete.RetentionSettings(enabled=True, retention_days=30.0)
        )
        scheduler = local_delete.RetentionScheduler(db, EventBus())
        result = await scheduler.run_once()
        assert result.deleted == 1

        for touched_id in (top_id, child_id):
            item = await _item_row(db, touched_id)
            assert item["state"] == "REMOVED_LOCAL"
            assert item["auto_queue_suppressed"] == 1
            assert item["suppressed_reason"] == "deleted_local"
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
