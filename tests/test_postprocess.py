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

import asyncio
import errno
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

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


def test_verify_hash_on_disk_fallback_with_no_expected_size_is_unaffected(tmp_path):
    """Backward compatible: `expected_total_bytes` defaults to `None`, which skips the new
    size check entirely -- the same permissiveness the fallback always had.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")

    result = verify.verify_item(item, hash_on_disk_fallback=True, expected_total_bytes=None)
    assert result.state == "VERIFIED"


def test_verify_hash_on_disk_fallback_catches_truncation(tmp_path):
    """prompts/open-issues.md #3: reading a short file to EOF raises nothing, so readability
    alone previously proved nothing about completeness -- a partial file passed. Passing the
    item's known remote size (as `core/postprocess.py._do_verify` now does) catches it.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")  # 11 bytes on disk

    result = verify.verify_item(item, hash_on_disk_fallback=True, expected_total_bytes=1000)
    assert result.state == "CORRUPT"
    assert "11 of 1000" in result.detail


def test_verify_hash_on_disk_fallback_matching_size_is_verified(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.txt").write_bytes(b"hello world")  # 11 bytes
    (item / "b.txt").write_bytes(b"!!")  # 2 bytes

    result = verify.verify_item(item, hash_on_disk_fallback=True, expected_total_bytes=13)
    assert result.state == "VERIFIED"
    assert "13 bytes total" in result.detail


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


@pytestmark_7z
def test_extract_success_merges_into_existing_item_dir_and_removes_unpack_dir(tmp_path):
    """This task's own "done when": extraction stages into `_UNPACK_<name>`, then merges into
    the item's own directory -- which already holds the source archive, so this is a merge
    into an *existing* directory, not a fresh move (DESIGN.md §6, docs/decisions.md).
    """
    item = tmp_path / "Release"
    item.mkdir()
    with zipfile.ZipFile(item / "payload.zip", "w") as zf:
        zf.writestr("inner.txt", "zip contents")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)

    assert result.ok, result.detail
    assert (item / "inner.txt").read_text() == "zip contents"
    assert (item / "payload.zip").exists()  # the archive itself is left alone
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert not (tmp_path / f"{extract.FAILED_PREFIX}Release").exists()


@pytestmark_7z
def test_extract_nested_archive_merges_into_existing_subdirectory(tmp_path):
    """A multi-CD-style release: the archive lives under a subdirectory of the item, and that
    subdirectory already exists at the final location -- staging must reproduce the same
    layout and merge recurses one level deeper, not just at the item root.
    """
    item = tmp_path / "Release"
    (item / "CD1").mkdir(parents=True)
    with zipfile.ZipFile(item / "CD1" / "cd1.zip", "w") as zf:
        zf.writestr("track1.bin", "cd1 contents")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)

    assert result.ok, result.detail
    assert (item / "CD1" / "track1.bin").read_text() == "cd1 contents"
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()


@pytestmark_7z
def test_extract_to_target_dir_stages_under_that_target_too(tmp_path):
    """DESIGN.md §6's `extract_target_dir` keeps taking precedence when set; `_UNPACK_`
    staging still applies, as a sibling of the configured target (docs/decisions.md).
    """
    item = tmp_path / "Release"
    item.mkdir()
    with zipfile.ZipFile(item / "payload.zip", "w") as zf:
        zf.writestr("inner.txt", "elsewhere")

    target = tmp_path / "extracted" / "Release"
    result = extract.extract_item(item, target_dir=target, binary=_SEVEN_ZIP_BIN)

    assert result.ok, result.detail
    assert (target / "inner.txt").read_text() == "elsewhere"
    assert not (target.parent / f"{extract.UNPACK_PREFIX}Release").exists()


@pytestmark_7z
def test_extract_loose_top_level_archive_file_in_place(tmp_path):
    """§4.7's loose top-level file case: `root` *is* the archive, not a directory containing
    it. "In place" means the containing directory -- matching the pre-staging behavior of
    extracting to `archive.parent` -- and must not crash trying to treat the archive itself
    as the item's own directory.
    """
    local_root = tmp_path / "local"
    local_root.mkdir()
    archive = local_root / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.txt", "loose file contents")

    result = extract.extract_item(archive, binary=_SEVEN_ZIP_BIN)

    assert result.ok, result.detail
    assert (local_root / "inner.txt").read_text() == "loose file contents"


