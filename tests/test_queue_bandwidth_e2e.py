"""Changing the site bandwidth limit with "also apply to in-progress", against the **real fake
seedbox** (DESIGN.md §14) -- `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md`.

The setting write, the validation floors, the already-paused cases and the transient admission
hold are `tests/test_queue_bandwidth.py`'s job (no process needed for any of them). This file
exists for the one claim that genuinely needs a real running lftp child: that stopping a
transfer and re-admitting it hands it a **new allocation** computed from the new throttle
(DESIGN.md §4.5 -- the only mechanism there is, since a running job's allocation is fixed at
spawn), and that it **resumes from the bytes already on disk** rather than starting over, with
no `FAILED` row, no `error_class`, and no `auto_queue_suppressed`.

Mirrors `tests/test_queue_pause_e2e.py`'s shape deliberately -- it is the same stop-and-requeue
machinery (`_pause_running_jobs`), reached from a second caller.
"""

from __future__ import annotations

import asyncio
import socket
import time

import aiosqlite
import pytest

from lftpweb.core.events import EventBus
from lftpweb.core.local_scan import effective_file_size
from lftpweb.core.queue import (
    TransferQueue,
    TransferSettings,
    load_transfer_settings,
    save_transfer_settings,
)
from lftpweb.core.remote import HostConfig
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"

MOVIE_REL_PATH = "Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv"
MOVIE_SIZE = 20_971_520

# Deliberately slow, so there is a real mid-transfer window to act on -- the same reasoning
# `tests/test_queue_pause_e2e.py` and `tests/test_queue.py` use for their own stop-mid-transfer
# tests. A tiny file at full loopback speed finishes inside one tick and leaves nothing to
# observe.
SLOW_BPS = 300_000
FAST_BPS = 50_000_000


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


async def _make_host_row(db) -> int:
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


async def _make_item_row(db, queue_id: int, rel_path: str, *, remote_size: int) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path, remote_size),
    )
    await db.commit()
    return cursor.lastrowid


def _host_config() -> HostConfig:
    return HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=SEEDBOX_PASSWORD,
        known_hosts_policy="insecure",
    )


async def _host_config_async():
    return _host_config()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    # Same isolation reasoning as tests/test_queue_pause_e2e.py's own fixture: this is about
    # admission and re-admission, not the settle gate.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def _queue_for(
    db, tmp_path, *, max_bandwidth_bps: int, throttle_bandwidth_bps: int | None = None
) -> TransferQueue:
    await save_transfer_settings(
        db,
        TransferSettings(
            max_bandwidth_bps=max_bandwidth_bps,
            throttle_bandwidth_bps=throttle_bandwidth_bps,
            max_concurrent_transfers=1,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=2,
            mirror_use_pget_n=2,
            pget_default_n=2,
        ),
    )
    return TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=_host_config_async,
    )


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


