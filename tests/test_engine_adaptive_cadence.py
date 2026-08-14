"""`prompts/2026-08-14-adaptive-scan-cadence-when-active.md`: while a queue is *active* (a
running job, an item mid download/verify/extract, an item held at the settle gate, or
post-processing in flight), `Engine` layers an extra ~`ACTIVE_SCAN_INTERVAL_S` local-only pass
on top of its own configured full-scan cadence -- restoring the local half of DESIGN.md §5's
original two-cadence design (docs/decisions.md's phase 2 entry). `tests/test_engine_scan_
cadence.py` already covers the pre-existing full-scan-only scheduling (`effective_scan_
interval`, `_is_due`/`_schedule_next`, no-overlap) and is left untouched by this task; this file
covers only what's new.

**The one thing that matters most**: a local-only pass must never advance, reset, or otherwise
touch `item_settle` (`test_local_only_pass_never_writes_item_settle` and
`test_local_only_pass_does_not_release_an_unsettled_item_early` below), and must never run
before its queue has completed at least one successful full scan
(`test_local_only_pass_never_runs_before_first_successful_full_scan`).
"""

from __future__ import annotations

import math
import time

import aiosqlite
import pytest

import lftpweb.core.engine as engine_module
from lftpweb.core import settle
from lftpweb.core.engine import (
    ACTIVE_SCAN_INTERVAL_S,
    Engine,
    QueueConfig,
    load_queues,
    queue_is_active,
    resolve_active_check_interval,
)
from lftpweb.core.events import EventBus
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.db import migrate

# --- resolve_active_check_interval: the pure resolver ---------------------------------------


def test_none_full_interval_stays_none():
    """An on-demand-only queue (`effective_scan_interval` already resolved to `None`) must not
    gain a timer of any kind just by becoming active.
    """
    assert resolve_active_check_interval(None) is None


def test_slower_configured_interval_is_floored_at_the_active_constant():
    assert resolve_active_check_interval(30.0) == ACTIVE_SCAN_INTERVAL_S


def test_faster_configured_interval_keeps_its_own_faster_cadence():
    """`min`, not "replace" -- a queue already configured faster than the active floor is not
    slowed down to it.
    """
    assert resolve_active_check_interval(2.0) == 2.0


def test_exactly_the_active_constant_is_unaffected():
    assert resolve_active_check_interval(ACTIVE_SCAN_INTERVAL_S) == ACTIVE_SCAN_INTERVAL_S


# --- queue_is_active: the predicate, against a bare DB (no Engine needed) -------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_queue(db, name: str = "q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/remote', '/local', 1, 'copy')",
        (host_id, name),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(
    db, queue_id: int, rel_path: str, *, state: str, substate: str | None = None
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, substate) VALUES (?, ?, 0, ?, ?)",
        (queue_id, rel_path, state, substate),
    )
    await db.commit()
    return cursor.lastrowid


async def test_idle_queue_is_not_active(db):
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "a", state="DOWNLOADED")
    assert await queue_is_active(db, queue_id) is False


async def test_queued_job_is_active(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "a", state="QUEUED")
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'pget', 'queued')", (item_id,)
    )
    await db.commit()
    assert await queue_is_active(db, queue_id) is True


async def test_running_job_is_active(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "a", state="DOWNLOADING")
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'pget', 'running')", (item_id,)
    )
    await db.commit()
    assert await queue_is_active(db, queue_id) is True


@pytest.mark.parametrize("state", ["DOWNLOADING", "VERIFYING", "EXTRACTING"])
async def test_transient_item_states_are_active(db, state):
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "a", state=state)
    assert await queue_is_active(db, queue_id) is True


async def test_item_held_at_the_settle_gate_is_active(db):
    """The "arriving" case the user named explicitly."""
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "a", state="REMOTE_ONLY", substate="settling")
    assert await queue_is_active(db, queue_id) is True


async def test_remote_only_without_settling_substate_is_not_active(db):
    queue_id = await _make_queue(db)
    await _make_item(db, queue_id, "a", state="REMOTE_ONLY", substate=None)
    assert await queue_is_active(db, queue_id) is False


async def test_postprocess_in_flight_item_is_active(db):
    queue_id = await _make_queue(db)
    item_id = await _make_item(db, queue_id, "a", state="VERIFIED")
    assert await queue_is_active(db, queue_id, frozenset({item_id})) is True