@pytestmark_7z
def test_extract_failure_leaves_failed_dir_as_evidence_and_no_unpack_dir(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "broken.zip").write_bytes(b"not actually a zip file")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)

    assert result.ok is False
    failed_dir = tmp_path / f"{extract.FAILED_PREFIX}Release"
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert failed_dir.is_dir()
    # Nothing landed under the final name -- the failure is confined to the evidence dir.
    assert not (item / "inner.txt").exists()


@pytestmark_7z
def test_extract_failed_dir_from_a_previous_attempt_is_replaced_not_a_conflict(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "broken.zip").write_bytes(b"not actually a zip file")
    failed_dir = tmp_path / f"{extract.FAILED_PREFIX}Release"
    failed_dir.mkdir()
    (failed_dir / "stale-evidence.txt").write_text("from a previous attempt")

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)

    assert result.ok is False
    assert failed_dir.is_dir()
    assert not (failed_dir / "stale-evidence.txt").exists()  # replaced, not merged with


@pytestmark_7z
def test_extract_never_writes_the_final_name_mid_extraction(tmp_path, monkeypatch):
    """The whole point of this task: an *arr watching `item` must see nothing, then see a
    complete release -- never a growing file at its final name while extraction runs.
    """
    item = tmp_path / "Release"
    item.mkdir()
    with zipfile.ZipFile(item / "payload.zip", "w") as zf:
        zf.writestr("inner.txt", "zip contents")

    real_run_7z = extract._run_7z
    seen_targets = []

    def spying_run_7z(binary, args):
        o_arg = next(a for a in args if a.startswith("-o"))
        seen_targets.append(Path(o_arg[2:]))
        assert not (item / "inner.txt").exists(), "must not appear at the final name mid-run"
        return real_run_7z(binary, args)

    monkeypatch.setattr(extract, "_run_7z", spying_run_7z)

    result = extract.extract_item(item, binary=_SEVEN_ZIP_BIN)

    assert result.ok, result.detail
    assert len(seen_targets) == 1
    assert seen_targets[0].name.startswith(extract.UNPACK_PREFIX)
    assert (item / "inner.txt").read_text() == "zip contents"


