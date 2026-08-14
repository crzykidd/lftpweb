"""Who wins over `item.state` when a scan pass disagrees with it (DESIGN.md §3.2, §6, §7.3).

`core/engine.py._persist` recomputes every item's structural state from remote-vs-local bytes
on every pass. Three modules write `item.state`, and each one owns it under different
conditions: `core/queue.py` while a job is queued/running or suppressed, `core/postprocess.py`
for the six states §6 produces, `core/reconcile.py` for everything else. Until this file
existed, only the first of those was actually protected -- every post-processing outcome was
silently overwritten with a plain structural state within one scan interval of being set, so
a `CORRUPT` or `EXTRACT_FAILED` release erased its own failure before anyone looked at it.

The two halves are tested together on purpose, because protecting a state without also being
able to *stop* protecting it is a worse bug than the one it fixes (docs/decisions.md):

1. while the content is present, a post-processing outcome survives the rescan;
2. when the content goes absent, the item still reaches `REMOVED_LOCAL` through §7.3's grace
   period, so §3.2 rule 3 keeps working for exactly the items post-processing produces;
3. a transient state left behind by a crashed worker un-wedges itself on the next scan.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

import lftpweb.core.engine as engine_module
from lftpweb.core.engine import Engine, QueueConfig
from lftpweb.core.events import EventBus
from lftpweb.core.local_scan import LocalEntry
from lftpweb.core.mount_sentinel import DEFAULT_GRACE_S
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

REL_PATH = "Release.One"
SIZE = 1_000


class _FakePool:
    """One fixed remote tree, handed back on every scan -- no SSH (same shape as
    `tests/test_ws_deltas.py`'s fake).
    """

    def __init__(self, tree: dict[str, RemoteEntry]) -> None:
        self._tree = tree

    async def scan(self, host, remote_path):  # noqa: ARG002 - matches RemoteConnectionPool.scan
        return self._tree, None


class _FakePipeline:
    """Stands in for `core/postprocess.PostprocessPipeline` for the one thing the engine reads
    off it: which items a verify/extract worker is running for at this instant.
    """

    def __init__(self, in_flight: set[int] | None = None) -> None:
        self._in_flight = frozenset(in_flight or ())

    def in_flight_item_ids(self) -> frozenset[int]:
        return self._in_flight


def _remote_tree() -> dict[str, RemoteEntry]:
    return {REL_PATH: RemoteEntry(rel_path=REL_PATH, is_dir=False, size=SIZE, mtime=1.0)}


def _local_tree(size: int | None) -> dict[str, LocalEntry]:
    """`None` = locally absent (the importer-took-it / unmounted case); otherwise a local copy
    of that many bytes, so `size == SIZE` reads DOWNLOADED and anything less reads PARTIAL.
    """
    if size is None:
        return {}
    return {REL_PATH: LocalEntry(rel_path=REL_PATH, is_dir=False, size=size)}


async def _make_engine(tmp_path, monkeypatch, *, local_size: int | None, state: str):
    """A one-item queue whose `item` row already carries `state`, ready to be scanned."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(tmp_path)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (queue_id, REL_PATH, SIZE, local_size, state),
    )
    item_id = cursor.lastrowid
    await db.commit()

    # This file is about state-precedence, not the settle gate -- which now defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3) and would otherwise hold a fresh
    # scan's DOWNLOADED items at REMOTE_ONLY/settling for their first couple of passes,
    # breaking every single-scan assertion below. Disabled to isolate what this file actually
    # tests; the gate itself is covered by tests/test_settle.py and tests/test_settle_gate_e2e.py.
    await save_settle_settings(db, SettleSettings(enabled=False))

    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree(local_size),  # noqa: ARG005
    )

    engine = Engine(db, str(tmp_path), EventBus())
    engine.pool = _FakePool(_remote_tree())
    q = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="q",
        remote_path="/remote",
        local_path=str(tmp_path),
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
    return engine, q, host, db, item_id


