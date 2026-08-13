"""End-to-end verification for phase 5's `move` mode, and (this task) the `_UNPACK_` extraction
staging convention, against the **real fake seedbox** (DESIGN.md §14) -- real ssh, real sftp,
real lftp, real asyncssh delete, real 7zz/7z, wired exactly the way `main.py` wires it
(`TransferQueue.postprocess` set, triggered from `_reap_one`'s job-success path, never called
directly). Skipped automatically if the seedbox isn't reachable
(`docker compose -f docker-compose.test.yml up -d` first).

This is the phase's own "done when": a `move` queue transfers an item, verifies it, and the
remote copy is **gone from the seedbox afterwards** -- confirmed by scanning the remote again
with a fresh `RemoteConnectionPool`, not by trusting `item.remote_deleted_at` alone. The
extraction test added by this task has its own "done when": a downloaded archive is extracted
through the full pipeline exactly as production wires it, and the result on disk carries only
the merged final content -- no `_UNPACK_`/`_FAILED_` staging directory left behind.

Deliberately does **not** reuse `docker/test-seedbox/seed_tree.sh`'s baked-in fixture files --
those are shared by every other test in this suite (test_queue.py, test_autoqueue_e2e.py, ...)
and a `move` queue *deletes its source*. These tests upload their own throwaway content into a
dedicated, uniquely-named remote subdirectory over SFTP at setup time instead, so a successful
run can never affect any other test's fixture data on the same shared container.
"""

from __future__ import annotations

import asyncio
import functools
import io
import os
import shutil
import socket
import zipfile
from uuid import uuid4

import aiosqlite
import pytest

from lftpweb.core import extract
from lftpweb.core.engine import Engine, QueueConfig
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

_CONTENT = b"phase 5 move-mode e2e fixture -- safe to delete, uploaded fresh by this test\n"


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
    # These tests build their `item` rows directly (never through an `Engine` scan pass), so
    # no `item_settle` row is ever populated -- and the settle gate now defaults on
    # (prompts/2026-08-12-settle-gate-followups.md item 3), which would hold every job at
    # REMOTE_ONLY/settling forever and never trigger post-processing at all. Disabled to
    # isolate what this file actually tests (verify/extract/move); the gate itself is covered
    # by tests/test_settle.py and tests/test_settle_gate_e2e.py.
    await save_settle_settings(conn, SettleSettings(enabled=False))
    yield conn
    await conn.close()


async def test_move_mode_transfers_verifies_and_deletes_the_remote_copy(db, tmp_path):
    remote_subdir = f"phase5-move-e2e-{uuid4().hex[:12]}"
    rel_path = "target.txt"
    remote_full = f"/data/pickup/{remote_subdir}/{rel_path}"

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    host = _host_config()
    try:
        conn = await scan_pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(f"/data/pickup/{remote_subdir}", exist_ok=True)
            async with sftp.open(remote_full, "wb") as f:
                await f.write(_CONTENT)

        # Confirm the fixture actually landed, and get its real remote size the same way a
        # scan would (DESIGN.md §5) -- don't hardcode len(_CONTENT) as the "remote" truth.
        entries, _ = await scan_pool.scan(host, f"/data/pickup/{remote_subdir}")
        assert rel_path in entries, "setup fixture failed to upload"
        remote_size = entries[rel_path].size
        assert remote_size == len(_CONTENT)

        # --- Build the DB rows a real scan would have produced -----------------------------
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
            "sync_mode, auto_verify) VALUES (?, 'e2e-move', ?, ?, 1, 'move', 1)",
            (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
        )
        queue_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
            (queue_id, rel_path, remote_size),
        )
        item_id = cursor.lastrowid
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

        async def host_provider():
            return host

        events = EventBus()
        # A second, independent RemoteConnectionPool for the pipeline -- exactly the
        # production shape (Engine owns one pool; PostprocessPipeline is handed it), kept
        # distinct from `scan_pool` above (this test's own verification instrument) so the
        # pipeline's use of the connection is never conflated with the test's own checks.
        pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
        # This fixture carries no .sfv/.md5 sidecar, so the hash-on-disk fallback must be on
        # for verification to reach VERIFIED rather than SKIPPED (DESIGN.md §6). Everything
        # else (extract/move) stays off, and verify itself would stay off too for a `copy`
        # queue -- it's *only* on here because this queue is `move`, which forces it
        # regardless of this global switch (exercised for real, not just asserted at the
        # unit level -- see test_postprocess.py for that).
        await save_postprocess_settings(db, PostprocessSettings(verify_hash_on_disk=True))
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

            # Not "wait for state == DOWNLOADED": for a fixture this small, postprocessing
            # (triggered the instant the job succeeds) can reach VERIFIED before this test's
            # own poll loop ever observes the intermediate DOWNLOADED/VERIFYING states -- a
            # real race, not a bug in the pipeline. "the job succeeded" is proven by the job
            # row itself; that's what a transfer having happened actually means here.
            async def job_succeeded():
                row = await (
                    await db.execute(
                        "SELECT state FROM job WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                        (item_id,),
                    )
                ).fetchone()
                return row is not None and row["state"] == "succeeded"

            assert await _wait_until(job_succeeded, timeout_s=30), "transfer job never succeeded"

            local_file = local_dir / rel_path
            assert local_file.read_bytes() == _CONTENT

            # Postprocessing is triggered fire-and-forget from _reap_one -- wait for the
            # pipeline's own bookkeeping (remote_deleted_at) rather than a fixed sleep.
            async def remote_delete_recorded():
                row = await (
                    await db.execute("SELECT remote_deleted_at FROM item WHERE id = ?", (item_id,))
                ).fetchone()
                return row is not None and row["remote_deleted_at"] is not None

            assert await _wait_until(
                remote_delete_recorded, timeout_s=15
            ), "move-mode postprocessing never recorded a remote delete"

            item_row = await (
                await db.execute(
                    "SELECT state, verified_at, remote_deleted_at FROM item WHERE id = ?",
                    (item_id,),
                )
            ).fetchone()
            assert item_row["state"] == "VERIFIED"
            assert item_row["verified_at"] is not None

            events_rows = await (
                await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
            ).fetchall()
            kinds = [r["kind"] for r in events_rows]
            assert "verify" in kinds
            assert "remote_delete" in kinds
            assert "remote_delete_withheld" not in kinds
        finally:
            await q.stop()

        # --- The actual proof: rescan the remote and confirm the file is gone --------------
        # A fresh RemoteConnectionPool, not the one the pipeline used, and a fresh `find`
        # over SSH -- confirms by observation, not by trusting our own bookkeeping.
        verify_pool = RemoteConnectionPool(tmp_path / "known_hosts_verify")
        try:
            entries_after, _ = await verify_pool.scan(host, f"/data/pickup/{remote_subdir}")
        finally:
            await verify_pool.close()
        assert (
            rel_path not in entries_after
        ), f"remote file {remote_full} is still present after a move-mode delete"
    finally:
        # Best-effort cleanup of the throwaway remote subdirectory (harmless if the delete
        # above already removed it).
        try:
            await scan_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await scan_pool.close()