def test_extract_no_archives_is_skipped_not_extracted(tmp_path):
    """Fix, 2026-08-12 (docs/decisions.md): `SKIPPED`, not `EXTRACTED` -- a no-op must never
    be reported as a success that claims work was done. `result.ok` (the `EXTRACTED`-only
    convenience) is therefore False here, unlike before this fix.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "video.mkv").write_bytes(b"not an archive")

    result = extract.extract_item(item, binary="does-not-need-to-exist")
    assert result.state == "SKIPPED"
    assert result.ok is False
    assert "no archives" in result.detail
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert not (tmp_path / f"{extract.FAILED_PREFIX}Release").exists()


# --- core/extract.py: extraction preconditions (fix, 2026-08-12) -----------------------------
#
# None of these need a real 7-Zip binary: `check_extract_preconditions` is pure filesystem
# I/O (stat + name matching) run *before* `extract_item` ever calls `_run_7z`, so every test
# below passes `binary="does-not-need-to-exist"` the same way the no-archives test above
# does, and asserts on real files on the real filesystem -- never a mocked `find_archives` or
# `check_extract_preconditions` return value.


def test_zero_length_head_is_a_named_precondition_failure_not_a_7z_attempt(tmp_path):
    item = tmp_path / "Release"
    item.mkdir()
    (item / "payload.zip").write_bytes(b"")  # zero-length -- e.g. a truncated transfer

    result = extract.extract_item(item, binary="does-not-need-to-exist")

    assert result.state == "EXTRACT_FAILED"
    assert "zero-length" in result.detail
    # No attempt was made -- no staging, and therefore no `_FAILED_` litter for something
    # 7zz was never even handed (see `extract_item`'s docstring).
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert not (tmp_path / f"{extract.FAILED_PREFIX}Release").exists()


def test_check_extract_preconditions_zero_length_file(tmp_path):
    archive = tmp_path / "payload.zip"
    archive.write_bytes(b"")
    reason = extract.check_extract_preconditions(archive)
    assert reason is not None
    assert "zero-length" in reason


def test_check_extract_preconditions_complete_single_volume_rar_passes(tmp_path):
    head = tmp_path / "release.rar"
    head.write_bytes(b"not real rar bytes, just non-empty")
    assert extract.check_extract_preconditions(head) is None


def test_check_extract_preconditions_old_style_complete_set_passes(tmp_path):
    """`release.rar` + `.r00`/`.r01`/`.r02`, all present and non-empty -- a real (if
    content-fake) multi-volume old-style rar fixture on the real filesystem, not a mock."""
    base = tmp_path / "release.rar"
    base.write_bytes(b"volume 1")
    (tmp_path / "release.r00").write_bytes(b"volume 2")
    (tmp_path / "release.r01").write_bytes(b"volume 3")
    (tmp_path / "release.r02").write_bytes(b"volume 4")

    assert extract.check_extract_preconditions(base) is None


def test_check_extract_preconditions_old_style_missing_middle_volume_fails(tmp_path):
    """The production shape this fix exists for: `.r00`, `.r01`, `.r03` present, `.r02`
    missing -- a gap, not merely "some siblings exist". Must be caught before 7zz ever sees
    the head, as a clean, named reason ("volume N of M missing"), not left to surface later
    as "Cannot open the file as archive".
    """
    base = tmp_path / "All.American.S08E06.1080p.WEB.h264-GGWP.rar"
    base.write_bytes(b"volume 1")
    (tmp_path / "All.American.S08E06.1080p.WEB.h264-GGWP.r00").write_bytes(b"volume 2")
    (tmp_path / "All.American.S08E06.1080p.WEB.h264-GGWP.r01").write_bytes(b"volume 3")
    # .r02 deliberately absent
    (tmp_path / "All.American.S08E06.1080p.WEB.h264-GGWP.r03").write_bytes(b"volume 5")

    reason = extract.check_extract_preconditions(base)
    assert reason is not None
    assert "volume 4 of 5 missing" in reason


def test_check_extract_preconditions_old_style_zero_length_volume_counts_as_missing(tmp_path):
    base = tmp_path / "release.rar"
    base.write_bytes(b"volume 1")
    (tmp_path / "release.r00").write_bytes(b"")  # zero-length -- as useless as absent
    (tmp_path / "release.r01").write_bytes(b"volume 3")

    reason = extract.check_extract_preconditions(base)
    assert reason is not None
    assert "volume 2 of 3 missing" in reason


def test_check_extract_preconditions_new_style_complete_set_passes(tmp_path):
    head = tmp_path / "release.part1.rar"
    head.write_bytes(b"volume 1")
    (tmp_path / "release.part2.rar").write_bytes(b"volume 2")
    (tmp_path / "release.part3.rar").write_bytes(b"volume 3")

    assert extract.check_extract_preconditions(head) is None


def test_check_extract_preconditions_new_style_missing_middle_volume_fails(tmp_path):
    head = tmp_path / "release.part1.rar"
    head.write_bytes(b"volume 1")
    (tmp_path / "release.part2.rar").write_bytes(b"volume 2")
    # .part3.rar deliberately absent
    (tmp_path / "release.part4.rar").write_bytes(b"volume 4")

    reason = extract.check_extract_preconditions(head)
    assert reason is not None
    assert "volume 3 of 4 missing" in reason


def test_extract_item_with_incomplete_volume_set_fails_before_creating_any_staging_dir(tmp_path):
    """End-to-end through `extract_item` (not just the precondition function directly): a
    gap in a real multi-volume rar set on the real filesystem must produce `EXTRACT_FAILED`
    with the named reason, and -- because the failure is caught before extraction is ever
    attempted -- neither `_UNPACK_` nor `_FAILED_` ever exists on disk for this run.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "Release.rar").write_bytes(b"volume 1")
    (item / "Release.r00").write_bytes(b"volume 2")
    # Release.r01 (volume 3) deliberately missing; Release.r02 (volume 4) present is what
    # makes this a *gap*, not merely "the set ends here" -- the shape this precondition can
    # actually detect (see `check_rar_volume_set`'s docstring on the wholly-absent-final-
    # volume limitation).
    (item / "Release.r02").write_bytes(b"volume 4")

    result = extract.extract_item(item, binary="does-not-need-to-exist")

    assert result.state == "EXTRACT_FAILED"
    assert "volume 3 of 4 missing" in result.detail
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()
    assert not (tmp_path / f"{extract.FAILED_PREFIX}Release").exists()