async def _state_of(db, item_id: int) -> tuple[str, str | None]:
    cursor = await db.execute("SELECT state, first_missing_at FROM item WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return row["state"], row["first_missing_at"]


# --- 1. Content present: which states survive a scan pass, and which correctly don't --------


@pytest.mark.parametrize(
    ("persisted_state", "expected_state"),
    [
        # The four outcomes (§6) are refinements of DOWNLOADED and must survive -- this is the
        # bug: every one of these read back as "DOWNLOADED" within ~30s before the fix.
        ("VERIFIED", "VERIFIED"),
        ("CORRUPT", "CORRUPT"),
        ("EXTRACTED", "EXTRACTED"),
        ("EXTRACT_FAILED", "EXTRACT_FAILED"),
        # The two transient states correctly do *not* survive on their own: with no worker
        # running for the item, one of these is a leftover from a crash, and recomputing it
        # structurally is how it gets un-wedged (see the dedicated test below).
        ("VERIFYING", "DOWNLOADED"),
        ("EXTRACTING", "DOWNLOADED"),
        # Unchanged baseline behaviour.
        ("DOWNLOADED", "DOWNLOADED"),
        ("PARTIAL", "DOWNLOADED"),
    ],
)
async def test_scan_pass_against_a_complete_local_copy(
    tmp_path, monkeypatch, persisted_state, expected_state
):
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state=persisted_state
    )
    try:
        await engine.scan_queue(q, host)
        state, first_missing_at = await _state_of(db, item_id)
        assert state == expected_state
        assert first_missing_at is None  # nothing is missing; the clock must not be running
    finally:
        await db.close()


async def test_an_outcome_survives_repeated_scans_not_just_the_first(tmp_path, monkeypatch):
    """The failure mode was a *periodic* overwrite, so one pass proves little."""
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state="EXTRACT_FAILED"
    )
    try:
        for _ in range(5):
            await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "EXTRACT_FAILED"
    finally:
        await db.close()


@pytest.mark.parametrize(
    "persisted_state", ["VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED", "DOWNLOADED"]
)
async def test_partial_wins_over_an_outcome(tmp_path, monkeypatch, persisted_state):
    """§3.2 rule 2 is absolute: local short of remote is PARTIAL, never DOWNLOADED -- and an
    outcome is a stronger claim than DOWNLOADED, so it cannot survive one either. The remote
    grew (rule 4) or something took bytes away; either way the item is re-queueable again and
    saying "VERIFIED" would be a claim about content that is no longer all there.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE // 2, state=persisted_state
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "PARTIAL"
    finally:
        await db.close()


# --- 2. Content absent: the outcome still reaches REMOVED_LOCAL via §7.3's grace period -----


@pytest.mark.parametrize(
    "persisted_state",
    ["DOWNLOADED", "VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED", "VERIFYING", "EXTRACTING"],
)
async def test_absent_local_copy_holds_the_state_then_becomes_removed_local(
    tmp_path, monkeypatch, persisted_state
):
    """The half that makes the protection above safe. An `EXTRACTED` release whose files an
    *arr importer moved out must not stay `EXTRACTED` forever (that would kill §3.2 rule 3 for
    it) and must not read as a fresh `REMOTE_ONLY` either (auto-queue would re-download the
    whole thing). It holds its state while the grace clock runs, then lands on `REMOVED_LOCAL`.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=None, state=persisted_state
    )
    try:
        await engine.scan_queue(q, host)
        state, first_missing_at = await _state_of(db, item_id)
        assert state == persisted_state, "the outcome must not be downgraded while grace runs"
        assert first_missing_at is not None, "the grace clock should have started"

        # A second pass inside the window changes nothing -- and, critically, does not restart
        # the clock (which would mean REMOVED_LOCAL never arrives at all).
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id)) == (persisted_state, first_missing_at)

        # Backdate the clock past the grace window rather than sleeping ten minutes.
        await db.execute(
            "UPDATE item SET first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
            "WHERE id = ?",
            (f"-{int(DEFAULT_GRACE_S) + 60} seconds", item_id),
        )
        await db.commit()

        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "REMOVED_LOCAL"
    finally:
        await db.close()


async def test_removed_local_stays_removed_local_across_further_scans(tmp_path, monkeypatch):
    """Once landed it is sticky (`core/mount_sentinel.py`), so auto-queue keeps skipping it."""
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=None, state="REMOVED_LOCAL"
    )
    try:
        for _ in range(3):
            await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "REMOVED_LOCAL"
    finally:
        await db.close()


