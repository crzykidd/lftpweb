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
import logging
from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_rtorrent import FakeRtorrentTorrent, run_fake_rtorrent_server
from fake_sabnzbd import run_fake_sabnzbd_server

from lftpweb.core.clientsync import (
    BACKOFF_FACTOR,
    INITIAL_BACKOFF_S,
    MAX_BACKOFF_S,
    ClientSyncScheduler,
    UnattributedClientInfo,
    _PREFLIGHT_PHASES,
    persist_observed_categories,
)
from lftpweb.core.clients.models import Transfer, TransferPhase
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.settle import SettleSettings, save_settle_settings
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


@pytest.fixture
async def fake_rtorrent_server():
    async with run_fake_rtorrent_server() as server:
        yield server


async def _seed_host(db: aiosqlite.Connection) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, username, auth_method) VALUES ('h', 'a', 'u', 'agent')"
    )
    await db.commit()
    return cursor.lastrowid


async def _seed_queue(
    db: aiosqlite.Connection,
    host_id: int,
    *,
    name: str = "q",
    remote_path: str = "/r",
    enabled=True,
):
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled) "
        "VALUES (?, ?, ?, '/l', ?)",
        (host_id, name, remote_path, 1 if enabled else 0),
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


async def _seed_category(
    db: aiosqlite.Connection,
    client_id: int,
    category: str,
    queue_id: int | None,
    *,
    excluded: bool = False,
):
    await db.execute(
        "INSERT INTO download_client_category (client_id, category, queue_id, excluded) "
        "VALUES (?, ?, ?, ?)",
        (client_id, category, queue_id, 1 if excluded else 0),
    )
    await db.commit()


async def _seed_item(
    db: aiosqlite.Connection,
    queue_id: int,
    rel_path: str,
    *,
    download_client_id: int | None = None,
    download_client_matched_at: str | None = None,
) -> int:
    """A minimal `item` row for the client-attribution tests below (migration 033,
    prompts/2026-08-30-downloader-icon-on-rows.md) -- `_write_client_attribution` matches a
    transfer's own `content_path` against exactly `queue_id`/`rel_path` here, the same two columns
    `_client_content_path_matches` combines with the queue's own `remote_path`.
    """
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, state, download_client_id, "
        "download_client_matched_at) VALUES (?, ?, 0, 'DOWNLOADED', ?, ?)",
        (queue_id, rel_path, download_client_id, download_client_matched_at),
    )
    await db.commit()
    return cursor.lastrowid


