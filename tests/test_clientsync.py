"""`core/clientsync.py` -- the download-client poller (docs/download-client-framework-spec.md
§9, stage 2a of #18), against a real fake SABnzbd (`tests/fake_sabnzbd.py`, a real listening
`uvicorn` socket, not a mock) -- the same "exercise the real HTTP request/response cycle"
philosophy `tests/test_clients_sabnzbd.py` and `tests/test_arrsync.py` already use for their own
layers.

Deliberately does **not** use `tests/fake_client.py`'s in-process `FakeDownloadClient` for the
multi-pass tests below: `ClientSyncScheduler` constructs a fresh connector instance from `config`
alone on every pass (mirroring `core/arrsync.py`'s own "one client per instance per pass"), so
any state a test wants to persist *across* passes (an outage, a recovery, a blank-queue blip)
has to live somewhere the connector reconnects to, not somewhere injected at construction time --
a real (fake) server is exactly that, `tests/fake_client.py`'s constructor-injected `state` is
not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_sabnzbd import run_fake_sabnzbd_server

from lftpweb.core.clientsync import (
    BACKOFF_FACTOR,
    INITIAL_BACKOFF_S,
    MAX_BACKOFF_S,
    ClientSyncScheduler,
)
from lftpweb.core.crypto import encrypt_secret
from lftpweb.db import migrate

# --- Fixtures / helpers -------------------------------------------------------------------------


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def fake_sabnzbd_server():
    async with run_fake_sabnzbd_server() as server:
        yield server


async def _seed_host(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method) VALUES ('h', 'a', 'u', 'agent')"
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_queue(db: aiosqlite.Connection, host_id: int, *, name: str = "q", enabled=True):
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled) "
        "VALUES (?, ?, '/r', '/l', ?)",
        (host_id, name, 1 if enabled else 0),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_client(
    db: aiosqlite.Connection,
    config_dir: str,
    *,
    client_type: str = "sabnzbd",
    name: str = "SABnzbd",
    base_url: str,
    api_key: str,
    enabled: bool = True,
) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config_json = json.dumps({"base_url": base_url})
    secret_enc = encrypt_secret(config_dir, json.dumps({"api_key": api_key}))
    cursor = await db.execute(
        "INSERT INTO download_client (name, client_type, config_json, secret_enc, enabled, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, client_type, config_json, secret_enc, 1 if enabled else 0, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_category(db: aiosqlite.Connection, client_id: int, category: str, queue_id: int):
    await db.execute(
        "INSERT INTO download_client_category (client_id, category, queue_id) VALUES (?, ?, ?)",
        (client_id, category, queue_id),
    )
    await db.commit()


async def _event_rows(db: aiosqlite.Connection, kind: str | None = None):
    if kind is None:
        cursor = await db.execute("SELECT kind, message FROM event ORDER BY id")
    else:
        cursor = await db.execute(
            "SELECT kind, message FROM event WHERE kind = ? ORDER BY id", (kind,)
        )
    return await cursor.fetchall()


def _queue_slot(nzo_id: str, *, cat: str = "ar-tv", status: str = "Downloading") -> dict:
    return {
        "nzo_id": nzo_id,
        "status": status,
        "filename": f"Show.{nzo_id}.1080p-GRP",
        "mb": "1024.0",
        "mbleft": "512.0",
        "timeleft": "0:30:00",
        "cat": cat,
    }


NOW0 = 1_000_000.0  # an arbitrary monotonic-clock reference, far from 0


# --- Backoff ladder + one-event-per-transition (spec §9) ----------------------------------------


async def test_backoff_ladder_grows_caps_and_resets(db, fake_sabnzbd_server, tmp_path):
    fake_sabnzbd_server.state.fail_all = True
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    now = NOW0
    await scheduler.run_once(now=now)
    assert scheduler._backoff[instance_id].delay_s == INITIAL_BACKOFF_S

    # Still inside the backoff window -- no new attempt, delay unchanged.
    now += INITIAL_BACKOFF_S / 2
    await scheduler.run_once(now=now)
    assert scheduler._backoff[instance_id].delay_s == INITIAL_BACKOFF_S

    # Backoff window elapsed -- a new attempt, still failing, delay grows.
    now = NOW0 + INITIAL_BACKOFF_S
    await scheduler.run_once(now=now)
    assert scheduler._backoff[instance_id].delay_s == INITIAL_BACKOFF_S * BACKOFF_FACTOR

    now += INITIAL_BACKOFF_S * BACKOFF_FACTOR
    await scheduler.run_once(now=now)
    assert scheduler._backoff[instance_id].delay_s == INITIAL_BACKOFF_S * BACKOFF_FACTOR**2

    # Keep failing until the cap is reached.
    for _ in range(10):
        now += scheduler._backoff[instance_id].delay_s
        await scheduler.run_once(now=now)
    assert scheduler._backoff[instance_id].delay_s == MAX_BACKOFF_S

    # Recovers -- backoff clears entirely.
    fake_sabnzbd_server.state.fail_all = False
    now += MAX_BACKOFF_S
    await scheduler.run_once(now=now)
    assert instance_id not in scheduler._backoff


async def test_one_event_per_failure_transition_not_per_failed_pass(
    db, fake_sabnzbd_server, tmp_path
):
    """Several consecutive failed passes of an ongoing outage must write exactly **one** audit
    event, not one per attempt -- spec §9's own "not per failed pass, or a dead client floods the
    event log." A fresh streak after recovery, and a change in *kind* mid-outage, each earn a new
    event -- both are genuinely new facts, not a continuation of the one already reported.
    """
    fake_sabnzbd_server.state.fail_all = True
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    now = NOW0
    await scheduler.run_once(now=now)
    now += scheduler._backoff[instance_id].delay_s
    await scheduler.run_once(now=now)
    now += scheduler._backoff[instance_id].delay_s
    await scheduler.run_once(now=now)

    events = await _event_rows(db, kind="client_error")
    assert len(events) == 1

    # Recovers, then fails again -- a genuinely new streak, a second event.
    fake_sabnzbd_server.state.fail_all = False
    now += scheduler._backoff[instance_id].delay_s
    await scheduler.run_once(now=now)
    assert instance_id not in scheduler._backoff

    fake_sabnzbd_server.state.fail_all = True
    now += 1.0
    await scheduler.run_once(now=now)
    events = await _event_rows(db, kind="client_error")
    assert len(events) == 2


# --- `ClientAuthenticationFailed` distinguished from unreachability (spec §9) --------------------


async def test_auth_failure_gets_its_own_kind_and_message(db, fake_sabnzbd_server, tmp_path):
    fake_sabnzbd_server.state.bad_api_key_mode = True
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)

    events = await _event_rows(db, kind="client_auth_failed")
    assert len(events) == 1
    assert "rejected" in events[0]["message"]
    assert "unreachable" not in events[0]["message"]
    assert scheduler._backoff[instance_id].delay_s == INITIAL_BACKOFF_S


async def test_unreachable_instance_gets_the_unreachable_kind(db, tmp_path):
    """A closed port -- `httpx.ConnectError`, `ClientUnreachable` -- gets its own distinct kind,
    never confused with an auth rejection (the fake SABnzbd server is never even started for
    this instance).
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url="http://127.0.0.1:1",  # nothing listens on port 1
        api_key="whatever",
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)

    events = await _event_rows(db, kind="client_unreachable")
    assert len(events) == 1
    assert instance_id in scheduler._backoff


async def test_failure_kind_change_mid_outage_reports_again(db, fake_sabnzbd_server, tmp_path):
    """An ongoing outage that changes shape -- auth rejection turns into a plain error -- is a
    materially different fact for an operator and earns a fresh event even without recovering
    first.
    """
    fake_sabnzbd_server.state.bad_api_key_mode = True
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert len(await _event_rows(db, kind="client_auth_failed")) == 1

    fake_sabnzbd_server.state.bad_api_key_mode = False
    fake_sabnzbd_server.state.unrecognized_403_mode = True
    now = NOW0 + INITIAL_BACKOFF_S
    await scheduler.run_once(now=now)
    assert len(await _event_rows(db, kind="client_error")) == 1
    assert len(await _event_rows(db, kind="client_auth_failed")) == 1  # unchanged


# --- Last-known survival + the blank-queue blip (spec §4.2, §9.2) -------------------------------


async def test_last_known_preflight_rows_survive_a_failed_pass(db, fake_sabnzbd_server, tmp_path):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].download_id == "nzo1"

    # The instance goes down entirely -- last-known rows must not vanish.
    fake_sabnzbd_server.state.fail_all = True
    await scheduler.run_once(now=NOW0 + INITIAL_BACKOFF_S)
    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].download_id == "nzo1"


async def test_blank_queue_blip_does_not_wipe_preflight_rows(db, fake_sabnzbd_server, tmp_path):
    """The v0.2.4-shaped production incident (spec §1, §4.2), reached directly this time rather
    than through the *arr's own relay -- `tests/fake_sabnzbd.py`'s own blank-queue mode
    (`queue_empty_for_requests`), built for exactly this. A blank response from a *successful*
    call must never read as "everything vanished."
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert len(scheduler.preflight_rows(frozenset({instance_id}))) == 1

    # One blank response -- a real, successful call that just happened to answer empty.
    fake_sabnzbd_server.state.queue_empty_for_requests = 1
    await scheduler.run_once(now=NOW0 + 10.0)
    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].download_id == "nzo1"
    # And this was never treated as a failure -- no backoff, no event.
    assert instance_id not in scheduler._backoff
    assert await _event_rows(db) == []


# --- Attribution / omission (spec §8.3) ----------------------------------------------------------


async def test_unmapped_category_is_silently_omitted(db, fake_sabnzbd_server, tmp_path):
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    # No category mapping configured at all.
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1", cat="ar-movies")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert scheduler.preflight_rows(frozenset({instance_id})) == []


async def test_category_mapped_to_disabled_queue_is_omitted(db, fake_sabnzbd_server, tmp_path):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, enabled=False)
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert scheduler.preflight_rows(frozenset({instance_id})) == []


async def test_transfer_with_no_category_is_omitted(db, fake_sabnzbd_server, tmp_path):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    slot = _queue_slot("nzo1")
    slot["cat"] = ""  # SABnzbd's own "no category" shape
    fake_sabnzbd_server.state.queue_slots = [slot]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert scheduler.preflight_rows(frozenset({instance_id})) == []


async def test_disabled_instance_never_polled(db, fake_sabnzbd_server, tmp_path):
    fake_sabnzbd_server.state.fail_all = True  # would fail loudly if ever contacted
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
        enabled=False,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert instance_id not in scheduler._backoff
    assert await _event_rows(db) == []


# --- Two cadences firing independently (spec §9.1) -----------------------------------------------


async def test_fast_and_slow_cadences_fire_independently(db, fake_sabnzbd_server, tmp_path):
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    # Pass 1: the very first pass always due for both cadences (`_last_slow_poll_at` starts
    # empty) -- one `queue` call (the fast/active-only cadence) plus one `history` call (the
    # slow/full-estate cadence, `list_transfers(active_only=False)`'s own second request).
    await scheduler.run_once(now=NOW0)
    # Pass 2, well inside `SLOW_INTERVAL_S`: fast only.
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)
    # Pass 3, still inside the slow window: fast only.
    await scheduler.run_once(now=NOW0 + 2 * scheduler.FAST_INTERVAL_S)
    # Pass 4, `SLOW_INTERVAL_S` later: both cadences due again.
    await scheduler.run_once(now=NOW0 + scheduler.SLOW_INTERVAL_S + 5.0)

    mode_calls = fake_sabnzbd_server.state.mode_calls
    assert mode_calls.count("queue") == 4
    assert mode_calls.count("history") == 2


# --- `is_alive` / `start`/`stop` (same shape as every other scheduler in this codebase) ----------


async def test_start_stop_is_alive(db, tmp_path):
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    assert not scheduler.is_alive
    await scheduler.start()
    assert scheduler.is_alive
    await scheduler.stop()
    assert not scheduler.is_alive