async def test_a_returning_local_copy_clears_the_state_and_the_clock(tmp_path, monkeypatch):
    """The reverse transition still works: an item whose local copy comes back is structural
    again from that scan on -- an outcome that was true of the *old* copy is not carried over.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=None, state="EXTRACTED"
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[1] is not None  # clock running

        monkeypatch.setattr(
            engine_module.local_scan,
            "scan_local",
            lambda root, **_kwargs: _local_tree(SIZE),  # noqa: ARG005
        )
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id)) == ("EXTRACTED", None)

        # ...and the item is genuinely back on the structural path: from a state the pipeline
        # doesn't own, the fresh reading wins outright.
        await db.execute("UPDATE item SET state = 'REMOVED_LOCAL' WHERE id = ?", (item_id,))
        await db.commit()
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id)) == ("DOWNLOADED", None)
    finally:
        await db.close()


# --- 3. Transient states: protected by the live worker, never by the state string -----------


@pytest.mark.parametrize("persisted_state", ["VERIFYING", "EXTRACTING"])
async def test_a_running_worker_protects_its_transient_state(
    tmp_path, monkeypatch, persisted_state
):
    """An extract can run far longer than a scan interval; while it does, the pipeline owns
    the item's state and the scan must leave it alone (it still refreshes the size columns).
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state=persisted_state
    )
    engine.postprocess = _FakePipeline({item_id})
    try:
        for _ in range(3):
            await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == persisted_state
    finally:
        await db.close()


@pytest.mark.parametrize("persisted_state", ["VERIFYING", "EXTRACTING"])
async def test_a_crashed_worker_does_not_wedge_the_item(tmp_path, monkeypatch, persisted_state):
    """The bug the protection above could easily have introduced, and the reason it is keyed
    on the live worker rather than on the state string.

    A process killed mid-extract leaves `EXTRACTING` in the database with nothing running.
    After the restart the pipeline's in-flight set is empty, so the very next scan recomputes
    the item structurally -- no startup sweep, no timeout, no state that can only be cleared
    by hand. (Phase 3 needed `_reconcile_orphaned_jobs` for the same shape of bug because
    `job.state` is durable; nothing durable is written for this one.)
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state=persisted_state
    )
    engine.postprocess = _FakePipeline(set())  # the worker is gone; the process restarted
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "DOWNLOADED"
    finally:
        await db.close()


async def test_job_lifecycle_states_are_still_protected(tmp_path, monkeypatch):
    """Regression guard on the seam this change extended: a `queued` job's item is still left
    alone by the scan (`core/queue.py` owns it), whatever else moved around it.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE, state="DOWNLOADING"
    )
    try:
        await db.execute(
            "INSERT INTO job (item_id, kind, state, lane) VALUES (?, 'pget', 'running', 'main')",
            (item_id,),
        )
        await db.commit()
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "DOWNLOADING"
    finally:
        await db.close()


# --- 4. move mode: the remote copy is already gone (prompts/2026-08-13-move-mode-outcome- ----
# --- survives-local-only.md) -------------------------------------------------------------------
#
# The reproduction: the first time `move` mode ran end to end against a real release, it
# downloaded, verified, deleted the remote, unrarred -- and every item read `LOCAL_ONLY` within
# one scan interval, losing the outcome §6 had just recorded. `core/reconcile.py` reads "remote
# absent, local present" as `LOCAL_ONLY` regardless of *why* the remote is absent -- correct for
# a file that was genuinely never tracked remotely, indistinguishable from a move-mode item's
# own remote copy the scan after `core/postprocess.py._maybe_delete_remote` deletes it on
# purpose. `remote_deleted_at` is what tells the two apart.


async def _make_move_engine(
    tmp_path,
    monkeypatch,
    *,
    local_size: int | None,
    state: str,
    remote_deleted_at: str | None,
):
    """Same shape as `_make_engine`, but for a `move`-mode queue whose remote copy is already
    gone (`_FakePool({})` -- the delete `core/postprocess.py._maybe_delete_remote` already
    performed) and whose row carries `remote_deleted_at` the way that delete leaves it.
    """
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'move')",
        (host_id, str(tmp_path)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, remote_deleted_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?)",
        (queue_id, REL_PATH, SIZE, local_size, state, remote_deleted_at),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await save_settle_settings(db, SettleSettings(enabled=False))

    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: _local_tree(local_size),  # noqa: ARG005
    )

    engine = Engine(db, str(tmp_path), EventBus())
    engine.pool = _FakePool({})  # the remote copy is already gone
    q = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="q",
        remote_path="/remote",
        local_path=str(tmp_path),
        staging_path=None,
        enabled=True,
        sync_mode="move",
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
    return engine, q, host, db, item_id


