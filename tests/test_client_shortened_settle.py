"""The client-shortened settle (2026-08-24, `prompts/2026-08-24-client-shortened-settle.md`;
reworked 2026-08-29, `prompts/done/2026-08-29-settle-verify-under-existing-toggle.md`) --
`core/autoqueue.py` item 8 of its own module docstring: once a client reports a release finished
(`SEEDING` or `COMPLETED`), this mechanism fingerprints the item's remote subtree, waits
`settle.CLIENT_RECHECK_INTERVAL_S` (5s), fingerprints it again, and queues only if nothing moved.

**Gated by `settle.SettleSettings.client_skip_enabled`, which now has one meaning and one
default.** The 2026-08-24 version of this mechanism registered unconditionally (no toggle of its
own, shipped default ON) alongside a separate, older pure time-hold that trusted a terminal
verdict once it was merely old enough -- gated by this same flag, but that flag defaulted off.
2026-08-29 deleted the old time-hold outright and folded this mechanism under the existing
toggle instead: **one toggle, one meaning -- skipping the wait means verifying that nothing
moved** -- and flipped the toggle's own default to `True`, since a verified skip carries none of
the old time-hold's "trusts an unconfirmed status mapping" risk. Most tests below never call
`save_settle_settings` at all, relying on `load_settle_settings`'s own default (`SettleSettings()`
with no arguments) -- that default is `client_skip_enabled=True`, so the mechanism fires exactly
as if it had been explicitly turned on. `test_client_skip_disabled_registers_nothing_and_does_no_io`
and `test_defaults_alone_register_a_pending_recheck` below pin both directions of that default
explicitly, rather than leaving it to be inferred from every other test merely happening to pass.

**The defect this task fixes, proven live against a real (fake) rTorrent first.**
`core/clients/rtorrent.py._classify_token` maps a finished, actively-seeding torrent to
`SEEDING`, never `COMPLETED` -- `tests/test_clients_rtorrent.py.
test_active_only_excludes_completed_but_keeps_seeding` documents that mapping as correct, and it
stays correct here (this task's own explicit rule: fix what the gate accepts, never what the
connector reports). Before this task's widening, `core/clientsync.py.ClientSyncScheduler.
completed_transfers` (renamed `finished_transfers` here) filtered to `TransferPhase.COMPLETED`
only, so the settle-gate skip was structurally unreachable for an ordinary seeding torrent.
`tests/test_settle.py.test_find_client_completion_matches_a_seeding_verdict` is the pure-function
half of that proof (watched to fail against the pre-widening code, then pass after); this file is
the wired-up half, and `test_seeding_torrent_reaches_finished_transfers` below is run against the
real fake rTorrent fixture -- `tests/test_settle_client_skip.py` used to run the equivalent SABnzbd
proof for the now-deleted mechanism; it is gone along with that mechanism (docs/decisions.md).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from fake_rtorrent import FakeRtorrentTorrent
from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.clientsync import ClientSyncScheduler
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.remote import HostConfig, RemoteEntry
from lftpweb.core.settle import CLIENT_RECHECK_INTERVAL_S, SettleSettings, save_settle_settings
from lftpweb.db import migrate

REMOTE_PATH = "/complete/ar-tv"
RELEASE = "Show.S01"
_HOST = HostConfig(id=1, address="seedbox.invalid", port=22, username="u", auth_method="key")


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _Recorder:
    def __init__(self):
        self.enqueued: list[int] = []

    async def __call__(self, item_id: int) -> int:
        self.enqueued.append(item_id)
        return item_id


class _FakePool:
    """A hand-built stand-in for `core/remote.py.RemoteConnectionPool`, the same "no mocked
    transport, but no real SSH either" shape a pure unit test needs for
    `AutoQueue._fetch_item_fingerprint` -- the wired SSH path itself is exercised by every other
    test in this repo that scans a queue for real (`tests/test_engine_*`), not re-proven here.
    Queued responses so a test can hand back a different remote tree on the second `scan()` call
    than the first (the "did anything move" comparison this whole mechanism exists to make).
    """

    def __init__(self, *responses: tuple[dict[str, RemoteEntry], str | None]):
        self._responses = list(responses)
        self.calls: list[str] = []

    async def scan(self, host: HostConfig, remote_path: str):
        self.calls.append(remote_path)
        if not self._responses:
            raise AssertionError("fake pool ran out of queued responses")
        return self._responses.pop(0)


def _entries(*, file_count: int, size_each: int, mtime: float) -> dict[str, RemoteEntry]:
    return {
        RELEASE: RemoteEntry(rel_path=RELEASE, is_dir=True),
        **{
            f"{RELEASE}/f{i}.mkv": RemoteEntry(
                rel_path=f"{RELEASE}/f{i}.mkv", is_dir=False, size=size_each, mtime=mtime
            )
            for i in range(file_count)
        },
    }


async def _make_queue(db: aiosqlite.Connection, local_path) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method, known_hosts_policy) "
        "VALUES ('h', 'example.invalid', 'u', 'key', 'insecure')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', ?, ?, 1, 'copy')",
        (host_id, REMOTE_PATH, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db: aiosqlite.Connection, queue_id: int, rel_path: str) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, state, auto_queue_suppressed) "
        "VALUES (?, ?, 1, 100, 'REMOTE_ONLY', 0)",
        (queue_id, rel_path),
    )
    await db.commit()
    return cursor.lastrowid


async def _set_settle_record(db: aiosqlite.Connection, queue_id: int, rel_path: str) -> None:
    """Deliberately **not settled yet** -- every test here is about whether the client-shortened
    settle can reach a queued state in place of the ordinary gate, not whether the ordinary gate
    itself already would have."""
    await db.execute(
        "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans) "
        "VALUES (?, ?, 1, 100, 1.0, 1)",
        (queue_id, rel_path),
    )
    await db.commit()


def _queue_config(queue_id: int, local_path):
    return QueueAutoConfig(
        id=queue_id,
        name="q",
        short_name=None,
        local_path=str(local_path),
        remote_path=REMOTE_PATH,
        auto_queue_enabled=True,
        patterns_only=False,
    )


async def _wire_rtorrent_client(
    db: aiosqlite.Connection, tmp_path, fake_rtorrent_server
) -> ClientSyncScheduler:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config_json = json.dumps(
        {
            "base_url": fake_rtorrent_server.base_url,
            "username": fake_rtorrent_server.state.username,
            "password": fake_rtorrent_server.state.password,
        }
    )
    await db.execute(
        "INSERT INTO download_client (name, client_type, config_json, secret_enc, enabled, "
        "created_at, updated_at) VALUES (?, 'rtorrent', ?, NULL, 1, ?, ?)",
        ("rTorrent", config_json, now, now),
    )
    await db.commit()
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=1_000_000.0)  # first pass always polls the slow/full cadence
    return scheduler


async def _events(db: aiosqlite.Connection, kind: str):
    cursor = await db.execute(
        "SELECT item_id, message FROM event WHERE kind = ? ORDER BY id", (kind,)
    )
    return await cursor.fetchall()


# --- The wired-up half of the SEEDING proof (see module docstring) -------------------------


async def test_seeding_torrent_reaches_finished_transfers(db, tmp_path, fake_rtorrent_server):
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,  # complete + active == SEEDING, per _classify_token
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    finished = scheduler.finished_transfers()
    assert len(finished) == 1
    _instance_id, instance_name, transfer = finished[0]
    assert instance_name == "rTorrent"
    assert transfer.content_path == f"{REMOTE_PATH}/{RELEASE}"


# --- on_scan only registers; it never fetches, sleeps, or enqueues for this mechanism -------


async def test_on_scan_registers_without_fetching_or_enqueuing(db, tmp_path, fake_rtorrent_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    fake_pool = _FakePool()  # no responses queued -- must never be called by on_scan itself
    aq.remote_pool = fake_pool

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)

    assert queued == 0
    assert recorder.enqueued == []
    assert fake_pool.calls == []  # the whole point: on_scan never does I/O for this mechanism
    assert item_id in aq._pending_recheck.get(queue_id, {})


async def test_defaults_alone_register_a_pending_recheck(db, tmp_path, fake_rtorrent_server):
    """Pins the default explicitly, rather than leaving it to be inferred from every other test
    in this file merely happening to pass (2026-08-29,
    prompts/done/2026-08-29-settle-verify-under-existing-toggle.md -- the user's own call: "yes,
    make it on by default since it verifies"). `save_settle_settings` is never called at all here
    -- `SettleSettings()` with no arguments is what `load_settle_settings` falls back to for a
    fresh install, and its `client_skip_enabled` field defaults `True`. Asserting
    `settle.SettleSettings().client_skip_enabled is True` on its own would not prove `on_scan`
    actually reads it; this drives the real registration path end to end instead.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    aq.remote_pool = _FakePool()  # no responses queued -- registering must not fetch

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)

    assert queued == 0
    assert recorder.enqueued == []
    assert item_id in aq._pending_recheck.get(queue_id, {})


