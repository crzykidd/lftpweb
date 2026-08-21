""" "Pause after current" / "pause now" against the **real fake seedbox** (DESIGN.md §14) --
mirrors `tests/test_queue.py`'s own real-ssh/real-lftp shape (skipped automatically if the
seedbox isn't reachable; `docker compose -f docker-compose.test.yml up --build -d` first).

DB/API-level pause behavior (persistence, the admission gate never reaching spawn, reordering
staying live, start_now's 409) is `tests/test_queue_pause.py`'s job -- this file is only for
what genuinely needs a real running lftp child: "pause now"'s SIGTERM-and-requeue must not
classify a SIGTERM'd exit as a failure, must not suppress the item, and must actually resume
from the partial rather than restart (`prompts/2026-08-20-queue-pause.md`'s own "the trap").
"""

from __future__ import annotations

import asyncio
import socket
import time

import aiosqlite
import pytest

from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig
from lftpweb.core.events import EventBus
from lftpweb.core.settle import SettleSettings, save_settle_settings
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


async def _make_db(tmp_path):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db


async def _make_host_row(db, *, password: str = SEEDBOX_PASSWORD) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', ?, ?, ?, 'password', 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queue_row(db, host_id: int, local_path) -> int:
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'test', '/data/pickup', ?, 1, 'copy')",
        (host_id, str(local_path)),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item_row(
    db, queue_id: int, rel_path: str, *, is_dir: bool, remote_size: int
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, ?, ?, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, 1 if is_dir else 0, remote_size),
    )
    await db.commit()
    return cursor.lastrowid


def _host_config(password: str = SEEDBOX_PASSWORD) -> HostConfig:
    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=password,
        known_hosts_policy="insecure",
    )


async def _host_config_async(password):
    return _host_config(password)


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


@pytest.fixture
async def db(tmp_path):
    conn = await _make_db(tmp_path)
    # Same isolation reasoning as tests/test_queue.py's own `db` fixture: these tests are about
    # pause/admission/resume, not the settle gate.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def _queue_for(
    db,
    tmp_path,
    *,
    max_bandwidth_bps=10_000_000,
    max_concurrent_transfers=2,
    tick_s=0.2,
    password=SEEDBOX_PASSWORD,
):
    await save_transfer_settings(
        db,
        TransferSettings(
            max_bandwidth_bps=max_bandwidth_bps,
            max_concurrent_transfers=max_concurrent_transfers,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=2,
            mirror_use_pget_n=2,
            pget_default_n=2,
        ),
    )
    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=tick_s,
        host_provider=lambda: _host_config_async(password),
    )
    return q


# --- "pause after current": nothing new admitted, running finishes normally ------------------


async def test_pause_after_current_holds_the_backlog_but_lets_the_running_job_finish(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    # A real, larger-ish file from the seeded tree (`docker/test-seedbox/seed_tree.sh`) under a
    # deliberately low bandwidth cap for job1 -- same reasoning as `tests/test_queue.py`'s own
    # stop-mid-transfer test: a tiny file at full bandwidth can succeed inside a single tick,
    # leaving nothing to observe as "running" before it's already done. job2 is the tiny
    # loose file -- what matters for it is only that it never starts while paused.
    item1 = await _make_item_row(
        db,
        queue_id,
        "Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv",
        is_dir=False,
        remote_size=20_971_520,
    )
    item2 = await _make_item_row(db, queue_id, "loose-notes.txt", is_dir=False, remote_size=512)

    q = await _queue_for(db, tmp_path, max_bandwidth_bps=2_000_000, max_concurrent_transfers=1)
    await q.start()
    try:
        job1 = await q.enqueue_item(item1)

        async def job1_running():
            row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job1,))).fetchone()
            return row is not None and row["state"] == "running"

        assert await _wait_until(job1_running, timeout_s=15)

        job2 = await q.enqueue_item(item2)
        await q.pause(stop_running=False)
        assert q.paused is True

        async def job1_done():
            row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job1,))).fetchone()
            return row is not None and row["state"] == "succeeded"

        # Already-running work is untouched by "pause after current".
        assert await _wait_until(job1_done, timeout_s=30)

        # Give the scheduler several ticks' worth of time to (wrongly) admit job2 if the gate
        # didn't hold -- a slot just freed up (job1 finished) and bandwidth is available.
        await asyncio.sleep(1.0)
        row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job2,))).fetchone()
        assert row["state"] == "queued", "nothing new may be admitted while paused"

        await q.unpause()

        async def job2_done():
            row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job2,))).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(job2_done, timeout_s=20)
    finally:
        await q.stop()


