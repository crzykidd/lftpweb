"""End-to-end verification for phase 4 against the **real fake seedbox** (DESIGN.md §14) --
real ssh, real sftp, real lftp, real `AutoQueue`/`TransferQueue`/pattern evaluator wiring.
Skipped automatically if the seedbox isn't reachable (`docker compose -f
docker-compose.test.yml up --build -d` first).

This is the phase's own "done when": a `file_exclude` of `*.nfo` leaves its release
`DOWNLOADED`, never permanently `PARTIAL` -- and here it's driven by `AutoQueue.on_scan`
picking the item up in the first place, exactly the path a real deployment uses, not a
directly-called `enqueue_item`.
"""

from __future__ import annotations

import asyncio
import socket

import aiosqlite
import pytest

from lftpweb.core.autoqueue import AutoQueue, QueueAutoConfig
from lftpweb.core.download_prefix import DownloadPrefixSettings, save_download_prefix_settings
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig
from lftpweb.core.events import EventBus
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"

# The seed tree's release with a real .nfo alongside a real .mkv and a nested Subs/eng.srt
# (docker/test-seedbox/seed_tree.sh) -- exactly the shape DESIGN.md §4.7's example describes.
RELEASE_NAME = "Some.Release.S01E01.720p.WEB"
RELEASE_TOTAL_BYTES = 5_242_880 + 1_024 + 2_048  # mkv + nfo + srt
NFO_BYTES = 1_024
MKV_BYTES = 5_242_880
SRT_BYTES = 2_048


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


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.3) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
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
    # This file drives `AutoQueue.on_scan` directly, without an `Engine` scan pass -- so no
    # `item_settle` row is ever populated, and the settle gate now defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3), which would leave the item
    # eligibility check permanently unsettled and never queue anything. Disabled to isolate
    # what this file actually tests (file_exclude patterns driving auto-queue); the gate
    # itself is covered by tests/test_settle.py and tests/test_settle_gate_e2e.py.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    # "Folder prefix during transfer" also defaults on (2026-08-14) and, since 2026-08-14
    # (prompts/done/2026-08-14-rename-after-postprocessing-not-before.md), the rename off a
    # prefixed directory happens only inside `core/postprocess.py`, triggered from
    # `core/queue.py._reap_one` -- which this file's `TransferQueue` never gets one wired into
    # (it drives `AutoQueue` directly, not the full `main.py` stack). Left on, a directory item
    # here would download into `.downloading-<name>/` and simply stay there forever, since
    # nothing would ever rename it back. Disabled for the same "isolate what this file actually
    # tests" reason as the settle gate above; the prefix rename itself is covered by
    # tests/test_download_prefix.py and tests/test_download_prefix_e2e.py.
    await save_download_prefix_settings(conn, DownloadPrefixSettings(enabled=False))
    yield conn
    await conn.close()


async def test_file_exclude_pattern_drives_autoqueue_to_a_clean_downloaded_release(db, tmp_path):
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode, "
        "auto_queue_enabled) VALUES (?, 'e2e', '/data/pickup', ?, 1, 'copy', 1)",
        (host_id, str(local_dir)),
    )
    queue_id = cursor.lastrowid
    await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr) VALUES (?, 'file_exclude', '*.nfo')",
        (queue_id,),
    )
    # The item as a real scan would first observe it: REMOTE_ONLY, top-level, a directory.
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, RELEASE_NAME, RELEASE_TOTAL_BYTES),
    )
    item_id = cursor.lastrowid
    # The three descendant rows a real `core/engine.py.scan_queue` pass would already have
    # written by the time auto-queue ever sees the parent -- the .nfo already `EXCLUDED` by
    # the pattern above (`core/reconcile.py` rule 1/8). `core/queue.py._reap_one`'s exit-0
    # completeness check (2026-08-14, prompts/2026-08-14-exit-zero-is-not-completion.md) reads
    # these rows to know the .nfo's bytes don't count toward this item's completeness --
    # without them here it would (correctly, for an *unscanned* item) fall back to the raw
    # rollup and never see the release as complete, since lftp is deliberately never asked to
    # fetch the excluded .nfo at all.
    await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
        (queue_id, f"{RELEASE_NAME}/{RELEASE_NAME}.mkv", MKV_BYTES),
    )
    await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'EXCLUDED')",
        (queue_id, f"{RELEASE_NAME}/{RELEASE_NAME}.nfo", NFO_BYTES),
    )
    await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
        (queue_id, f"{RELEASE_NAME}/Subs/eng.srt", SRT_BYTES),
    )
    await db.commit()

    await save_transfer_settings(
        db,
        TransferSettings(
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

    async def _host_config():
        return HostConfig(
            id=host_id,
            address=SEEDBOX_HOST,
            port=SEEDBOX_PORT,
            username=SEEDBOX_USER,
            auth_method="password",
            password=SEEDBOX_PASSWORD,
            known_hosts_policy="insecure",
        )

    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=EventBus(),
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=_host_config,
    )
    await q.start()
    try:
        # The mount sentinel gate (DESIGN.md §7.3, required this phase): must be present
        # before AutoQueue will act on anything for this queue.
        write_if_needed(str(local_dir))

        autoqueue = AutoQueue(db, enqueue_item=q.enqueue_item)
        queued = await autoqueue.on_scan(
            QueueAutoConfig(
                id=queue_id,
                name="e2e",
                local_path=str(local_dir),
                auto_queue_enabled=True,
                patterns_only=False,
            )
        )
        assert queued == 1, "auto-queue should have picked up the release via its patterns"

        async def item_downloaded():
            row = await (
                await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            return row is not None and row["state"] == "DOWNLOADED"

        assert await _wait_until(item_downloaded, timeout_s=30)

        release_dir = local_dir / RELEASE_NAME
        mkv = release_dir / f"{RELEASE_NAME}.mkv"
        nfo = release_dir / f"{RELEASE_NAME}.nfo"
        srt = release_dir / "Subs" / "eng.srt"

        assert mkv.exists() and mkv.stat().st_size == MKV_BYTES
        assert srt.exists() and srt.stat().st_size == SRT_BYTES
        # The whole point: --exclude-glob kept the .nfo from ever arriving, and the item
        # still reached DOWNLOADED rather than sitting PARTIAL forever.
        assert not nfo.exists()

        item_row = await (
            await db.execute(
                "SELECT state, remote_size, local_size FROM item WHERE id = ?", (item_id,)
            )
        ).fetchone()
        assert item_row["state"] == "DOWNLOADED"
    finally:
        await q.stop()
