"""Unit tests for the phase 5 post-processing pipeline (DESIGN.md §6, §7.4) -- verification,
extraction, the cross-device-safe staging move, and the `move`-mode delete gate. No fake
seedbox needed: `PostprocessPipeline`'s remote delete is exercised against a stub `_RemotePool`
here; the real asyncssh path is covered end-to-end by `tests/test_postprocess_e2e.py`.

`core/extract.py` needs a real 7-Zip binary. The container image ships Alpine's `7zip`
package, whose binary is `7zz`; a Debian/Ubuntu dev host's `7zip` package (installed for this
session: `apt-get install 7zip`) names the identical upstream binary `7z` instead -- so every
extraction test passes `binary=_SEVEN_ZIP_BIN` (env-overridable) rather than hardcoding
either name. Skipped automatically if no such binary is on PATH.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import zipfile

import aiosqlite
import pytest

from lftpweb.core import extract, postprocess, verify
from lftpweb.core.events import EventBus
from lftpweb.core.remote import HostConfig
from lftpweb.db import migrate

_SEVEN_ZIP_BIN = os.environ.get("LFTPWEB_7Z_BIN") or next(
    (b for b in ("7zz", "7z") if shutil.which(b)), None
)

pytestmark_7z = pytest.mark.skipif(
    _SEVEN_ZIP_BIN is None, reason="no 7zz/7z binary on PATH -- `apt-get install 7zip` or similar"
)


# --- core/verify.py -------------------------------------------------------------------------


def test_verify_sfv_match_is_verified(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")
    import zlib

    crc = zlib.crc32(b"hello world") & 0xFFFFFFFF
    (item / "checksums.sfv").write_text(f"a.txt {crc:08x}\n")

    result = verify.verify_item(item)
    assert result.state == "VERIFIED"


def test_verify_sfv_mismatch_is_corrupt(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")
    (item / "checksums.sfv").write_text("a.txt deadbeef\n")

    result = verify.verify_item(item)
    assert result.state == "CORRUPT"
    assert "a.txt" in result.detail


def test_verify_md5_match_is_verified(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")
    import hashlib

    digest = hashlib.md5(b"hello world").hexdigest()
    (item / "checksums.md5").write_text(f"{digest}  a.txt\n")

    assert verify.verify_item(item).state == "VERIFIED"


def test_verify_sfv_references_missing_file_is_corrupt(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "checksums.sfv").write_text("missing.txt deadbeef\n")

    result = verify.verify_item(item)
    assert result.state == "CORRUPT"
    assert "missing" in result.detail


def test_verify_no_sidecar_and_fallback_disabled_is_skipped(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")

    result = verify.verify_item(item, hash_on_disk_fallback=False)
    assert result.state == "SKIPPED"


def test_verify_no_sidecar_hash_on_disk_fallback_reads_every_file(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")
    (item / "b.txt").write_bytes(b"more data")

    result = verify.verify_item(item, hash_on_disk_fallback=True)
    assert result.state == "VERIFIED"
    assert "fallback" in result.detail


def test_verify_single_loose_file_item(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "notes.txt"
    target.write_bytes(b"loose file contents")
    import zlib

    crc = zlib.crc32(b"loose file contents") & 0xFFFFFFFF
    (root / "notes.sfv").write_text(f"notes.txt {crc:08x}\n")

    assert verify.verify_item(target).state == "VERIFIED"


# --- core/extract.py -------------------------------------------------------------------------


@pytestmark_7z
def test_extract_zip_in_place(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    archive = item / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.txt", "zip contents")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.ok, result.detail
    assert (item / "inner.txt").read_text() == "zip contents"


@pytestmark_7z
def test_extract_7z_format_round_trip(tmp_path):
    """DESIGN.md §6/NOTICE: 7zz is the only archive tool -- exercise the native .7z format
    too, not just zip, since that's the format lftpweb's own container can *create* (for this
    test's fixture) as well as extract.
    """
    item = tmp_path / "Release"
    item.mkdir()
    payload_dir = tmp_path / "payload_src"
    payload_dir.mkdir()
    (payload_dir / "inner.bin").write_bytes(b"7z contents\x00\x01")

    archive = item / "payload.7z"
    subprocess.run(
        [_SEVEN_ZIP_BIN, "a", str(archive), str(payload_dir / "inner.bin")],
        check=True,
        capture_output=True,
    )
    assert archive.exists()

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.ok, result.detail
    assert (item / "inner.bin").read_bytes() == b"7z contents\x00\x01"


@pytestmark_7z
def test_extract_to_target_dir(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    with zipfile.ZipFile(item / "payload.zip", "w") as zf:
        zf.writestr("inner.txt", "elsewhere")

    target = tmp_path / "extracted" / "Release"
    result = extract.extract_item(item, target_dir=target, binary=_SEVEN_ZIP_BIN)
    assert result.ok, result.detail
    assert (target / "inner.txt").read_text() == "elsewhere"
    assert not (item / "inner.txt").exists()


@pytestmark_7z
def test_extract_password_protected_archive_tries_password_list(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    payload = tmp_path / "secret.txt"
    payload.write_text("shh")
    archive = item / "payload.zip"
    subprocess.run(
        [_SEVEN_ZIP_BIN, "a", "-pcorrect-horse", str(archive), str(payload)],
        check=True,
        capture_output=True,
    )

    result = extract.extract_item(
        item, passwords=("wrong-guess", "correct-horse"), binary=_SEVEN_ZIP_BIN
    )
    assert result.ok, result.detail
    assert (item / "secret.txt").read_text() == "shh"


@pytestmark_7z
def test_extract_corrupt_archive_fails_without_raising(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "broken.zip").write_bytes(b"not actually a zip file")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)
    assert result.ok is False
    assert "broken.zip" in result.detail


def test_extract_no_archives_is_a_no_op_success(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "video.mkv").write_bytes(b"not an archive")

    result = extract.extract_item(item, binary="does-not-need-to-exist")
    assert result.ok is True
    assert "no archives" in result.detail


def test_first_rar_volume_detection_is_name_based_only():
    """Pure name matching, no filesystem access needed: DESIGN.md §6's "extract from the
    first volume only" -- 7zz is only ever handed `.part1.rar` (or a bare `.rar`), never
    `.part2.rar`/`.part02.rar`, which it follows on its own once given the first volume.
    """
    assert extract._is_first_rar_volume("release.part1.rar") is True
    assert extract._is_first_rar_volume("release.part2.rar") is False
    assert extract._is_first_rar_volume("release.part02.rar") is False
    assert extract._is_first_rar_volume("release.rar") is True


# --- move_tree (staging -> final, DESIGN.md §6) -----------------------------------------------


def test_move_tree_same_device_fast_path(tmp_path):
    src = tmp_path / "src" / "Release"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("data")
    dst = tmp_path / "dst" / "Release"

    postprocess.move_tree(src, dst)

    assert not src.exists()
    assert (dst / "a.txt").read_text() == "data"


def test_move_tree_cross_device_fallback_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Release"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("cross device data")
    (src / "Subs").mkdir()
    (src / "Subs" / "b.srt").write_text("subs")
    dst = tmp_path / "dst" / "Release"

    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(a, b)

    monkeypatch.setattr(postprocess.os, "rename", flaky_rename)

    postprocess.move_tree(src, dst)

    assert not src.exists()
    assert (dst / "a.txt").read_text() == "cross device data"
    assert (dst / "Subs" / "b.srt").read_text() == "subs"
    # No leftover temp directories in dst's parent.
    leftovers = [p for p in dst.parent.iterdir() if p.name.startswith(".lftpweb-moving-")]
    assert leftovers == []


def test_move_tree_cross_device_copy_failure_leaves_no_partial_file_at_destination(
    tmp_path, monkeypatch
):
    """The phase 5 prompt's own words: "a partial copy must never be mistaken for a complete
    one." Simulate `os.rename` always raising EXDEV (the expected NFS case) and the copy step
    failing partway through -- `dst` must never exist afterward, and the temp sibling must be
    cleaned up.
    """
    src = tmp_path / "src" / "Release"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("first file copies fine")
    (src / "b.txt").write_text("second file will fail")
    dst = tmp_path / "dst" / "Release"
    dst.parent.mkdir(parents=True)

    def always_exdev(a, b):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(postprocess.os, "rename", always_exdev)

    real_copyfileobj = shutil.copyfileobj
    call_count = {"n": 0}

    def flaky_copyfileobj(fsrc, fdst, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:  # let the first file copy, fail on the second
            raise OSError("simulated disk error mid-copy")
        return real_copyfileobj(fsrc, fdst, *a, **kw)

    monkeypatch.setattr(postprocess.shutil, "copyfileobj", flaky_copyfileobj)

    with pytest.raises(OSError):
        postprocess.move_tree(src, dst)

    assert not dst.exists(), "destination must never hold a partial copy"
    assert src.exists(), "source must be untouched when the move fails"
    leftovers = [p for p in dst.parent.iterdir() if p.name.startswith(".lftpweb-moving-")]
    assert leftovers == [], "the temp copy must be cleaned up on failure"


def test_move_tree_refuses_to_overwrite_an_existing_destination(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("new")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "a.txt").write_text("already here")

    with pytest.raises(FileExistsError):
        postprocess.move_tree(src, dst)
    assert (dst / "a.txt").read_text() == "already here"


# --- PostprocessSettings load/save ------------------------------------------------------------


async def test_postprocess_settings_default_off():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await migrate(db)
    try:
        settings = await postprocess.load_postprocess_settings(db)
        assert settings.verify_enabled is False
        assert settings.extract_enabled is False
        assert settings.move_enabled is False
        assert settings.concurrency == 1
    finally:
        await db.close()


async def test_postprocess_settings_round_trip():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await migrate(db)
    try:
        saved = postprocess.PostprocessSettings(
            verify_enabled=True,
            verify_hash_on_disk=True,
            extract_enabled=True,
            extract_target_dir="/config/extracted",
            extract_passwords=("a", "b"),
            move_enabled=True,
            concurrency=4,
        )
        await postprocess.save_postprocess_settings(db, saved)
        loaded = await postprocess.load_postprocess_settings(db)
        assert loaded == saved
    finally:
        await db.close()


# --- PostprocessPipeline.process_item -- the move-mode delete gate, without a real seedbox --


class _FakeRemotePool:
    """Stands in for `core/remote.py`'s `RemoteConnectionPool` -- records every delete
    attempt so the gate's decisions are assertable without a live SSH connection. The real
    asyncssh path (`RemoteConnectionPool.delete_path`) is exercised by
    `tests/test_postprocess_e2e.py` against the fake seedbox.
    """

    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[HostConfig, str]] = []
        self.fail = fail

    async def delete_path(self, host: HostConfig, remote_path: str) -> None:
        self.calls.append((host, remote_path))
        if self.fail:
            raise RuntimeError("simulated remote delete failure")


def _host_config() -> HostConfig:
    return HostConfig(
        id=1, address="seedbox.invalid", port=22, username="u", auth_method="key", key_path="/k"
    )


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await migrate(db)
    return db


async def _make_host_and_queue_rows(
    db, *, sync_mode: str, auto_verify=0, auto_extract=0, auto_move=0, staging_path=None
):
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', 'seedbox.invalid', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, enabled, "
        "sync_mode, auto_verify, auto_extract, auto_move) VALUES (?, 'q', '/data/pickup', ?, ?, 1, ?, ?, ?, ?)",
        (host_id, "/local", staging_path, sync_mode, auto_verify, auto_extract, auto_move),
    )
    queue_id = cursor.lastrowid
    await db.commit()
    return host_id, queue_id


async def _make_item_row(db, queue_id, rel_path, *, state="DOWNLOADED", remote_size=100):
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (queue_id, rel_path, remote_size, remote_size, state),
    )
    await db.commit()
    return cursor.lastrowid


async def test_move_mode_withholds_delete_when_no_verification_evidence(tmp_path):
    """The prompt's own required test: an unverified item leaves the remote intact, and the
    withheld delete is recorded as an event.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"downloaded but not verifiable")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        # Fix up local_path to the real tmp_path (helper above hardcodes "/local").
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        settings = postprocess.PostprocessSettings()  # everything off; move forces verify on
        await pipeline.process_item(item_id, settings)

        assert pool.calls == [], "no delete should have been issued"
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["remote_deleted_at"] is None

        events = await (await db.execute("SELECT kind, message FROM event")).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete_withheld" in kinds
        assert "remote_delete" not in kinds
    finally:
        await db.close()