async def _item_attribution(db: aiosqlite.Connection, item_id: int) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT download_client_id, download_client_matched_at FROM item WHERE id = ?",
        (item_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


async def _preflight_instance_row(db: aiosqlite.Connection, client_id: int) -> aiosqlite.Row:
    """The minimal `instance` row `_update_preflight` itself reads (`id`/`name`/`client_type`) --
    for the path-attribution tests below, which call `_update_preflight` directly against
    hand-built `Transfer`s rather than a fake server, since the attribution decision itself has
    nothing to do with any one connector's wire format. Named distinctly from the
    poll-status-focused `_client_row` helper further down this file (a different column set,
    same table) so the two never shadow each other.
    """
    cursor = await db.execute(
        "SELECT id, name, client_type FROM download_client WHERE id = ?", (client_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


def _content_transfer(
    client_id: str = "t1",
    *,
    phase: TransferPhase = TransferPhase.DOWNLOADING,
    category: str | None = None,
    content_path: str | None = None,
) -> Transfer:
    """Named distinctly from the phase-allowlist section's own `_transfer(client_id, phase,
    **kwargs)` helper further down this file (which defaults `category` to `"ar-tv"` -- wrong for
    these tests, which care about `category`/`content_path` precisely) -- two helpers building
    the same dataclass for two different sections' own needs, not one trying to serve both.
    """
    return Transfer(
        client_id=client_id,
        name=f"Show.{client_id}",
        phase=phase,
        raw_status="Downloading",
        raw={},
        category=category,
        content_path=content_path,
    )


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
    # `FAST_INTERVAL_S`, not an arbitrary `1.0` -- this instance has nothing in Preflight (an
    # empty queue), so Defect 2's own due-check (`ACTIVE_POLL_INTERVAL_S`'s own docstring,
    # `_process_instance`) falls back to `FAST_INTERVAL_S` and would otherwise skip this pass
    # entirely, never even attempting the poll that is supposed to produce the second event.
    now += scheduler.FAST_INTERVAL_S
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
    # And this was never treated as a failure -- no backoff, no *failure* event. The only event
    # on file is the one-time `client_poll_first_success` the first (successful) pass above wrote
    # (finding #2, 2026-08-23) -- the blank-queue blip on the second pass, itself also a success,
    # writes no event at all (not a per-poll event, this task's own explicit rule), proving the
    # blip specifically was never mistaken for a failure.
    assert instance_id not in scheduler._backoff
    events = await _event_rows(db)
    assert [e["kind"] for e in events] == ["client_poll_first_success"]


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


# --- Path-based attribution (spec §8.3 correction, round 4, 2026-08-23) --------------------
#
# `_update_preflight` is called directly against hand-built `Transfer`s here, bypassing the fake
# server entirely -- the attribution decision itself (path vs. category vs. neither) has nothing
# to do with any one connector's wire format, and `_preflight_instance_row` gives these tests the
# same `instance` row shape `_process_instance` would otherwise have to fetch through a real poll
# pass.


async def test_path_attribution_needs_no_category_mapping(db, tmp_path):
    """The headline behaviour (prompts/2026-08-23-path-attribution-and-category-escape-hatch.md):
    a transfer whose `content_path` sits under a queue's `remote_path` is attributed with **no**
    category mapping configured at all.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/home/crzykidd/downloads/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(
        content_path="/home/crzykidd/downloads/complete/ar-tv/Show.S01/Show.S01E01.mkv"
    )

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].queue_id == queue_id
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


async def test_path_attribution_is_component_boundary_not_prefix(db, tmp_path):
    """`/complete/ar-tv` must not match `/complete/ar-tv-extra` -- a bare `str.startswith` would
    let a sibling directory that merely shares a name prefix swallow the wrong queue.
    """
    host_id = await _seed_host(db)
    await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv-extra/Show.S01")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []
    info = await scheduler.unattributed_clients(frozenset({instance_id}))
    assert len(info) == 1
    assert info[0].no_category_count == 1  # no category on this transfer either


async def test_no_content_path_falls_back_to_category_mapping(db, tmp_path):
    """A transfer with no `content_path` yet (still queued at the client, nothing on disk) is
    exactly and only where the category mapping is still needed.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="ar-tv", content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].queue_id == queue_id


async def test_no_path_and_no_mapping_is_unattributable(db, tmp_path):
    """Unchanged: a transfer with neither a matching path nor a mapped category is silently
    omitted, exactly as before this task.
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category=None, content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []
    info = await scheduler.unattributed_clients(frozenset({instance_id}))
    assert len(info) == 1
    assert info[0].categories == ()
    assert info[0].no_category_count == 1


async def test_path_and_category_disagree_path_wins_and_is_logged(db, tmp_path, caplog):
    """Path and mapping disagreeing: the path wins (the bytes are actually there; a mapping can
    be stale), and the disagreement is visible rather than silently resolved.
    """
    host_id = await _seed_host(db)
    path_queue_id = await _seed_queue(db, host_id, name="by-path", remote_path="/complete/ar-tv")
    category_queue_id = await _seed_queue(
        db, host_id, name="by-category", remote_path="/complete/other"
    )
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-movies", category_queue_id)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="ar-movies", content_path="/complete/ar-tv/Show.S01")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].queue_id == path_queue_id  # path wins over the mapped category
    assert any("path wins" in record.message for record in caplog.records)


# --- Which client fetched this item (migration 033, 2026-08-30,
# prompts/2026-08-30-downloader-icon-on-rows.md) -- `_write_client_attribution`, called from
# `_update_preflight` itself so this write never depends on any user-facing toggle. ---------------


async def test_update_preflight_writes_client_attribution_for_a_path_matched_item(db, tmp_path):
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    item_id = await _seed_item(db, queue_id, "Show.S01")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    row = await _item_attribution(db, item_id)
    assert row["download_client_id"] == instance_id
    assert row["download_client_matched_at"] is not None


async def test_update_preflight_does_not_attribute_a_category_only_transfer(db, tmp_path):
    """A transfer with no `content_path` yet can only ever identify a *queue* (via the category
    mapping) -- never a specific *item* inside it, so this must write nothing (this task's own
    "match quality" rule: reuse `_client_content_path_matches`/the category map, never invent a
    third notion that would let a category-only transfer guess an item).
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    item_id = await _seed_item(db, queue_id, "Show.S01")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="ar-tv", content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    row = await _item_attribution(db, item_id)
    assert row["download_client_id"] is None


async def test_update_preflight_does_not_rewrite_matched_at_on_an_unchanged_repeat_pass(
    db, tmp_path
):
    """Write once and leave it (this task's own explicit rule): a quiet repeat match against the
    *same* instance must issue no `UPDATE` at all, so `download_client_matched_at` never drifts.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    item_id = await _seed_item(db, queue_id, "Show.S01")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)
    first = await _item_attribution(db, item_id)

    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)
    second = await _item_attribution(db, item_id)

    assert second["download_client_id"] == instance_id
    assert second["download_client_matched_at"] == first["download_client_matched_at"]


async def test_update_preflight_overwrites_attribution_from_a_different_instance(db, tmp_path):
    """A release genuinely re-fetched by a different client is a real fact worth recording, not
    noise -- the one case an already-attributed item is still a write candidate.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    old_instance_id = await _seed_client(
        db, str(tmp_path), base_url="http://old.invalid", api_key="k", name="Old"
    )
    item_id = await _seed_item(
        db,
        queue_id,
        "Show.S01",
        download_client_id=old_instance_id,
        download_client_matched_at="2026-08-01T00:00:00.000000Z",
    )
    new_instance_id = await _seed_client(
        db, str(tmp_path), base_url="http://new.invalid", api_key="k", name="New"
    )
    instance_row = await _preflight_instance_row(db, new_instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    row = await _item_attribution(db, item_id)
    assert row["download_client_id"] == new_instance_id
    assert row["download_client_matched_at"] != "2026-08-01T00:00:00.000000Z"


async def test_client_attribution_is_written_independent_of_client_skip_enabled_off(db, tmp_path):
    """The mistake this task's own handoff prompt names as most likely to be made here: writing
    attribution from the `client_skip_enabled` recheck path instead of from `_update_preflight`
    itself. Proven the direct way -- persist the toggle **off** and show the write still happens,
    since `_write_client_attribution` never reads this setting at all.
    """
    await save_settle_settings(db, SettleSettings(enabled=True, client_skip_enabled=False))
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    item_id = await _seed_item(db, queue_id, "Show.S01")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv/Show.S01/Show.S01E01.mkv")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    row = await _item_attribution(db, item_id)
    assert row["download_client_id"] == instance_id


# --- Unattributed-clients banner (finding #2, 2026-08-23) ----------------------------------


async def test_unattributed_clients_is_silent_for_a_category_seen_for_the_first_time(
    db, fake_sabnzbd_server, tmp_path
):
    """**Superseded by 2026-08-23's own follow-up task** (finding #2's original live scenario:
    an enabled, authenticating client reporting real items with no category -> queue mapping,
    which used to surface here immediately). `persist_observed_categories` now runs *before*
    this same pass's own `_update_preflight` (`_process_instance`'s own ordering), so a category
    genuinely never seen before is auto-recorded as excluded ("not used here," the safer
    default) before the banner's own request-time exclusion check ever runs -- so the very first
    pass a brand-new category appears on produces **no** banner entry, immediately, not merely
    "eventually." This is intentional: the old always-on nagging for a fresh category is exactly
    what `ClientsTab.tsx`'s own "N new since you last looked" signal replaces it with.
    `test_unattributed_clients_breaks_out_items_with_no_category_at_all` below covers the case
    that still *does* fire -- a category already on file as undecided.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    fake_sabnzbd_server.state.queue_slots = [
        _queue_slot("nzo1", cat="ar-tv"),
        _queue_slot("nzo2", cat="ar-movies"),
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert scheduler.preflight_rows(frozenset({instance_id})) == []
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []

    # And Settings now shows both categories, excluded by default -- defect 1's own fix: they
    # reached the database at all, not just the (now-silent) Preflight banner.
    cursor = await db.execute(
        "SELECT category, queue_id, excluded FROM download_client_category "
        "WHERE client_id = ? ORDER BY category",
        (instance_id,),
    )
    rows = await cursor.fetchall()
    assert [(r["category"], r["queue_id"], bool(r["excluded"])) for r in rows] == [
        ("ar-movies", None, True),
        ("ar-tv", None, True),
    ]


async def test_unattributed_clients_breaks_out_items_with_no_category_at_all(
    db, fake_sabnzbd_server, tmp_path
):
    """Live evidence, round 4: "no category at all" and "a category with no mapping" are
    different problems with different fixes -- conflating them into one count sends the user
    chasing a category mapping that was never the issue. One item carries a real, unmapped
    category; the other carries none at all -- the banner must be able to tell them apart.

    **"ar-movies" is pre-seeded as an already-undecided row** (2026-08-23 follow-up,
    `test_unattributed_clients_is_silent_for_a_category_seen_for_the_first_time` above) --
    `persist_observed_categories` only ever inserts a category never seen before, so a category
    already on file as undecided (a pre-migration row, or one a person manually reset) is left
    exactly as it was, and the banner still fires for it. A category with no name at all is a
    different, always-live case (`no_category_count`), never auto-excludable by category name.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-movies", queue_id=None, excluded=False)
    no_cat_slot = _queue_slot("nzo1")
    no_cat_slot["cat"] = ""  # SABnzbd's own "no category" shape
    fake_sabnzbd_server.state.queue_slots = [
        no_cat_slot,
        _queue_slot("nzo2", cat="ar-movies"),
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    info = await scheduler.unattributed_clients(frozenset({instance_id}))
    assert info == [
        UnattributedClientInfo(
            instance_id=instance_id,
            name="SABnzbd",
            count=2,
            categories=("ar-movies",),
            no_category_count=1,
        )
    ]


async def test_unattributed_clients_omits_a_client_with_nothing_unattributable(
    db, fake_sabnzbd_server, tmp_path
):
    """A quiet, fully-attributed (or genuinely empty) client must not appear in the banner at
    all -- "0 unattributable" is not a fact worth a line, and would bury the client that
    actually needs attention.
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
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1")]  # cat="ar-tv" -- mapped

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


async def test_unattributed_clients_ignores_an_instance_not_in_the_enabled_set(
    db, fake_sabnzbd_server, tmp_path
):
    """`ar-movies` is pre-seeded undecided -- see
    `test_unattributed_clients_is_silent_for_a_category_seen_for_the_first_time`'s own docstring
    for why a category never seen before would otherwise auto-exclude before this test's own
    enabled-set assertion ever got to matter.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    await _seed_category(db, instance_id, "ar-movies", queue_id=None, excluded=False)
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1", cat="ar-movies")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)
    assert await scheduler.unattributed_clients(frozenset()) == []
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == [
        UnattributedClientInfo(
            instance_id=instance_id,
            name="SABnzbd",
            count=1,
            categories=("ar-movies",),
            no_category_count=0,
        )
    ]


# --- Three-state categories / exclusion (finding #15/#16, 2026-08-23) --------------------------


async def test_excluded_category_transfer_is_omitted_and_not_counted(db, tmp_path):
    """Finding #15/#16: a category explicitly marked "not used by this instance" behaves like
    silent omission (no Preflight row) but crucially must never count toward the unattributed
    banner -- that is the entire point of the three-state redesign, and the deployment reason is
    the two-lftpweb-instances-one-seedbox shape finding #16 names: this category belongs to the
    other instance, permanently, and the user has already said so.
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "other-site-tv", queue_id=None, excluded=True)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="other-site-tv", content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


async def test_client_fully_bound_or_excluded_produces_no_banner(db, tmp_path):
    """The direct assertion finding #15 asked for: a client whose every category is bound or
    explicitly excluded is fully configured, and the banner must be silent -- not merely reduced.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id=queue_id)
    await _seed_category(db, instance_id, "other-site-movies", queue_id=None, excluded=True)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfers = [
        _content_transfer(client_id="t1", category="ar-tv", content_path=None),
        _content_transfer(client_id="t2", category="other-site-movies", content_path=None),
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, transfers, now=NOW0)

    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []
    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].download_id == "t1"


async def test_excluded_category_does_not_suppress_a_genuinely_unattributed_sibling(db, tmp_path):
    """Excluding one category must not blur a different, genuinely-undecided one -- the banner
    should still fire for the category nobody has looked at.
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "other-site-movies", queue_id=None, excluded=True)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfers = [
        _content_transfer(client_id="t1", category="other-site-movies", content_path=None),
        _content_transfer(client_id="t2", category="ar-books", content_path=None),
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, transfers, now=NOW0)

    info = await scheduler.unattributed_clients(frozenset({instance_id}))
    assert len(info) == 1
    assert info[0].categories == ("ar-books",)
    assert info[0].count == 1


async def test_excluding_a_category_clears_the_banner_without_another_poll_pass(db, tmp_path):
    """The regression test for the live defect (2026-08-23,
    prompts/2026-08-23-auto-add-categories-default-excluded.md): marking a category excluded in
    Settings used to keep showing the banner until the *next poll pass* happened to run, because
    the exclusion filter was baked into the cached count at poll time. `unattributed_clients` now
    re-derives its verdict against a *fresh* `_excluded_categories` read on every call -- so
    excluding a category between two calls, with no intervening `_update_preflight`, must be
    reflected on the very next read. This must fail against the pre-fix code (where the poll-time
    bake-in meant the banner kept showing the now-excluded category until another pass ran).
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "dc-tv", queue_id=None, excluded=False)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="dc-tv", content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    # Before excluding: the category is undecided, so the banner fires.
    info = await scheduler.unattributed_clients(frozenset({instance_id}))
    assert len(info) == 1
    assert info[0].categories == ("dc-tv",)

    # The user marks it excluded in Settings -- no poll pass happens in between.
    await db.execute(
        "UPDATE download_client_category SET excluded = 1, queue_id = NULL "
        "WHERE client_id = ? AND category = ?",
        (instance_id, "dc-tv"),
    )
    await db.commit()

    # The very next read must already reflect it -- not "eventually, next poll."
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


# --- Poller-observed categories are persisted (defect 1, 2026-08-23,
# prompts/2026-08-23-auto-add-categories-default-excluded.md) -------------------------------


async def test_persist_observed_categories_defaults_a_new_category_to_excluded(db, tmp_path):
    """Defect 2, same task: a newly recorded category defaults to excluded ("not used here"),
    not undecided -- the safer default for the two-lftpweb-instances-one-seedbox shape finding
    #16 named (the other instance's categories keep appearing here forever; arriving excluded
    means they're never walked, never proposed as debris, and never inside the delete
    containment boundary until deliberately opted in).
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await persist_observed_categories(db, instance_id, ["dc-tv"])

    cursor = await db.execute(
        "SELECT queue_id, excluded, source, first_seen_at FROM download_client_category "
        "WHERE client_id = ? AND category = ?",
        (instance_id, "dc-tv"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["queue_id"] is None
    assert bool(row["excluded"]) is True
    assert row["source"] == "client"
    assert row["first_seen_at"] is not None


async def test_a_full_poll_pass_persists_every_category_it_observes(
    db, fake_sabnzbd_server, tmp_path
):
    """End to end through `run_once` -- a category reported by a real poll pass (not a Test)
    reaches `download_client_category` on its own, with no manual persistence call needed.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    fake_sabnzbd_server.state.queue_slots = [_queue_slot("nzo1", cat="dc-tv")]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)

    cursor = await db.execute(
        "SELECT queue_id, excluded FROM download_client_category "
        "WHERE client_id = ? AND category = ?",
        (instance_id, "dc-tv"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["queue_id"] is None
    assert bool(row["excluded"]) is True


async def test_banner_is_silent_for_a_client_whose_categories_are_all_newly_recorded_defaults(
    db, tmp_path
):
    """The direct round trip: `persist_observed_categories` (defect 2's default) followed by a
    poll pass reporting only that category must produce no banner entry at all -- the default
    is quiet by construction, the same as an explicitly hand-excluded category (finding #15).
    """
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await persist_observed_categories(db, instance_id, ["dc-tv"])
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="dc-tv", content_path=None)

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


async def test_opting_a_newly_recorded_category_into_a_queue_makes_it_attributable(db, tmp_path):
    """A newly observed category defaults to excluded, but that is never permanent -- binding it
    to a queue (the opt-in) must make its content attributable and scannable again, exactly as if
    it had been bound from the start.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/dc-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await persist_observed_categories(db, instance_id, ["dc-tv"])

    # The opt-in: the user binds the category to a queue, which also clears `excluded`
    # (mutually exclusive, `DownloadClientCategoryIn`'s own validator).
    await db.execute(
        "UPDATE download_client_category SET queue_id = ?, excluded = 0 "
        "WHERE client_id = ? AND category = ?",
        (queue_id, instance_id, "dc-tv"),
    )
    await db.commit()

    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(category="dc-tv", content_path=None)
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].queue_id == queue_id
    assert await scheduler.unattributed_clients(frozenset({instance_id})) == []


async def test_persist_observed_categories_never_touches_an_already_decided_row(db, tmp_path):
    """This function must never overwrite a saved decision -- an already-bound or already-
    excluded category re-observed on a later pass is left completely alone.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id=queue_id)

    await persist_observed_categories(db, instance_id, ["ar-tv"])

    cursor = await db.execute(
        "SELECT queue_id, excluded FROM download_client_category "
        "WHERE client_id = ? AND category = ?",
        (instance_id, "ar-tv"),
    )
    row = await cursor.fetchone()
    assert row["queue_id"] == queue_id
    assert bool(row["excluded"]) is False