# --- "pause now": SIGTERM the running child, return it to queued in place --------------------


async def test_pause_now_requeues_the_running_job_without_suppression_or_failure(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(
        db,
        queue_id,
        "Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv",
        is_dir=False,
        remote_size=20_971_520,
    )

    # Low bandwidth cap, same as tests/test_queue.py's own stop-mid-transfer test, so the
    # mid-transfer window is deterministic rather than racing a fast loopback transfer.
    q = await _queue_for(db, tmp_path, max_bandwidth_bps=300_000, max_concurrent_transfers=1)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def running():
            row = await (
                await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "running" and row["pid"] is not None

        assert await _wait_until(running, timeout_s=15)
        await asyncio.sleep(2.0)  # let real bytes accumulate under the cap

        before = await (
            await db.execute("SELECT queue_position, attempt FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        position_before = before["queue_position"]
        attempt_before = before["attempt"]

        target = local_dir / "Movie.Title.2024.2160p" / "Movie.Title.2024.2160p.mkv"
        from lftpweb.core.local_scan import effective_file_size

        partial_before = effective_file_size(target)
        assert 0 < partial_before < 20_971_520, "must genuinely be mid-transfer before pausing"

        await q.pause(stop_running=True)
        assert q.paused is True

        # The job returns to `queued`, **in place** -- same row, same position, same attempt --
        # never `STOPPED`/`FAILED`, never suppressed. This is the exact trap the task warns
        # about: reusing `stop_job`'s semantics here would set `auto_queue_suppressed` and the
        # item would never come back on unpause.
        job_row = await (
            await db.execute(
                "SELECT state, pid, exit_code, error_class, queue_position, attempt "
                "FROM job WHERE id = ?",
                (job_id,),
            )
        ).fetchone()
        assert job_row["state"] == "queued"
        assert job_row["pid"] is None
        assert job_row["error_class"] is None
        assert job_row["queue_position"] == position_before
        assert job_row["attempt"] == attempt_before

        item_row = await (
            await db.execute(
                "SELECT state, auto_queue_suppressed, suppressed_reason, error_class "
                "FROM item WHERE id = ?",
                (item_id,),
            )
        ).fetchone()
        assert item_row["state"] == "QUEUED"
        assert item_row["auto_queue_suppressed"] == 0
        assert item_row["suppressed_reason"] is None
        assert item_row["error_class"] is None

        # Partial bytes are untouched by the pause itself (the resume assertion below is the
        # stronger proof it isn't restarting from zero, but this pins down the immediate
        # post-pause state too).
        partial_after_pause = effective_file_size(target)
        assert partial_after_pause >= partial_before

        # While still paused, nothing re-admits it.
        await asyncio.sleep(1.0)
        still_queued = await (
            await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert still_queued["state"] == "queued"

        # Raise the bandwidth cap before unpausing -- admission decisions are computed fresh at
        # spawn from whatever `TransferSettings` currently says (this is exactly the "you can
        # fix things up before letting it go" moment pausing is for), which turns the resumed
        # transfer from a deliberately slow, deterministic window back into a fast loopback
        # transfer so this test doesn't have to wait out the same 300 KB/s cap for ~19 more MB.
        await save_transfer_settings(
            db,
            TransferSettings(
                max_bandwidth_bps=50_000_000,
                max_concurrent_transfers=1,
                small_item_threshold_bytes=0,
                small_lane_reserve_bps=0,
                min_share_floor_bps=0,
                mirror_parallel_transfer_count=2,
                mirror_use_pget_n=2,
                pget_default_n=2,
            ),
        )
        await q.unpause()

        async def done():
            row = await (
                await db.execute("SELECT id, state FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(done, timeout_s=30)

        # Same job row all the way through -- a fresh row (a retry-shaped implementation)
        # would have left this one `cancelled`/`failed` and inserted a new id instead.
        final = await (
            await db.execute("SELECT id, state FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert final["id"] == job_id
        assert final["state"] == "succeeded"

        assert target.exists()
        assert target.stat().st_size == 20_971_520

        item_final = await (
            await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert item_final["state"] == "DOWNLOADED"
    finally:
        await q.stop()
