"""End-to-end verification for "folder prefix during transfer" (2026-08-14,
`prompts/2026-08-14-in-flight-folder-prefix.md`, `core/download_prefix.py`) against the **real
fake seedbox** (DESIGN.md §14) -- real ssh, real sftp, real lftp, wired exactly the way
`main.py` wires `TransferQueue`/`PostprocessPipeline`. Skipped automatically if the seedbox
isn't reachable (`docker compose -f docker-compose.test.yml up -d` first). Shaped after
`tests/test_postprocess_e2e.py` and `tests/test_queue.py`.

This is the task's own "done when": a directory item, downloaded through the real `mirror`
lftp command with the prefix feature on, ends up on disk under its **real** name with no trace
of the prefixed directory left behind -- and, for a `move` queue, only after verification, with
the remote copy deleted afterwards, exactly like an unprefixed `move` transfer already does.

**The rename moved to `core/postprocess.py` as the pipeline's own last step** (2026-08-14,
`prompts/done/2026-08-14-rename-after-postprocessing-not-before.md`, reversing the ordering
decision this file's own tests originally verified -- see `docs/decisions.md` for both entries).
Every test below now wires a real `PostprocessPipeline` alongside the `TransferQueue`, exactly
as `main.py` does; without one, `core/queue.py._reap_one` no longer renames anything itself. The
two tests at the bottom of this file (`test_real_name_does_not_exist_while_verification_is_
running_and_does_exist_after`, `test_corrupt_item_never_appears_under_its_real_name`) are this
task's own required coverage -- the first is its single most important assertion.

Deliberately does **not** reuse `docker/test-seedbox/seed_tree.sh`'s baked-in fixture tree --
shared by every other test on this container, and the `move` test here deletes its source. Each
test uploads its own throwaway content into a dedicated, uniquely-named remote subdirectory over
SFTP at setup time.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from uuid import uuid4

import aiosqlite
import pytest

from lftpweb.core import download_prefix, verify
from lftpweb.core.events import EventBus
from lftpweb.core.postprocess import (
    PostprocessPipeline,
    PostprocessSettings,
    save_postprocess_settings,
)
from lftpweb.core.queue import TransferQueue, TransferSettings, save_transfer_settings
from lftpweb.core.remote import HostConfig, RemoteConnectionPool
from lftpweb.core.settle import SettleSettings, save_settle_settings
from lftpweb.db import migrate

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"

_FILE_A = b"folder-prefix e2e fixture, file a -- safe to delete, uploaded fresh by this test\n"
_FILE_B = b"folder-prefix e2e fixture, file b -- safe to delete, uploaded fresh by this test\n"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up -d`",
)


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


async def _wait_until(predicate, timeout_s: float = 30.0, interval_s: float = 0.2) -> bool:
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
    # These tests build `item` rows directly, never through a real `Engine` scan -- no
    # `item_settle` row is ever populated, and the settle gate defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3), which would hold every job at
    # REMOTE_ONLY/settling forever. Disabled to isolate what this file actually tests.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def _upload_release_dir(host: HostConfig, tmp_path, remote_subdir: str) -> int:
    """Uploads a small two-file "release" directory into a fresh, uniquely-named remote
    subdirectory and returns its total size (the same way a real scan would establish
    `item.remote_size` -- never hardcoded from the fixture bytes directly).
    """
    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    conn = await scan_pool.get_connection(host)
    async with conn.start_sftp_client() as sftp:
        remote_dir = f"/data/pickup/{remote_subdir}/Release.Name"
        await sftp.makedirs(remote_dir, exist_ok=True)
        async with sftp.open(f"{remote_dir}/a.mkv", "wb") as f:
            await f.write(_FILE_A)
        async with sftp.open(f"{remote_dir}/b.mkv", "wb") as f:
            await f.write(_FILE_B)

    entries, _ = await scan_pool.scan(host, f"/data/pickup/{remote_subdir}")
    assert (
        "Release.Name/a.mkv" in entries and "Release.Name/b.mkv" in entries
    ), "setup fixture failed to upload"
    return entries["Release.Name/a.mkv"].size + entries["Release.Name/b.mkv"].size


async def test_directory_item_downloads_into_prefixed_folder_and_is_renamed_on_completion(
    db, tmp_path
):
    remote_subdir = f"prefix-e2e-{uuid4().hex[:12]}"
    host = _host_config()
    remote_size = await _upload_release_dir(host, tmp_path, remote_subdir)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'e2e-prefix', ?, ?, 1, 'copy')",
        (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'Release.Name', 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, remote_size),
    )
    item_id = cursor.lastrowid
    await db.commit()

    # The feature under test: on, site-wide, with the shipped default prefix.
    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
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

    async def host_provider():
        return host

    events = EventBus()
    # The rename off the prefix moved to `core/postprocess.py` (2026-08-14,
    # prompts/done/2026-08-14-rename-after-postprocessing-not-before.md) -- without a wired
    # pipeline, `core/queue.py._reap_one` no longer renames anything itself, so this test would
    # never see the item reach its real name at all. Everything here is off by default
    # (`PostprocessSettings()`), so the pipeline reaches the rename immediately once triggered.
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
    )
    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=events,
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    q.postprocess = pipeline

    await q.start()
    try:
        await q.enqueue_item(item_id)

        # Poll for the *physical* prefixed directory to actually appear at least once -- proof
        # this is really the on-disk path lftp wrote to, not merely an assertion about the final
        # state. Best-effort: a fast enough transfer (this fixture is tiny) can complete between
        # two polls, so this is recorded but not asserted on its own -- the final-state
        # assertions below are the ones that must hold regardless.
        prefixed_dir = local_dir / ".downloading-Release.Name"

        async def prefixed_dir_exists():
            return prefixed_dir.exists()

        saw_prefixed_dir = await _wait_until(prefixed_dir_exists, timeout_s=5, interval_s=0.05)

        async def job_succeeded():
            row = await (
                await db.execute(
                    "SELECT state FROM job WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                    (item_id,),
                )
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(job_succeeded, timeout_s=30), "transfer job never succeeded"

        # The rename is now post-processing's own last step, not part of the DOWNLOADED
        # transition itself -- wait for the pipeline to actually finish (fire-and-forget via
        # `trigger()`) rather than just for `item.state == 'DOWNLOADED'`.
        await pipeline.wait_idle()

        async def prefix_cleared():
            row = await (
                await db.execute(
                    "SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,)
                )
            ).fetchone()
            return row is not None and row["pending_download_prefix"] is None

        assert await _wait_until(prefix_cleared, timeout_s=15), "never renamed off the prefix"

        final_dir = local_dir / "Release.Name"
        assert final_dir.is_dir(), "must end up under its real, unprefixed name"
        assert (final_dir / "a.mkv").read_bytes() == _FILE_A
        assert (final_dir / "b.mkv").read_bytes() == _FILE_B
        assert not prefixed_dir.exists(), "the prefixed directory must be gone once renamed"

        row = await (
            await db.execute(
                "SELECT rel_path, pending_download_prefix FROM item WHERE id = ?", (item_id,)
            )
        ).fetchone()
        assert row["rel_path"] == "Release.Name", "rel_path must never carry the prefix"
        assert row["pending_download_prefix"] is None

        if not saw_prefixed_dir:
            pytest.skip(
                "transfer completed too fast to observe the prefixed directory mid-flight -- "
                "final-state assertions above still passed and are what this test guards"
            )
    finally:
        await q.stop()
        await pipeline.wait_idle()
        # Best-effort cleanup of the throwaway remote subdirectory (this is a `copy` queue, so
        # nothing else ever removes it) -- matches tests/test_postprocess_e2e.py's own pattern.
        cleanup_pool = RemoteConnectionPool(tmp_path / "known_hosts_cleanup")
        try:
            await cleanup_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await cleanup_pool.close()


async def test_move_queue_with_prefix_enabled_still_verifies_before_deleting_remote(db, tmp_path):
    remote_subdir = f"prefix-e2e-move-{uuid4().hex[:12]}"
    host = _host_config()
    remote_size = await _upload_release_dir(host, tmp_path, remote_subdir)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "sync_mode, auto_verify) VALUES (?, 'e2e-prefix-move', ?, ?, 1, 'move', 1)",
        (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'Release.Name', 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, remote_size),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
    # No .sfv/.md5 sidecar in this fixture -- hash-on-disk fallback verification, same as
    # test_postprocess_e2e.py's own move-mode test.
    await save_postprocess_settings(db, PostprocessSettings(verify_hash_on_disk=True))
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

    async def host_provider():
        return host

    events = EventBus()
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
    )

    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=events,
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    q.postprocess = pipeline

    await q.start()
    try:
        await q.enqueue_item(item_id)

        async def remote_delete_recorded():
            row = await (
                await db.execute("SELECT remote_deleted_at FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            return row is not None and row["remote_deleted_at"] is not None

        assert await _wait_until(
            remote_delete_recorded, timeout_s=30
        ), "move-mode postprocessing never recorded a remote delete"

        final_dir = local_dir / "Release.Name"
        assert final_dir.is_dir(), "renamed to its real name before postprocessing ran"
        assert (final_dir / "a.mkv").read_bytes() == _FILE_A
        assert (final_dir / "b.mkv").read_bytes() == _FILE_B
        assert not (local_dir / ".downloading-Release.Name").exists()

        item_row = await (
            await db.execute(
                "SELECT state, verified_at, pending_download_prefix FROM item WHERE id = ?",
                (item_id,),
            )
        ).fetchone()
        assert item_row["state"] == "VERIFIED"
        assert item_row["verified_at"] is not None
        assert item_row["pending_download_prefix"] is None

        # A second, independent scan confirms the remote copy is genuinely gone -- not just
        # that item.remote_deleted_at got set (test_postprocess_e2e.py's own bar).
        confirm_pool = RemoteConnectionPool(tmp_path / "known_hosts_confirm")
        entries, _ = await confirm_pool.scan(host, f"/data/pickup/{remote_subdir}")
        assert "Release.Name/a.mkv" not in entries
        assert "Release.Name/b.mkv" not in entries
    finally:
        await q.stop()
        await pipeline.wait_idle()
        # Best-effort cleanup -- harmless if the move-mode delete above already removed it.
        cleanup_pool = RemoteConnectionPool(tmp_path / "known_hosts_cleanup")
        try:
            await cleanup_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await cleanup_pool.close()


async def test_stop_mid_transfer_then_resume_finds_the_partial_in_the_prefixed_directory(
    db, tmp_path
):
    """The prompt's own testing requirement: "Resume finds an existing partial inside the
    prefixed directory." Shaped after `tests/test_queue.py`'s own
    `test_stop_mid_transfer_then_resume_continues_from_partial` -- a bandwidth cap makes the
    "mid-transfer" window deterministic rather than racing a fast loopback transfer, then a
    stop, then a manual re-queue that must resume via `-c` into the *same*, still-prefixed,
    directory rather than starting over under a freshly (re)computed name.
    """
    remote_subdir = f"prefix-e2e-resume-{uuid4().hex[:12]}"
    host = _host_config()

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    conn = await scan_pool.get_connection(host)
    big_content = os.urandom(5 * 1024 * 1024)  # 5 MB -- big enough for a deterministic partial
    async with conn.start_sftp_client() as sftp:
        remote_dir = f"/data/pickup/{remote_subdir}/Big.Release"
        await sftp.makedirs(remote_dir, exist_ok=True)
        async with sftp.open(f"{remote_dir}/big.bin", "wb") as f:
            await f.write(big_content)
    entries, _ = await scan_pool.scan(host, f"/data/pickup/{remote_subdir}")
    assert "Big.Release/big.bin" in entries, "setup fixture failed to upload"
    remote_size = entries["Big.Release/big.bin"].size

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'e2e-prefix-resume', ?, ?, 1, 'copy')",
        (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'Big.Release', 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, remote_size),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
    # Low bandwidth cap, same as test_queue.py's own resume test -- makes the mid-transfer
    # window deterministic instead of racing a fast loopback transfer.
    await save_transfer_settings(
        db,
        TransferSettings(
            max_bandwidth_bps=300_000,
            max_concurrent_transfers=1,
            small_item_threshold_bytes=0,
            small_lane_reserve_bps=0,
            min_share_floor_bps=0,
            mirror_parallel_transfer_count=1,
            mirror_use_pget_n=1,
            pget_default_n=1,
        ),
    )

    async def host_provider():
        return host

    events = EventBus()
    # See the sibling test above -- without a wired pipeline, nothing renames the item off the
    # prefix any more (`core/postprocess.py` owns that now, 2026-08-14,
    # prompts/done/2026-08-14-rename-after-postprocessing-not-before.md).
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
    )
    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=events,
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    q.postprocess = pipeline
    await q.start()
    try:
        job_id = await q.enqueue_item(item_id)

        async def job_running():
            row = await (
                await db.execute("SELECT state, pid FROM job WHERE id = ?", (job_id,))
            ).fetchone()
            return row is not None and row["state"] == "running" and row["pid"] is not None

        assert await _wait_until(job_running, timeout_s=15)
        await asyncio.sleep(2.0)  # let real bytes accumulate under the cap

        await q.stop_job(job_id)

        item_row = await (
            await db.execute(
                "SELECT state, pending_download_prefix FROM item WHERE id = ?", (item_id,)
            )
        ).fetchone()
        assert item_row["state"] == "STOPPED"
        assert item_row["pending_download_prefix"] == ".downloading-"

        prefixed_dir = local_dir / ".downloading-Big.Release"
        assert prefixed_dir.is_dir(), "the partial must sit under the prefixed directory"
        assert not (local_dir / "Big.Release").exists(), "must not exist under its real name yet"
        from lftpweb.core.local_scan import scan_local

        partial_entries = scan_local(prefixed_dir)
        partial_size = sum(e.size for e in partial_entries.values() if not e.is_dir)
        assert 0 < partial_size < remote_size, "partial bytes must exist and be incomplete"

        # Resume: manual re-queue clears suppression and must target the *same* physical
        # directory (`_resolve_download_prefix_for_spawn` reusing `pending_download_prefix`),
        # continuing via `-c` rather than starting over under a freshly recomputed name.
        job_id2 = await q.retry_item(item_id)
        assert job_id2 != job_id

        async def job2_running():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id2,))
            ).fetchone()
            return row is not None and row["state"] == "running"

        assert await _wait_until(job2_running, timeout_s=15)
        await asyncio.sleep(0.5)
        mid_resume_entries = scan_local(prefixed_dir)
        mid_resume_size = sum(e.size for e in mid_resume_entries.values() if not e.is_dir)
        assert mid_resume_size >= partial_size, "resume must not restart from zero"

        async def job2_done():
            row = await (
                await db.execute("SELECT state FROM job WHERE id = ?", (job_id2,))
            ).fetchone()
            return row is not None and row["state"] == "succeeded"

        assert await _wait_until(job2_done, timeout_s=90)

        async def item_downloaded():
            row = await (
                await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            return row is not None and row["state"] == "DOWNLOADED"

        assert await _wait_until(item_downloaded, timeout_s=15)

        # The rename is post-processing's own last step now, not part of the DOWNLOADED
        # transition -- wait for the (fire-and-forget) pipeline to actually finish.
        await pipeline.wait_idle()

        async def prefix_cleared():
            row = await (
                await db.execute(
                    "SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,)
                )
            ).fetchone()
            return row is not None and row["pending_download_prefix"] is None

        assert await _wait_until(prefix_cleared, timeout_s=15), "never renamed off the prefix"

        final_dir = local_dir / "Big.Release"
        assert final_dir.is_dir()
        assert (final_dir / "big.bin").read_bytes() == big_content
        assert not prefixed_dir.exists()
        final_item = await (
            await db.execute("SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert final_item["pending_download_prefix"] is None
    finally:
        await q.stop()
        await pipeline.wait_idle()
        cleanup_pool = RemoteConnectionPool(tmp_path / "known_hosts_cleanup")
        try:
            await cleanup_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await cleanup_pool.close()


async def test_real_name_does_not_exist_while_verification_is_running_and_does_exist_after(
    db, tmp_path, monkeypatch
):
    """This task's own single most important assertion (per its prompt): while verification is
    genuinely still running, the item's real, unprefixed name must not exist on disk at all --
    the whole point of moving the rename to the end of post-processing rather than the start.
    `verify.verify_item` is monkeypatched to sleep briefly so the `VERIFYING` window is long
    enough to observe deterministically, rather than racing a fast loopback verify.
    """
    real_verify_item = verify.verify_item

    def _slow_verify_item(root, **kwargs):
        time.sleep(1.5)
        return real_verify_item(root, **kwargs)

    monkeypatch.setattr(verify, "verify_item", _slow_verify_item)

    remote_subdir = f"prefix-e2e-slow-verify-{uuid4().hex[:12]}"
    host = _host_config()
    remote_size = await _upload_release_dir(host, tmp_path, remote_subdir)

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "sync_mode, auto_verify) VALUES (?, 'e2e-slow-verify', ?, ?, 1, 'copy', 1)",
        (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'Release.Name', 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, remote_size),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
    # Hash-on-disk fallback on -> no sidecar needed for verify to actually do the (slowed-down)
    # work; queue's own `auto_verify=1` above is what turns verification on at all.
    await save_postprocess_settings(db, PostprocessSettings(verify_hash_on_disk=True))
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

    async def host_provider():
        return host

    events = EventBus()
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
    )
    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=events,
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    q.postprocess = pipeline

    await q.start()
    try:
        await q.enqueue_item(item_id)

        final_dir = local_dir / "Release.Name"

        async def verifying():
            row = await (
                await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            return row is not None and row["state"] == "VERIFYING"

        assert await _wait_until(verifying, timeout_s=30), "item never reached VERIFYING"

        # Caught genuinely mid-verify (the slowdown above buys ~1.5s of window at a 0.2s poll
        # interval): the real, unprefixed name must not exist on disk at all yet.
        assert (
            not final_dir.exists()
        ), "must not be published under its real name while verification is still running"
        prefixed_dir = local_dir / ".downloading-Release.Name"
        assert prefixed_dir.is_dir(), "the bytes are all here, just still under the prefixed name"

        async def prefix_cleared():
            row = await (
                await db.execute(
                    "SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,)
                )
            ).fetchone()
            return row is not None and row["pending_download_prefix"] is None

        assert await _wait_until(
            prefix_cleared, timeout_s=15
        ), "never renamed after verification finished"

        assert final_dir.is_dir(), "must exist under its real name once verification is done"
        assert (final_dir / "a.mkv").read_bytes() == _FILE_A
        assert (final_dir / "b.mkv").read_bytes() == _FILE_B
        assert not prefixed_dir.exists()

        item = await (
            await db.execute("SELECT state, verified_at FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert item["state"] == "VERIFIED"
        assert item["verified_at"] is not None
    finally:
        await q.stop()
        await pipeline.wait_idle()
        cleanup_pool = RemoteConnectionPool(tmp_path / "known_hosts_cleanup")
        try:
            await cleanup_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await cleanup_pool.close()


async def test_corrupt_item_never_appears_under_its_real_name(db, tmp_path):
    """The prompt's own required test: a release that turns out `CORRUPT` must never be renamed
    -- its bytes stay hidden under the prefixed directory, forever, until a human retries it.
    An importer that skips hidden folders must never find this release under its real name
    either -- that is the whole problem this task exists to close.
    """
    remote_subdir = f"prefix-e2e-corrupt-{uuid4().hex[:12]}"
    host = _host_config()

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    conn = await scan_pool.get_connection(host)
    async with conn.start_sftp_client() as sftp:
        remote_dir = f"/data/pickup/{remote_subdir}/Release.Name"
        await sftp.makedirs(remote_dir, exist_ok=True)
        async with sftp.open(f"{remote_dir}/a.mkv", "wb") as f:
            await f.write(_FILE_A)
        async with sftp.open(f"{remote_dir}/b.mkv", "wb") as f:
            await f.write(_FILE_B)
        # A checksum sidecar with a deliberately *wrong* CRC32 for both files -- `verify.py`
        # prefers sidecar evidence over the hash-on-disk fallback, so this reliably produces
        # CORRUPT regardless of whether that fallback is even enabled.
        async with sftp.open(f"{remote_dir}/checksums.sfv", "wb") as f:
            await f.write(b"a.mkv deadbeef\nb.mkv deadbeef\n")

    entries, _ = await scan_pool.scan(host, f"/data/pickup/{remote_subdir}")
    assert "Release.Name/a.mkv" in entries and "Release.Name/checksums.sfv" in entries
    remote_size = sum(e.size for rel, e in entries.items() if rel.startswith("Release.Name/"))

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, password_enc, "
        "known_hosts_policy) VALUES ('seedbox', ?, ?, ?, 'password', NULL, 'insecure')",
        (SEEDBOX_HOST, SEEDBOX_PORT, SEEDBOX_USER),
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, "
        "sync_mode, auto_verify) VALUES (?, 'e2e-corrupt', ?, ?, 1, 'copy', 1)",
        (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
    )
    queue_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, 'Release.Name', 1, ?, 0, 'REMOTE_ONLY')",
        (queue_id, remote_size),
    )
    item_id = cursor.lastrowid
    await db.commit()

    await download_prefix.save_download_prefix_settings(
        db, download_prefix.DownloadPrefixSettings(enabled=True, prefix=".downloading-")
    )
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

    async def host_provider():
        return host

    events = EventBus()
    pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
    pipeline = PostprocessPipeline(
        db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
    )
    q = TransferQueue(
        db=db,
        config_dir=str(tmp_path),
        events=events,
        run_dir=str(tmp_path / "run"),
        tick_s=0.2,
        host_provider=host_provider,
    )
    q.postprocess = pipeline

    await q.start()
    try:
        await q.enqueue_item(item_id)

        async def item_corrupt():
            row = await (
                await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            return row is not None and row["state"] == "CORRUPT"

        assert await _wait_until(item_corrupt, timeout_s=30), "item never reached CORRUPT"
        await pipeline.wait_idle()

        final_dir = local_dir / "Release.Name"
        prefixed_dir = local_dir / ".downloading-Release.Name"
        assert not final_dir.exists(), "a CORRUPT item must never appear under its real name"
        assert prefixed_dir.is_dir(), "bytes stay under the prefixed directory"
        assert (prefixed_dir / "a.mkv").read_bytes() == _FILE_A
        assert (prefixed_dir / "b.mkv").read_bytes() == _FILE_B

        item = await (
            await db.execute("SELECT pending_download_prefix FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert item["pending_download_prefix"] == ".downloading-"

        events = await (
            await db.execute(
                "SELECT kind FROM event WHERE item_id = ? "
                "AND kind = 'download_prefix_rename_withheld'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1
    finally:
        await q.stop()
        await pipeline.wait_idle()
        cleanup_pool = RemoteConnectionPool(tmp_path / "known_hosts_cleanup")
        try:
            await cleanup_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await cleanup_pool.close()