# --- Observed attribution stats (Part 3, 2026-08-23) --------------------------------------------


async def _attribution_row(db: aiosqlite.Connection, instance_id: int) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT attribution_sample_size, attribution_matched_by_path FROM download_client "
        "WHERE id = ?",
        (instance_id,),
    )
    return await cursor.fetchone()


async def test_attribution_stats_all_matched_by_path(db, tmp_path):
    """The SABnzbd-shaped case: every transfer's own `content_path` already answers the question,
    so the relevance copy should read "N of N matched by folder" -- computed from observation,
    never from `client_type`.
    """
    host_id = await _seed_host(db)
    await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfers = [
        _content_transfer(client_id=f"t{i}", content_path=f"/complete/ar-tv/Show.S0{i}")
        for i in range(3)
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, transfers, now=NOW0)

    row = await _attribution_row(db, instance_id)
    assert row["attribution_sample_size"] == 3
    assert row["attribution_matched_by_path"] == 3


async def test_attribution_stats_none_matched_needs_category(db, tmp_path):
    """The rTorrent-shaped case: `content_path` is the seeding directory, unrelated to the
    queue's own `remote_path` -- 0 of N matched by folder, so the mapping is genuinely required.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id=queue_id)
    instance_row = await _preflight_instance_row(db, instance_id)
    transfers = [
        _content_transfer(client_id="t1", category="ar-tv", content_path="/rtorrent/data/Show.S01"),
        _content_transfer(client_id="t2", category="ar-tv", content_path="/rtorrent/data/Show.S02"),
    ]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, transfers, now=NOW0)

    row = await _attribution_row(db, instance_id)
    assert row["attribution_sample_size"] == 2
    assert row["attribution_matched_by_path"] == 0


async def test_attribution_stats_never_regress_to_zero_on_a_quiet_pass(db, tmp_path):
    """A pass with nothing to attribute leaves the last real reading on the row -- overwriting a
    real "1 of 1" with a fabricated "0 of 0" during a quiet pass would make the relevance copy
    flicker for no reason.
    """
    host_id = await _seed_host(db)
    await _seed_queue(db, host_id, remote_path="/complete/ar-tv")
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://x.invalid", api_key="k")
    instance_row = await _preflight_instance_row(db, instance_id)
    transfer = _content_transfer(content_path="/complete/ar-tv/Show.S01")

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(instance_row, [transfer], now=NOW0)
    row = await _attribution_row(db, instance_id)
    assert row["attribution_sample_size"] == 1

    await scheduler._update_preflight(instance_row, [], now=NOW0 + 10.0)
    row = await _attribution_row(db, instance_id)
    assert row["attribution_sample_size"] == 1  # unchanged, not reset to 0


# --- Per-pass poll status (finding #2's reinforcing observation, 2026-08-23) ----------------


async def _client_row(db: aiosqlite.Connection, instance_id: int) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT last_poll_at, last_poll_ok, last_poll_message, last_success_at "
        "FROM download_client WHERE id = ?",
        (instance_id,),
    )
    return await cursor.fetchone()


async def test_never_polled_instance_has_null_poll_columns(db, tmp_path):
    """The row a disabled (or just-created) instance starts with -- distinguishable from both a
    working and a failing one, never a false "healthy" default (migration 029's own reasoning).
    """
    instance_id = await _seed_client(
        db, str(tmp_path), base_url="http://127.0.0.1:1", api_key="x", enabled=False
    )
    row = await _client_row(db, instance_id)
    assert row["last_poll_at"] is None
    assert row["last_poll_ok"] is None
    assert row["last_success_at"] is None


async def test_successful_poll_stamps_ok_and_last_success_at(db, fake_sabnzbd_server, tmp_path):
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)

    row = await _client_row(db, instance_id)
    assert row["last_poll_at"] is not None
    assert row["last_poll_ok"] == 1
    assert row["last_poll_message"] is None
    assert row["last_success_at"] is not None


async def test_failed_poll_stamps_the_failure_kinds_own_verb_not_last_success_at(
    db, fake_sabnzbd_server, tmp_path
):
    """`last_poll_message` reuses `_FAILURE_VERB`'s own wording -- "rejected the configured
    credential" reads as that on the Clients row, never as "unreachable" (this task's own stated
    requirement), and the audit log and the row can never disagree about the same failure.
    `last_success_at` stays `None`: this instance has never once succeeded.
    """
    fake_sabnzbd_server.state.bad_api_key_mode = True
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)

    row = await _client_row(db, instance_id)
    assert row["last_poll_ok"] == 0
    assert row["last_poll_message"] == "rejected the configured credential"
    assert row["last_success_at"] is None


async def test_first_success_event_fires_once_never_per_poll(db, fake_sabnzbd_server, tmp_path):
    """The positive signal itself (finding #2's reinforcing observation: "there doesn't seem to
    be any event entries ... a fully-working client is completely invisible"). One
    `client_poll_first_success` event on the pass that first succeeds; **no event at all** on
    every later successful pass -- this task's own explicit "do not emit a per-poll event" rule,
    asserted here across several passes, not just one.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    now = NOW0
    for _ in range(5):
        await scheduler.run_once(now=now)
        now += scheduler.FAST_INTERVAL_S

    events = await _event_rows(db, kind="client_poll_first_success")
    assert len(events) == 1
    assert f"id={instance_id}" in events[0]["message"]
    assert "first successful poll" in events[0]["message"]


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


# --- Preflight phase allowlist (spec §9.2, findings #12/#4, 2026-08-23) -------------------------
#
# `_update_preflight` is exercised directly here, with hand-built `Transfer` objects, rather
# than round-tripped through a fake server per phase -- the allowlist is a pure function of
# `transfer.phase`, and building nine fake-SABnzbd-status strings just to reach the same branch
# would test SABnzbd's own `map_phase` a second time, not this filter. The live, end-to-end
# rTorrent scenario the two findings were actually observed against is covered separately below
# (`test_rtorrent_active_only_true_admits_only_incoming_rows`), through the real fixture.


def _transfer(client_id: str, phase: TransferPhase, **kwargs) -> Transfer:
    kwargs.setdefault("category", "ar-tv")
    return Transfer(
        client_id=client_id,
        name=f"Release.{client_id}",
        phase=phase,
        raw_status=phase.value,
        raw={},
        **kwargs,
    )


async def test_preflight_phase_allowlist_covers_every_transfer_phase():
    """The guard against this bug class recurring (the task's own explicit ask): a denylist over
    an open-ended enum silently admits every phase nobody has thought about, which is exactly how
    `SEEDING` slipped into Preflight (#12) while `PAUSED` fell out of it (#4) at the same time.
    This asserts the allowlist plus its explicit, named exclusions cover the entire nine-value
    `TransferPhase` enum with no leftover -- a `TransferPhase` member added later and decided for
    neither list fails this test immediately, rather than silently landing on whichever side a
    denylist-shaped filter would have defaulted it to.
    """
    excluded_by_decision = {
        TransferPhase.SEEDING,  # the estate, not incoming work -- Disk review's business (#12)
        TransferPhase.COMPLETED,  # retirement-on-handover's job, not this filter's
        TransferPhase.FAILED,  # nothing coming -- the withhold gate is the surface for this
        TransferPhase.UNKNOWN,  # never blocks, and must not populate either (spec §4.2)
    }
    assert _PREFLIGHT_PHASES.isdisjoint(excluded_by_decision)
    assert _PREFLIGHT_PHASES | excluded_by_decision == set(TransferPhase)


async def test_seeding_transfer_produces_no_preflight_row(db, tmp_path):
    """Finding #12, asserted directly: a `SEEDING` transfer must never become a Preflight row."""
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://unused.test", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    row = await (
        await db.execute(
            "SELECT id, name, client_type FROM download_client WHERE id = ?", (instance_id,)
        )
    ).fetchone()

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(row, [_transfer("t1", TransferPhase.SEEDING)], NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []


async def test_paused_partial_transfer_appears_in_preflight(db, tmp_path):
    """Finding #4, asserted directly: a paused, partially-complete transfer must produce a row --
    it is known-but-not-arriving, exactly Preflight's own definition of useful, and previously
    the same filter excluded it for no stated reason at all.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://unused.test", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    row = await (
        await db.execute(
            "SELECT id, name, client_type FROM download_client WHERE id = ?", (instance_id,)
        )
    ).fetchone()

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    paused = _transfer("t1", TransferPhase.PAUSED, size_bytes=1000, bytes_done=600)
    await scheduler._update_preflight(row, [paused], NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert len(rows) == 1
    assert rows[0].download_id == "t1"


async def test_incoming_phases_all_produce_preflight_rows(db, tmp_path):
    """`QUEUED`/`DOWNLOADING`/`VERIFYING`/`EXTRACTING` -- the allowlist's other four members --
    each produce a row.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://unused.test", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    row = await (
        await db.execute(
            "SELECT id, name, client_type FROM download_client WHERE id = ?", (instance_id,)
        )
    ).fetchone()

    incoming = [
        TransferPhase.QUEUED,
        TransferPhase.DOWNLOADING,
        TransferPhase.VERIFYING,
        TransferPhase.EXTRACTING,
    ]
    transfers = [_transfer(f"t{i}", phase) for i, phase in enumerate(incoming)]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(row, transfers, NOW0)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert {r.download_id for r in rows} == {f"t{i}" for i in range(len(incoming))}


async def test_terminal_and_unknown_phases_produce_no_preflight_rows(db, tmp_path):
    """`COMPLETED`/`FAILED`/`UNKNOWN` -- the allowlist's three non-`SEEDING` exclusions -- each
    produce no row.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_client(db, str(tmp_path), base_url="http://unused.test", api_key="k")
    await _seed_category(db, instance_id, "ar-tv", queue_id)
    row = await (
        await db.execute(
            "SELECT id, name, client_type FROM download_client WHERE id = ?", (instance_id,)
        )
    ).fetchone()

    excluded = [TransferPhase.COMPLETED, TransferPhase.FAILED, TransferPhase.UNKNOWN]
    transfers = [_transfer(f"t{i}", phase) for i, phase in enumerate(excluded)]

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler._update_preflight(row, transfers, NOW0)

    assert scheduler.preflight_rows(frozenset({instance_id})) == []


async def test_rtorrent_active_only_true_admits_only_incoming_rows(
    db, fake_rtorrent_server, tmp_path
):
    """The live scenario, end to end (finding #12): rTorrent's own `active_only=True` filter
    excludes only `COMPLETED` (`core/clients/rtorrent.py.list_transfers`'s own doc-derived
    contract -- "a torrent never leaves the list") -- a seeding torrent passes that filter as
    readily as a downloading one. Without an allowlist at the Preflight projection itself, every
    seeding torrent in the estate becomes a Preflight row; with it, only the transfer still
    incoming does.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_rtorrent_client(
        db,
        str(tmp_path),
        base_url=fake_rtorrent_server.base_url,
        username=fake_rtorrent_server.state.username,
        password=fake_rtorrent_server.state.password,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)

    # A seeding torrent -- complete and active, rTorrent's own shape for "fully downloaded and
    # still uploading to peers" (`_classify_token`: complete + is_active -> "seeding").
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="A" * 40,
            name="Old.Seeding.Release",
            size_bytes=1000,
            completed_bytes=1000,
            complete=1,
            is_active=1,
            state=1,
            custom1="ar-tv",
        )
    )
    # A transfer still actually incoming.
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash="B" * 40,
            name="New.Incoming.Release",
            size_bytes=1000,
            completed_bytes=200,
            left_bytes=800,
            complete=0,
            is_active=1,
            state=1,
            custom1="ar-tv",
        )
    )

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    # Pass 1: always slow-due (active_only=False) -- populates the cache either way.
    await scheduler.run_once(now=NOW0)
    # Pass 2: fast-only, `active_only=True` -- the exact call the live bug was observed through.
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)

    rows = scheduler.preflight_rows(frozenset({instance_id}))
    assert [r.title for r in rows] == ["New.Incoming.Release"]


