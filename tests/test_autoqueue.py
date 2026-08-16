"""Unit tests for `core/autoqueue.py` -- the mount gate, suppression, and retroactive
pattern evaluation (DESIGN.md §4.6, §4.7; docs/decisions.md's mount-sentinel-in-phase-4
entry). No lftp, no filesystem transfer -- `enqueue_item` is a plain recording stub, since
this module's whole job is deciding *whether* to call it, not what it does.
"""

from __future__ import annotations

import aiosqlite
import pytest

from lftpweb.core.autoqueue import (
    AutoQueue,
    AutoQueueSettings,
    QueueAutoConfig,
    save_autoqueue_settings,
)
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


async def test_active_job_excludes_an_otherwise_eligible_item_via_the_query_itself(db, tmp_path):
    """2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md): this module's docstring
    has always claimed "only a top-level item with no active job ... is eligible", but nothing
    in the `SELECT` enforced it -- it relied entirely on `state` never being `QUEUED`/
    `DOWNLOADING` while a job is active, which happens to hold today but was never asserted by
    the query. Proven here the way the task asks: an item left in an eligible *state*
    (`REMOTE_ONLY`, not `QUEUED`) but carrying a `running` `job` row anyway -- exactly the shape
    two concurrent job rows for one item could produce before `core/queue.py.enqueue_item`'s own
    idempotency fix -- must still be excluded, and only the `NOT EXISTS (... job ...)` clause
    can be doing that here, since the state alone says "eligible."
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, "Release.One")  # REMOTE_ONLY, unsuppressed
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
        "VALUES (?, 'mirror', 'running', 'main', 0, 1, 0)",
        (item_id,),
    )
    await db.commit()
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_failed_and_removed_states_are_also_never_picked_up(db, tmp_path):
    """`REMOVED_LOCAL` is deliberately not in this list -- see the tests below. Unlike
    `FAILED`/`REMOVED_BOTH`/`QUEUED`/`DOWNLOADING`/`DOWNLOADED`, whether a `REMOVED_LOCAL` item
    is picked up depends on `AutoQueueSettings.re_download_externally_removed`
    (`re_download_externally_removed_setting_*` tests below), not on the state name alone --
    but with that setting at its default (`False`, and this fixture's default `db`), it is
    excluded exactly like this group.
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


async def test_removed_local_unsuppressed_is_not_eligible_by_default(db, tmp_path):
    """Reverted, 2026-08-12 (docs/decisions.md): a same-day change (issue 4) made `REMOVED_LOCAL`
    eligible unconditionally on the premise that only lftpweb's own deletes needed excluding.
    That premise was wrong -- on a `copy`-mode queue with auto-queue on, an item something
    *outside* lftpweb removed (an `*arr` importer picking up a finished release, a human, a
    script) still matches its own select pattern, so making it eligible again re-downloads the
    same release forever, every scan, until the remote copy is also gone. `AutoQueueSettings.
    re_download_externally_removed` defaults `False`, so at the fixture's default settings this
    item -- `REMOVED_LOCAL`, `auto_queue_suppressed` clear, nothing this codebase touched --
    stays right where a `STOPPED`/`FAILED` item does: not picked up.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, "Release.One", state="REMOVED_LOCAL", suppressed=0)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_importer_moving_a_completed_release_out_does_not_cause_a_redownload(db, tmp_path):
    """The regression this revert exists to prevent, named for the scenario rather than the
    mechanism: a `copy`-mode queue with auto-queue on and a select pattern that still matches
    the release name. An `*arr` importer (or a human) moves the finished release out of the
    local downloads directory -- the ordinary, expected end of a successful import (DESIGN.md
    §7.2) -- and the item rides §7.3's grace period to `REMOVED_LOCAL` with
    `auto_queue_suppressed` clear, since nothing in this codebase decided to remove it. The next
    scan's auto-queue pass must not re-fetch it: the remote copy is untouched in `copy` mode, so
    a naive "still matches, still absent locally" reading would re-download, re-import, and
    repeat on every scan interval forever.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'select', 'Release*')", (queue_id,)
    )
    await db.commit()
    await _make_item(db, queue_id, "Release.One", state="REMOVED_LOCAL", suppressed=0)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_removed_local_suppressed_by_our_own_delete_is_never_resurrected(db, tmp_path):
    """A `REMOVED_LOCAL` item this codebase deleted on purpose carries `auto_queue_suppressed =
    1` (`core/local_delete.py.delete_local` sets it in the same write) and must stay excluded --
    lftpweb never re-fetches what it deleted itself, regardless of the eligible-states tuple. In
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


async def test_re_download_externally_removed_setting_makes_unsuppressed_removed_local_eligible(
    db, tmp_path
):
    """The opt-in: with `AutoQueueSettings.re_download_externally_removed = True`, the exact
    item the default-off test above leaves alone -- `REMOVED_LOCAL`, `auto_queue_suppressed`
    clear -- becomes eligible again, for anyone who genuinely wants a `copy`-mode queue to
    re-fetch what something outside lftpweb removed.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    await save_autoqueue_settings(db, AutoQueueSettings(re_download_externally_removed=True))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, "Release.One", state="REMOVED_LOCAL", suppressed=0)
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [item_id]


async def test_re_download_externally_removed_setting_never_resurrects_our_own_delete(db, tmp_path):
    """The setting governs only the externally-removed case, never lftpweb's own deletions --
    even with it on, a `REMOVED_LOCAL` item carrying `auto_queue_suppressed = 1` (our own
    delete) stays excluded. There is no value of this setting that re-fetches what lftpweb
    deleted itself.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    await save_autoqueue_settings(db, AutoQueueSettings(re_download_externally_removed=True))
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


# --- `_UNPACK_`/`_FAILED_` exclusion (2026-08-15, "show it, don't grab it," docs/decisions.md) --


async def test_unpack_prefixed_item_is_never_auto_queued_even_with_a_matching_pattern(
    db, tmp_path
):
    """The user's seedbox runs SABnzbd, which stages an in-progress unpack on the *remote*
    side under `_UNPACK_<name>` before renaming it to the release's final name -- this shows
    up as an ordinary `REMOTE_ONLY` item (this module never filters scan visibility; see
    `core/local_scan.py` for the *local*, on-disk counterpart of this same prefix, which does
    filter, at scan time, for a different reason). A broad `*` select pattern still must never
    grab it -- the exclusion is checked before pattern matching, unconditionally.
    """
    from lftpweb.core.extract import UNPACK_PREFIX
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'select', '*')", (queue_id,)
    )
    await db.commit()
    await _make_item(db, queue_id, f"{UNPACK_PREFIX}Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_failed_prefixed_item_is_never_auto_queued_either(db, tmp_path):
    """Same exclusion, same rationale, for a `_FAILED_` leftover -- `core/extract.py`'s other
    staging prefix.
    """
    from lftpweb.core.extract import FAILED_PREFIX
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'select', '*')", (queue_id,)
    )
    await db.commit()
    await _make_item(db, queue_id, f"{FAILED_PREFIX}Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_same_item_renamed_off_the_unpack_prefix_is_auto_queued(db, tmp_path):
    """Once SAB finishes and renames `_UNPACK_<name>` to `<name>`, the release is an ordinary,
    eligible `REMOTE_ONLY` item again -- proving the exclusion is scoped to the still-prefixed
    name, not a permanent block on the release itself.
    """
    from lftpweb.core.mount_sentinel import write_if_needed

    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'select', '*')", (queue_id,)
    )
    await db.commit()
    item_id = await _make_item(db, queue_id, "Release.One")
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    queued = await aq.on_scan(_mounted_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [item_id]


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
