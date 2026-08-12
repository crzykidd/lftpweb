"""Unit tests for `core/autoqueue.py` -- the mount gate, suppression, and retroactive
pattern evaluation (DESIGN.md §4.6, §4.7; docs/decisions.md's mount-sentinel-in-phase-4
entry). No lftp, no filesystem transfer -- `enqueue_item` is a plain recording stub, since
this module's whole job is deciding *whether* to call it, not what it does.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.db import migrate


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_queue(db, local_path) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'example.invalid', 'u', 'key', 'insecure')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id, rel_path, *, is_dir=True, state="REMOTE_ONLY", suppressed=0):
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, state, auto_queue_suppressed) "
        "VALUES (?, ?, ?, 100, ?, ?)",
        (queue_id, rel_path, 1 if is_dir else 0, state, suppressed),
    )
    await db.commit()
    return cursor.lastrowid


class _Recorder:
    def __init__(self):
        self.enqueued: list[int] = []

    async def __call__(self, item_id: int) -> int:
        self.enqueued.append(item_id)
        return item_id


def _mounted_config(queue_id, local_path, *, enabled=True, patterns_only=False):
    return QueueAutoConfig(
        id=queue_id,
        local_path=str(local_path),
        auto_queue_enabled=enabled,
        patterns_only=patterns_only,
    )


async def test_disabled_queue_does_nothing(db, tmp_path):
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path, enabled=False))
    assert queued == 0
    assert recorder.enqueued == []


async def test_mount_gate_blocks_everything_for_the_queue(db, tmp_path):
    # tmp_path itself has no sentinel -- the mount gate must refuse to act at all.
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []
    assert queue_id in aq.gated  # surfaced, not silent


async def test_sentinel_present_lets_auto_queue_proceed(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [1]
    assert queue_id not in aq.gated


async def test_stopped_item_is_never_resurrected_even_though_its_pattern_matches(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One", state="STOPPED", suppressed=1)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_failed_and_removed_states_are_also_never_picked_up(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    for i, state in enumerate(
        ("FAILED", "REMOVED_LOCAL", "REMOVED_BOTH", "QUEUED", "DOWNLOADING", "DOWNLOADED")
    ):
        await _make_item(
            db, queue_id, f"Release.{i}", state=state, suppressed=1 if state == "FAILED" else 0
        )
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_manual_clear_of_suppression_makes_a_stopped_item_eligible_again(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, "Release.One", state="STOPPED", suppressed=1)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    assert await aq.on_scan(_mounted_config(queue_id, tmp_path)) == 0

    # A manual re-queue (core/queue.py.retry_item) clears suppression and resets state --
    # simulated directly here since this module doesn't own that transition.
    await db.execute(
        "UPDATE item SET state = 'REMOTE_ONLY', auto_queue_suppressed = 0 WHERE id = ?", (item_id,)
    )
    await db.commit()
    assert await aq.on_scan(_mounted_config(queue_id, tmp_path)) == 1


async def test_retroactive_pattern_catches_a_previously_seen_item(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Wanted.Release")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    # patterns-only with no select yet -> nothing matches.
    assert await aq.on_scan(_mounted_config(queue_id, tmp_path, patterns_only=True)) == 0

    # A select pattern added *after* the item was already sitting REMOTE_ONLY must catch it
    # on the very next pass -- DESIGN.md §4.7's "retroactive," not just future scans.
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'select', 'Wanted*')", (queue_id,)
    )
    await db.commit()
    assert await aq.on_scan(_mounted_config(queue_id, tmp_path, patterns_only=True)) == 1


async def test_skip_pattern_prevents_intake(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Some.Release.SAMPLE")
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'skip', '*SAMPLE*')", (queue_id,)
    )
    await db.commit()
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    assert await aq.on_scan(_mounted_config(queue_id, tmp_path)) == 0


async def test_file_exclude_suppresses_intake_of_a_loose_top_level_file(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "notes.nfo", is_dir=False)
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'file_exclude', '*.nfo')",
        (queue_id,),
    )
    await db.commit()
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    assert await aq.on_scan(_mounted_config(queue_id, tmp_path)) == 0


async def test_global_pattern_queue_id_null_applies_to_every_queue(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Some.Release.SAMPLE")
    await db.execute("INSERT INTO pattern (queue_id, kind, expr) VALUES (NULL, 'skip', '*SAMPLE*')")
    await db.commit()
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    assert await aq.on_scan(_mounted_config(queue_id, tmp_path)) == 0