# --- Two cadences, corrected to split cheap-vs-expensive (spec §9.1, this stage's own fix) -----


async def test_fast_and_slow_cadences_fire_independently(db, fake_sabnzbd_server, tmp_path):
    """SABnzbd's own `Operation.LIST_HISTORY` is declared `NATIVE` (`USENET_BASELINE`) -- a
    real, cheap, independent call -- so **every** pass (fast or slow) now makes both a `queue`
    and a `history` request: the corrected split (module docstring) is cheap-vs-expensive, not
    active-vs-everything, and for a connector with a cheap history source there is no tick where
    skipping it would be correct. This replaces the pre-correction assertion (`history` only on
    the slow cadence, `count == 2`) -- that was the very split stage 2b found to strand a
    terminal verdict behind `SLOW_INTERVAL_S`; see `test_derived_history_connector_never_
    doubles_the_expensive_call` below for the mirror-image proof on a connector where the
    correction goes the *other* way.
    """
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    # Pass 1: the very first pass always due for the slow cadence (`_last_slow_poll_at` starts
    # empty) -- `list_transfers(active_only=False)`'s own `queue` + `history` pair.
    await scheduler.run_once(now=NOW0)
    # Pass 2, well inside `SLOW_INTERVAL_S`: the fast cadence's `active_only=True` `queue` call,
    # plus the cheap extra `list_history()` call this stage adds for a NATIVE-history connector.
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)
    # Pass 3, still inside the slow window: same as pass 2.
    await scheduler.run_once(now=NOW0 + 2 * scheduler.FAST_INTERVAL_S)
    # Pass 4, `SLOW_INTERVAL_S` later: the slow cadence's own pair again.
    await scheduler.run_once(now=NOW0 + scheduler.SLOW_INTERVAL_S + 5.0)

    mode_calls = fake_sabnzbd_server.state.mode_calls
    assert mode_calls.count("queue") == 4
    assert mode_calls.count("history") == 4


