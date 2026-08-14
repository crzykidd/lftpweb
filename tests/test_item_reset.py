"""Unit tests for `core/local_delete.py`'s reset-item-tracking primitives (`reset_item`,
`reset_queue`, `reset_pattern_matches`/`reset_by_pattern`) and `core/engine.py.Engine.
forget_rel_paths` (`prompts/2026-08-13-reset-item-tracking.md`).

Same no-fake-seedbox shape `tests/test_local_delete.py` uses: every guard here is SQLite (and,
for the "next scan creates a fresh row" tests, a real `Engine.scan_queue` pass against a fake
remote pool) -- nothing here needs lftp or a real SSH connection.
"""

from __future__ import annotations

import aiosqlite

import lftpweb.core.engine as engine_module
from lftpweb.core import local_delete
from lftpweb.core import patterns as patterns_core
from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.engine import Engine, QueueConfig, build_scan_counts_predicate
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.patterns import CompiledPatterns
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    # Same isolation reason as tests/test_local_delete.py's own fixture: the settle gate
    # defaults on and would otherwise hold a fresh scan's items at REMOTE_ONLY/settling.
    await save_settle_settings(db, SettleSettings(enabled=False))
    return db


async def _make_queue(db, local_path, *, name="q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'seedbox.invalid', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/remote', ?, 1, 'copy')",
        (host_id, name, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(
    db, queue_id, rel_path, *, is_dir=False, state="STOPPED", local_size=100, remote_size=100
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, "
        "auto_queue_suppressed, suppressed_reason) VALUES (?, ?, ?, ?, ?, ?, 1, 'user_stopped')",
        (queue_id, rel_path, 1 if is_dir else 0, remote_size, local_size, state),
    )
    await db.commit()
    return cursor.lastrowid


async def _queue_row(db, queue_id):
    cursor = await db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
    return await cursor.fetchone()


async def _item_row(db, item_id):
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
    return await cursor.fetchone()


class _FakePool:
    def __init__(self, tree):
        self._tree = tree

    async def scan(self, host, remote_path):  # noqa: ARG002
        return self._tree, None


def _host_config(host_id):
    return HostConfig(
        id=host_id,
        address="127.0.0.1",
        port=22,
        username="u",
        auth_method="key",
        key_path="/k",
        known_hosts_policy="strict",
    )


def _queue_config(queue_id, host_id, local_root, *, name="q"):
    return QueueConfig(
        id=queue_id,
        host_id=host_id,
        name=name,
        remote_path="/remote",
        local_path=str(local_root),
        staging_path=None,
        enabled=True,
        sync_mode="copy",
    )


# --- The core promise: item, item_settle, and deleted_archive all cleared ---------------------


async def test_reset_item_clears_all_three_tables(tmp_path):
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    (local_root / "Release" / "a.mkv").write_bytes(b"x" * 10)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)
        await db.execute(
            "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, matched_scans) "
            "VALUES (?, 'Release', 1, 10, 2)",
            (queue_id,),
        )
        await db.execute(
            "INSERT INTO deleted_archive (queue_id, rel_path) VALUES (?, 'Release/a.rar')",
            (queue_id,),
        )
        await db.commit()

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert outcome.reset_top_level == 1
        assert outcome.withheld == ()
        # Includes the deleted_archive row's own path even though it has no `item` row of its
        # own in this test -- the trap fix (`_subtree_deleted_archive_paths`).
        assert set(outcome.affected_rel_paths) == {"Release", "Release/a.rar"}

        cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        assert await cursor.fetchone() is None
        cursor = await db.execute(
            "SELECT * FROM item_settle WHERE queue_id = ? AND rel_path = 'Release'", (queue_id,)
        )
        assert await cursor.fetchone() is None
        cursor = await db.execute(
            "SELECT * FROM deleted_archive WHERE queue_id = ? AND rel_path = 'Release/a.rar'",
            (queue_id,),
        )
        assert await cursor.fetchone() is None

        # Local files were never touched -- this resets tracking, not data.
        assert (local_root / "Release" / "a.mkv").exists()
    finally:
        await db.close()


async def test_reset_clears_the_whole_subtree_not_just_the_clicked_row(tmp_path):
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        root_id = await _make_item(db, queue_id, "Release", is_dir=True)
        await _make_item(db, queue_id, "Release/a.mkv")
        await _make_item(db, queue_id, "Release/sub", is_dir=True)
        await _make_item(db, queue_id, "Release/sub/b.mkv")
        # A sibling sharing a name prefix must survive untouched.
        sibling_id = await _make_item(db, queue_id, "Release-extra")

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, root_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert set(outcome.affected_rel_paths) == {
            "Release",
            "Release/a.mkv",
            "Release/sub",
            "Release/sub/b.mkv",
        }
        cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_id,))
        remaining = {row["id"] for row in await cursor.fetchall()}
        assert remaining == {sibling_id}
    finally:
        await db.close()


