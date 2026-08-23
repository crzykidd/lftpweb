"""End-to-end wiring for the settle-gate skip (stage 2b of #18,
`docs/download-client-framework-spec.md` §14, `prompts/2026-08-23-settle-gate-skip.md`) --
`core/autoqueue.py.on_scan` consulting a real `core/clientsync.py.ClientSyncScheduler` against a
real (fake) SABnzbd server, the same "real HTTP request/response cycle" philosophy
`tests/test_clientsync.py` already uses for the poller alone.

`tests/test_settle.py` covers `core/settle.py.find_client_completion`/
`_client_content_path_matches` as pure functions, with hand-built `Transfer` candidates.
`tests/test_autoqueue.py` covers the settle gate's own eligibility half without any client
involvement at all. This file is the seam between the two: does `AutoQueue.on_scan` actually
reach into a wired `ClientSyncScheduler`'s cache, under `settle.SettleSettings.
client_skip_enabled`, and does every uncertain path here fall back to today's exact behavior
(this task's own non-negotiable)?
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest
from fake_sabnzbd import run_fake_sabnzbd_server

from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
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


async def _set_settle_record(db: aiosqlite.Connection, queue_id: int, rel_path: str) -> None:
    """A settle record that is deliberately **not settled yet** (`matched_scans=1`, "now") --
    every test in this file exercises whether the *client verdict* can satisfy the gate in
    place of waiting out `matched_scans`/`SETTLE_MIN_AGE_S`, so the fingerprint half must start
    genuinely unsettled.
    """
    await db.execute(
        "INSERT INTO item_settle (queue_id, rel_path, file_count, total_bytes, max_mtime, matched_scans) "
        "VALUES (?, ?, 1, 100, 1.0, 1)",
        (queue_id, rel_path),
    )
    await db.commit()


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


async def _wire(
    db: aiosqlite.Connection,
    tmp_path,
    fake_sabnzbd_server,
    *,
    queue_id: int,
    enabled: bool = True,
    client_skip_enabled: bool = True,
) -> tuple[AutoQueue, _Recorder, ClientSyncScheduler]:
    await save_settle_settings(
        db, SettleSettings(enabled=enabled, client_skip_enabled=client_skip_enabled)
    )
    await _seed_client(
        db,
        str(tmp_path),
        base_url=fake_sabnzbd_server.base_url,
        api_key=fake_sabnzbd_server.state.api_key,
    )
    scheduler = ClientSyncScheduler(db=db, config_dir=str(tmp_path))
    await scheduler.run_once(now=1_000_000.0)  # first pass always polls the slow/full cadence

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)
    aq.client_sync = scheduler
    return aq, recorder, scheduler


# --- The happy path: a genuine terminal COMPLETED verdict skips the wait ------------------


async def test_completed_verdict_with_exact_path_match_satisfies_the_gate(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
            # Completed well over `CLIENT_COMPLETION_HOLD_S` ago (2026-08-23,
            # prompts/2026-08-23-client-completion-delay.md) -- this test's own point is the
            # match-and-skip wiring, not the delay, so the completion is already old enough to
            # satisfy the gate with no added wait. `test_a_young_completion_holds_...` below
            # covers the delay itself.
            "completed": 1_000_000 - 3600,
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)

    assert queued == 1
    assert recorder.enqueued == [item_id]
    events = await _events(db, "settle_client_skip")
    assert len(events) == 1
    assert events[0]["item_id"] == item_id
    assert "SABnzbd" in events[0]["message"]
    assert RELEASE in events[0]["message"]


# --- The completion delay itself (2026-08-23, prompts/2026-08-23-client-completion-delay.md,
# finding #9) -- a terminal COMPLETED verdict must hold `settle.CLIENT_COMPLETION_HOLD_S` before
# it satisfies the gate, measured from the client's own completion time.


async def test_a_young_completion_holds_back_and_later_satisfies_the_gate(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
            "completed": 1_000_000,  # completes exactly at this pass's own `now`
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)

    # First pass, 3s after the client's own completion time -- younger than the 10s hold.
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_003.0)
    assert queued == 0
    assert recorder.enqueued == []
    assert await _events(db, "settle_client_skip") == []

    # Second pass, once the hold has actually elapsed -- the same item now satisfies the gate.
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_010.0)
    assert queued == 1
    assert recorder.enqueued == [item_id]
    events = await _events(db, "settle_client_skip")
    assert len(events) == 1
    assert events[0]["item_id"] == item_id


async def test_no_completed_at_falls_back_to_first_observation(db, tmp_path, fake_sabnzbd_server):
    """A connector reporting no `completed` field at all (`_epoch_to_iso` on a missing value
    returns `None`) must fall back to measuring the hold from the first pass lftpweb itself
    observed the verdict -- not satisfy the gate immediately, and not stay held forever either.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
            # No "completed" key at all -- `completed_at` on the resulting `Transfer` is `None`.
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)

    # First pass -- this is the very first time lftpweb has seen the verdict, so
    # `first_observed_at` is set to this pass's own `now`; the hold has not elapsed yet.
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_000.0)
    assert queued == 0
    assert recorder.enqueued == []

    # Second pass, still under 10s after the first observation -- still held.
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_005.0)
    assert queued == 0
    assert recorder.enqueued == []

    # Third pass, 10s after the *first* observation (not the second) -- proves the clock started
    # on the first sighting and was carried forward, not reset by the intervening pass.
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path), now=1_000_010.0)
    assert queued == 1
    assert recorder.enqueued == [item_id]