async def test_sab_fast_tick_surfaces_a_fresh_terminal_verdict_without_waiting_for_the_slow_poll(
    db, fake_sabnzbd_server, tmp_path
):
    """The bug stage 2b found and this stage's cadence fix corrects (spec §9.1's own correction
    note): a finished SABnzbd item leaves the queue and appears only in history, so a terminal
    verdict used to be stranded behind `SLOW_INTERVAL_S` (5 minutes) regardless of how often the
    fast tick ran. After the fix, a release completing between two fast ticks must be visible to
    `finished_transfers()` after the very next `FAST_INTERVAL_S` pass, not five minutes later.
    """
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)  # first pass, slow-due, nothing in history yet
    assert scheduler.finished_transfers() == []

    # A release finishes between passes -- now sitting in SABnzbd's own history.
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": "Show.S01",
            "status": "Completed",
            "storage": "/complete/ar-tv/Show.S01",
            "bytes": 100,
        }
    ]
    # Well inside SLOW_INTERVAL_S -- a fast-only pass.
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)

    completed = scheduler.finished_transfers()
    assert len(completed) == 1
    instance_id, instance_name, transfer = completed[0]
    assert instance_name == "SABnzbd"
    assert transfer.content_path == "/complete/ar-tv/Show.S01"


async def test_rtorrent_seeding_on_a_fast_tick_reaches_finished_transfers_without_the_slow_poll(
    db, fake_rtorrent_server, tmp_path
):
    """Defect 1 (2026-08-29, prompts/2026-08-29-preflight-poll-freshness.md). Commit cc5f75d
    widened `settle.FINISHED_TRANSFER_PHASES` to `{COMPLETED, SEEDING}` specifically because a
    finished, actively-seeding rTorrent torrent classifies as `SEEDING`, never `COMPLETED`
    (`core/clients/rtorrent.py._classify_token`) -- but this fast-tick merge, a third
    hand-restated copy of "what counts as terminal," was never updated to match, **and** its
    `elif cheap_history:` gate excluded rTorrent from the merge entirely: `Operation.
    LIST_HISTORY` is `DERIVED` for rTorrent (module docstring), so `cheap_history` is always
    `False` for it, and the merge branch never ran on a fast tick regardless of phase -- even
    though the fast tick's own `active_only=True` call already reports `SEEDING` torrents at
    zero extra cost (rTorrent's own contract: "a torrent never leaves the list," `active_only`
    excludes only `COMPLETED` -- see `test_rtorrent_active_only_true_admits_only_incoming_rows`
    above). Before the fix, a torrent finishing between two fast ticks stayed invisible to
    `finished_transfers()` for up to `SLOW_INTERVAL_S` (5 minutes) regardless of how often the
    fast tick ran -- exactly the gap the same commit's 5s verified settle skip depends on being
    closed for the ordinary rTorrent case.
    """
    host_id = await _seed_host(db)
    queue_id = await _seed_queue(db, host_id)
    instance_id = await _seed_rtorrent_client(
        db,
        str(tmp_path),
        base_url=fake_rtorrent_server.base_url,
        username=fake_rtorrent_server.state.username,
        password=fake_rtorrent_server.state.password,
    )
    await _seed_category(db, instance_id, "ar-tv", queue_id)

    torrent_hash = "C" * 40
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash=torrent_hash,
            name="Still.Downloading",
            size_bytes=1000,
            completed_bytes=200,
            left_bytes=800,
            complete=0,
            is_active=1,
            state=1,
            custom1="ar-tv",
        )
    )

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)  # first pass, always slow-due -- nothing finished yet
    assert scheduler.finished_transfers() == []

    # The torrent finishes and starts seeding, between fast ticks (same hash -- `state.add`
    # mutates in place, `FakeRtorrentState.add`'s own dict-by-hash shape).
    fake_rtorrent_server.state.add(
        FakeRtorrentTorrent(
            torrent_hash=torrent_hash,
            name="Still.Downloading",
            size_bytes=1000,
            completed_bytes=1000,
            complete=1,
            is_active=1,
            state=1,
            custom1="ar-tv",
        )
    )
    # Well inside SLOW_INTERVAL_S -- a fast-only pass: `active_only=True`, no `list_history()`
    # call at all for a DERIVED-history connector (`test_derived_history_connector_never_
    # doubles_the_expensive_call` below proves that half separately).
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)

    finished = scheduler.finished_transfers()
    assert len(finished) == 1
    _, name, transfer = finished[0]
    assert name == "rTorrent"
    assert transfer.phase == TransferPhase.SEEDING


async def _seed_rtorrent_client(
    db: aiosqlite.Connection, config_dir: str, *, base_url: str, username: str, password: str
) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config_json = json.dumps({"base_url": base_url, "username": username, "password": password})
    cursor = await db.execute(
        "INSERT INTO download_client (name, client_type, config_json, secret_enc, enabled, "
        "created_at, updated_at) VALUES (?, 'rtorrent', ?, NULL, 1, ?, ?)",
        ("rTorrent", config_json, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def test_derived_history_connector_never_doubles_the_expensive_call(
    db, fake_rtorrent_server, tmp_path
):
    """The mirror-image proof, over a connector where the correction goes the *other* way.
    rTorrent's own `TORRENT_BASELINE` declares `Operation.LIST_HISTORY` `DERIVED` -- "a torrent
    never leaves the list" -- because `RtorrentClient.list_history` re-fetches the identical
    expensive `d.multicall2` full listing `list_transfers` already pays for (there is no cheaper
    call to make). A fast tick must therefore still issue exactly **one** `d.multicall2` call,
    never two -- calling `list_history()` too would silently double the exact cost spec §9.1
    exists to avoid ("listing 500 seeding torrents every 10 seconds is waste"). Decided purely
    from `capabilities.supports(Operation.LIST_HISTORY)` -- no `client_type` branch anywhere in
    `ClientSyncScheduler` is what makes both this test and the SABnzbd one above pass through the
    identical scheduler code path.
    """
    await _seed_rtorrent_client(
        db,
        str(tmp_path),
        base_url=fake_rtorrent_server.base_url,
        username=fake_rtorrent_server.state.username,
        password=fake_rtorrent_server.state.password,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))

    def _multicall_count() -> int:
        return sum(
            1
            for call in fake_rtorrent_server.state.action_calls
            if call["method"] == "d.multicall2"
        )

    await scheduler.run_once(now=NOW0)  # first pass, always slow-due -- one call
    assert _multicall_count() == 1

    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)  # fast-only pass
    assert _multicall_count() == 2  # exactly one more, never two more


