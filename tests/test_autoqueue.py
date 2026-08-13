"""Unit tests for `core/autoqueue.py` -- the mount gate, suppression, and retroactive
pattern evaluation (DESIGN.md §4.6, §4.7; docs/decisions.md's mount-sentinel-in-phase-4
entry). No lftp, no filesystem transfer -- `enqueue_item` is a plain recording stub, since
this module's whole job is deciding *whether* to call it, not what it does.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    # Every test in this file except the "settle gate's eligibility half" section below is
    # about mount/suppression/pattern behavior, not the settle gate -- which now defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3) and would otherwise hold back every
    # item here that has no `item_settle` row (i.e. all of them). Disabled here; the settle
    # section re-enables it explicitly per test, right where it's actually being exercised.
    await save_settle_settings(conn, SettleSettings(enabled=False))
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
    """`REMOVED_LOCAL` is deliberately not in this list -- see the two tests below (issue 4,
    2026-08-12): whether it's picked up now depends on `auto_queue_suppressed`, not on the
    state name alone.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    for i, state in enumerate(("FAILED", "REMOVED_BOTH", "QUEUED", "DOWNLOADING", "DOWNLOADED")):
        await _make_item(
            db, queue_id, f"Release.{i}", state=state, suppressed=1 if state == "FAILED" else 0
        )
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_removed_local_unsuppressed_is_eligible_again(db, tmp_path):
    """Issue 4 (prompts/open-issues.md, coupled to "7 + 8 -- the deletion cluster"), fixed
    2026-08-12: an item whose local copy was moved away by a human or an *arr importer --
    `REMOVED_LOCAL` with `auto_queue_suppressed` still clear, since nothing in this codebase
    decided to remove it -- must be re-fetchable again, not stuck forever just because of its
    state name.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, "Release.One", state="REMOVED_LOCAL", suppressed=0)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [item_id]


async def test_removed_local_suppressed_by_our_own_delete_is_never_resurrected(db, tmp_path):
    """The other half of the same fix: a `REMOVED_LOCAL` item this codebase deleted on purpose
    carries `auto_queue_suppressed = 1` (`core/local_delete.py.delete_local` sets it in the
    same write) and must stay excluded -- otherwise fixing issue 4 would turn retention into a
    30-second re-download loop, exactly the trap prompts/open-issues.md warns about. In
    practice `delete_local` sets `state = 'REMOVED_BOTH'`, never bare `REMOVED_LOCAL`, but this
    asserts the actual safety mechanism (the flag), not an implementation detail of which state
    string happens to accompany it.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One", state="REMOVED_LOCAL", suppressed=1)
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


# --- the settle gate's eligibility half (prompts/open-issues.md #2, `core/settle.py`) -----


async def _set_settle_record(
    db, queue_id, rel_path, matched_scans: int, *, updated_at: str | None = None
) -> None:
    """`updated_at` carries `SettleRecord.first_matched_at`
    (prompts/2026-08-12-settle-gate-followups.md item 2) -- omitted, it defaults to "now" (the
    schema's own `DEFAULT`), which is *not* old enough to clear `settle.SETTLE_MIN_AGE_S` by
    the time a test's own assertion runs a moment later. Callers wanting a genuinely settled
    row must pass `updated_at=_LONG_AGO` explicitly; this is deliberate, not an oversight --
    it's exactly the age-floor behavior this task added.
    """
    if updated_at is None:
        await db.execute(
            "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans) "
            "VALUES (?, ?, 1, 100, 1.0, ?)",
            (queue_id, rel_path, matched_scans),
        )
    else:
        await db.execute(
            "INSERT INTO item_settle "
            "(queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans, updated_at) "
            "VALUES (?, ?, 1, 100, 1.0, ?, ?)",
            (queue_id, rel_path, matched_scans, updated_at),
        )
    await db.commit()


# Far enough in the past that `settle.SETTLE_MIN_AGE_S` has elapsed under any real clock --
# used wherever a test needs a record that reads as genuinely settled by age, not just by count.
_LONG_AGO = "2000-01-01T00:00:00.000000Z"


async def test_settle_gate_is_on_by_default(tmp_path):
    """prompts/2026-08-12-settle-gate-followups.md item 3: the default flipped from off to on
    -- this used to assert the opposite. Deliberately does **not** use this file's own `db`
    fixture, which explicitly disables the gate for every other test here (see that fixture's
    comment); this test needs a genuinely fresh database with no `setting` row at all, to prove
    "on" is what a real fresh install gets, not merely what an explicit opt-in produces (that's
    `test_settle_gate_on_with_no_settle_row_yet_is_conservative` below).
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    try:
        write_if_needed(str(tmp_path))
        queue_id = await _make_queue(conn, tmp_path)
        await _make_item(conn, queue_id, "Release.One")
        recorder = _Recorder()
        aq = AutoQueue(conn, recorder)

        queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
        assert queued == 0, "an item with no item_settle row must be held back by default now"
        assert recorder.enqueued == []
    finally:
        await conn.close()


async def test_settle_gate_on_holds_back_an_unsettled_item(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed
    from lftpweb.core.settle import SettleSettings, save_settle_settings

    await save_settle_settings(db, SettleSettings(enabled=True))
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    await _set_settle_record(db, queue_id, "Release.One", matched_scans=1)  # not yet settled
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_settle_gate_on_queues_a_settled_item(db, tmp_path):
    from lftpweb.core.mount_sentinel import write_if_needed
    from lftpweb.core.settle import SettleSettings, save_settle_settings

    await save_settle_settings(db, SettleSettings(enabled=True))
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    # settled: matched_scans met *and* old enough (updated_at far in the past)
    await _set_settle_record(db, queue_id, "Release.One", matched_scans=2, updated_at=_LONG_AGO)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [1]


async def test_settle_gate_on_holds_back_a_recently_matched_item_even_though_the_count_is_met(
    db, tmp_path
):
    """prompts/2026-08-12-settle-gate-followups.md item 2: `REQUIRED_SETTLE_SCANS` alone is
    not sufficient -- a matched item whose streak only just began (default `updated_at`, "now")
    must still be held back until `SETTLE_MIN_AGE_S` has also elapsed.
    """
    from lftpweb.core.mount_sentinel import write_if_needed
    from lftpweb.core.settle import SettleSettings, save_settle_settings

    await save_settle_settings(db, SettleSettings(enabled=True))
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    # matched_scans meets REQUIRED_SETTLE_SCANS, but updated_at defaults to "now" -- not old
    # enough by SETTLE_MIN_AGE_S yet.
    await _set_settle_record(db, queue_id, "Release.One", matched_scans=2)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_settle_gate_on_with_no_settle_row_yet_is_conservative(db, tmp_path):
    """A missing `item_settle` row means the settle-aware scan path hasn't run for this item
    yet -- treated as *not* settled (never queued on the strength of an absence), rather than
    optimistically queued.
    """
    from lftpweb.core.mount_sentinel import write_if_needed
    from lftpweb.core.settle import SettleSettings, save_settle_settings

    await save_settle_settings(db, SettleSettings(enabled=True))
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []
