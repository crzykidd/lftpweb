"""Per-queue scan interval (prompts/done/2026-08-12-per-queue-scan-interval.md), migration
009. `core/settle.py`'s wall-clock floor is proven immune to scan cadence already in
`tests/test_settle.py::test_atomic_arrival_settles_after_exactly_two_scans_and_the_age_floor`
(it injects `now` directly and asserts `is_settled` stays `False` until `SETTLE_MIN_AGE_S` has
elapsed regardless of how many matching scans happened) -- not repeated here.

This file covers the three things that are new: `effective_scan_interval`'s NULL/0/positive
resolution, the multi-cadence loop waking at the earliest per-queue next-due and scanning only
what's due (not everything on the shortest queue's cadence -- the easy wrong implementation the
handoff prompt named explicitly), and that an overrunning scan cannot stack a second concurrent
scan of the same queue.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

import lftpweb.core.engine as engine_module
from lftpweb.core.engine import Engine, QueueConfig, effective_scan_interval
from lftpweb.core.events import EventBus
from lftpweb.core.remote import HostConfig
from lftpweb.db import migrate

# --- effective_scan_interval: the pure resolver ---------------------------------------------


def test_null_means_use_the_site_default():
    assert effective_scan_interval(None, 30.0) == 30.0


def test_zero_means_on_demand_only():
    assert effective_scan_interval(0, 30.0) is None


def test_negative_also_means_on_demand_only():
    # Defensive only -- api/settings.py._reject_invalid_scan_interval and the DB's own CHECK
    # (migration 009) both refuse a negative value before it ever reaches this function; this
    # pins the fallback reading rather than a crash if either guard is ever bypassed.
    assert effective_scan_interval(-5, 30.0) is None


def test_positive_value_is_used_literally():
    assert effective_scan_interval(10.0, 30.0) == 10.0


# --- Engine fixture: a real DB (host + two queues), a stubbed scan_queue --------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_engine_with_queues(db, tmp_path, *, fast_interval, slow_interval):
    """One host, two enabled queues -- `fast` at `fast_interval` seconds, `slow` at
    `slow_interval` (`None` for either means "use the site default," matching the API/DB's own
    NULL convention).
    """
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid

    ids = {}
    for name, interval in (("fast", fast_interval), ("slow", slow_interval)):
        local = tmp_path / name
        local.mkdir()
        cursor = await db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
            "sync_mode, scan_interval_s) VALUES (?, ?, ?, ?, 1, 'copy', ?)",
            (host_id, name, f"/remote/{name}", str(local), interval),
        )
        await db.commit()
        ids[name] = cursor.lastrowid

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
    return engine, host, ids


class _ScanSpy:
    """Replaces `Engine.scan_queue` -- records which queue names were scanned, on which call
    (`scan_all` invocation number), without touching the network/filesystem at all.

    `overlaps` detects the one failure mode this feature introduces: two overlapping scans of
    the same queue (`test_overrunning_scan_of_one_queue_never_overlaps_itself` below). Recorded
    into a plain list rather than raised as an `AssertionError` from inside `__call__` --
    `Engine._loop`'s own `except Exception` ("one bad cycle must not kill the loop") would
    silently swallow a raised assertion, which would make the test pass for the wrong reason
    (an unraised, unchecked violation) instead of failing loudly.
    """

    def __init__(self, ids_to_name: dict[int, str], *, delay: float = 0.0):
        self.calls: list[tuple[int, str]] = []  # (scan_all call index, queue name)
        self.overlaps: list[str] = []
        self.delay = delay
        self._ids_to_name = ids_to_name
        self._busy: set[int] = set()
        self.call_index = 0

    async def __call__(self, q: QueueConfig, host) -> None:  # noqa: ARG002 - host unused
        if q.id in self._busy:
            self.overlaps.append(q.name)
        self._busy.add(q.id)
        try:
            self.calls.append((self.call_index, q.name))
            if self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self._busy.discard(q.id)


async def test_scan_all_first_pass_scans_every_enabled_queue_regardless_of_interval(db, tmp_path):
    """Matches every prior phase: a queue with no completed scan yet is due immediately, no
    matter what its own interval says.
    """
    engine, host, ids = await _make_engine_with_queues(
        db, tmp_path, fast_interval=10.0, slow_interval=60.0
    )
    spy = _ScanSpy(ids)
    engine.scan_queue = spy
    await engine.scan_all()
    assert {name for _, name in spy.calls} == {"fast", "slow"}


async def test_scan_all_only_scans_what_is_due_not_everything_on_the_fastest_cadence(
    db, tmp_path, monkeypatch
):
    """The behavior the handoff prompt named as "the easy wrong implementation": a fast queue
    becoming due must not also re-scan a slow queue that isn't due yet.
    """
    engine, host, ids = await _make_engine_with_queues(
        db, tmp_path, fast_interval=10.0, slow_interval=60.0
    )
    spy = _ScanSpy(ids)
    engine.scan_queue = spy

    clock = [1000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1 (t=1000): both due (never scanned)
    spy.call_index = 1
    clock[0] += 15  # t=1015: fast (due at 1010) is due again, slow (due at 1060) is not
    await engine.scan_all()

    pass_1 = {name for idx, name in spy.calls if idx == 0}
    pass_2 = {name for idx, name in spy.calls if idx == 1}
    assert pass_1 == {"fast", "slow"}
    assert pass_2 == {"fast"}, "the slow queue was rescanned before its own interval elapsed"


async def test_none_interval_queue_is_never_scanned_on_a_timer(db, tmp_path, monkeypatch):
    """`scan_interval_s = 0` -- "none" in the UI -- must never fire again on its own after the
    first (unavoidable, "never scanned yet") pass. Only a forced pass (`request_rescan()`, "the
    Rescan button still work[s]" per the handoff prompt) reaches it after that.
    """
    engine, host, ids = await _make_engine_with_queues(
        db, tmp_path, fast_interval=0.0, slow_interval=30.0
    )
    spy = _ScanSpy(ids)
    engine.scan_queue = spy

    clock = [2000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1: both due (never scanned)
    spy.call_index = 1
    clock[0] += 10_000  # an enormous amount of time -- "none" must still never come due
    await engine.scan_all()  # pass 2, not forced
    spy.call_index = 2
    await engine.scan_all(force=True)  # pass 3, forced -- the "Rescan now" path

    pass_2 = {name for idx, name in spy.calls if idx == 1}
    pass_3 = {name for idx, name in spy.calls if idx == 2}
    assert "fast" not in pass_2, "a 'none'-interval queue was scanned on a timer"
    assert "fast" in pass_3, "request_rescan()/force must still reach a 'none'-interval queue"


async def test_forced_rescan_reschedules_the_queues_own_next_due(db, tmp_path, monkeypatch):
    """A forced pass is a real scan, not a free one -- it must restart the scanned queue's own
    clock from its completion, not leave the previous schedule (or worse, cause it to look
    "overdue forever" and re-fire on every subsequent unforced pass).
    """
    engine, host, ids = await _make_engine_with_queues(
        db, tmp_path, fast_interval=10.0, slow_interval=30.0
    )
    spy = _ScanSpy(ids)
    engine.scan_queue = spy

    clock = [5000.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    await engine.scan_all()  # pass 1 (t=5000)
    spy.call_index = 1
    clock[0] += 5  # t=5005 -- fast is not yet due (due at 5010)
    await engine.scan_all(force=True)  # forced: scans both anyway, reschedules fast to 5015
    spy.call_index = 2
    clock[0] += 6  # t=5011 -- would have been due under the old schedule, not the new one
    await engine.scan_all()  # unforced

    pass_3 = {name for idx, name in spy.calls if idx == 2}
    assert pass_3 == set(), "forced rescan did not restart the queue's own next-due"


async def test_overrunning_scan_of_one_queue_never_overlaps_itself(db, tmp_path, monkeypatch):
    """The exact failure mode a fast interval introduces: a queue whose scan takes longer than
    its own interval must never have a second scan of *itself* start while the first is still
    running. Runs the real `_loop`, not a mocked-out timing calculation, so this exercises the
    actual wake/schedule code path end to end.
    """
    engine, host, ids = await _make_engine_with_queues(
        db, tmp_path, fast_interval=0.03, slow_interval=None
    )
    # `scan_queue` is asked to take far longer (0.12s) than its own interval (0.03s) -- if the
    # loop's scheduling were naive (e.g. a fixed timeout ignoring how long the previous pass
    # took), the next wake would fire while this one is still in flight.
    spy = _ScanSpy(ids, delay=0.12)
    engine.scan_queue = spy

    await engine.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        await engine.stop()

    fast_calls = [name for _, name in spy.calls if name == "fast"]
    assert spy.overlaps == [], "an overrunning scan started a second concurrent scan of itself"
    assert len(fast_calls) >= 2, "the loop should still make forward progress despite the overrun"