async def _pump_until(q: TransferQueue, predicate, timeout_s: float = 60.0) -> bool:
    """Drive `tick()` by hand instead of running the scheduler loop.

    **The loop is deliberately never started in this file.** `set_site_bandwidth` ends with
    `request_tick()`, so a running loop would re-admit within milliseconds and the
    stopped-and-requeued-in-place state -- the very thing that must be correct for the partial
    bytes to survive -- would be unobservable from the test. Driving admission by hand makes
    each transition a statement rather than a race.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await q.tick()
        if await predicate():
            return True
        await asyncio.sleep(0.2)
    return False


async def test_apply_to_in_progress_re_admits_at_the_new_limit_and_resumes(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, MOVIE_REL_PATH, remote_size=MOVIE_SIZE)

    # The ceiling is the *fast* one throughout -- Settings -> Transfer owns it and this feature
    # never writes it (2026-08-21, the two-value model). What starts slow is the **throttle**,
    # and dragging it back up to the ceiling is exactly the move the one-value design made
    # impossible (a ratchet -- see `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`).
    q = await _queue_for(db, tmp_path, max_bandwidth_bps=FAST_BPS, throttle_bandwidth_bps=SLOW_BPS)
    try:
        job_id = await q.enqueue_item(item_id)
        await q._admit()

        row = await (
            await db.execute("SELECT state, pid, rate_limit_bps FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert row["state"] == "running"
        assert row["pid"] is not None
        assert row["rate_limit_bps"] == SLOW_BPS, "admitted against the *old* throttle"

        target = local_dir / "Movie.Title.2024.2160p" / "Movie.Title.2024.2160p.mkv"

        async def has_partial_bytes():
            return 0 < effective_file_size(target) < MOVIE_SIZE

        assert await _wait_until(
            has_partial_bytes, timeout_s=20
        ), "must genuinely be mid-transfer before the bandwidth change"
        partial_before = effective_file_size(target)

        before = await (
            await db.execute("SELECT queue_position, attempt FROM job WHERE id = ?", (job_id,))
        ).fetchone()

        # The bandwidth change itself, with "also apply to in-progress".
        outcome = await q.set_site_bandwidth(FAST_BPS, apply_to_running=True)
        assert outcome.interrupted == 1
        assert outcome.skipped_because_paused is False

        # Stopped and returned to `queued` **in place** -- the same row, the same position, the
        # same attempt, no exit classification. This is the property the whole feature rests on:
        # anything with `stop_job`'s §4.6 semantics would set `auto_queue_suppressed` and the
        # item would never come back.
        stopped = await (
            await db.execute(
                "SELECT state, pid, error_class, queue_position, attempt FROM job WHERE id = ?",
                (job_id,),
            )
        ).fetchone()
        assert stopped["state"] == "queued"
        assert stopped["pid"] is None
        assert stopped["error_class"] is None
        assert stopped["queue_position"] == before["queue_position"]
        assert stopped["attempt"] == before["attempt"]

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

        # The partial bytes are still on disk -- nothing was cleaned up.
        assert effective_file_size(target) >= partial_before

        # Re-admission: a **new** allocation, computed from the new throttle. This is the
        # invariant being obeyed, not bypassed -- the job stopped being a running job, so the
        # scheduler hands it a fresh allocation the ordinary way (§4.5).
        await q._admit()
        readmitted = await (
            await db.execute(
                "SELECT id, state, rate_limit_bps, attempt FROM job WHERE id = ?",
                (job_id,),
            )
        ).fetchone()
        assert readmitted["id"] == job_id, "same job row -- not a retry-shaped fresh insert"
        assert readmitted["state"] == "running"
        assert readmitted["rate_limit_bps"] == FAST_BPS
        assert readmitted["attempt"] == before["attempt"], "a resume, not a retry"

        # ...and the ceiling itself never moved. The slider raised the throttle back to it;
        # under the one-value design it could only ever have raised the ceiling with it.
        settings_after = await load_transfer_settings(db)
        assert settings_after.max_bandwidth_bps == FAST_BPS
        assert settings_after.throttle_bandwidth_bps == FAST_BPS

        # **The resume proof.** lftp is spawned with `-c`, so a resumed transfer continues into
        # the existing partial file; a *restart* would truncate it back to zero first. Sampled
        # on every pump iteration from re-admission through completion, so the truncation
        # window can't be missed between two coarse checks.
        async def succeeded_without_losing_the_partial():
            assert (
                effective_file_size(target) >= partial_before
            ), "the file shrank after re-admission -- the transfer restarted instead of resuming"
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _pump_until(q, succeeded_without_losing_the_partial, timeout_s=60)

        assert target.exists()
        assert target.stat().st_size == MOVIE_SIZE
        final_item = await (
            await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert final_item["state"] == "DOWNLOADED"
    finally:
        await q.stop()


async def test_future_items_only_leaves_the_running_transfer_at_its_original_rate(db, tmp_path):
    """DESIGN.md §4.5's invariant, against a real child: the default option writes the number
    and interrupts nothing. The running job keeps the allocation it was admitted with, and the
    *next* item admits at the new one.
    """
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, MOVIE_REL_PATH, remote_size=MOVIE_SIZE)

    # The ceiling is the *fast* one throughout -- Settings -> Transfer owns it and this feature
    # never writes it (2026-08-21, the two-value model). What starts slow is the **throttle**,
    # and dragging it back up to the ceiling is exactly the move the one-value design made
    # impossible (a ratchet -- see `prompts/done/2026-08-21-bandwidth-ceiling-and-autocommit.md`).
    q = await _queue_for(db, tmp_path, max_bandwidth_bps=FAST_BPS, throttle_bandwidth_bps=SLOW_BPS)
    try:
        job_id = await q.enqueue_item(item_id)
        await q._admit()
        running = await (
            await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert running["state"] == "running"
        pid_before = running["pid"]

        outcome = await q.set_site_bandwidth(FAST_BPS, apply_to_running=False)
        assert outcome.interrupted == 0

        # Not stopped, not re-spawned, not re-shaped: same pid, same allocation.
        after = await (
            await db.execute("SELECT state, pid, rate_limit_bps FROM job WHERE id = ?", (job_id,))
        ).fetchone()
        assert after["state"] == "running"
        assert after["pid"] == pid_before
        assert after["rate_limit_bps"] == SLOW_BPS
        assert q._running[job_id].rate_limit_bps == SLOW_BPS
    finally:
        await q.stop()