# --- core/extract.py.sweep_failed_dirs (fix 3, 2026-08-12) -----------------------------------


def test_sweep_failed_dirs_removes_dirs_older_than_retention(tmp_path):
    old_dir = tmp_path / f"{extract.FAILED_PREFIX}OldRelease"
    old_dir.mkdir()
    (old_dir / "evidence.txt").write_text("stale")
    old_ts = time.time() - 20 * 86400  # 20 days old
    os.utime(old_dir, (old_ts, old_ts))

    removed = extract.sweep_failed_dirs(tmp_path, max_age_days=14.0)

    assert len(removed) == 1
    assert removed[0][0] == old_dir.resolve()
    assert removed[0][1] >= 14.0
    assert not old_dir.exists()


def test_sweep_failed_dirs_leaves_recent_dirs_alone(tmp_path):
    recent_dir = tmp_path / f"{extract.FAILED_PREFIX}FreshRelease"
    recent_dir.mkdir()  # mtime is "now"

    removed = extract.sweep_failed_dirs(tmp_path, max_age_days=14.0)

    assert removed == []
    assert recent_dir.exists()


def test_sweep_failed_dirs_ignores_non_failed_directories(tmp_path):
    """A same-age ordinary directory (e.g. a genuine downloaded item) must never be swept --
    only the `_FAILED_` prefix, matched exactly, is ever a candidate.
    """
    other = tmp_path / "OrdinaryRelease"
    other.mkdir()
    old_ts = time.time() - 100 * 86400
    os.utime(other, (old_ts, old_ts))

    removed = extract.sweep_failed_dirs(tmp_path, max_age_days=14.0)

    assert removed == []
    assert other.exists()


def test_sweep_failed_dirs_refuses_a_symlink_escaping_the_queue_root(tmp_path):
    """Containment re-verified inside the sweep itself, not assumed from naming alone -- a
    `_FAILED_`-prefixed entry that is actually a symlink pointing outside `queue_root` must
    never be followed and removed.
    """
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "do-not-delete.txt").write_text("must survive")

    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    escape = queue_root / f"{extract.FAILED_PREFIX}Escape"
    escape.symlink_to(outside, target_is_directory=True)
    old_ts = time.time() - 100 * 86400
    os.utime(str(escape), (old_ts, old_ts), follow_symlinks=False)

    try:
        removed = extract.sweep_failed_dirs(queue_root, max_age_days=14.0)
        assert removed == []
        assert (outside / "do-not-delete.txt").exists()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_sweep_failed_dirs_is_a_no_op_on_a_root_that_does_not_exist(tmp_path):
    assert extract.sweep_failed_dirs(tmp_path / "gone", max_age_days=14.0) == []


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


# --- move_tree(merge=True) -- core/extract.py's _UNPACK_ -> final-directory merge (this task) -