async def test_postprocess_in_flight_id_from_a_different_queue_does_not_count(db):
    queue_a = await _make_queue(db, "a")
    queue_b = await _make_queue(db, "b")
    item_b = await _make_item(db, queue_b, "x", state="VERIFIED")
    assert await queue_is_active(db, queue_a, frozenset({item_b})) is False


async def test_active_job_on_a_different_queue_does_not_count(db):
    """Isolation: a busy queue must not make every *other* queue read as active too."""
    queue_a = await _make_queue(db, "a")
    queue_b = await _make_queue(db, "b")
    item_b = await _make_item(db, queue_b, "x", state="DOWNLOADING")
    await db.execute(
        "INSERT INTO job (item_id, kind, state) VALUES (?, 'pget', 'running')", (item_b,)
    )
    await db.commit()
    assert await queue_is_active(db, queue_a) is False


# --- Engine scheduling integration: the local-only clock layered on the full-scan one --------


async def _make_engine_with_one_queue(db, tmp_path, *, scan_interval_s):
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid

    local = tmp_path / "q"
    local.mkdir()
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode, "
        "scan_interval_s) VALUES (?, 'q', '/remote', ?, 1, 'copy', ?)",
        (host_id, str(local), scan_interval_s),
    )
    await db.commit()
    queue_id = cursor.lastrowid

    engine = Engine(db, str(tmp_path), EventBus(), scan_interval_s=30.0)
    host = HostConfig(
        id=host_id,
        address="127.0.0.1",
        port=22,
        username="u",
        auth_method="password",
        password="x",
        known_hosts_policy="insecure",
    )
    queues = await load_queues(db)
    q = next(qc for qc in queues if qc.id == queue_id)
    return engine, host, q


class _FullScanSpy:
    """Replaces `Engine.scan_queue`. Unlike `tests/test_engine_scan_cadence.py`'s `_ScanSpy`,
    this one also seeds `engine._cached_remote_tree` -- the real `scan_queue` always does this
    on a successful remote read, and the scheduling tests below need that cache present to ever
    reach the local-only branch at all.
    """

    def __init__(self, engine: Engine, *, seed_cache: bool = True) -> None:
        self.engine = engine
        self.seed_cache = seed_cache
        self.calls: list[int] = []

    async def __call__(self, q: QueueConfig, host) -> None:  # noqa: ARG002 - host unused
        self.calls.append(q.id)
        if self.seed_cache:
            self.engine._cached_remote_tree[q.id] = {}


class _LocalOnlySpy:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, q: QueueConfig) -> None:
        self.calls.append(q.id)


async def test_local_only_pass_runs_between_full_scans_while_active(db, tmp_path, monkeypatch):
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)
    await _make_item(db, q.id, "a", state="DOWNLOADING")  # makes the queue active

    full_spy = _FullScanSpy(engine)
    local_spy = _LocalOnlySpy()
    engine.scan_queue = full_spy
    engine._scan_queue_local_only = local_spy

    clock = [1000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1 (t=1000): never scanned -> full scan
    assert full_spy.calls == [q.id]
    assert local_spy.calls == []
    assert engine._next_due[q.id] == pytest.approx(1030.0)  # site default, 30s
    assert engine._next_local_due[q.id] == pytest.approx(1005.0)  # min(30, 5)

    clock[0] = 1006.0  # past local-due (1005), well before full-due (1030)
    await engine.scan_all()  # pass 2
    assert full_spy.calls == [q.id], "a full scan ran when only the local clock was due"
    assert local_spy.calls == [q.id]
    assert engine._next_local_due[q.id] == pytest.approx(1011.0)


async def test_local_only_pass_does_not_run_while_idle(db, tmp_path, monkeypatch):
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)
    # No active item at all -- an idle queue.

    full_spy = _FullScanSpy(engine)
    local_spy = _LocalOnlySpy()
    engine.scan_queue = full_spy
    engine._scan_queue_local_only = local_spy

    clock = [2000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1: full scan (never scanned yet)
    clock[0] = 2006.0  # local clock due, full clock not
    await engine.scan_all()  # pass 2

    assert local_spy.calls == [], "an idle queue's local-only pass ran anyway"
    # Still rescheduled -- proves this isn't just "never checked again" but "checked and found
    # idle," so a queue that becomes active later is noticed within one heartbeat.
    assert engine._next_local_due[q.id] == pytest.approx(2011.0)


async def test_local_only_pass_never_runs_before_first_successful_full_scan(
    db, tmp_path, monkeypatch
):
    """Guard: even if the queue is active and its local-check clock keeps coming due, a
    local-only pass must never fire until `_cached_remote_tree` actually has this queue's tree
    in it -- i.e. until a full scan has actually succeeded, not merely been attempted.
    """
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)
    await _make_item(db, q.id, "a", state="DOWNLOADING")

    # `seed_cache=False` simulates a full scan attempt that never got far enough to read the
    # remote (e.g. the host is unreachable) -- `scan_queue` never raises to its caller, so
    # `scan_all` still reschedules both clocks exactly as it would on success.
    full_spy = _FullScanSpy(engine, seed_cache=False)
    local_spy = _LocalOnlySpy()
    engine.scan_queue = full_spy
    engine._scan_queue_local_only = local_spy

    clock = [3000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1: attempted, but no tree cached
    assert q.id not in engine._cached_remote_tree

    for _ in range(5):
        clock[0] += 5.0  # keep landing on the local clock's own due time
        await engine.scan_all()

    assert local_spy.calls == [], "a local-only pass ran with no cached remote tree"
    assert full_spy.calls == [q.id], "the full-scan clock fired again, which would defeat the test"


async def test_active_but_on_demand_only_queue_never_gets_a_local_timer(db, tmp_path, monkeypatch):
    """`scan_interval_s = 0` ("none" in the UI) -- must not gain a 5s timer just by becoming
    active. Mirrors `tests/test_engine_scan_cadence.py::test_none_interval_queue_is_never_
    scanned_on_a_timer` for the local clock.
    """
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=0)
    await _make_item(db, q.id, "a", state="DOWNLOADING")

    full_spy = _FullScanSpy(engine)
    local_spy = _LocalOnlySpy()
    engine.scan_queue = full_spy
    engine._scan_queue_local_only = local_spy

    clock = [4000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1: never scanned -> full scan even for an on-demand queue
    assert engine._next_local_due[q.id] == math.inf

    clock[0] += 100_000.0  # an enormous amount of time
    await engine.scan_all(force=False)

    assert local_spy.calls == [], "an on-demand-only queue's local pass fired on a timer"
    assert full_spy.calls == [q.id], "an on-demand-only queue was rescanned on a timer"


async def test_faster_than_active_floor_queue_keeps_its_own_local_cadence(db, tmp_path):
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=2.0)
    engine._schedule_next_local(q, now=1000.0)
    assert engine._next_local_due[q.id] == pytest.approx(1002.0)


async def test_next_wake_delay_considers_the_earlier_of_both_clocks(db, tmp_path, monkeypatch):
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)
    engine.queue_meta = {q.id: q}

    clock = [1000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    engine._next_due[q.id] = 1050.0  # far away (full-scan clock)
    engine._next_local_due[q.id] = 1010.0  # sooner (local-check clock)

    # The local clock is the sooner of the two, so the wake delay must track it, not the full
    # clock's much later due time.
    assert engine._next_wake_delay() == pytest.approx(10.0)


# --- The critical constraint: item_settle is never touched by a local-only pass -------------


def _remote_tree_for_one_file(rel_dir: str, rel_file: str, *, size: int, mtime: float):
    return {
        rel_dir: RemoteEntry(rel_path=rel_dir, is_dir=True),
        rel_file: RemoteEntry(rel_path=rel_file, is_dir=False, size=size, mtime=mtime),
    }


async def test_local_only_pass_never_writes_item_settle(db, tmp_path):
    """The load-bearing assertion: a local-only pass must leave `item_settle`
    byte-for-byte unchanged, whether or not the item it's reconciling happens to look complete.
    """
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)

    item_dir = tmp_path / "q" / "release"
    item_dir.mkdir()
    (item_dir / "file1.bin").write_bytes(b"x" * 10)

    await _make_item(db, q.id, "release", state="REMOTE_ONLY", substate="settling")
    # A real settle record: not yet settled (matched_scans below REQUIRED_SETTLE_SCANS).
    record = settle.SettleRecord(
        fingerprint=(1, 10, 12345.0), matched_scans=1, first_matched_at=time.time()
    )
    await settle.save_settle_records(db, q.id, {"release": record})
    await db.commit()
    # Read back through the identical (lossy, microsecond-precision ISO) round trip the
    # assertion below re-reads through -- comparing to the DB's own prior reading rather than
    # to the freshly-constructed `record` avoids a false failure from that round trip alone,
    # which has nothing to do with whether the local-only pass touched anything.
    before = (await settle.load_settle_records(db, q.id))["release"]

    engine._cached_remote_tree[q.id] = _remote_tree_for_one_file(
        "release", "release/file1.bin", size=10, mtime=12345.0
    )

    await engine._scan_queue_local_only(q)

    after = (await settle.load_settle_records(db, q.id))["release"]
    assert after == before, "a local-only pass wrote to item_settle"