# --- Defect 2 (2026-08-29, prompts/2026-08-29-preflight-poll-freshness.md): a shorter cadence
# for an instance that currently has something in Preflight --------------------------------------


async def test_active_poll_interval_applies_once_something_is_in_preflight(
    db, fake_sabnzbd_server, tmp_path
):
    """ "With something in flight, the next tick is due after the new short interval, not
    `FAST_INTERVAL_S`" -- the handoff prompt's own first cadence assertion. A pass that sees a
    Preflight-eligible transfer marks this instance active (`_update_preflight` ->
    `ClientSyncScheduler._active_instances`); the very next pass, only `ACTIVE_POLL_INTERVAL_S`
    later (well short of `FAST_INTERVAL_S`), must still be due and actually contact the client.
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
    await scheduler.run_once(now=NOW0)  # first pass, slow-due -- populates `_active_instances`
    assert instance_id in scheduler._active_instances
    calls_before = len(fake_sabnzbd_server.state.mode_calls)

    await scheduler.run_once(now=NOW0 + scheduler.ACTIVE_POLL_INTERVAL_S)
    assert len(fake_sabnzbd_server.state.mode_calls) > calls_before


async def test_active_poll_interval_does_not_apply_with_nothing_in_flight(
    db, fake_sabnzbd_server, tmp_path
):
    """ "With nothing in flight, cadence is unchanged at `FAST_INTERVAL_S`" -- the handoff
    prompt's own second cadence assertion, the mirror image of the test above. An instance whose
    most recent pass saw no Preflight-eligible transfer is never sped up: a pass only
    `ACTIVE_POLL_INTERVAL_S` later is not yet due and makes no call at all, while a pass a full
    `FAST_INTERVAL_S` later is.
    """
    instance_id = await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    # No category/queue mapping and an empty queue -- nothing this instance could ever attribute,
    # so `_active_instances` stays empty throughout.

    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=NOW0)  # first pass, slow-due
    assert instance_id not in scheduler._active_instances
    calls_after_first = len(fake_sabnzbd_server.state.mode_calls)

    # Well short of `FAST_INTERVAL_S` -- not due, no call made.
    await scheduler.run_once(now=NOW0 + scheduler.ACTIVE_POLL_INTERVAL_S)
    assert len(fake_sabnzbd_server.state.mode_calls) == calls_after_first

    # A full `FAST_INTERVAL_S` later -- due, exactly the pre-existing cadence.
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)
    assert len(fake_sabnzbd_server.state.mode_calls) > calls_after_first


async def test_backoff_wins_over_the_active_poll_interval(db, fake_sabnzbd_server, tmp_path):
    """ "The backoff ladder wins" -- the handoff prompt's own explicit "test this" instruction, the
    one interaction most likely to go wrong: an instance backing off must not be dragged back to
    a fast poll merely because it has Preflight rows cached from before it broke. A pass that
    sees a transfer marks the instance active; the client then starts failing, and a follow-up
    pass well inside `ACTIVE_POLL_INTERVAL_S` reach (let alone `INITIAL_BACKOFF_S`) must still be
    skipped entirely -- no call, no second failure event -- because the backoff check (spec: "a
    backed-off instance is never contacted") runs and returns *before* the active-poll due-check
    is ever consulted.
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
    await scheduler.run_once(now=NOW0)  # slow-due, succeeds -- marks the instance active
    assert instance_id in scheduler._active_instances

    fake_sabnzbd_server.state.fail_all = True
    await scheduler.run_once(now=NOW0 + scheduler.FAST_INTERVAL_S)  # fails -- backoff engaged
    assert instance_id in scheduler._backoff
    calls_before = len(fake_sabnzbd_server.state.mode_calls)
    events_before = await _event_rows(db, kind="client_error")

    # Still active-flagged, and well inside `ACTIVE_POLL_INTERVAL_S` reach of the failure -- if
    # the active interval were consulted before backoff, this would be "due." It must not be.
    await scheduler.run_once(
        now=NOW0 + scheduler.FAST_INTERVAL_S + scheduler.ACTIVE_POLL_INTERVAL_S
    )
    assert len(fake_sabnzbd_server.state.mode_calls) == calls_before
    events_after = await _event_rows(db, kind="client_error")
    assert len(events_after) == len(events_before)


async def test_slow_cadence_is_untouched_by_the_active_poll_interval(
    db, fake_sabnzbd_server, tmp_path
):
    """`ACTIVE_POLL_INTERVAL_S` applies only to the fast, active-only call -- `SLOW_INTERVAL_S`
    stays exactly as it was, however often the fast/active cadence now runs. Driving several
    active-interval-spaced passes, all well inside `SLOW_INTERVAL_S`, must never re-trigger the
    full-estate call (`_last_slow_poll_at` must not advance) -- it advances only once a pass is
    actually `SLOW_INTERVAL_S` past the first one.
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
    await scheduler.run_once(now=NOW0)  # first pass, always slow-due
    first_slow_poll_at = scheduler._last_slow_poll_at[instance_id]

    now = NOW0
    for _ in range(5):
        now += scheduler.ACTIVE_POLL_INTERVAL_S
        await scheduler.run_once(now=now)
        # Still well inside `SLOW_INTERVAL_S` from the first pass -- the fast/active cadence
        # running frequently must never move the slow cadence's own bookkeeping.
        assert scheduler._last_slow_poll_at[instance_id] == first_slow_poll_at

    now = NOW0 + scheduler.SLOW_INTERVAL_S + 1.0
    await scheduler.run_once(now=now)
    assert scheduler._last_slow_poll_at[instance_id] == now


# --- `is_alive` / `start`/`stop` (same shape as every other scheduler in this codebase) ----------


async def test_start_stop_is_alive(db, tmp_path):
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    assert not scheduler.is_alive
    await scheduler.start()
    assert scheduler.is_alive
    await scheduler.stop()
    assert not scheduler.is_alive
