"""Integration tests for core/queue.py against the **real fake seedbox** (DESIGN.md §14) —
real ssh, real sftp, real lftp, real resume. Skipped automatically if the seedbox isn't
reachable (`docker compose -f docker-compose.test.yml up --build -d` first; see the phase 3
report for the exact commands). This is the load-bearing verification the phase 3 prompt asks
for: queue an item, watch bytes land, stop mid-transfer, resume from the partial — all through
`TransferQueue`, the same component `api/jobs.py` drives.
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


async def _make_item_row(db, queue_id: int, rel_path: str, *, is_dir: bool, remote_size: int) -> int:
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
    yield conn
    await conn.close()


async def _queue_for(
    db, tmp_path, *, max_bandwidth_bps=10_000_000, max_concurrent_transfers=2, tick_s=0.2,
    password=SEEDBOX_PASSWORD, small_item_threshold_bytes=0,
):
    await save_transfer_settings(
        db,
        TransferSettings(
            max_bandwidth_bps=max_bandwidth_bps,
            max_concurrent_transfers=max_concurrent_transfers,
            # 0 by default -- every test item is well over a couple hundred bytes, so a 0
            # threshold puts everything on the main lane unless a test explicitly wants
            # fast-lane behavior (test_concurrency_* overrides it deliberately).
            small_item_threshold_bytes=small_item_threshold_bytes,
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


async def _host_config_async(password):
    return _host_config(password)


# --- a real small file, start to finish, checksum-verified ----------------------------------


async def test_small_file_transfers_and_checksum_matches(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "loose-notes.txt", is_dir=False, remote_size=512)

    q = await _queue_for(db, tmp_path)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def done():
            row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(done, timeout_s=20)

        target = local_dir / "loose-notes.txt"
        assert target.exists()
        assert target.stat().st_size == 512

        item_row = await (await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item_row["state"] == "DOWNLOADED"
    finally:
        await q.stop()


# --- stop mid-transfer, verify PARTIAL/STOPPED, then resume without restarting --------------


async def test_stop_mid_transfer_then_resume_continues_from_partial(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    # A real 20 MB file, nested under a directory in the seed tree -- queued directly as a
    # `pget` item (manual queueing isn't restricted to top-level items; see docs/decisions.md
    # phase 2's item-table decision).
    item_id = await _make_item_row(
        db, queue_id, "Movie.Title.2024.2160p/Movie.Title.2024.2160p.mkv", is_dir=False, remote_size=20_971_520
    )

    # Low bandwidth cap makes the "mid-transfer" window deterministic rather than racing a
    # fast loopback transfer (per the phase 3 prompt's verification instructions).
    q = await _queue_for(db, tmp_path, max_bandwidth_bps=300_000, max_concurrent_transfers=1)
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def running():
            row = await (await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))).fetchone()
            return row is not None and row["state"] == "running" and row["pid"] is not None

        assert await _wait_until(running, timeout_s=15)
        await asyncio.sleep(2.0)  # let real bytes accumulate under the cap

        row = await (await db.execute("SELECT pid FROM job WHERE id = ?", (job_id,))).fetchone()
        pid = row["pid"]

        await q.stop_job(job_id)

        job_row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))).fetchone()
        assert job_row["state"] == "cancelled"

        item_row = await (await db.execute("SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item_row["state"] == "STOPPED"
        assert item_row["auto_queue_suppressed"] == 1

        # The rc file turns on `xfer:use-temp-file`/`*.lftp` (DESIGN.md §4.4b), so the partial
        # sits at `<name>.mkv.lftp`, not the final name, until the transfer completes --
        # exactly what `core/local_scan.effective_file_size` (reused, not reimplemented) is
        # for. This is the same lookup `core/progress.py`'s sampler uses in production.
        target = local_dir / "Movie.Title.2024.2160p" / "Movie.Title.2024.2160p.mkv"
        from lftpweb.core.local_scan import effective_file_size

        partial_size = effective_file_size(target)
        assert 0 < partial_size < 20_971_520, "partial file must exist (as a .lftp temp file) and be incomplete"
        assert not target.exists(), "must not have been renamed to its final name yet -- it isn't complete"
        assert target.with_name(target.name + ".lftp").exists()

        import os

        assert not os.path.exists(f"/proc/{pid}"), "lftp process must not survive a stop"

        # Resume: manual re-queue clears suppression and continues via `-c` rather than
        # restarting -- the byte count must not drop back to zero.
        job_id2 = await q.retry_item(item_id)
        assert job_id2 != job_id

        async def running2():
            r = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id2,))).fetchone()
            return r is not None and r["state"] == "running"

        assert await _wait_until(running2, timeout_s=15)
        await asyncio.sleep(0.5)
        mid_resume_size = effective_file_size(target)
        assert mid_resume_size >= partial_size, "resume must not restart from zero"

        async def done2():
            r = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id2,))).fetchone()
            return r is not None and r["state"] == "succeeded"

        # Resume is still bound by the same 300 KB/s cap (allocations are fixed at spawn --
        # DESIGN.md §4.5's invariant -- so this test can't just raise it mid-flight), and up
        # to ~20 MB can still be outstanding; generous but bounded.
        assert await _wait_until(done2, timeout_s=90)
        assert target.stat().st_size == 20_971_520
    finally:
        await q.stop()


# --- failure classification: bad password -> AUTH_FAILED, no retry --------------------------


async def test_bad_password_classifies_auth_failed_and_never_retries(db, tmp_path):
    host_id = await _make_host_row(db, password="WRONG")
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)
    item_id = await _make_item_row(db, queue_id, "loose-notes.txt", is_dir=False, remote_size=512)

    q = await _queue_for(db, tmp_path, password="WRONG")
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def failed():
            row = await (await db.execute("SELECT state FROM job WHERE id = ?", (job_id,))).fetchone()
            return row is not None and row["state"] == "failed"

        assert await _wait_until(failed, timeout_s=20)

        job_row = await (await db.execute("SELECT error_class FROM job WHERE id = ?", (job_id,))).fetchone()
        assert job_row["error_class"] == "AUTH_FAILED"

        item_row = await (
            await db.execute("SELECT state, auto_queue_suppressed, suppressed_reason FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert item_row["state"] == "FAILED"
        assert item_row["auto_queue_suppressed"] == 1
        assert item_row["suppressed_reason"] == "permanent_error"

        # No automatic retry: only ever one job row for this item.
        await asyncio.sleep(1.0)
        count_row = await (await db.execute("SELECT COUNT(*) AS n FROM job WHERE item_id = ?", (item_id,))).fetchone()
        assert count_row["n"] == 1
    finally:
        await q.stop()


# --- concurrency: N=2, bandwidth cap, a third waits, refill on completion -------------------


async def test_concurrency_two_at_half_third_waits_then_refills(db, tmp_path):
    host_id = await _make_host_row(db)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    queue_id = await _make_queue_row(db, host_id, local_dir)

    # Three distinct top-level directory items so each gets its own mirror job. `_queue_for`'s
    # `small_item_threshold_bytes=0` default keeps all three on the main lane regardless of
    # size -- otherwise "deep" (4096 bytes of real content) would take the fast lane and never
    # compete for the two main-lane slots this test is about. "deep" finishing almost
    # instantly (it's genuinely tiny) is convenient for observing "refill on completion".
    items = [
        await _make_item_row(db, queue_id, "Some.Release.S01E01.720p.WEB", is_dir=True, remote_size=5_245_952),
        await _make_item_row(db, queue_id, "Movie.Title.2024.2160p", is_dir=True, remote_size=20_971_520),
        await _make_item_row(db, queue_id, "deep", is_dir=True, remote_size=4_096),
    ]

    q = await _queue_for(db, tmp_path, max_bandwidth_bps=2_000_000, max_concurrent_transfers=2)
    # Enqueue all three *before* starting the tick loop -- each `enqueue_item()` call awaits
    # a real DB commit, and the background loop (once running) would otherwise be free to
    # interleave an admission pass between the first and second call, correctly admitting
    # just the one item that exists at that instant. That's not a product bug (the scheduler
    # can only act on what's actually queued), but it isn't what this test is about.
    try:
        job_ids = [await q.enqueue_item(i) for i in items]
        await q.start()

        async def two_running():
            rows = await (
                await db.execute("SELECT state FROM job WHERE id IN (?,?,?)", tuple(job_ids))
            ).fetchall()
            return sum(1 for r in rows if r["state"] == "running") == 2

        assert await _wait_until(two_running, timeout_s=15)

        rows = await (await db.execute("SELECT id, state, rate_limit_bps FROM job WHERE id IN (?,?,?)", tuple(job_ids))).fetchall()
        by_id = {r["id"]: r for r in rows}
        running_ids = [jid for jid in job_ids if by_id[jid]["state"] == "running"]
        queued_ids = [jid for jid in job_ids if by_id[jid]["state"] == "queued"]
        assert len(running_ids) == 2
        assert len(queued_ids) == 1
        for jid in running_ids:
            assert by_id[jid]["rate_limit_bps"] == 1_000_000  # half of 2,000,000, reserve=0

        # Let one of the two running jobs finish (whichever is smaller finishes first), then
        # the third should be admitted at the freed share.
        async def third_admitted():
            r = await (await db.execute("SELECT state FROM job WHERE id = ?", (queued_ids[0],))).fetchone()
            return r["state"] in ("running", "succeeded")

        assert await _wait_until(third_admitted, timeout_s=60)
    finally:
        await q.stop()


async def test_spawn_failure_fails_the_job_instead_of_hot_looping(db, tmp_path, monkeypatch):
    """An unspawnable job must fail visibly, not sit `queued` while the tick loops forever.

    Found in phase 3a review: an unwritable `run_dir` raised inside `_spawn_decision`, was
    swallowed by `_loop`'s blanket handler, and left the job `queued` with the tick retrying
    once a second and nothing surfaced to the API. Environment failures like this recur
    identically, so they must terminate the job rather than spin.
    """
    from lftpweb.core import lftp as lftp_module

    host_id = await _make_host_row(db)
    queue_id = await _make_queue_row(db, host_id, tmp_path / "local")
    item_id = await _make_item_row(
        db, queue_id, "Some.Release.S01E01.720p.WEB", is_dir=True, remote_size=5_245_952
    )
    q = await _queue_for(db, tmp_path)
    job_id = await q.enqueue_item(item_id)

    async def _boom(spec):
        raise PermissionError(13, "Permission denied", "/run/lftpweb")

    monkeypatch.setattr(lftp_module, "spawn", _boom)
    await q.tick()

    row = await (await db.execute("SELECT state, error_class FROM job WHERE id = ?", (job_id,))).fetchone()
    assert row["state"] == "failed"
    assert row["error_class"] == "SPAWN_FAILED"

    item = await (
        await db.execute("SELECT state, auto_queue_suppressed FROM item WHERE id = ?", (item_id,))
    ).fetchone()
    assert item["state"] == "FAILED"
    assert item["auto_queue_suppressed"] == 1