def test_move_tree_merge_into_existing_directory_combines_contents(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "new.txt").write_text("from src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "existing.txt").write_text("already there")

    postprocess.move_tree(src, dst, merge=True)

    assert not src.exists()
    assert (dst / "existing.txt").read_text() == "already there"
    assert (dst / "new.txt").read_text() == "from src"


def test_move_tree_merge_recurses_into_same_named_subdirectories(tmp_path):
    src = tmp_path / "src"
    (src / "Subs").mkdir(parents=True)
    (src / "Subs" / "new.srt").write_text("new sub")
    dst = tmp_path / "dst"
    (dst / "Subs").mkdir(parents=True)
    (dst / "Subs" / "existing.srt").write_text("existing sub")

    postprocess.move_tree(src, dst, merge=True)

    assert not src.exists()
    assert (dst / "Subs" / "existing.srt").read_text() == "existing sub"
    assert (dst / "Subs" / "new.srt").read_text() == "new sub"


def test_move_tree_merge_moves_a_new_subdirectory_wholesale(tmp_path):
    """A src subdirectory with no same-named counterpart at dst doesn't need to recurse --
    the whole subtree moves in one `move_tree` call, same as any other non-colliding entry.
    """
    src = tmp_path / "src"
    (src / "NewSub").mkdir(parents=True)
    (src / "NewSub" / "f.txt").write_text("new subtree")
    dst = tmp_path / "dst"
    dst.mkdir()

    postprocess.move_tree(src, dst, merge=True)

    assert not src.exists()
    assert (dst / "NewSub" / "f.txt").read_text() == "new subtree"


def test_move_tree_merge_raises_on_a_genuine_file_collision(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "clash.txt").write_text("from src")
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "clash.txt").write_text("already there")

    with pytest.raises(FileExistsError):
        postprocess.move_tree(src, dst, merge=True)
    # Nothing silently overwritten or partially moved -- both sides untouched by the failure.
    assert (dst / "clash.txt").read_text() == "already there"
    assert (src / "clash.txt").read_text() == "from src"


def test_move_tree_merge_without_an_existing_destination_behaves_like_a_plain_move(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("data")
    dst = tmp_path / "dst"  # does not exist yet

    postprocess.move_tree(src, dst, merge=True)

    assert not src.exists()
    assert (dst / "a.txt").read_text() == "data"


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
        # Fix, 2026-08-12: a new capability defaults off, same rule as everything else here.
        assert settings.failed_retention_enabled is False
        assert settings.failed_retention_days == extract.FAILED_RETENTION_DEFAULT_DAYS
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
            failed_retention_enabled=True,
            failed_retention_days=21.0,
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
        content = b"downloaded and verifiable"
        (local_root / rel_path).write_bytes(content)

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        # remote_size must match what's actually on disk -- the hash-on-disk fallback now
        # checks total bytes against it too (prompts/open-issues.md #3), same as a real scan
        # would have recorded.
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=len(content))

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


# --- PostprocessPipeline._do_extract -- fixes 1-3 of this task, through the real pipeline ------


async def test_pipeline_no_archives_never_stamps_extracted_and_preserves_verified(tmp_path):
    """Fix 1, exercised end to end: a plain (non-archive) download on a queue with both verify
    and extract on must never be stamped `EXTRACTED`, and -- the non-obvious part -- must not
    be knocked back to `DOWNLOADED` either. Verification ran first this pass and produced a
    real `VERIFIED`; the extract step finding nothing to do must leave that alone.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        content = b"just a normal, non-archive download"
        (local_root / rel_path).write_bytes(content)

        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="copy", auto_verify=1, auto_extract=1
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        # remote_size must match what's actually on disk -- see the sibling test above.
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=len(content))

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            verify_enabled=True, verify_hash_on_disk=True, extract_enabled=True
        )
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "VERIFIED"  # not EXTRACTED, and not reverted to DOWNLOADED
        assert item["extracted_at"] is None
        assert item["verified_at"] is not None

        events = await (
            await db.execute("SELECT kind, level, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        extract_events = [e for e in events if e["kind"] == "extract"]
        assert len(extract_events) == 1
        assert extract_events[0]["level"] == "info"
        assert "no archives" in extract_events[0]["message"]
    finally:
        await db.close()


async def test_pipeline_incomplete_volume_set_reports_extract_failed_with_named_reason(tmp_path):
    """Fix 2, exercised end to end: the precondition gate must run for real inside
    `_do_extract`, not just at the `core/extract.py` unit level -- a `copy`-mode queue with
    verification off (the production shape this fix was written for) must still gate
    extraction on set completeness, not just on a scan's size rollup.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Release"
        item_dir = local_root / rel_path
        item_dir.mkdir()
        (item_dir / "Release.rar").write_bytes(b"volume 1")
        (item_dir / "Release.r00").write_bytes(b"volume 2")
        # Release.r01 (volume 3) missing; Release.r02 (volume 4) present makes it a gap.
        (item_dir / "Release.r02").write_bytes(b"volume 4")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(extract_enabled=True)
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACT_FAILED"
        assert item["error_class"] == "EXTRACT_FAILED"
        assert "volume 3 of 4 missing" in item["error_detail"]
        assert item["extracted_at"] is None

        # No attempt was made -- caught before any staging directory was ever created.
        assert not (local_root / f"{extract.UNPACK_PREFIX}{rel_path}").exists()
        assert not (local_root / f"{extract.FAILED_PREFIX}{rel_path}").exists()
    finally:
        await db.close()