@pytest.mark.parametrize("persisted_state", ["VERIFIED", "CORRUPT", "EXTRACTED", "EXTRACT_FAILED"])
async def test_move_mode_outcome_survives_when_remote_already_deleted(
    tmp_path, monkeypatch, persisted_state
):
    """The reproduction, at the engine level: verify -> delete the remote -> (optionally)
    extract already happened; the very next scan must not overwrite the outcome with
    `LOCAL_ONLY`. Covers `VERIFIED` (extraction disabled) and `EXTRACTED`/`CORRUPT`/
    `EXTRACT_FAILED` (extraction enabled or failed) alike -- the fix is keyed on `TERMINAL_
    STATES`, not on any one of them.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path,
        monkeypatch,
        local_size=SIZE,
        state=persisted_state,
        remote_deleted_at="2026-08-13T00:00:00.000000Z",
    )
    try:
        await engine.scan_queue(q, host)
        state, first_missing_at = await _state_of(db, item_id)
        assert state == persisted_state, "must not be overwritten with LOCAL_ONLY"
        assert first_missing_at is None, "content is present; nothing should start the clock"

        # Not just one pass.
        for _ in range(3):
            await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == persisted_state
    finally:
        await db.close()


async def test_a_genuine_local_only_item_is_unaffected(tmp_path, monkeypatch):
    """The gate is `remote_deleted_at`, not `LOCAL_ONLY` alone: an item that reads LOCAL_ONLY
    for any other reason -- nothing in this codebase ever deleted its remote copy -- must not
    be mistaken for a move-mode item's own post-delete reading.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path, monkeypatch, local_size=SIZE, state="EXTRACTED", remote_deleted_at=None
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "LOCAL_ONLY"
    finally:
        await db.close()