async def test_move_mode_item_survives_the_next_scan_as_verified_not_local_only(db, tmp_path):
    """The reproduction (prompts/2026-08-13-move-mode-outcome-survives-local-only.md), against
    the real fake seedbox rather than only a unit test on the pure function -- this bug existed
    precisely because the unit-level rule (`outcome_survives_rescan`) looked right on its own.

    Same sequence as the test above -- transfer, verify, delete -- through the real pipeline,
    then a real `core/engine.py.Engine.scan_queue` against the seedbox: a genuine SSH `find`
    that finds nothing left at this path (the delete above really happened), and a genuine
    `core/local_scan.py` walk that finds the file still on disk. Before the fix this reads
    `LOCAL_ONLY` within one scan; the whole point of this task is that it must not.
    """
    remote_subdir = f"phase5-move-scan-e2e-{uuid4().hex[:12]}"
    rel_path = "target.txt"
    remote_full = f"/data/pickup/{remote_subdir}/{rel_path}"

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    host = _host_config()
    try:
        conn = await scan_pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(f"/data/pickup/{remote_subdir}", exist_ok=True)
            async with sftp.open(remote_full, "wb") as f:
                await f.write(_CONTENT)

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
            "sync_mode, auto_verify) VALUES (?, 'e2e-move-scan', ?, ?, 1, 'move', 1)",
            (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
        )
        queue_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, ?, 0, ?, 0, 'REMOTE_ONLY')",
            (queue_id, rel_path, len(_CONTENT)),
        )
        item_id = cursor.lastrowid
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

        async def host_provider():
            return host

        events = EventBus()
        pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
        await save_postprocess_settings(db, PostprocessSettings(verify_hash_on_disk=True))
        pipeline = PostprocessPipeline(
            db=db, events=events, remote_pool=pipeline_pool, host_provider=host_provider
        )

        q_transfer = TransferQueue(
            db=db,
            config_dir=str(tmp_path),
            events=events,
            run_dir=str(tmp_path / "run"),
            tick_s=0.2,
            host_provider=host_provider,
        )
        q_transfer.postprocess = pipeline

        await q_transfer.start()
        try:
            await q_transfer.enqueue_item(item_id)

            async def remote_delete_recorded():
                row = await (
                    await db.execute("SELECT remote_deleted_at FROM item WHERE id = ?", (item_id,))
                ).fetchone()
                return row is not None and row["remote_deleted_at"] is not None

            assert await _wait_until(
                remote_delete_recorded, timeout_s=30
            ), "move-mode postprocessing never recorded a remote delete"
        finally:
            await q_transfer.stop()

        item_row = await (
            await db.execute("SELECT state, remote_deleted_at FROM item WHERE id = ?", (item_id,))
        ).fetchone()
        assert item_row["state"] == "VERIFIED", "setup did not actually verify -- test is void"
        assert item_row["remote_deleted_at"] is not None, "setup did not actually delete"

        # --- The reproduction: a real scan must not overwrite VERIFIED with LOCAL_ONLY -------
        engine = Engine(db, str(tmp_path), events)
        q = QueueConfig(
            id=queue_id,
            host_id=host_id,
            name="e2e-move-scan",
            remote_path=f"/data/pickup/{remote_subdir}",
            local_path=str(local_dir),
            staging_path=None,
            enabled=True,
            sync_mode="move",
        )
        try:
            for _ in range(3):
                await engine.scan_queue(q, host)
                row = await (
                    await db.execute(
                        "SELECT state, first_missing_at FROM item WHERE id = ?", (item_id,)
                    )
                ).fetchone()
                assert row["state"] == "VERIFIED", "must not read LOCAL_ONLY"
                assert row["first_missing_at"] is None, "content is present; nothing is missing"
        finally:
            await engine.pool.close()
    finally:
        try:
            await scan_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await scan_pool.close()


_SEVEN_ZIP_BIN = os.environ.get("LFTPWEB_7Z_BIN") or next(
    (b for b in ("7zz", "7z") if shutil.which(b)), None
)