async def test_move_mode_deletes_remote_only_after_verification(tmp_path):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"downloaded and verifiable")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        # hash-on-disk fallback on -> no sidecar needed to reach VERIFIED.
        settings = postprocess.PostprocessSettings(verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        assert len(pool.calls) == 1
        _, remote_path = pool.calls[0]
        assert remote_path == f"/data/pickup/{rel_path}"

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["remote_deleted_at"] is not None
        assert item["state"] == "VERIFIED"  # unchanged by the delete step itself

        events = await (await db.execute("SELECT kind FROM event")).fetchall()
        assert "remote_delete" in [e["kind"] for e in events]
    finally:
        await db.close()


async def test_move_mode_corrupt_item_withholds_delete(tmp_path):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release"
        item_dir = local_root / rel_path
        item_dir.mkdir()
        (item_dir / "a.txt").write_bytes(b"hello world")
        (item_dir / "checksums.sfv").write_text("a.txt deadbeef\n")  # deliberately wrong

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=11)

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert pool.calls == []
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "CORRUPT"
        assert item["remote_deleted_at"] is None
    finally:
        await db.close()


async def test_every_step_defaults_off_for_a_copy_mode_queue(tmp_path):
    """DESIGN.md §6's non-negotiable, exercised end to end through the pipeline: with global
    settings and queue toggles all at their defaults (off, off, off, copy mode), processing a
    freshly-DOWNLOADED item changes nothing at all.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"just a normal download")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert pool.calls == []
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "DOWNLOADED"  # untouched
        assert item["verified_at"] is None
        assert item["remote_deleted_at"] is None

        events = await (await db.execute("SELECT * FROM event")).fetchall()
        assert events == []
    finally:
        await db.close()


async def _async_host() -> HostConfig:
    return _host_config()