async def test_partial_wins_over_the_outcome_even_with_remote_deleted(tmp_path, monkeypatch):
    """Rule 2 is absolute regardless of *why* `remote_deleted_at` happens to be set: with the
    remote copy genuinely still present (so `core/reconcile.py` can actually produce
    `PARTIAL`) and local short of it, the structural reading must still win over the outcome.
    `remote_deleted_at` only ever refines a `LOCAL_ONLY` reading -- see
    `test_outcome_survives_rescan_local_only_with_remote_deleted` in tests/test_postprocess.py
    for the direct pure-function version of this same case; this is the engine-level twin.
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=SIZE // 2, state="EXTRACTED"
    )
    await db.execute(
        "UPDATE item SET remote_deleted_at = ? WHERE id = ?",
        ("2026-08-13T00:00:00.000000Z", item_id),
    )
    await db.commit()
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "PARTIAL"
    finally:
        await db.close()


async def test_move_mode_item_that_leaves_both_trees_reaches_removed_both(tmp_path, monkeypatch):
    """The other half of the reproduction. Once `_do_move` (auto_move) relocates the local
    copy too -- or an importer takes it -- the rel_path is in *neither* tree, so
    `core/reconcile.py` produces no node for it at all and the LOCAL_ONLY fix above is never
    even asked. Without `core/engine.py._persist`'s own "vanished from both trees" handling,
    this row would simply never be written again: `EXTRACTED` forever, freezing the item on
    its outcome instead of letting §7.3's grace period carry it to a terminal removed state the
    way an ordinary importer-moved-it-out item already does. This proves the fix does not
    introduce that freeze.

    **`REMOVED_BOTH`, not `REMOVED_LOCAL`** (2026-08-13,
    `prompts/2026-08-13-vanished-rows-should-leave-the-tree.md`, closing the gap
    `prompts/open-issues.md` recorded as "`resolve_absence` never writes `REMOVED_BOTH`"): the
    remote copy is gone too -- this is a `move` queue whose remote was already deleted before
    this test even starts -- so `REMOVED_LOCAL` ("remote still present") would assert something
    false. Before this fix, `_persist`'s vanished-sweep reused `resolve_absence`'s grace-period
    machinery (correctly) but published its literal `"REMOVED_LOCAL"` output verbatim
    (incorrectly) for a `rel_path` it already knows is in neither tree.
    """
    local_present = {"value": True}

    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path,
        monkeypatch,
        local_size=SIZE,
        state="EXTRACTED",
        remote_deleted_at="2026-08-13T00:00:00.000000Z",
    )
    # Override the fixture's fixed local-scan stub with one this test can flip mid-run.
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: (_local_tree(SIZE) if local_present["value"] else {}),  # noqa: ARG005
    )
    try:
        # 1. Remote already gone, local still here -- LOCAL_ONLY structurally, must hold (the
        # first half of the reproduction, proven again here for continuity with what follows).
        await engine.scan_queue(q, host)
        state, first_missing_at = await _state_of(db, item_id)
        assert state == "EXTRACTED"
        assert first_missing_at is None

        # 2. `_do_move` relocates the local copy out of local_path too. The rel_path now
        # exists in neither tree.
        local_present["value"] = False
        await engine.scan_queue(q, host)
        state, first_missing_at = await _state_of(db, item_id)
        assert state == "EXTRACTED", "the outcome must not be downgraded while grace runs"
        assert first_missing_at is not None, "the grace clock should have started"

        # 3. A second pass inside the window changes nothing -- and does not restart the clock.
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id)) == ("EXTRACTED", first_missing_at)

        # 4. Backdate the clock past the grace window rather than sleeping ten minutes.
        await db.execute(
            "UPDATE item SET first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
            "WHERE id = ?",
            (f"-{int(DEFAULT_GRACE_S) + 60} seconds", item_id),
        )
        await db.commit()

        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "REMOVED_BOTH"
    finally:
        await db.close()


async def test_a_vanished_local_only_row_rests_at_removed_both_not_left_alone(
    tmp_path, monkeypatch
):
    """The "vanished from both trees" loop consults `resolve_absence` first (which has no
    opinion about `LOCAL_ONLY` -- it is not in `_STICKY_PREV_STATES`, never content the grace
    period tracks) and then, since 2026-08-13
    (`prompts/2026-08-13-delete-state-truthfulness.md`, defect 3),
    `core/mount_sentinel.py.resolve_vanished` as the fallback: `LOCAL_ONLY` asserted concrete
    content was actually here, so a row that leaves both trees with that history must not be
    frozen on it forever (`REMOTE_ONLY`, "nothing was ever here", correctly stays frozen --
    see `test_a_vanished_remote_only_row_is_still_left_alone` below). Never `REMOVED_LOCAL`
    (that would assert a remote copy that is not there).
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path, monkeypatch, local_size=None, state="LOCAL_ONLY", remote_deleted_at=None
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "REMOVED_BOTH"
    finally:
        await db.close()