async def test_extract_stages_through_unpack_and_merges_into_final_directory(
    db, tmp_path, monkeypatch
):
    """This task's own e2e coverage: a `copy`-mode queue transfers a directory item containing
    a zip archive, the pipeline extracts it for real (the dev host's 7z, via `LFTPWEB_7Z_BIN`
    -- see `tests/test_postprocess.py`'s module docstring), and the only thing left on disk is
    the merged final content. No `_UNPACK_`/`_FAILED_` directory anywhere under the local root.
    """
    if _SEVEN_ZIP_BIN is None:
        pytest.skip("no 7zz/7z binary on PATH -- `apt-get install 7zip` or similar")
    # Unlike the unit tests in test_postprocess.py, this goes through the real production call
    # site (`postprocess.py._do_extract`), which never passes `binary=` -- it relies on
    # `extract_item`'s default, which (being a plain default argument) is bound to
    # `extract.DEFAULT_BINARY`'s value at *function-definition* time, so patching the module
    # attribute afterwards has no effect. Patch the function itself instead: same
    # accommodation for this dev host's differently-named 7-Zip binary, applied at the one
    # call site that doesn't expose a parameter for it.
    monkeypatch.setattr(
        extract, "extract_item", functools.partial(extract.extract_item, binary=_SEVEN_ZIP_BIN)
    )

    remote_subdir = f"phase5-extract-e2e-{uuid4().hex[:12]}"
    rel_path = "Release"
    archive_buf = io.BytesIO()
    with zipfile.ZipFile(archive_buf, "w") as zf:
        zf.writestr("inner.txt", "extracted via the real postprocessing pipeline")
    archive_content = archive_buf.getvalue()

    scan_pool = RemoteConnectionPool(tmp_path / "known_hosts_scan")
    host = _host_config()
    try:
        conn = await scan_pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await sftp.makedirs(f"/data/pickup/{remote_subdir}/{rel_path}", exist_ok=True)
            async with sftp.open(f"/data/pickup/{remote_subdir}/{rel_path}/payload.zip", "wb") as f:
                await f.write(archive_content)

        entries, _ = await scan_pool.scan(host, f"/data/pickup/{remote_subdir}")
        assert f"{rel_path}/payload.zip" in entries, "setup fixture failed to upload"
        remote_size = entries[f"{rel_path}/payload.zip"].size

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
            "sync_mode, auto_verify, auto_extract) VALUES "
            "(?, 'e2e-extract', ?, ?, 1, 'copy', 0, 1)",
            (host_id, f"/data/pickup/{remote_subdir}", str(local_dir)),
        )
        queue_id = cursor.lastrowid
        cursor = await db.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, ?, 1, ?, 0, 'REMOTE_ONLY')",
            (queue_id, rel_path, remote_size),
        )
        item_id = cursor.lastrowid
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

        async def host_provider():
            return host

        events = EventBus()
        pipeline_pool = RemoteConnectionPool(tmp_path / "known_hosts_pipeline")
        # verify is off (copy mode doesn't force it) -- only extraction is under test here.
        await save_postprocess_settings(
            db, PostprocessSettings(extract_enabled=True, extract_passwords=())
        )
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

            async def job_succeeded():
                row = await (
                    await db.execute(
                        "SELECT state FROM job WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                        (item_id,),
                    )
                ).fetchone()
                return row is not None and row["state"] == "succeeded"

            assert await _wait_until(job_succeeded, timeout_s=30), "transfer job never succeeded"

            async def extraction_finished():
                row = await (
                    await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
                ).fetchone()
                return row is not None and row["state"] in ("EXTRACTED", "EXTRACT_FAILED")

            assert await _wait_until(
                extraction_finished, timeout_s=30
            ), "postprocessing never reached a terminal extraction state"

            item_row = await (
                await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            assert item_row["state"] == "EXTRACTED"

            events_rows = await (
                await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
            ).fetchall()
            extract_events = [r["message"] for r in events_rows if r["kind"] == "extract"]
            assert extract_events, "no extract event recorded"
            assert "1 of 1" in extract_events[0]
        finally:
            await q.stop()

        # --- The actual proof: only the merged final content remains on disk ---------------
        item_dir = local_dir / rel_path
        assert (
            item_dir / "inner.txt"
        ).read_text() == "extracted via the real postprocessing pipeline"
        assert (item_dir / "payload.zip").exists(), "the source archive is left in place"
        assert not (local_dir / f"{extract.UNPACK_PREFIX}{rel_path}").exists()
        assert not (local_dir / f"{extract.FAILED_PREFIX}{rel_path}").exists()
    finally:
        try:
            await scan_pool.delete_path(host, f"/data/pickup/{remote_subdir}")
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        await scan_pool.close()