# --- Guards: refuse, don't race -----------------------------------------------------------


async def test_reset_item_refuses_when_an_active_job_exists(tmp_path):
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)
        await db.execute(
            "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (item_id,)
        )
        await db.commit()

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert outcome.reset_top_level == 0
        assert outcome.affected_rel_paths == ()
        assert len(outcome.withheld) == 1
        assert "active job" in outcome.withheld[0]["reason"]
        # Refused, not raced -- the row is untouched.
        assert await _item_row(db, item_id) is not None
    finally:
        await db.close()


async def test_reset_item_refuses_when_postprocess_in_flight(tmp_path):
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset({item_id}),
        )

        assert outcome.reset_top_level == 0
        assert "post-processing" in outcome.withheld[0]["reason"]
        assert await _item_row(db, item_id) is not None
    finally:
        await db.close()


async def test_reset_item_refuses_when_a_delete_is_in_flight(tmp_path):
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)
        delete_in_flight = local_delete.DeleteInFlight()
        delete_in_flight.mark([item_id])

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
            delete_in_flight=delete_in_flight,
        )

        assert outcome.reset_top_level == 0
        assert "delete" in outcome.withheld[0]["reason"]
        assert await _item_row(db, item_id) is not None
    finally:
        await db.close()


# --- The trap case: a stale deleted_archive row must not survive a reset ----------------------


async def test_reset_clears_a_stale_deleted_archive_row_so_the_predicate_reads_normally(tmp_path):
    """`prompts/2026-08-13-reset-item-tracking.md`'s named trap: a `deleted_archive` row folds
    straight into `core/engine.py.build_scan_counts_predicate`'s completeness seam. If a reset
    left it behind, a freshly re-downloaded archive at the identical path would read `EXCLUDED`
    immediately, with no error and no obvious cause.
    """
    local_root = tmp_path / "local"
    (local_root / "Release").mkdir(parents=True)
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        item_id = await _make_item(db, queue_id, "Release", is_dir=True)
        await local_delete.save_deleted_archive_paths(db, queue_id, ["Release/a.rar"])

        no_pattern_exclusions = patterns_core.build_counts_predicate(CompiledPatterns.compile([]))
        archive_entry = RemoteEntry(rel_path="Release/a.rar", is_dir=False)

        # Before reset: the predicate excludes it, exactly as designed while the row survives.
        deleted_before = await local_delete.load_deleted_archive_paths(db, queue_id)
        predicate_before = build_scan_counts_predicate(no_pattern_exclusions, deleted_before)
        assert predicate_before("Release/a.rar", archive_entry) is False

        await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        deleted_after = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted_after == frozenset()
        predicate_after = build_scan_counts_predicate(no_pattern_exclusions, deleted_after)
        # A freshly re-downloaded archive at the same path now counts normally.
        assert predicate_after("Release/a.rar", archive_entry) is True
    finally:
        await db.close()


# --- Whole-queue scope -------------------------------------------------------------------------