# --- Non-negotiable: setting off is byte-identical to today --------------------------------


async def test_client_skip_disabled_is_byte_identical_to_the_settle_gate_alone(
    db, tmp_path, fake_sabnzbd_server
):
    """The exact same completed-verdict setup as the happy-path test above, with only
    `client_skip_enabled=False` changed -- proves the setting being off produces the identical
    "held back" outcome `tests/test_autoqueue.py.test_settle_gate_on_holds_back_an_unsettled_item`
    already asserts for a queue with no client involvement at all.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    item_id = await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
        }
    ]

    aq, recorder, _ = await _wire(
        db, tmp_path, fake_sabnzbd_server, queue_id=queue_id, client_skip_enabled=False
    )
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []
    assert await _events(db, "settle_client_skip") == []
    assert item_id  # the item exists and was simply left gated, not touched


# --- Only a terminal, history-derived COMPLETED counts --------------------------------------


async def test_a_queue_side_status_does_not_satisfy_the_gate(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    # Still actively downloading, per SABnzbd's own queue -- never appears in history at all.
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

    assert queued == 0
    assert recorder.enqueued == []
    assert await _events(db, "settle_client_skip") == []


async def test_an_unknown_history_phase_does_not_satisfy_the_gate(
    db, tmp_path, fake_sabnzbd_server
):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
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

    assert queued == 0
    assert recorder.enqueued == []


# --- Unreachable client / blank response -- the v0.2.4 production incident's shape ---------


async def test_blank_queue_and_history_response_does_not_satisfy_the_gate(
    db, tmp_path, fake_sabnzbd_server
):
    """`tests/fake_sabnzbd.py`'s `queue_empty_for_requests` -- the v0.2.4 production incident
    fixture -- combined with an empty `history_slots`, models a client that answers but has
    nothing to report. Must read as "no information," never as a wrong verdict either way.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.queue_empty_for_requests = 10
    fake_sabnzbd_server.state.history_slots = []

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []


async def test_unreachable_client_does_not_satisfy_the_gate(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.fail_all = True

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []


# --- A near-miss path never matches (component-boundary rule, wired end to end) ------------


async def test_a_near_miss_path_does_not_satisfy_the_gate(db, tmp_path, fake_sabnzbd_server):
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)  # /complete/ar-tv/Show.S01
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": f"{RELEASE}.EXTRA",
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}.EXTRA",  # a sibling, not this release
            "bytes": 100,
        }
    ]

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []


# --- No client involvement at all -----------------------------------------------------------


async def test_no_client_sync_wired_is_untouched(db, tmp_path, fake_sabnzbd_server):
    """`AutoQueue.client_sync` stays `None` -- every deployment/test that predates this task, and
    the state before `main.py`'s lifespan wires it in. Even with `client_skip_enabled=True` and
    a completed verdict sitting in the fake server, nothing can be consulted.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = [
        {
            "nzo_id": "nzo1",
            "name": RELEASE,
            "status": "Completed",
            "storage": f"{REMOTE_PATH}/{RELEASE}",
            "bytes": 100,
        }
    ]
    await save_settle_settings(db, SettleSettings(enabled=True, client_skip_enabled=True))

    recorder = _Recorder()
    aq = AutoQueue(db, recorder)  # aq.client_sync left at its own default: None

    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))
    assert queued == 0
    assert recorder.enqueued == []


async def test_an_item_no_client_has_ever_heard_of_is_untouched(db, tmp_path, fake_sabnzbd_server):
    """A wired, healthy client-sync source with no matching release at all (an empty history) --
    the ordinary "this item just isn't a client's own release" case, not a failure of any kind.
    """
    write_if_needed(str(tmp_path))
    queue_id = await _make_queue(db, tmp_path)
    await _make_item(db, queue_id, RELEASE)
    await _set_settle_record(db, queue_id, RELEASE)
    fake_sabnzbd_server.state.history_slots = []

    aq, recorder, _ = await _wire(db, tmp_path, fake_sabnzbd_server, queue_id=queue_id)
    queued = await aq.on_scan(_queue_config(queue_id, tmp_path))

    assert queued == 0
    assert recorder.enqueued == []