async def test_a_vanished_remote_only_row_is_still_left_alone(tmp_path, monkeypatch):
    """The narrowing's other half: `REMOTE_ONLY` -- nothing was ever fetched, so there is no
    "removed" story to tell -- must keep the pre-`resolve_vanished` behavior of simply being
    left out of the published tree, not relabeled `REMOVED_BOTH`. This is the common case (a
    remote file that dropped off an unrelated scan), and `tests/test_ws_deltas.py`'s scan-delta
    tests already depend on it at the WebSocket-payload level; this is the same guarantee
    checked directly against the persisted row.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path, monkeypatch, local_size=None, state="REMOTE_ONLY", remote_deleted_at=None
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, item_id))[0] == "REMOTE_ONLY"
    finally:
        await db.close()


# --- A terminal removed row must leave the *published* tree, not just get written -----------
#
# 2026-08-13 (prompts/2026-08-13-vanished-rows-should-leave-the-tree.md), a regression the user
# found within hours of `56ec523` (the fix proven above -- the vanished sweep must keep
# *writing* a row in neither tree, or it freezes forever): `written.add(rel_path)` at the end of
# that same sweep made every row it resolves *published* forever too, since `written` is exactly
# what `core/engine.py._project` filters publication by. A row that reaches a terminal removed
# state (`REMOVED_LOCAL`/`REMOVED_BOTH`) with nothing left in either tree is meant to be kept
# only "as history" (`_project`'s own docstring) -- these tests assert that on the actual wire
# shapes (`queue_delta`'s `removed` list, a fresh `snapshot()`), the way the user actually
# observed the bug, not just against the database (which `_persist` writing correctly would
# still pass).


async def _next_queue_delta(subscription: asyncio.Queue) -> dict:
    """Pop from the subscription until a `queue_delta` arrives, discarding any interleaved
    `scan_complete` -- same shape as `tests/test_ws_deltas.py`'s `_next_message`, duplicated
    rather than imported since there is no `tests` package for a cross-file import.
    """
    while True:
        message = await subscription.get()
        if message["type"] == "queue_delta":
            return message
        assert (
            message["type"] == "scan_complete"
        ), f"unexpected message while waiting for queue_delta: {message['type']!r}"


async def test_vanished_both_row_stops_publishing_once_it_reaches_removed_both(
    tmp_path, monkeypatch
):
    """The user's exact scenario, end to end: a `move` queue whose remote copy is already gone,
    then the local copy removed *outside* lftpweb too (their CLI `rm`, simulated here the same
    way `_do_move` relocating it out of `local_path` would be). While the grace period is
    running the row must keep publishing, holding its outcome state -- the content could still
    come back. Once the grace period elapses and it lands on a terminal state, it must leave the
    published tree: reported once in `queue_delta`'s `removed` list, and absent from every
    `snapshot()` after.
    """
    local_present = {"value": True}
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path,
        monkeypatch,
        local_size=SIZE,
        state="EXTRACTED",
        remote_deleted_at="2026-08-13T00:00:00.000000Z",
    )
    monkeypatch.setattr(
        engine_module.local_scan,
        "scan_local",
        lambda root, **_kwargs: (_local_tree(SIZE) if local_present["value"] else {}),  # noqa: ARG005
    )
    try:
        subscription = engine.events.subscribe()

        # 1. Baseline: local present, remote already gone (move mode) -- published normally.
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert REL_PATH in {node["rel_path"] for node in delta["changed"]}
        snapshot = (await engine.snapshot())[0]
        assert REL_PATH in {node["rel_path"] for node in snapshot["nodes"]}

        # 2. The CLI `rm`: local copy gone too, rel_path in neither tree now. The grace period
        # is running -- must still be published, still reading the outcome it held.
        local_present["value"] = False
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert REL_PATH not in delta["removed"], "must keep publishing during the grace period"
        snapshot = (await engine.snapshot())[0]
        published = {node["rel_path"]: node["state"] for node in snapshot["nodes"]}
        assert published.get(REL_PATH) == "EXTRACTED"

        # 3. A second pass inside the window: still published, unchanged.
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert REL_PATH not in delta["removed"]

        # 4. Backdate the clock past the grace window rather than sleeping ten minutes.
        await db.execute(
            "UPDATE item SET first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
            "WHERE id = ?",
            (f"-{int(DEFAULT_GRACE_S) + 60} seconds", item_id),
        )
        await db.commit()

        # 5. The grace period elapses: the row reaches REMOVED_BOTH (terminal, in neither tree)
        # and must leave the published tree in this very pass's delta.
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert (await _state_of(db, item_id))[0] == "REMOVED_BOTH"
        assert REL_PATH in delta["removed"], "a terminal removed row must leave the published tree"
        assert REL_PATH not in {node["rel_path"] for node in delta["changed"]}

        snapshot = (await engine.snapshot())[0]
        assert REL_PATH not in {
            node["rel_path"] for node in snapshot["nodes"]
        }, "a fresh snapshot must not resurrect a row this pass already resolved to terminal"

        # 6. And it stays gone -- not a one-scan blip. The row is still *written* every pass
        # (56ec523's fix, still correct -- the History page reads it straight from `item`), just
        # never published again.
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert delta["removed"] == [], "already removed; nothing left to report a second time"
        assert (await _state_of(db, item_id))[0] == "REMOVED_BOTH"
        snapshot = (await engine.snapshot())[0]
        assert REL_PATH not in {node["rel_path"] for node in snapshot["nodes"]}
    finally:
        await db.close()


async def test_removed_local_with_surviving_remote_stays_published(tmp_path, monkeypatch):
    """The regression risk called out by name: manual delete while the remote survives. This
    row never enters the vanished sweep at all -- the remote copy keeps it in `nodes` every
    scan, so it is published through the ordinary per-node path, `written.add(rel_path)`
    unconditional at the top of that loop -- but a fix scoped to the wrong place could easily
    have broken it anyway. Guarded explicitly, at the wire level: after the grace period lands
    it on `REMOVED_LOCAL`, the row must still be in the published tree, and `remote_size` must
    still be present (what `FileTree.tsx.rowAction` keys "Re-Download" on, alongside the state).
    """
    engine, q, host, db, item_id = await _make_engine(
        tmp_path, monkeypatch, local_size=None, state="DOWNLOADED"
    )
    try:
        subscription = engine.events.subscribe()
        await engine.scan_queue(q, host)
        await _next_queue_delta(subscription)

        await db.execute(
            "UPDATE item SET first_missing_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
            "WHERE id = ?",
            (f"-{int(DEFAULT_GRACE_S) + 60} seconds", item_id),
        )
        await db.commit()

        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert (await _state_of(db, item_id))[0] == "REMOVED_LOCAL"
        assert (
            REL_PATH not in delta["removed"]
        ), "remote still exists -- must stay visible, not be dropped from the tree"

        snapshot = (await engine.snapshot())[0]
        nodes_by_path = {node["rel_path"]: node for node in snapshot["nodes"]}
        assert REL_PATH in nodes_by_path, "Re-Download must still have a row to act on"
        assert nodes_by_path[REL_PATH]["state"] == "REMOVED_LOCAL"
        assert (
            nodes_by_path[REL_PATH]["remote_size"] is not None
        ), "the remote copy surviving is exactly what makes Re-Download meaningful"

        # And a further scan doesn't disturb it either.
        await engine.scan_queue(q, host)
        delta = await _next_queue_delta(subscription)
        assert REL_PATH not in delta["removed"]
    finally:
        await db.close()


async def test_a_row_that_leaves_both_trees_and_later_returns_is_published_again(
    tmp_path, monkeypatch
):
    """The other side of the same fix: dropping a terminal row from `written` must not be
    permanent. DESIGN.md §3.2 rule 6: "if the same rel_path reappears... it is a genuinely new
    item" -- once the ordinary per-node path sees content on either side again, the row must
    re-enter `written` and be published like any other row, not stay silently excluded because
    an earlier pass once resolved it to `REMOVED_BOTH`.
    """
    engine, q, host, db, item_id = await _make_move_engine(
        tmp_path, monkeypatch, local_size=None, state="REMOVED_BOTH", remote_deleted_at=None
    )
    monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root, **_kwargs: {})  # noqa: ARG005
    try:
        # Baseline: genuinely in neither tree, REMOVED_BOTH already resting there -- stays
        # unpublished, exactly `test_a_vanished_local_only_row_rests_at_removed_both_not_left_
        # alone`'s sibling case at the wire level.
        await engine.scan_queue(q, host)
        snapshot = (await engine.snapshot())[0]
        assert REL_PATH not in {node["rel_path"] for node in snapshot["nodes"]}

        # The re-upload: remote content returns (local stays absent).
        engine.pool = _FakePool(_remote_tree())
        await engine.scan_queue(q, host)

        state, _ = await _state_of(db, item_id)
        assert state == "REMOTE_ONLY", "a fresh structural reading, not the stale REMOVED_BOTH"
        snapshot = (await engine.snapshot())[0]
        published = {node["rel_path"]: node["state"] for node in snapshot["nodes"]}
        assert published.get(REL_PATH) == "REMOTE_ONLY", "content returned -- published again"
    finally:
        await db.close()


# --- 5. A mirror job's children are protected too (2026-08-14) --------------------------------
#
# Reported live: with "folder prefix during transfer" enabled, the files inside a downloading
# release flipped between PARTIAL and REMOTE_ONLY every few seconds.
#
# Two writers, one unprotected row. A `mirror` job is tracked against the *top-level* item only,
# so a child file has no `job` row of its own and `_protected_rel_paths` never caught it -- while
# `core/queue.py._publish_child_progress` writes exactly those children's `local_size`/`state` on
# every progress tick. The prefix is what makes it reproducible rather than theoretical:
# `scan_local(extra_dir_prefixes=...)` deliberately hides the in-flight `.downloading-<name>/`
# tree, so the reconciler sees no local bytes for those children and computes REMOTE_ONLY.
#
# The 5s active-queue pass turned a 30s flip into a 5s one and is what made it visible, but
# neither feature is the cause -- the subtree simply was never protected.


async def _make_parent_child_engine(tmp_path, monkeypatch, *, child_state: str):
    """A directory item with a running `mirror` job and one child file, scanned against a local
    tree that shows *nothing* -- exactly what `scan_local` returns while the prefixed in-flight
    directory is filtered out.
    """
    parent_rel = "Release.Dir"
    child_rel = f"{parent_rel}/episode.mkv"

    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', ?, 1, 'copy')",
        (host_id, str(tmp_path)),
    )
    queue_id = cursor.lastrowid

    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 1, ?, NULL, 'DOWNLOADING')",
        (queue_id, parent_rel, SIZE),
    )
    parent_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (queue_id, child_rel, SIZE, SIZE // 2, child_state),
    )
    child_id = cursor.lastrowid
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane) VALUES (?, 'mirror', 'running', 'main')",
        (parent_id,),
    )
    await db.commit()

    await save_settle_settings(db, SettleSettings(enabled=False))
    # The prefixed in-flight directory is filtered out of the local walk, so the reconciler sees
    # an empty local tree even though bytes are landing.
    monkeypatch.setattr(engine_module.local_scan, "scan_local", lambda root, **_kwargs: {})  # noqa: ARG005

    engine = Engine(db, str(tmp_path), EventBus())
    engine.pool = _FakePool(
        {
            parent_rel: RemoteEntry(rel_path=parent_rel, is_dir=True, size=0, mtime=1.0),
            child_rel: RemoteEntry(rel_path=child_rel, is_dir=False, size=SIZE, mtime=1.0),
        }
    )
    q = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="q",
        remote_path="/remote",
        local_path=str(tmp_path),
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
        known_hosts_policy="strict",
    )
    return engine, q, host, db, parent_id, child_id


async def test_a_childs_state_is_not_overwritten_while_its_parent_job_runs(tmp_path, monkeypatch):
    """The reported bug. `core/queue.py._publish_child_progress` set PARTIAL; the scan used to
    recompute REMOTE_ONLY off an empty local tree and stomp it, once per pass.
    """
    engine, q, host, db, _parent_id, child_id = await _make_parent_child_engine(
        tmp_path, monkeypatch, child_state="PARTIAL"
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, child_id))[0] == "PARTIAL"
        # Repeated passes must not flip it either -- the flip was per-scan, not first-scan-only.
        await engine.scan_queue(q, host)
        assert (await _state_of(db, child_id))[0] == "PARTIAL"
    finally:
        await db.close()


async def test_the_parent_itself_is_still_protected(tmp_path, monkeypatch):
    """Unchanged behaviour, guarded: the top-level item has its own job row and was already
    protected before this change.
    """
    engine, q, host, db, parent_id, _child_id = await _make_parent_child_engine(
        tmp_path, monkeypatch, child_state="PARTIAL"
    )
    try:
        await engine.scan_queue(q, host)
        assert (await _state_of(db, parent_id))[0] == "DOWNLOADING"
    finally:
        await db.close()


async def test_a_child_is_recomputed_again_once_the_job_finishes(tmp_path, monkeypatch):
    """Protection must not outlive the job -- otherwise a crashed or completed transfer would
    wedge every child at whatever the last progress tick wrote, which is the same failure shape
    `test_a_crashed_worker_does_not_wedge_the_item` guards for the transient states.
    """
    engine, q, host, db, _parent_id, child_id = await _make_parent_child_engine(
        tmp_path, monkeypatch, child_state="PARTIAL"
    )
    try:
        await db.execute("UPDATE job SET state = 'succeeded'")
        await db.commit()
        await engine.scan_queue(q, host)
        assert (await _state_of(db, child_id))[
            0
        ] == "REMOTE_ONLY", "no active job -- the structural reading wins again"
    finally:
        await db.close()