async def test_reset_queue_clears_every_item_of_that_queue_and_nothing_else(tmp_path):
    local_a = tmp_path / "a"
    local_b = tmp_path / "b"
    local_a.mkdir()
    local_b.mkdir()
    write_if_needed(str(local_a))
    write_if_needed(str(local_b))

    db = await _make_db()
    try:
        queue_a = await _make_queue(db, local_a, name="qa")
        queue_b = await _make_queue(db, local_b, name="qb")
        a1 = await _make_item(db, queue_a, "One", is_dir=True)
        await _make_item(db, queue_a, "One/f.mkv")
        a2 = await _make_item(db, queue_a, "Two", is_dir=True)
        # A different queue sharing an identical rel_path must survive untouched.
        b1 = await _make_item(db, queue_b, "One", is_dir=True)

        outcome = await local_delete.reset_queue(
            db,
            queue=await _queue_row(db, queue_a),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert outcome.reset_top_level == 2
        assert set(outcome.affected_rel_paths) == {"One", "One/f.mkv", "Two"}

        cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_a,))
        assert await cursor.fetchall() == []
        cursor = await db.execute("SELECT id FROM item WHERE queue_id = ?", (queue_b,))
        assert {row["id"] for row in await cursor.fetchall()} == {b1}
        for stale_id in (a1, a2):
            assert await _item_row(db, stale_id) is None
    finally:
        await db.close()


async def test_reset_queue_skips_a_busy_item_but_resets_the_rest(tmp_path):
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        busy_id = await _make_item(db, queue_id, "Busy", is_dir=True)
        idle_id = await _make_item(db, queue_id, "Idle", is_dir=True)
        await db.execute(
            "INSERT INTO job (item_id, kind, state) VALUES (?, 'mirror', 'running')", (busy_id,)
        )
        await db.commit()

        outcome = await local_delete.reset_queue(
            db,
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert outcome.reset_top_level == 1
        assert outcome.affected_rel_paths == ("Idle",)
        assert len(outcome.withheld) == 1
        assert outcome.withheld[0]["rel_path"] == "Busy"
        assert await _item_row(db, busy_id) is not None
        assert await _item_row(db, idle_id) is None
    finally:
        await db.close()


# --- Purge by pattern, single-queue only --------------------------------------------------


async def test_reset_pattern_matches_reuses_the_single_evaluator(tmp_path):
    """Glob vs. substring dispatch, case-insensitivity -- `core/patterns.py.pattern_matches`
    itself, not a second implementation (DESIGN.md §12).
    """
    local_root = tmp_path / "local"
    local_root.mkdir()

    db = await _make_db()
    try:
        queue_id = await _make_queue(db, local_root)
        await _make_item(db, queue_id, "Show.S01E01.Release", is_dir=True)
        await _make_item(db, queue_id, "Show.S01E02.Release", is_dir=True)
        await _make_item(db, queue_id, "Movie.2020", is_dir=True)
        # Nested rows are never candidates -- only top-level items.
        await _make_item(db, queue_id, "Movie.2020/sample.mkv")

        matches = await local_delete.reset_pattern_matches(
            db, queue_id=queue_id, pattern="show.s01*"
        )
        assert {row["rel_path"] for row in matches} == {
            "Show.S01E01.Release",
            "Show.S01E02.Release",
        }

        substring_matches = await local_delete.reset_pattern_matches(
            db, queue_id=queue_id, pattern="movie"
        )
        assert {row["rel_path"] for row in substring_matches} == {"Movie.2020"}
    finally:
        await db.close()


async def test_reset_by_pattern_is_scoped_to_a_single_queue(tmp_path):
    local_a = tmp_path / "a"
    local_b = tmp_path / "b"
    local_a.mkdir()
    local_b.mkdir()

    db = await _make_db()
    try:
        queue_a = await _make_queue(db, local_a, name="qa")
        queue_b = await _make_queue(db, local_b, name="qb")
        a_id = await _make_item(db, queue_a, "Junk.Release", is_dir=True)
        b_id = await _make_item(db, queue_b, "Junk.Release", is_dir=True)

        outcome = await local_delete.reset_by_pattern(
            db,
            queue=await _queue_row(db, queue_a),
            pattern="junk*",
            caller="manual",
            in_flight_item_ids=frozenset(),
        )

        assert outcome.reset_top_level == 1
        assert await _item_row(db, a_id) is None
        # The identical rel_path in the other queue is untouched -- no cross-queue purge.
        assert await _item_row(db, b_id) is not None
    finally:
        await db.close()


# --- The path is genuinely new afterward: a real scan re-creates it, unsuppressed -------------


async def test_reset_item_then_a_real_scan_creates_a_fresh_unsuppressed_row(tmp_path, monkeypatch):
    """The whole point of the feature: a path that was suppressed (`STOPPED`/`user_stopped`)
    reads as brand new after a reset, and auto-queue picks it back up.
    """
    rel_path = "Old.Release"
    local_root = tmp_path

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

        item_id = await _make_item(db, queue_id, rel_path, is_dir=True, state="STOPPED")
        assert (await _item_row(db, item_id))["auto_queue_suppressed"] == 1

        outcome = await local_delete.reset_item(
            db,
            item=await _item_row(db, item_id),
            queue=await _queue_row(db, queue_id),
            caller="manual",
            in_flight_item_ids=frozenset(),
        )
        assert outcome.reset_top_level == 1
        assert await _item_row(db, item_id) is None

        # Nothing local yet -- the remote tree now has this release for the first time (a fresh
        # release reusing the same name, the exact scenario the user reported hitting).
        remote_tree = {
            rel_path: RemoteEntry(rel_path=rel_path, is_dir=True),
            f"{rel_path}/a.mkv": RemoteEntry(
                rel_path=f"{rel_path}/a.mkv", is_dir=False, size=10, mtime=1.0
            ),
        }
        monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root, **_kwargs: {})

        engine = Engine(db, str(tmp_path), EventBus())
        engine.pool = _FakePool(remote_tree)
        q = _queue_config(queue_id, host_id, local_root)
        host = _host_config(host_id)
        await engine.scan_queue(q, host)

        cursor = await db.execute(
            "SELECT id, state, auto_queue_suppressed, suppressed_reason FROM item "
            "WHERE queue_id = ? AND rel_path = ?",
            (queue_id, rel_path),
        )
        fresh = await cursor.fetchone()
        assert fresh is not None
        assert fresh["id"] != item_id  # a genuinely new row, not the old one resurrected
        assert fresh["auto_queue_suppressed"] == 0
        assert fresh["suppressed_reason"] is None
        assert fresh["state"] == "REMOTE_ONLY"

        # And auto-queue picks it up, unlike the pre-reset (suppressed) row.
        enqueued: list[int] = []

        async def _enqueue(iid: int) -> int:
            enqueued.append(iid)
            return iid

        aq = AutoQueue(db, _enqueue)
        queued = await aq.on_scan(
            QueueAutoConfig(
                id=queue_id,
                local_path=str(local_root),
                auto_queue_enabled=True,
                patterns_only=False,
            )
        )
        assert queued == 1
        assert enqueued == [fresh["id"]]
    finally:
        await db.close()