async def test_local_only_pass_does_not_release_an_unsettled_item_early(db, tmp_path):
    """The corruption bug the settle gate exists to prevent, reintroduced via a naive local-only
    pass: local bytes catching up to a *stale cached* remote total must not be read as
    "settled" just because the structural byte comparison says complete. The gate's
    last-persisted verdict (not yet settled: one matching scan is short of
    `REQUIRED_SETTLE_SCANS`) must still hold the item at REMOTE_ONLY/settling.
    """
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)

    item_dir = tmp_path / "q" / "release"
    item_dir.mkdir()
    (item_dir / "file1.bin").write_bytes(b"x" * 10)

    await _make_item(db, q.id, "release", state="REMOTE_ONLY", substate="settling")
    record = settle.SettleRecord(
        fingerprint=(1, 10, 12345.0), matched_scans=1, first_matched_at=time.time()
    )
    await settle.save_settle_records(db, q.id, {"release": record})
    await db.commit()

    engine._cached_remote_tree[q.id] = _remote_tree_for_one_file(
        "release", "release/file1.bin", size=10, mtime=12345.0
    )

    await engine._scan_queue_local_only(q)

    cursor = await db.execute(
        "SELECT state, substate FROM item WHERE queue_id = ? AND rel_path = 'release'", (q.id,)
    )
    row = await cursor.fetchone()
    assert (row["state"], row["substate"]) == (
        "REMOTE_ONLY",
        "settling",
    ), "a local-only pass released an item the settle gate had not actually cleared"


async def test_local_only_pass_can_still_release_an_already_settled_item(db, tmp_path):
    """The flip side, so the fix above isn't mistaken for "local-only passes can never complete
    an item": once the gate's own last-persisted verdict already says settled (enough matched
    scans, and old enough), a local-only pass detecting local bytes finally catching up is
    exactly `core/engine.py`'s own point 4 -- "the transition to DOWNLOADED after a job reaps" --
    and must be allowed through, still without writing `item_settle`.
    """
    engine, host, q = await _make_engine_with_one_queue(db, tmp_path, scan_interval_s=None)

    item_dir = tmp_path / "q" / "release"
    item_dir.mkdir()
    (item_dir / "file1.bin").write_bytes(b"x" * 10)

    await _make_item(db, q.id, "release", state="REMOTE_ONLY", substate="settling")
    # Settled: two matching scans, and old enough to clear SETTLE_MIN_AGE_S.
    settled_record = settle.SettleRecord(
        fingerprint=(1, 10, 12345.0),
        matched_scans=2,
        first_matched_at=time.time() - settle.SETTLE_MIN_AGE_S - 1.0,
    )
    await settle.save_settle_records(db, q.id, {"release": settled_record})
    await db.commit()
    # Same round-trip-comparison reasoning as the sibling test above.
    before = (await settle.load_settle_records(db, q.id))["release"]

    engine._cached_remote_tree[q.id] = _remote_tree_for_one_file(
        "release", "release/file1.bin", size=10, mtime=12345.0
    )

    await engine._scan_queue_local_only(q)

    cursor = await db.execute(
        "SELECT state, substate FROM item WHERE queue_id = ? AND rel_path = 'release'", (q.id,)
    )
    row = await cursor.fetchone()
    assert row["state"] == "DOWNLOADED"

    after = (await settle.load_settle_records(db, q.id))["release"]
    assert after == before, "a local-only pass wrote to item_settle even on a legitimate release"