async def test_pipeline_failed_retention_off_by_default_leaves_stale_dir_alone(tmp_path):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"just a normal download")

        stale = local_root / f"{extract.FAILED_PREFIX}SomeOldRelease"
        stale.mkdir()
        old_ts = time.time() - 100 * 86400
        os.utime(stale, (old_ts, old_ts))

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        # extract_enabled on, failed_retention_enabled left at its default (off).
        await pipeline.process_item(item_id, postprocess.PostprocessSettings(extract_enabled=True))

        assert stale.exists(), "retention is off by default -- nothing should have swept it"
        events = await (await db.execute("SELECT kind FROM event")).fetchall()
        assert "failed_dir_removed" not in [e["kind"] for e in events]
    finally:
        await db.close()


async def test_pipeline_failed_retention_enabled_sweeps_stale_dir_and_records_event(tmp_path):
    """Fix 3, exercised end to end: enabling retention sweeps a stale `_FAILED_` sibling on
    the same pass that would otherwise create a new one, and writes an `event` row that can be
    traced back to the item it belonged to (recovered from the directory's own name).
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()

        stale_rel_path = "Old.Failed.Release"
        stale = local_root / f"{extract.FAILED_PREFIX}{stale_rel_path}"
        stale.mkdir()
        old_ts = time.time() - 100 * 86400
        os.utime(stale, (old_ts, old_ts))

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        # The item that originally produced the stale `_FAILED_` dir -- already terminal
        # (EXTRACT_FAILED) from a previous run, unrelated to the item this pass processes.
        stale_owner_id = await _make_item_row(db, queue_id, stale_rel_path, state="EXTRACT_FAILED")

        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"just a normal download, unrelated to the stale dir")
        item_id = await _make_item_row(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, failed_retention_enabled=True, failed_retention_days=14.0
        )
        await pipeline.process_item(item_id, settings)

        assert not stale.exists(), "stale _FAILED_ dir should have been swept"

        removal_events = await (
            await db.execute("SELECT item_id, message FROM event WHERE kind = 'failed_dir_removed'")
        ).fetchall()
        assert len(removal_events) == 1
        assert removal_events[0]["item_id"] == stale_owner_id
        assert str(stale.resolve()) in removal_events[0]["message"]
    finally:
        await db.close()


async def test_pipeline_failed_retention_sweep_leaves_recent_dirs_alone(tmp_path):
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()

        recent = local_root / f"{extract.FAILED_PREFIX}RecentFailure"
        recent.mkdir()  # mtime "now"

        rel_path = "loose.txt"
        (local_root / rel_path).write_bytes(b"just a normal download")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, failed_retention_enabled=True, failed_retention_days=14.0
        )
        await pipeline.process_item(item_id, settings)

        assert recent.exists()
    finally:
        await db.close()


# --- The states this module owns: precedence over a rescan, and the in-flight registry -------
#
# The engine-side half (a real scan pass against a real `item` row) lives in
# `tests/test_state_persistence.py`; these are the two pure/local seams it leans on.


@pytest.mark.parametrize(
    ("prev_state", "structural_state", "expected"),
    [
        # The four outcomes are refinements of DOWNLOADED, so they win over a fresh one.
        ("VERIFIED", "DOWNLOADED", True),
        ("CORRUPT", "DOWNLOADED", True),
        ("EXTRACTED", "DOWNLOADED", True),
        ("EXTRACT_FAILED", "DOWNLOADED", True),
        # ...and only over DOWNLOADED. PARTIAL means the bytes are no longer all there
        # (DESIGN.md §3.2 rule 2), REMOTE_ONLY is absence and belongs to the grace period in
        # core/mount_sentinel.py -- neither may be overridden by a stale outcome.
        ("VERIFIED", "PARTIAL", False),
        ("EXTRACTED", "PARTIAL", False),
        ("CORRUPT", "REMOTE_ONLY", False),
        ("EXTRACT_FAILED", "REMOTE_ONLY", False),
        # Transient states are protected by `in_flight_item_ids()` (i.e. by a worker actually
        # running), never by the state string -- otherwise a crash would wedge the item.
        ("VERIFYING", "DOWNLOADED", False),
        ("EXTRACTING", "DOWNLOADED", False),
        # States this module doesn't own are none of its business.
        ("DOWNLOADED", "DOWNLOADED", False),
        ("STOPPED", "DOWNLOADED", False),
        ("REMOVED_LOCAL", "DOWNLOADED", False),
        (None, "DOWNLOADED", False),
    ],
)
def test_outcome_survives_rescan(prev_state, structural_state, expected):
    assert postprocess.outcome_survives_rescan(prev_state, structural_state) is expected


def test_owned_states_cover_exactly_the_six_states_design_3_2_names():
    assert postprocess.OWNED_STATES == {
        "VERIFYING",
        "VERIFIED",
        "CORRUPT",
        "EXTRACTING",
        "EXTRACTED",
        "EXTRACT_FAILED",
    }
    assert not (postprocess.TRANSIENT_STATES & postprocess.TERMINAL_STATES)


def _bare_pipeline(db=None):
    return postprocess.PostprocessPipeline(db=db, events=EventBus(), remote_pool=_FakeRemotePool())


async def test_in_flight_holds_the_item_for_exactly_the_length_of_the_run(monkeypatch):
    pipeline = _bare_pipeline()
    seen: list[frozenset[int]] = []

    async def _fake_process(item_id, settings=None):  # noqa: ARG001
        seen.append(pipeline.in_flight_item_ids())

    monkeypatch.setattr(pipeline, "_process_item", _fake_process)

    assert pipeline.in_flight_item_ids() == frozenset()
    await pipeline.process_item(42)
    assert seen == [frozenset({42})]
    assert pipeline.in_flight_item_ids() == frozenset()


async def test_a_worker_that_raises_still_releases_the_item(monkeypatch):
    """The wedge guard: if the item stayed in the set, `core/engine.py` would keep protecting
    a `VERIFYING`/`EXTRACTING` state with nobody working on it, for the life of the process.
    """
    pipeline = _bare_pipeline()

    async def _boom(item_id, settings=None):  # noqa: ARG001
        raise RuntimeError("worker died mid-extract")

    monkeypatch.setattr(pipeline, "_process_item", _boom)

    with pytest.raises(RuntimeError):
        await pipeline.process_item(7)
    assert pipeline.in_flight_item_ids() == frozenset()

    # And through the guarded path the pipeline actually uses (which swallows the exception
    # so one bad item can't break the others), the item is released just the same.
    db = await _make_db()
    try:
        pipeline.db = db
        await pipeline._run_guarded(7)
        assert pipeline.in_flight_item_ids() == frozenset()
    finally:
        await db.close()


async def test_overlapping_runs_for_one_item_release_only_once_both_are_done(monkeypatch):
    """`concurrency > 1` allows two triggers for the same item to overlap; the first to finish
    must not un-protect an item the second is still working on.
    """
    pipeline = _bare_pipeline()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(item_id, settings=None):  # noqa: ARG001
        started.set()
        await release.wait()

    monkeypatch.setattr(pipeline, "_process_item", _slow)

    slow = asyncio.create_task(pipeline.process_item(9))
    await started.wait()

    async def _fast(item_id, settings=None):  # noqa: ARG001
        return None

    monkeypatch.setattr(pipeline, "_process_item", _fast)
    await pipeline.process_item(9)  # the overlapping run finishes first
    assert pipeline.in_flight_item_ids() == frozenset({9}), "the slow run is still going"

    release.set()
    await slow
    assert pipeline.in_flight_item_ids() == frozenset()