# --- Engine.forget_rel_paths: the in-memory cache is evicted, not just the database -----------


async def test_forget_rel_paths_evicts_the_cached_model_and_publishes_removed():
    db = await _make_db()
    try:
        events = EventBus()
        subscriber = events.subscribe()
        engine = Engine(db, "/tmp", events)
        engine.models[1] = {
            "Release": {"id": 1, "rel_path": "Release", "state": "STOPPED"},
            "Other": {"id": 2, "rel_path": "Other", "state": "DOWNLOADED"},
        }
        engine.queue_meta[1] = QueueConfig(
            id=1,
            host_id=1,
            name="q",
            remote_path="/r",
            local_path="/l",
            staging_path=None,
            enabled=True,
            sync_mode="copy",
        )

        engine.forget_rel_paths(1, ["Release"])

        assert "Release" not in engine.models[1]
        assert "Other" in engine.models[1]
        msg = subscriber.get_nowait()
        assert msg["type"] == "queue_delta"
        assert msg["queue_id"] == 1
        assert msg["queue_name"] == "q"
        assert msg["removed"] == ["Release"]
        assert msg["changed"] == []
    finally:
        await db.close()


async def test_forget_rel_paths_is_a_no_op_for_an_unknown_queue():
    db = await _make_db()
    try:
        events = EventBus()
        subscriber = events.subscribe()
        engine = Engine(db, "/tmp", events)
        # No queue 99 in engine.models at all -- must not raise, must not publish.
        engine.forget_rel_paths(99, ["whatever"])
        assert subscriber.empty()
    finally:
        await db.close()