async def test_client_skip_disabled_registers_nothing_and_does_no_io(
    db, tmp_path, fake_rtorrent_server
):
    """The opt-out direction, now that the default is on (2026-08-29,
    prompts/done/2026-08-29-settle-verify-under-existing-toggle.md): with `client_skip_enabled`
    explicitly `False`, a finished client verdict must register **no** pending recheck and the
    ticker's remote pool must never be touched -- byte-identical to the ordinary settle gate
    running with no client involvement at all. Named so it is obvious this protects the opt-out,
    the mirror of `test_defaults_alone_register_a_pending_recheck` above.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    await save_settle_settings(db, SettleSettings(enabled=True, client_skip_enabled=False))
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    fake_pool = _FakePool()  # no responses queued -- must never be called with the toggle off
    aq.remote_pool = fake_pool

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)

    assert queued == 0
    assert recorder.enqueued == []
    assert fake_pool.calls == []
    assert aq._pending_recheck.get(queue_id, {}) == {}


# --- The recheck itself: unchanged fingerprint queues; changed fingerprint falls back -------


async def test_unchanged_fingerprint_across_the_recheck_queues_the_item(
    db, tmp_path, fake_rtorrent_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    same_entries = _entries(file_count=3, size_each=100, mtime=1_000.0)
    fake_pool = _FakePool((same_entries, None), (same_entries, None))
    aq.remote_pool = fake_pool

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)
    assert recorder.enqueued == []

    # Tick 1: no fingerprint yet -- takes the first one. Not due for a second yet.
    queued = await aq.advance_pending_rechecks(now=1_000_000.0)
    assert queued == 0
    assert recorder.enqueued == []
    assert len(fake_pool.calls) == 1

    # Tick shortly after: still under CLIENT_RECHECK_INTERVAL_S -- no second fetch yet.
    queued = await aq.advance_pending_rechecks(now=1_000_000.0 + CLIENT_RECHECK_INTERVAL_S - 1)
    assert queued == 0
    assert len(fake_pool.calls) == 1

    # Tick at/after the interval: takes the second fingerprint -- identical -- queues it.
    queued = await aq.advance_pending_rechecks(now=1_000_000.0 + CLIENT_RECHECK_INTERVAL_S)
    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert len(fake_pool.calls) == 2
    assert item_id not in aq._pending_recheck.get(queue_id, {})

    events = await _events(db, "settle_client_recheck_skip")
    assert len(events) == 1
    assert events[0]["item_id"] == item_id
    assert "rTorrent" in events[0]["message"]
    assert RELEASE in events[0]["message"]


async def test_changed_fingerprint_falls_back_to_the_ordinary_settle_gate(
    db, tmp_path, fake_rtorrent_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    first = _entries(file_count=3, size_each=100, mtime=1_000.0)
    grown = _entries(file_count=3, size_each=200, mtime=1_050.0)  # still arriving
    fake_pool = _FakePool((first, None), (grown, None))
    aq.remote_pool = fake_pool

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)
    await aq.advance_pending_rechecks(now=1_000_000.0)  # first fingerprint

    queued = await aq.advance_pending_rechecks(now=1_000_000.0 + CLIENT_RECHECK_INTERVAL_S)

    assert queued == 0
    assert recorder.enqueued == []
    assert item_id not in aq._pending_recheck.get(queue_id, {})
    assert await _events(db, "settle_client_recheck_skip") == []


# --- The ticker's own lifecycle -------------------------------------------------------------


async def test_start_and_stop_the_recheck_ticker(db, tmp_path):
    """Not a timing test (`RECHECK_TICK_S` is 5s -- too slow to wait out in a unit test, and
    every other test in this file drives `advance_pending_rechecks` directly with an injected
    `now` instead). Just proves the background task actually starts and can be cancelled
    cleanly, the same lifecycle `ClientSyncScheduler.start`/`stop` already have their own
    equivalent smoke coverage for.
    """
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)

    await aq.start()
    assert aq._recheck_task is not None
    await aq.start()  # idempotent -- a second call while already running is a no-op
    await aq.stop()
    assert aq._recheck_task is None
    await aq.stop()  # idempotent the other way too


# --- No remote-scan capability wired -- falls back exactly like an unreachable client would --


async def test_no_remote_pool_wired_falls_back(db, tmp_path, fake_rtorrent_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    # aq.remote_pool / aq.host_provider left at their own defaults: None

    await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)
    assert item_id in aq._pending_recheck.get(queue_id, {})

    queued = await aq.advance_pending_rechecks(now=1_000_000.0)

    assert queued == 0
    assert recorder.enqueued == []
    assert item_id not in aq._pending_recheck.get(queue_id, {})


# --- Idle ticker does no I/O at all ----------------------------------------------------------


async def test_advance_pending_rechecks_is_a_no_op_when_nothing_is_pending(db, tmp_path):
    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    fake_pool = _FakePool()
    aq.remote_pool = fake_pool

    queued = await aq.advance_pending_rechecks(now=1_000_000.0)

    assert queued == 0
    assert fake_pool.calls == []


# --- A since-suppressed item is never resurrected by a converging recheck -------------------


async def test_a_since_suppressed_item_is_not_queued_even_after_a_matching_recheck(
    db, tmp_path, fake_rtorrent_server
):
    """`advance_pending_rechecks`'s own safety net (`AutoQueue._still_auto_queue_eligible`): the
    SQL eligibility check that first admitted this item into `self._pending_recheck` (in
    `on_scan`) can be stale by the time the recheck actually converges -- the one thing that can
    genuinely change in that window without another `on_scan` pass observing it first is a user
    action, modelled here directly against the item row the same way a manual "Stop"/suppress
    would leave it.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    same_entries = _entries(file_count=3, size_each=100, mtime=1_000.0)
    fake_pool = _FakePool((same_entries, None), (same_entries, None))
    aq.remote_pool = fake_pool

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)
    await aq.advance_pending_rechecks(now=1_000_000.0)

    # The user stops/suppresses the item while the recheck is in flight.
    await db.execute(
        "UPDATE item SET auto_queue_suppressed = 1, state = 'STOPPED' WHERE id = ?", (item_id,)
    )
    await db.commit()

    queued = await aq.advance_pending_rechecks(now=1_000_000.0 + CLIENT_RECHECK_INTERVAL_S)

    assert queued == 0
    assert recorder.enqueued == []
    assert await _events(db, "settle_client_recheck_skip") == []


# --- Off switch: settle disabled entirely means nothing here fires either -------------------


async def test_settle_disabled_registers_nothing(db, tmp_path, fake_rtorrent_server):
    """With `SettleSettings.enabled` off, there is no gate of any kind -- the item is enqueued
    immediately by `on_scan`'s own unconditional fallthrough, the identical behavior a queue with
    no client involvement at all would show. The client-shortened settle never gets a chance to
    register anything, because the whole "not settled" branch it lives in is only reached when
    the settle gate itself is on.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    await save_settle_settings(db, SettleSettings(enabled=False))
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name=RELEASE,
            complete=1,
            is_active=1,
            base_path=f"{REMOTE_PATH}/{RELEASE}",
        )
    )
    scheduler = await _wire_rtorrent_client(db, tmp_path, fake_rtorrent_server)

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    aq.remote_pool = _FakePool()

    async def _host_provider():
        return _HOST

    aq.host_provider = _host_provider

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert aq._pending_recheck.get(queue_id, {}) == {}
    assert aq.remote_pool.calls == []
