"""End-to-end wiring for the withhold gate (stage 3 of #18, `docs/transfers-redesign-spec.md`
§4.3, `docs/download-client-framework-spec.md` §14, `prompts/2026-08-23-withhold-and-cadence.md`)
-- `core/autoqueue.py.on_scan`'s third gate, over a real (fake) SABnzbd server, the identical
wiring shape `tests/test_settle_client_skip.py` already establishes for the settle-gate skip.

`tests/test_settle.py` covers `core/settle.py.find_client_failure` as a pure function, with
hand-built `Transfer` candidates. This file is the seam: does `AutoQueue.on_scan` actually reach
into a wired `ClientSyncScheduler`'s cache, under `autoqueue.WithholdSettings.enabled`, and does
every uncertain path fall back to today's exact behavior (spec §4.2's own non-negotiable)?

The settle gate is deliberately left **disabled** in every test here (`SettleSettings(enabled=
False)`) -- the withhold gate must engage independent of it (its whole point is catching the case
where an item's fingerprint already reads settled, or the settle gate is off entirely), and
disabling it keeps every test's "would this have been enqueued anyway" baseline unambiguous: with
the settle gate off and the withhold gate off, an eligible item is queued immediately.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_sabnzbd import run_fake_sabnzbd_server

from lftpweb.core.autoqueue import (
    AutoQueue,
    QueueAutoConfig,
    WithholdSettings,
    load_withhold_settings,
    save_withhold_settings,
)
from lftpweb.core.clientsync import ClientSyncScheduler
from lftpweb.core.crypto import encrypt_secret
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

REMOTE_PATH = "/complete/ar-tv"
RELEASE = "Show.S01"


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


class _Recorder:
    def __init__(self):
        self.enqueued: list[int] = []

    async def __call__(self, item_id: int) -> int:
        self.enqueued.append(item_id)
        return item_id


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


async def _item_row(db: aiosqlite.Connection, item_id: int) -> aiosqlite.Row:
    cursor = await db.execute(
        "SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return row


async def _seed_client(
    db: aiosqlite.Connection, config_dir: str, *, base_url: str, api_key: str
) -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    config_json = json.dumps({"base_url": base_url})
    secret_enc = encrypt_secret(config_dir, json.dumps({"api_key": api_key}))
    cursor = await db.execute(
        "INSERT INTO download_client (name, client_type, config_json, secret_enc, enabled, "
        "created_at, updated_at) VALUES (?, 'sabnzbd', ?, ?, 1, ?, ?)",
        ("SABnzbd", config_json, secret_enc, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def _events(db: aiosqlite.Connection, kind: str):
    cursor = await db.execute(
        "SELECT item_id, message FROM event WHERE kind = ? ORDER BY id", (kind,)
    )
    return await cursor.fetchall()


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


def _failed_slot(*, nzo_id: str = "nzo-failed", storage: str, fail_message: str | None = None):
    return {
        "nzo_id": nzo_id,
        "name": RELEASE,
        "status": "Failed",
        "storage": storage,
        "bytes": 40,
        "fail_message": fail_message,
    }


def _completed_slot(*, nzo_id: str = "nzo-ok", storage: str):
    return {
        "nzo_id": nzo_id,
        "name": RELEASE,
        "status": "Completed",
        "storage": storage,
        "bytes": 100,
    }


async def _wire(
    db: aiosqlite.Connection,
    tmp_path,
    fake_sabnzbd_server,
    *,
    queue_id: int,
    withhold_enabled: bool = True,
    now: float = 1_000_000.0,
) -> tuple[AutoQueue, _Recorder, ClientSyncScheduler]:
    await save_settle_settings(db, SettleSettings(enabled=False))
    await save_withhold_settings(db, WithholdSettings(enabled=withhold_enabled))
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=now)  # first pass always polls the slow/full cadence

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    return aq, recorder, scheduler


# --- Settings default off, and round-trip ----------------------------------------------------


async def test_withhold_settings_default_off_and_round_trip(db):
    settings = await load_withhold_settings(db)
    assert settings.enabled is False

    await save_withhold_settings(db, WithholdSettings(enabled=True))
    settings = await load_withhold_settings(db)
    assert settings.enabled is True

    await save_withhold_settings(db, WithholdSettings(enabled=False))
    settings = await load_withhold_settings(db)
    assert settings.enabled is False


# --- The happy path: an explicit terminal FAILED verdict withholds ------------------------


async def test_explicit_failed_verdict_with_exact_path_match_withholds(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        _failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}", fail_message="unpack failed")
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []
    events = await _events(db, "autoqueue_withheld")
    assert len(events) == 1
    assert events[0]["item_id"] == item_id
    assert "SABnzbd" in events[0]["message"]
    assert RELEASE in events[0]["message"]
    assert "unpack failed" in events[0]["message"]
    assert item_id in aq.withheld.get(queue_id, {})


# --- Non-negotiable: setting off is byte-identical to today --------------------------------


async def test_withhold_disabled_is_byte_identical_to_no_withhold_gate_at_all(
    db, tmp_path, fake_sabnzbd_server
):
    """The exact same failed-verdict setup as the happy-path test above, with only
    `WithholdSettings.enabled=False` changed -- must enqueue exactly as it would with no client
    involvement at all (settle already disabled in `_wire`, so this is the plain "eligible item,
    nothing holding it back" baseline).
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [_failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}")]

    aq, recorder, _ = await _wire(
        db, tmp_path, fake_sabnzbd_server, queue_id=queue_id, withhold_enabled=False
    )
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []
    assert aq.withheld.get(queue_id, {}) == {}


# --- Only an explicit, terminal FAILED verdict withholds ------------------------------------


async def test_a_queue_side_status_never_withholds(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    # Still actively downloading -- never appears in history at all.
    fake_sabnzbd_server.state.queue_slots = [
        {
            "nzo_id": "nzo1",
            "status": "Downloading",
            "filename": RELEASE,
            "mb": "100.0",
            "mbleft": "50.0",
            "timeleft": "0:10:00",
            "cat": "ar-tv",
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


async def test_an_unknown_history_phase_never_withholds(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "SomeStatusThisCodebaseHasNeverSeen",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


async def test_blank_queue_and_history_response_never_withholds(db, tmp_path, fake_sabnzbd_server):
    """The v0.2.4-shaped production incident's own fixture -- a blank response from a
    *successful* call must never read as a failure, let alone an explicit one.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.queue_empty_for_requests = 10
    fake_sabnzbd_server.state.history_slots = []

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


async def test_unreachable_client_never_withholds(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.fail_all = True

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


async def test_an_item_no_client_has_ever_heard_of_never_withholds(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = []

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


async def test_no_client_sync_wired_never_withholds(db, tmp_path, fake_sabnzbd_server):
    """`AutoQueue.client_sync` stays `None` -- every deployment/test that predates this task."""
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [_failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}")]
    await save_settle_settings(db, SettleSettings(enabled=False))
    await save_withhold_settings(db, WithholdSettings(enabled=True))

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)  # aq.client_sync left at its own default: None

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


# --- A near-miss path never matches (component-boundary rule) -------------------------------


async def test_a_near_miss_path_never_withholds(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)  # /complete/ar-tv/Show.S01
    fake_sabnzbd_server.state.history_slots = [
        _failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}.EXTRA")  # a sibling, not this release
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert await _events(db, "autoqueue_withheld") == []


# --- Self-lift: a later COMPLETED verdict for the same release clears the withhold ----------


async def test_withhold_lifts_when_the_client_later_reports_the_same_release_completed(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [_failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}")]

    aq, recorder, scheduler = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []
    assert len(await _events(db, "autoqueue_withheld")) == 1
    assert item_id in aq.withheld.get(queue_id, {})

    # A retry lands, and the client's own history now also carries a genuine COMPLETED verdict
    # for the same release -- the old FAILED entry is left sitting there too, unchanged, exactly
    # as a real SABnzbd history would.
    fake_sabnzbd_server.state.history_slots.append(
        _completed_slot(storage=f"{REMOTE_PATH}/{RELEASE}")
    )
    await scheduler.run_once(now=1_000_000.0 + scheduler.SLOW_INTERVAL_S + 5.0)

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))
    assert queued == 1
    assert recorder.enqueued == [item_id]
    assert item_id not in aq.withheld.get(queue_id, {})
    lifted = await _events(db, "autoqueue_withhold_lifted")
    assert len(lifted) == 1
    assert lifted[0]["item_id"] == item_id


# --- Never touches item.state or auto_queue_suppressed --------------------------------------


async def test_withhold_never_writes_item_state_or_auto_queue_suppressed(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [_failed_slot(storage=f"{REMOTE_PATH}/{RELEASE}")]

    before = await _item_row(db, item_id)
    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))
    assert queued == 0

    after = await _item_row(db, item_id)
    assert after["state"] == before["state"] == "REMOTE_ONLY"
    assert after["auto_queue_suppressed"] == before["auto_queue_suppressed"] == 0
