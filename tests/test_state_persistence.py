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
        lambda root: _local_tree(local_size),  # noqa: ARG005
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
            lambda root: _local_tree(SIZE),  # noqa: ARG005
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
