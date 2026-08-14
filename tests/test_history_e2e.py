"""End-to-end verification for the History page's backend (DESIGN.md §9.2, phase 6) against
the **real fake seedbox** (§14) -- the phase 6 prompt's own "done when": a real transfer
appears in `/api/history/jobs` with its byte count, and a forced failure (bad password) carries
its error class and a non-empty `output_tail` fetched via the on-demand endpoint. Skipped
automatically if the seedbox isn't reachable (`docker compose -f docker-compose.test.yml up
--build -d` first). Modeled directly on `tests/test_queue.py`'s own seedbox fixtures/helpers.
"""

from __future__ import annotations

import asyncio
import socket
import time

import aiosqlite
import pytest

from lftpweb.api import history
from lftpweb.core.events import EventBus
from lftpweb.core.queue import TransferQueue, save_transfer_settings
from lftpweb.core.queue import TransferSettings as _TS
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up --build -d`",
)


class _FakeState:
    def __init__(self, db):
        self.db = db


class _FakeApp:
    def __init__(self, db):
        self.state = _FakeState(db)


class _FakeRequest:
    def __init__(self, db):
        self.app = _FakeApp(db)


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_host(db, *, password: str) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', ?, ?, ?, 'password', 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queue(db, host_id: int, local_path, *, name: str = "history-e2e") -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/data/pickup', ?, 1, 'copy')",
        (host_id, name, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(db, queue_id: int, rel_path: str, *, remote_size: int) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, remote_size),
    )
    await db.commit()
    return cursor.lastrowid


def _host_config(password: str):
    from lftpweb.core.remote import HostConfig

    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=password,
        known_hosts_policy="insecure",
    )


async def _queue_for(db, tmp_path, *, password: str, tick_s: float = 0.2) -> TransferQueue:
    await save_transfer_settings(
        db,
        _TS(
            max_bandwidth_bps=10_000_000,
            max_concurrent_transfers=2,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=2,
            mirror_use_pget_n=2,
            pget_default_n=2,
        ),
    )

    async def host_provider():
        return _host_config(password)

    return TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=tick_s,
        host_provider=host_provider,
    )


async def test_real_transfer_appears_in_history_with_byte_count(db, tmp_path):
    host_id = await _make_host(db, password=SEEDBOX_PASSWORD)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir)
    item_id = await _make_item(db, queue_id, "loose-notes.txt", remote_size=512)

    q = await _queue_for(db, tmp_path, password=SEEDBOX_PASSWORD)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def succeeded():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(succeeded, timeout_s=20), "real transfer never succeeded"
    finally:
        await q.stop()

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    row = resp.jobs[0]
    assert row.id == job_id
    assert row.state == "succeeded"
    assert row.rel_path == "loose-notes.txt"
    assert row.bytes_total == 512
    assert row.bytes_done == 512
    # 2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md, defect 2): a succeeded
    # job's own `output_tail` is retained now, not nulled -- the one job whose success was in
    # doubt on a real instance had its own lftp output captured and then unconditionally
    # thrown away by this same code path, which is exactly the evidence that would have
    # explained the gap. See `core/queue.py._reap_one`'s own comment on this UPDATE.
    assert row.has_output_tail is True


async def test_forced_failure_carries_error_class_and_output_tail(db, tmp_path):
    host_id = await _make_host(db, password="WRONG-PASSWORD")
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue(db, host_id, local_dir, name="history-e2e-fail")
    item_id = await _make_item(db, queue_id, "loose-notes.txt", remote_size=512)

    q = await _queue_for(db, tmp_path, password="WRONG-PASSWORD")
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def failed():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "failed"

        assert await _wait_until(failed, timeout_s=20), "forced failure never landed"
    finally:
        await q.stop()

    resp = await history.list_history_jobs(_FakeRequest(db))
    assert len(resp.jobs) == 1
    row = resp.jobs[0]
    assert row.state == "failed"
    assert row.error_class == "AUTH_FAILED"
    assert row.has_output_tail is True

    output = await history.get_job_output(job_id, _FakeRequest(db))
    assert output.error_class == "AUTH_FAILED"
    assert output.output_tail is not None
    assert len(output.output_tail) > 0
