"""Unit tests for the phase 5 post-processing pipeline (DESIGN.md §6, §7.4) -- verification,
extraction, the cross-device-safe staging move, and the `move`-mode delete gate. No fake
seedbox needed: `PostprocessPipeline`'s remote delete is exercised against a stub `_RemotePool`
here; the real asyncssh path is covered end-to-end by `tests/test_postprocess_e2e.py`.

`core/extract.py` needs a real 7-Zip binary. The container image ships Alpine's `7zip`
package, whose binary is `7zz`; a Debian/Ubuntu dev host's `7zip` package (installed for this
session: `apt-get install 7zip`) names the identical upstream binary `7z` instead -- so every
extraction test passes `binary=_SEVEN_ZIP_BIN` (env-overridable) rather than hardcoding
either name. Skipped automatically if no such binary is on PATH.

`.rar` needs a real `unrar` binary instead (2026-08-12 fix -- docs/decisions.md: 7zz has no
RAR codec in Alpine's build, contrary to what this project believed for nine phases). Built
from source in `docker/Dockerfile`'s `unrar-builder` stage; not installed by any package
manager, so a dev host running these tests needs it built and on `PATH`, or `LFTPWEB_UNRAR_BIN`
pointed at it. Skipped automatically otherwise -- see `pytestmark_unrar` below.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import io
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import aiosqlite
import pytest

from lftpweb.core import extract, local_delete, postprocess, verify
from lftpweb.core.autoqueue import ELIGIBLE_STATES
from lftpweb.core.engine import Engine, QueueConfig, build_scan_counts_predicate
from lftpweb.core.events import EventBus
from lftpweb.core.mount_sentinel import write_if_needed
from lftpweb.core.remote import HostConfig, RemoteEntry
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


# --- core/verify.py: upstream-extracted releases (fix, 2026-08-15, docs/decisions.md) --------
#
# A release rar'd at origin but extracted *upstream* (the seedbox's own SABnzbd unpacks it,
# deletes the rars, keeps the `.sfv`) arrives locally as e.g. `movie.mkv` + `movie.sfv`, where
# the sidecar lists rar volumes that were never local to begin with. Live case:
# National.Lampoons.Animal.House.1978.iNTERNAL.1080p.BluRay.x264-EwDp on the ar-movies queue.
# The rule (narrow on purpose -- this is the one gate ahead of an irreversible remote delete):
# every referenced file absent + other content present -> SKIPPED; any referenced file present
# (including a half-deleted set) -> unchanged, stays CORRUPT; sidecar and nothing else ->
# stays CORRUPT (degenerate, nothing the sidecar could have been vouching for).


def test_verify_sfv_all_referenced_files_absent_with_content_present_is_skipped(tmp_path):
    """The Animal House shape: an `.sfv` listing several rar volumes, none of them present,
    alongside the one file that *is* present -- the mkv the archives were extracted to,
    upstream, before this ever reached the local disk.
    """
    item = tmp_path / "National.Lampoons.Animal.House.1978.iNTERNAL.1080p.BluRay.x264-EwDp"
    item.mkdir()
    (item / "ewdp-animalhouse.mkv").write_bytes(b"movie bytes")
    (item / "ewdp-animalhouse.sfv").write_text(
        "ewdp-animalhouse.r00 deadbeef\n"
        "ewdp-animalhouse.r01 deadbeef\n"
        "ewdp-animalhouse.rar deadbeef\n"
    )

    result = verify.verify_item(item)
    assert result.state == "SKIPPED"
    assert "extracted upstream" in result.detail
    assert "3" in result.detail


def test_verify_sfv_mixed_presence_stays_corrupt(tmp_path):
    """Rule 2: not every referenced entry absent -- some rars present (and passing), some
    absent (a half-deleted archive set). By the time extraction would notice the gap, the
    remote copy is already gone under `move`, so this must not be relaxed to `SKIPPED`.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.rar").write_bytes(b"hello world")
    import zlib

    crc = zlib.crc32(b"hello world") & 0xFFFFFFFF
    (item / "checksums.sfv").write_text(f"a.rar {crc:08x}\nb.rar deadbeef\n")

    result = verify.verify_item(item)
    assert result.state == "CORRUPT"
    assert "b.rar" in result.detail


def test_verify_sfv_present_and_corrupt_stays_corrupt(tmp_path):
    """Every referenced file is present, but one fails its checksum -- unaffected by the new
    all-absent rule, since not everything is absent.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "a.rar").write_bytes(b"hello world")
    (item / "b.rar").write_bytes(b"corrupted")
    import zlib

    good_crc = zlib.crc32(b"hello world") & 0xFFFFFFFF
    (item / "checksums.sfv").write_text(f"a.rar {good_crc:08x}\nb.rar deadbeef\n")

    result = verify.verify_item(item)
    assert result.state == "CORRUPT"
    assert "b.rar" in result.detail


def test_verify_sfv_only_sidecar_and_nothing_else_stays_corrupt(tmp_path):
    """Rule 3, the degenerate case: the item *is* the sidecar, no other content at all. All
    referenced entries are absent, but there's nothing the sidecar could have been vouching
    for -- this is not an upstream-extraction signal, it's an empty release.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "checksums.sfv").write_text("a.rar deadbeef\nb.rar deadbeef\n")

    result = verify.verify_item(item)
    assert result.state == "CORRUPT"


def test_verify_md5_all_referenced_files_absent_with_content_present_is_skipped(tmp_path):
    """md5-flavored twin of the Animal House case -- the rule applies to both sidecar
    formats.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "movie.mkv").write_bytes(b"movie bytes")
    (item / "checksums.md5").write_text(
        "deadbeefdeadbeefdeadbeefdeadbeef  a.rar\nfeedfacefeedfacefeedfacefeedface  b.rar\n"
    )

    result = verify.verify_item(item)
    assert result.state == "SKIPPED"
    assert "extracted upstream" in result.detail
    assert "2" in result.detail


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


def test_find_escaping_path_flags_a_symlink_pointing_out_of_staging(tmp_path):
    """Audit S2: the containment helper returns None for a fully-contained tree and the
    offending entry for a symlink whose target is outside the staging root. No archive tooling
    needed -- this pins the security predicate directly.
    """
    staging = tmp_path / "staging"
    (staging / "sub").mkdir(parents=True)
    (staging / "sub" / "normal.txt").write_text("fine")
    assert extract.find_escaping_path(staging) is None

    outside = tmp_path / "outside"
    outside.mkdir()
    (staging / "evil").symlink_to(outside)
    escaping = extract.find_escaping_path(staging)
    assert escaping is not None
    assert escaping.name == "evil"


def test_extract_item_refuses_to_publish_an_escaping_symlink(tmp_path, monkeypatch):
    """Audit S2: extraction that plants a symlink escaping the staging root fails
    EXTRACT_FAILED, withholds the merge into the final tree, and keeps the offending output as
    `_FAILED_` evidence. Verified without a real malicious archive by patching the per-archive
    extractor to plant the symlink a hostile archive member would.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "payload.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # find_archives sees a .zip
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not be reachable")

    def fake_extract_archive(archive, target_dir, **kwargs):
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "escape").symlink_to(outside)  # a symlink member pointing outside staging
        return extract.ExtractResult(state="EXTRACTED", detail="faked")

    monkeypatch.setattr(extract, "extract_archive", fake_extract_archive)
    monkeypatch.setattr(extract, "check_extract_preconditions", lambda a: None)

    result = extract.extract_item(item, binary="unused")
    assert not result.ok
    assert result.state == "EXTRACT_FAILED"
    assert "escaping the staging root" in result.detail
    # The escape never reached the published tree; evidence is kept as _FAILED_.
    assert not (item / "escape").exists()
    assert (item.parent / f"{extract.FAILED_PREFIX}{item.name}").is_dir()


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


# --- core/extract.py.list_top_level_debris_dirs (orphan-debris sweep, 2026-08-18,
# prompts/done/2026-08-18-sweep-orphaned-extract-debris.md) -----------------------------------


def test_list_top_level_debris_dirs_finds_both_prefixes(tmp_path):
    failed = tmp_path / f"{extract.FAILED_PREFIX}Release.One"
    unpack = tmp_path / f"{extract.UNPACK_PREFIX}Release.Two"
    failed.mkdir()
    unpack.mkdir()

    found = extract.list_top_level_debris_dirs(tmp_path)

    assert set(found) == {failed.resolve(), unpack.resolve()}


def test_list_top_level_debris_dirs_ignores_age_unlike_sweep_failed_dirs(tmp_path):
    """Unlike `sweep_failed_dirs`, this is a pure enumeration with no age filter at all --
    the orphan sweep built on top of it decides "sweep or not" from the `item` table, never
    from mtime.
    """
    failed = tmp_path / f"{extract.FAILED_PREFIX}FreshRelease"
    failed.mkdir()  # mtime is "now"

    found = extract.list_top_level_debris_dirs(tmp_path)

    assert found == [failed.resolve()]


def test_list_top_level_debris_dirs_ignores_ordinary_directories_and_files(tmp_path):
    (tmp_path / "OrdinaryRelease").mkdir()
    (tmp_path / "some.mkv").write_bytes(b"x")

    assert extract.list_top_level_debris_dirs(tmp_path) == []


def test_list_top_level_debris_dirs_only_direct_children(tmp_path):
    """A `_FAILED_`/`_UNPACK_`-prefixed directory nested *inside* an ordinary release must
    never be a candidate -- only a direct child of `queue_root` is, matching
    `sweep_failed_dirs`'s own containment shape.
    """
    nested = tmp_path / "Release" / f"{extract.FAILED_PREFIX}Nested"
    nested.mkdir(parents=True)

    assert extract.list_top_level_debris_dirs(tmp_path) == []


def test_list_top_level_debris_dirs_refuses_a_symlink_escaping_the_queue_root(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "do-not-delete.txt").write_text("must survive")

    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    escape = queue_root / f"{extract.FAILED_PREFIX}Escape"
    escape.symlink_to(outside, target_is_directory=True)

    try:
        assert extract.list_top_level_debris_dirs(queue_root) == []
        assert (outside / "do-not-delete.txt").exists()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_list_top_level_debris_dirs_is_a_no_op_on_a_root_that_does_not_exist(tmp_path):
    assert extract.list_top_level_debris_dirs(tmp_path / "gone") == []


def test_first_rar_volume_detection_is_name_based_only():
    """Pure name matching, no filesystem access needed: DESIGN.md §6's "extract from the
    first volume only" -- `unrar` is only ever handed `.part1.rar` (or a bare `.rar`), never
    `.part2.rar`/`.part02.rar`, which it follows on its own once given the first volume.
    """
    assert extract._is_first_rar_volume("release.part1.rar") is True
    assert extract._is_first_rar_volume("release.part2.rar") is False
    assert extract._is_first_rar_volume("release.part02.rar") is False
    assert extract._is_first_rar_volume("release.rar") is True


# --- core/extract.py: rar decoding via `unrar` (2026-08-12 fix -- docs/decisions.md) ----------
#
# The bug this task exists to fix: `core/extract.py` routed `.rar` through 7zz for nine phases
# of green CI, and Alpine's `7zip` package has never had a RAR codec at all (`7zz i` inside the
# built image lists no Rar/Rar5 handler -- verified by building the image and inspecting it, not
# by reading upstream 7-Zip's changelog). Every rar fixture that existed before this fix was
# fake bytes (`b"volume 1"`, `b"not real rar bytes, just non-empty"`, above) -- they exercised
# `check_extract_preconditions`'s naming/gap-detection logic, which is pure filesystem I/O, but
# never asked a real decoder to open a real archive. This section replaces that blind spot.
#
# **Two fixtures, hand-built as raw RAR4 container bytes, not compressed with any tool.** No
# compressor exists anywhere in this project's toolchain to *create* a rar -- `unrar` only
# decompresses, and RARLAB's own licence forbids using its source to build a RAR-compatible
# archiver, which is exactly why no Alpine package ships one either. RARLAB's unrar source
# (`unrar/headers.hpp`, `unrar/arcread.cpp`) documents the RAR 1.5-4.x container format in full,
# and that source's own licence explicitly permits this: "source code may be used in any
# software to handle RAR archives... without limitations free of charge." Both fixtures use the
# `store` method (method byte `0x30`) -- zero compression, just marker + main header + file
# header(s) + raw bytes + end-of-archive marker, each header's CRC16 computed the same way
# `RawRead::GetCRC15` does. Before being committed, both were cross-validated two ways: (1)
# against a real 7-Zip build with an actual RAR codec (unlike Alpine's 7zz, a desktop `7z`
# reads and extracts them, confirming the container bytes are spec-shaped, not just
# unrar-shaped) and (2) inside the actual built runtime and dev container images via
# `extract.extract_item`, not just this test file's own subprocess calls.
#
# `_RAR_SINGLE`: one file ("hello.txt" -> b"hello world\n"), no volumes.
_RAR_SINGLE = (
    b"Rar!\x1a\x07\x00\xcf\x90s\x00\x00\r\x00\x00\x00\x00\x00\x00\x00fit\x00\x00)\x00\x0c\x00"
    b"\x00\x00\x0c\x00\x00\x00\x00-;\x08\xaf\x00\x00!(\x140\t\x00 \x00\x00\x00hello.txthello "
    b"world\n\x04\xb0{\x00\x00\x07\x00"
)

# `_RAR_MULTIVOL_VOL1` / `_RAR_MULTIVOL_VOL2`: one file ("multi.txt" -> the 20 ASCII bytes
# b"0123456789abcdefghij"), old-style split at the 10-byte midpoint -- `<base>.rar` (volume 1,
# `LHD_SPLIT_AFTER`, `FileCRC` set to the RAR sentinel `0xFFFFFFFF` that tells `unrar` to skip
# the per-volume packed-data hash check rather than compute one for an arbitrary byte split) and
# `<base>.r00` (volume 2, `LHD_SPLIT_BEFORE`, `FileCRC` = the CRC32 of the complete 20-byte
# file, which is what `unrar` actually validates against once the last volume is read). This is
# the exact naming convention `_rar_volume_number` above already assumes for old-style sets.
_RAR_MULTIVOL_VOL1 = (
    b"Rar!\x1a\x07\x00\xb2\xefs\x01\x01\r\x00\x00\x00\x00\x00\x00\x003;t\x02\x00)\x00\n\x00"
    b"\x00\x00\x14\x00\x00\x00\x00\xff\xff\xff\xff\x00\x00!(\x140\t\x00 \x00\x00\x00multi.txt"
    b"0123456789a\xd7{\x01\x00\x07\x00"
)
_RAR_MULTIVOL_VOL2 = (
    b"Rar!\x1a\x07\x00\xf1\xfbs\x01\x00\r\x00\x00\x00\x00\x00\x00\x00\x16\x8ct\x01\x00)\x00\n\x00"
    b"\x00\x00\x14\x00\x00\x00\x00)\r\x8cc\x00\x00!(\x140\t\x00 \x00\x00\x00multi.txtabcdefghij"
    b"\x04\xb0{\x00\x00\x07\x00"
)

_UNRAR_BIN = os.environ.get("LFTPWEB_UNRAR_BIN") or shutil.which("unrar")
pytestmark_unrar = pytest.mark.skipif(
    _UNRAR_BIN is None,
    reason="no unrar binary on PATH -- build it (docker/Dockerfile's unrar-builder stage) or "
    "set LFTPWEB_UNRAR_BIN",
)


@pytestmark_unrar
def test_unrar_binary_reports_rar_decode_capability(tmp_path):
    """Layer 1 of the regression guard (2026-08-12, docs/decisions.md): fails if the resolved
    `unrar` binary cannot actually parse a RAR archive's own headers -- catching, for example,
    a build that silently produced a non-functional binary (missing libstdc++ at runtime is
    exactly the failure this project hit while developing this fix, before `-static-libstdc++`
    was added to `docker/Dockerfile`'s unrar-builder stage). Deliberately does not assert
    anything about a package name in the Dockerfile, which proves nothing about what actually
    got built -- it greps the decoder's own listing output for the file the fixture contains,
    the RAR analogue of `7zz i`'s format list.
    """
    archive = tmp_path / "single.rar"
    archive.write_bytes(_RAR_SINGLE)

    result = subprocess.run(
        [_UNRAR_BIN, "l", "-p-", str(archive)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hello.txt" in result.stdout


@pytestmark_unrar
def test_extract_real_rar_archive_single_volume(tmp_path):
    """Layer 2 of the regression guard: a genuine RAR archive, actually extracted through the
    full `extract_item` pipeline (staging, `unrar` invocation, merge into the final directory)
    -- not just a naming/precondition check against fake bytes.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "release.rar").write_bytes(_RAR_SINGLE)

    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)

    assert result.ok, result.detail
    assert (item / "hello.txt").read_text() == "hello world\n"
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()


@pytestmark_unrar
def test_extract_real_rar_archive_multivolume_old_style(tmp_path):
    """The multi-volume upgrade this task calls for by name: `check_extract_preconditions`
    already has real-filesystem tests for old-style `.rar`+`.r00` completeness (naming and gap
    detection only, above) -- this is the decode-level counterpart, an actual two-volume RAR set
    handed to `unrar` and extracted end to end, reassembling both volumes' data into one file.
    """
    item = tmp_path / "Release"
    item.mkdir()
    (item / "release.rar").write_bytes(_RAR_MULTIVOL_VOL1)
    (item / "release.r00").write_bytes(_RAR_MULTIVOL_VOL2)

    result = extract.extract_item(item, rar_binary=_UNRAR_BIN)

    assert result.ok, result.detail
    assert (item / "multi.txt").read_text() == "0123456789abcdefghij"
    assert not (tmp_path / f"{extract.UNPACK_PREFIX}Release").exists()


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
    db,
    *,
    sync_mode: str,
    auto_verify=0,
    auto_extract=0,
    auto_move=0,
    auto_delete_archives=0,
    staging_path=None,
):
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('seedbox', 'seedbox.invalid', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, enabled, "
        "sync_mode, auto_verify, auto_extract, auto_move, auto_delete_archives) "
        "VALUES (?, 'q', '/data/pickup', ?, ?, 1, ?, ?, ?, ?, ?)",
        (
            host_id,
            "/local",
            staging_path,
            sync_mode,
            auto_verify,
            auto_extract,
            auto_move,
            auto_delete_archives,
        ),
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


async def test_move_mode_deletes_remote_on_skipped_verification_completeness_evidence_only(
    tmp_path,
):
    """(2026-08-14, prompts/done/2026-08-14-skipped-verification-must-not-withhold-the-move-delete.md)
    `SKIPPED` -- no `.sfv`/`.md5` sidecar and hash-on-disk verification disabled, "no evidence
    either way" -- must **not** withhold a `move`-mode delete: we require verification to pass
    where it applies, not that it ran. The delete proceeds on the completeness evidence the item
    already cleared to get here (lftp exit 0, the settle gate, the filesystem completeness
    check), and the event message says so rather than reading like a checksum-backed delete.
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
        # Everything off; move forces verify on, but no sidecar and hash-on-disk fallback
        # disabled means it comes back SKIPPED, not VERIFIED.
        settings = postprocess.PostprocessSettings()
        await pipeline.process_item(item_id, settings)

        assert len(pool.calls) == 1, "the delete must proceed on a SKIPPED verification"
        _, remote_path = pool.calls[0]
        assert remote_path == f"/data/pickup/{rel_path}"

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["remote_deleted_at"] is not None

        events = await (await db.execute("SELECT kind, level, message FROM event")).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete" in kinds
        assert "remote_delete_withheld" not in kinds
        delete_event = next(e for e in events if e["kind"] == "remote_delete")
        assert delete_event["level"] == "warning"
        assert "completeness evidence alone" in delete_event["message"]
        assert "no .sfv/.md5 sidecar" in delete_event["message"]
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

        events = await (await db.execute("SELECT kind, level, message FROM event")).fetchall()
        assert "remote_delete" in [e["kind"] for e in events]
        delete_event = next(e for e in events if e["kind"] == "remote_delete")
        # Checksum-backed wording, unchanged by this task -- only the SKIPPED path gets the
        # new "completeness evidence alone" phrasing.
        assert delete_event["level"] == "info"
        assert "deleted verified remote copy" in delete_event["message"]
        assert "completeness evidence" not in delete_event["message"]
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

        events = await (await db.execute("SELECT kind, message FROM event")).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete_withheld" in kinds
        assert "remote_delete" not in kinds
        withheld = next(e for e in events if e["kind"] == "remote_delete_withheld")
        assert "CORRUPT" in withheld["message"]
    finally:
        await db.close()


# --- The move-delete ladder (2026-08-16, prompts/done/2026-08-16-move-delete-gate-ladder.md,
# resolving open issue #2 / docs/audit-v0.1.0.md G1): the delete now waits on extraction and,
# for an *arr-tracked item, on *arr import too -- these tests are the point of that task. -------


@pytestmark_unrar
async def test_move_mode_extraction_failure_defers_delete_not_withholds(tmp_path):
    """Rung 3: an archive release whose extraction fails must not have already lost its remote
    copy -- the exact bug this task closes (before it, the delete ran between verify and
    extract, so this state was unreachable with the remote copy still present). The deferral is
    named (`remote_delete_deferred`, not `remote_delete_withheld`), and fixing the archive set
    and letting the pipeline re-run delivers the delete -- there is no automatic retry.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        rel_path = "Release"
        item_dir = local_root / rel_path
        item_dir.mkdir()
        (item_dir / "release.rar").write_bytes(_RAR_MULTIVOL_VOL1)
        # release.r00 (the second, completing volume) intentionally withheld -- the set is
        # incomplete, so extraction must fail. No sidecar, so verify comes back SKIPPED, not
        # CORRUPT -- this test is specifically about the extraction rung, not the verify one.
        # `check_rar_volume_set`'s own docstring: a wholly-absent *final* volume can't be
        # detected from filenames alone, so this fails as a genuine `unrar` extraction error,
        # not the precondition check -- still a real `EXTRACT_FAILED`, which is all this test
        # needs.

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path)
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings(extract_enabled=True))

        assert pool.calls == [], "extraction failed -- the source must not be deleted"
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACT_FAILED"
        assert item["remote_deleted_at"] is None
        assert item["remote_delete_pending"] is None

        events = await (
            await db.execute("SELECT kind, level, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete_deferred" in kinds
        assert "remote_delete_withheld" not in kinds
        assert "remote_delete" not in kinds
        deferred = next(e for e in events if e["kind"] == "remote_delete_deferred")
        assert deferred["level"] == "warning"
        assert "awaiting extraction" in deferred["message"]

        # Fix the archive set (add the completing volume) and let the pipeline re-run (a fresh
        # DOWNLOADED, the same shape a re-queue produces) -- the delete fires now that rung 3
        # clears.
        (item_dir / "release.r00").write_bytes(_RAR_MULTIVOL_VOL2)
        await db.execute("UPDATE item SET state = 'DOWNLOADED' WHERE id = ?", (item_id,))
        await db.commit()
        await pipeline.process_item(item_id, postprocess.PostprocessSettings(extract_enabled=True))

        assert len(pool.calls) == 1
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED"
        assert item["remote_deleted_at"] is not None
        assert item["remote_delete_pending"] is None
    finally:
        await db.close()


async def test_move_mode_arr_tracked_item_defers_delete_to_arrsync(tmp_path):
    """Rung 4: an item that is already *arr-tracked (`arr_status` non-null) by the time the
    pipeline's delete gate runs must not have its source deleted here at all -- rungs 1-3 having
    cleared hands the decision to `core/arrsync.py` instead (`item.remote_delete_pending`
    records the handoff, carrying the verify evidence forward).
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "Show.S01E01.mkv"
        content = b"an episode, already matched by the bound *arr instance"
        (local_root / rel_path).write_bytes(content)

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=len(content))
        # The poller runs on its own clock and matched this item before postprocess got here.
        await db.execute(
            "UPDATE item SET arr_status = 'detected', arr_status_at = ? WHERE id = ?",
            ("2026-08-16T00:00:00.000000Z", item_id),
        )
        await db.commit()

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        settings = postprocess.PostprocessSettings(verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        assert pool.calls == [], "an *arr-tracked item must not delete here -- arrsync owns it"
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "VERIFIED"
        assert item["remote_deleted_at"] is None
        assert item["remote_delete_pending"] == "VERIFIED"

        events = await (
            await db.execute("SELECT kind, level, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete_deferred" in kinds
        assert "remote_delete" not in kinds
        deferred = next(e for e in events if e["kind"] == "remote_delete_deferred")
        assert deferred["level"] == "info"
        assert "awaiting *arr import" in deferred["message"]
    finally:
        await db.close()


async def test_move_mode_unmatched_item_on_a_bound_queue_still_deletes_at_rung_3(tmp_path):
    """The gate keys on `item.arr_status`, never on whether the *queue* is bound
    (`arr_instance_id`) -- a queue bound to a real instance can still contain an item the
    instance never heard of (a hand-dropped file, a replaced grab), and that item must not wait
    forever on an *arr that will never match it.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        rel_path = "loose.txt"
        content = b"never matched by the bound *arr instance"
        (local_root / rel_path).write_bytes(content)

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        cursor = await db.execute(
            "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
            "notify_on_complete, created_at, updated_at) VALUES "
            "('Sonarr', 'sonarr', 'http://arr.invalid', 'enc', 1, 0, "
            "'2026-08-16T00:00:00.000000Z', '2026-08-16T00:00:00.000000Z')"
        )
        instance_id = cursor.lastrowid
        await db.execute(
            "UPDATE path_queue SET arr_instance_id = ? WHERE id = ?", (instance_id, queue_id)
        )
        await db.commit()
        # arr_status left NULL -- this item was never matched by the bound instance.
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=len(content))

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        settings = postprocess.PostprocessSettings(verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        assert len(pool.calls) == 1, "an unmatched item must delete at rung 3, not wait"
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["remote_deleted_at"] is not None
        assert item["remote_delete_pending"] is None
    finally:
        await db.close()


async def test_move_mode_corrupt_vetoes_even_when_the_item_is_arr_tracked(tmp_path):
    """CORRUPT is a hard veto at every rung -- including rung 4. An *arr-tracked item that
    verifies CORRUPT must be withheld outright, never deferred to `core/arrsync.py`: a stale
    `remote_delete_pending` would let a later confirmed `imported` transition delete a copy this
    same run just found reason to keep.
    """
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
        await db.execute(
            "UPDATE item SET arr_status = 'detected', arr_status_at = ? WHERE id = ?",
            ("2026-08-16T00:00:00.000000Z", item_id),
        )
        await db.commit()

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert pool.calls == []
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "CORRUPT"
        assert item["remote_deleted_at"] is None
        assert item["remote_delete_pending"] is None

        events = await (
            await db.execute("SELECT kind FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "remote_delete_withheld" in kinds
        assert "remote_delete_deferred" not in kinds
    finally:
        await db.close()


async def test_move_mode_deferred_item_is_scan_stable_while_both_copies_exist(tmp_path):
    """An item deferred at rung 4 keeps its remote copy -- both trees genuinely have content --
    so a real scan pass must read it exactly like any other complete `move`-mode item, not flap
    it toward `PARTIAL`/`DOWNLOADED` or start the absence-grace clock. Three passes in a row, the
    same "not just the first" discipline other scan-stability tests in this file use.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        rel_path = "Show.S01E01.mkv"
        content = b"an episode, already matched by the bound *arr instance"
        (local_root / rel_path).write_bytes(content)

        host_id, queue_id = await _make_host_and_queue_rows(db, sync_mode="move")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, rel_path, remote_size=len(content))
        await db.execute(
            "UPDATE item SET arr_status = 'detected', arr_status_at = ? WHERE id = ?",
            ("2026-08-16T00:00:00.000000Z", item_id),
        )
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "VERIFIED", "setup did not actually verify -- test is void"
        assert item["remote_delete_pending"] == "VERIFIED"
        assert item["remote_deleted_at"] is None

        remote_tree = {
            rel_path: RemoteEntry(rel_path=rel_path, is_dir=False, size=len(content), mtime=1.0),
        }
        engine = Engine(db, str(tmp_path), EventBus())
        engine.pool = _FakeScanPool(remote_tree)
        q, host = _queue_and_host(host_id, queue_id, local_root)

        for _ in range(3):
            await engine.scan_queue(q, host)
            row = await (
                await db.execute(
                    "SELECT state, first_missing_at, remote_delete_pending, remote_deleted_at "
                    "FROM item WHERE id = ?",
                    (item_id,),
                )
            ).fetchone()
            assert row["state"] == "VERIFIED", "must not flap while both copies genuinely exist"
            assert row["state"] not in ELIGIBLE_STATES, "must never become auto-queue eligible"
            assert row["first_missing_at"] is None, "content is not absent, nothing should start"
            assert row["remote_delete_pending"] == "VERIFIED", "scan must not touch the handoff"
            assert row["remote_deleted_at"] is None
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
        # Without `remote_deleted_at`, LOCAL_ONLY is not covered at all -- a genuinely
        # never-tracked local file must not be mistaken for a move-mode item whose remote copy
        # this codebase deleted on purpose.
        ("VERIFIED", "LOCAL_ONLY", False),
        ("EXTRACTED", "LOCAL_ONLY", False),
    ],
)
def test_outcome_survives_rescan(prev_state, structural_state, expected):
    assert postprocess.outcome_survives_rescan(prev_state, structural_state) is expected


@pytest.mark.parametrize(
    ("prev_state", "structural_state", "expected"),
    [
        # 2026-08-13 (prompts/2026-08-13-move-mode-outcome-survives-local-only.md): a
        # move-mode item's own remote copy, once this codebase deletes it, reads exactly like a
        # never-tracked local file to core/reconcile.py -- REMOTE_ONLY. `remote_deleted_at` is
        # what tells the two apart, and only while the bytes are still all here (LOCAL_ONLY).
        ("VERIFIED", "LOCAL_ONLY", True),
        ("CORRUPT", "LOCAL_ONLY", True),
        ("EXTRACTED", "LOCAL_ONLY", True),
        ("EXTRACT_FAILED", "LOCAL_ONLY", True),
        # PARTIAL still beats the outcome even with the remote copy gone -- rule 2 is absolute
        # regardless of *why* the byte comparison fell short.
        ("VERIFIED", "PARTIAL", False),
        ("EXTRACTED", "PARTIAL", False),
        # DOWNLOADED already wins unconditionally; a real DOWNLOADED reading with
        # remote_deleted_at set is not this codebase's normal shape (the delete only fires once
        # the item already left DOWNLOADED for an outcome) but must not regress either way.
        ("VERIFIED", "DOWNLOADED", True),
        # A state this module doesn't own is still none of its business.
        ("DOWNLOADED", "LOCAL_ONLY", False),
        (None, "LOCAL_ONLY", False),
    ],
)
def test_outcome_survives_rescan_local_only_with_remote_deleted(
    prev_state, structural_state, expected
):
    assert (
        postprocess.outcome_survives_rescan(
            prev_state, structural_state, remote_deleted_at="2026-08-13T00:00:00.000000Z"
        )
        is expected
    )


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


# --- Delete archives after extract (2026-08-13,
# prompts/2026-08-13-delete-archives-after-extract.md) -----------------------------------------
#
# **The trap this whole feature exists to avoid**, restated because it is the point of every
# test below: deleting a release's archive volumes after extraction drops its local byte total
# below remote. The very next scan (`core/reconcile.py`) would read `local < remote` -> `PARTIAL`
# (DESIGN.md §3.2 rule 2), and rule 9 / `outcome_survives_rescan` says `PARTIAL` beats any
# post-processing outcome -- so `EXTRACTED` would not protect the item and auto-queue would
# re-fetch, re-extract, and re-delete it every scan interval, forever. This is the same shape as
# the `REMOVED_LOCAL` bug shipped and reverted the same night in `6d3bd95`
# (`prompts/open-issues.md` "4"). `test_archive_cleanup_does_not_cause_a_partial_re_download_loop`
# below is the regression guard for exactly that; everything above it is unit-level scaffolding
# building up to it, using the same real RAR fixtures (`_RAR_SINGLE`, `_RAR_MULTIVOL_VOL1/2`)
# defined earlier in this file, never fake bytes -- fake fixtures are why a nine-phase bug
# (rar extraction never working at all) went unnoticed.


def _sfv_bytes() -> bytes:
    """Sidecar content is never read by these tests (verify is off except in the move-mode
    test, which uses the hash-on-disk fallback instead) -- only that the file survives
    cleanup untouched.
    """
    return b"multi.txt 00000000\n"


async def _make_multivolume_rar_release(local_root: Path, rel_path: str = "Release") -> Path:
    """A directory item holding a real two-volume old-style rar set (`_RAR_MULTIVOL_VOL1` /
    `_RAR_MULTIVOL_VOL2`, defined earlier in this file) plus a `.sfv` sidecar -- the shape
    `delete_extracted_archives` must handle: remove both volumes, leave the sidecar alone.
    """
    item_dir = local_root / rel_path
    item_dir.mkdir(parents=True)
    (item_dir / "release.rar").write_bytes(_RAR_MULTIVOL_VOL1)
    (item_dir / "release.r00").write_bytes(_RAR_MULTIVOL_VOL2)
    (item_dir / "checksums.sfv").write_bytes(_sfv_bytes())
    return item_dir


# --- core/extract.py.archive_volume_paths ------------------------------------------------------


@pytestmark_7z
def test_archive_volume_paths_non_rar_format_is_just_the_head(tmp_path):
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("inner.txt", "contents")
    assert extract.archive_volume_paths(archive) == [archive]


def test_archive_volume_paths_expands_old_style_multivolume_rar(tmp_path):
    head = tmp_path / "release.rar"
    head.write_bytes(_RAR_MULTIVOL_VOL1)
    vol2 = tmp_path / "release.r00"
    vol2.write_bytes(_RAR_MULTIVOL_VOL2)
    (tmp_path / "checksums.sfv").write_bytes(_sfv_bytes())  # must never be picked up

    assert extract.archive_volume_paths(head) == [head, vol2]


def test_archive_volume_paths_expands_new_style_partn_rar(tmp_path):
    head = tmp_path / "release.part1.rar"
    head.write_bytes(b"volume 1")
    part2 = tmp_path / "release.part2.rar"
    part2.write_bytes(b"volume 2")
    part3 = tmp_path / "release.part3.rar"
    part3.write_bytes(b"volume 3")

    assert extract.archive_volume_paths(head) == [head, part2, part3]


def test_archive_volume_paths_single_volume_rar_is_just_the_head(tmp_path):
    head = tmp_path / "release.rar"
    head.write_bytes(_RAR_SINGLE)
    assert extract.archive_volume_paths(head) == [head]


# --- core/extract.py.is_archive_member (fix, 2026-08-15, docs/decisions.md) -------------------
# Reused by `core/verify.py` so a leftover archive volume never counts as the non-sidecar
# content that makes "every sidecar entry absent" read as an upstream extraction.


def test_is_archive_member_true_for_a_rar_head():
    assert extract.is_archive_member(Path("release.rar")) is True


def test_is_archive_member_true_for_an_old_style_continuation_volume():
    assert extract.is_archive_member(Path("release.r00")) is True


def test_is_archive_member_true_for_a_new_style_part_volume():
    assert extract.is_archive_member(Path("release.part2.rar")) is True


def test_is_archive_member_true_for_simple_and_compound_suffixes():
    assert extract.is_archive_member(Path("payload.zip")) is True
    assert extract.is_archive_member(Path("payload.tar.gz")) is True


def test_is_archive_member_false_for_ordinary_content():
    assert extract.is_archive_member(Path("movie.mkv")) is False
    assert extract.is_archive_member(Path("readme.nfo")) is False


# --- core/engine.py.build_scan_counts_predicate -- pure composition, no DB/filesystem ----------


def test_build_scan_counts_predicate_excludes_a_deleted_archive_path():
    entry = RemoteEntry(rel_path="Release/release.rar", is_dir=False, size=78, mtime=1.0)
    predicate = build_scan_counts_predicate(
        lambda rel_path, e: True, frozenset({"Release/release.rar"})
    )  # noqa: E501
    assert predicate("Release/release.rar", entry) is False


def test_build_scan_counts_predicate_still_defers_to_the_pattern_predicate():
    entry = RemoteEntry(rel_path="Release/notes.nfo", is_dir=False, size=5, mtime=1.0)
    predicate = build_scan_counts_predicate(lambda rel_path, e: False, frozenset())
    assert predicate("Release/notes.nfo", entry) is False


def test_build_scan_counts_predicate_true_only_when_both_sources_say_so():
    entry = RemoteEntry(rel_path="Release/movie.mkv", is_dir=False, size=1000, mtime=1.0)
    predicate = build_scan_counts_predicate(lambda rel_path, e: True, frozenset())
    assert predicate("Release/movie.mkv", entry) is True


# --- core/local_delete.py.delete_extracted_archives, through the real pipeline -----------------


@pytestmark_unrar
async def test_pipeline_deletes_every_archive_volume_after_success_and_preserves_sidecar(tmp_path):
    """The feature's own success path: a full multi-volume set -- head *and* the `.r00`
    continuation volume `find_archives` never returns -- through the real pipeline with a real
    `unrar`, not just `core/extract.py` in isolation.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = await _make_multivolume_rar_release(local_root)

        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="copy", auto_extract=1, auto_delete_archives=1
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED")
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, delete_archives_after_extract=True
        )
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED", item["error_detail"]

        assert not (item_dir / "release.rar").exists()
        assert not (item_dir / "release.r00").exists()
        assert (item_dir / "checksums.sfv").exists(), "sidecars must survive cleanup"
        assert (item_dir / "multi.txt").read_text() == "0123456789abcdefghij"

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == {"Release/release.rar", "Release/release.r00"}

        events = await (
            await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        cleanup_events = [e for e in events if e["kind"] == "archive_cleanup"]
        assert len(cleanup_events) == 1
        assert "release.rar" in cleanup_events[0]["message"]
        assert "release.r00" in cleanup_events[0]["message"]
        assert "archive_cleanup_withheld" not in kinds
    finally:
        await db.close()


@pytestmark_unrar
async def test_pipeline_and_gating_all_six_inherit_and_override_combinations(tmp_path):
    """Item 1's own required test, updated for inherit-or-override (2026-08-13,
    `prompts/2026-08-13-postprocess-inherit-or-override.md`): archive cleanup (migration 012)
    used to be ANDed across the site-wide flag and the queue's own `auto_delete_archives` --
    this asserted only that shape. The AND is gone: `auto_delete_archives=None` now inherits
    the site-wide flag wherever it moves, and an explicit override (`True`/`False`) wins
    regardless of the site-wide flag in *either* direction -- including the one combination the
    old AND could never produce, `site=False, queue=True` (an explicit per-queue "yes, this one
    anyway" now actually deletes, where the AND silently withheld it). Six combinations, not
    four: site on/off crossed with queue inherit/override-on/override-off.
    """
    for site_on, queue_value, expect_deleted in (
        (True, True, True),  # override on, site on -> on
        (True, False, False),  # override off, site on -> off (override wins)
        (True, None, True),  # inherit, site on -> on
        (False, True, True),  # override on, site off -> on (override wins, unlike the old AND)
        (False, False, False),  # override off, site off -> off
        (False, None, False),  # inherit, site off -> off
    ):
        db = await _make_db()
        try:
            local_root = tmp_path / f"local-{site_on}-{queue_value}"
            local_root.mkdir()
            write_if_needed(str(local_root))
            item_dir = await _make_multivolume_rar_release(local_root)

            queue_column_value = None if queue_value is None else int(queue_value)
            _, queue_id = await _make_host_and_queue_rows(
                db, sync_mode="copy", auto_extract=1, auto_delete_archives=queue_column_value
            )
            await db.execute(
                "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
            )
            await db.commit()
            item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED")
            await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
            await db.commit()

            pipeline = postprocess.PostprocessPipeline(
                db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
            )
            settings = postprocess.PostprocessSettings(
                extract_enabled=True, delete_archives_after_extract=site_on
            )
            await pipeline.process_item(item_id, settings)

            item = await (
                await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
            ).fetchone()
            assert item["state"] == "EXTRACTED", (site_on, queue_value, item["error_detail"])

            still_there = (item_dir / "release.rar").exists()
            assert still_there == (not expect_deleted), (site_on, queue_value)

            deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
            assert bool(deleted) == expect_deleted, (site_on, queue_value)
        finally:
            await db.close()


def test_effective_resolves_inherit_from_site_and_ignores_override_for_explicit_values():
    """`core/postprocess.py._effective`'s own contract (2026-08-13,
    `prompts/2026-08-13-postprocess-inherit-or-override.md`): a `None` queue value tracks the
    site-wide value wherever it moves; an explicit `0`/`1` never does, in either direction.
    """
    assert postprocess._effective(None, True) is True
    assert postprocess._effective(None, False) is False
    assert postprocess._effective(1, False) is True  # override wins over an off site
    assert postprocess._effective(0, True) is False  # override wins over an on site


@pytestmark_unrar
async def test_pipeline_default_off_leaves_archives_on_disk(tmp_path):
    """Default off, non-negotiable (this project's own rule for anything that deletes) --
    extraction succeeds but nothing about the archives changes when the setting is left at its
    default.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = await _make_multivolume_rar_release(local_root)

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED")
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        # delete_archives_after_extract left at its default (False).
        await pipeline.process_item(item_id, postprocess.PostprocessSettings(extract_enabled=True))

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED"
        assert (item_dir / "release.rar").exists()
        assert (item_dir / "release.r00").exists()

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == frozenset()

        events = await (await db.execute("SELECT kind FROM event")).fetchall()
        assert "archive_cleanup" not in [e["kind"] for e in events]
    finally:
        await db.close()


async def test_pipeline_never_deletes_archives_on_extract_failed(tmp_path):
    """Never on a precondition failure (a gap in the volume set, here) -- the cleanup call is
    never even reached, because `_do_extract` only calls it when `result.state == 'EXTRACTED'`.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = local_root / "Release"
        item_dir.mkdir()
        (item_dir / "Release.rar").write_bytes(b"volume 1")
        (item_dir / "Release.r00").write_bytes(b"volume 2")
        # Release.r01 (volume 3) missing -- an incomplete set, same fixture shape as the
        # existing precondition-failure test above.
        (item_dir / "Release.r02").write_bytes(b"volume 4")

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, "Release")
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, delete_archives_after_extract=True
        )
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACT_FAILED"
        assert (item_dir / "Release.rar").exists()
        assert (item_dir / "Release.r00").exists()
        assert (item_dir / "Release.r02").exists()

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == frozenset()

        events = await (await db.execute("SELECT kind FROM event")).fetchall()
        kinds = [e["kind"] for e in events]
        assert "archive_cleanup" not in kinds
        assert "archive_cleanup_withheld" not in kinds
    finally:
        await db.close()


@pytestmark_unrar
async def test_pipeline_never_deletes_a_loose_top_level_archive_file(tmp_path):
    """DESIGN.md §4.7's loose top-level file case, and the reason `delete_extracted_archives`
    withholds it outright rather than deleting: removing the item's own single file *is*
    removing the whole item (`core/local_delete.py.delete_local`'s job, not this one's), and the
    directory-only vacuous-`DOWNLOADED` branch this feature otherwise relies on
    (`core/reconcile.py`'s `relevant == 0` split) does not exist at the file level -- excluding
    it would read `EXCLUDED`, not the outcome-preserving `DOWNLOADED`, and drop `EXTRACTED`.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        (local_root / "release.rar").write_bytes(_RAR_SINGLE)

        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="copy", auto_extract=1, auto_delete_archives=1
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        # is_dir defaults to 0 in _make_item_row -- exactly the loose-file case.
        item_id = await _make_item_row(db, queue_id, "release.rar")

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, delete_archives_after_extract=True
        )
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED"
        assert (local_root / "release.rar").exists(), "the item's own archive must survive"
        assert (local_root / "hello.txt").read_text() == "hello world\n"

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == frozenset()

        events = await (
            await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        withheld = [e for e in events if e["kind"] == "archive_cleanup_withheld"]
        assert len(withheld) == 1
        assert "loose top-level file" in withheld[0]["message"]
    finally:
        await db.close()


@pytestmark_unrar
async def test_move_mode_archive_cleanup_runs_before_the_ladders_own_delete(tmp_path):
    """§5's own required check, updated for the ladder (2026-08-16,
    prompts/done/2026-08-16-move-delete-gate-ladder.md): on a `move` queue the remote delete is
    now the pipeline's *last* step, so extraction -- and this feature's own archive cleanup,
    which rides `_do_extract`'s success path -- runs *before* the remote copy is gone, not after
    it as before this task. Archive cleanup is still deliberately not gated on the remote delete
    (see docs/decisions.md): a successful extraction has already decoded the payload onto disk
    as ordinary files, so the archive volumes are a spent intermediate nobody re-reads, not the
    release itself -- true regardless of which order the two deletions happen in.
    """
    db = await _make_db()
    try:
        import zlib

        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = local_root / "Release"
        item_dir.mkdir()
        (item_dir / "release.rar").write_bytes(_RAR_MULTIVOL_VOL1)
        (item_dir / "release.r00").write_bytes(_RAR_MULTIVOL_VOL2)
        # A *real* sfv, checksumming the two archive volumes that actually exist at verify
        # time (verify runs before extraction) -- not `_sfv_bytes()`'s placeholder, which
        # references "multi.txt" (the file the rar contains, not yet extracted) and would read
        # CORRUPT ("missing") if used here.
        rar_crc = zlib.crc32(_RAR_MULTIVOL_VOL1) & 0xFFFFFFFF
        r00_crc = zlib.crc32(_RAR_MULTIVOL_VOL2) & 0xFFFFFFFF
        sfv_content = f"release.rar {rar_crc:08x}\nrelease.r00 {r00_crc:08x}\n".encode()
        (item_dir / "checksums.sfv").write_bytes(sfv_content)
        total_bytes = len(_RAR_MULTIVOL_VOL1) + len(_RAR_MULTIVOL_VOL2) + len(sfv_content)

        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="move", auto_extract=1, auto_delete_archives=1
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(
            db, queue_id, "Release", state="DOWNLOADED", remote_size=total_bytes
        )
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pool = _FakeRemotePool()
        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=pool, host_provider=lambda: _async_host()
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, delete_archives_after_extract=True
        )
        await pipeline.process_item(item_id, settings)

        # The remote delete happened (move mode's own gate, unrelated to this feature)...
        assert len(pool.calls) == 1
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["remote_deleted_at"] is not None
        # ...and cleanup still ran, this time *before* it rather than after.
        assert item["state"] == "EXTRACTED"
        assert not (item_dir / "release.rar").exists()
        assert not (item_dir / "release.r00").exists()
        assert (item_dir / "checksums.sfv").exists()

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == {"Release/release.rar", "Release/release.r00"}

        events = await (
            await db.execute("SELECT kind FROM event WHERE item_id = ? ORDER BY id", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert kinds.index("archive_cleanup") < kinds.index(
            "remote_delete"
        ), "the ladder's own delete must be the pipeline's last step, after archive cleanup"
    finally:
        await db.close()


@pytestmark_unrar
async def test_cleanup_composes_with_the_relocate_step_leaving_no_orphans(tmp_path):
    """§6's own required check: `_do_move` relocates the item to `staging_path` *after*
    `_do_extract` (which now includes cleanup) runs -- `_process_item`'s fixed order means
    cleanup always happens before relocation, never the reverse, so there is no ordering to
    reconcile. Prove the composition: the relocated directory holds the extracted content and
    the sidecar, never the archives, and the original location is empty.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = await _make_multivolume_rar_release(local_root)

        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        _, queue_id = await _make_host_and_queue_rows(
            db,
            sync_mode="copy",
            auto_extract=1,
            auto_move=1,
            auto_delete_archives=1,
            staging_path=str(staging_root),
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED")
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            extract_enabled=True, delete_archives_after_extract=True, move_enabled=True
        )
        await pipeline.process_item(item_id, settings)

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED"

        assert not item_dir.exists(), "the whole item should have relocated to staging"
        dest = staging_root / "Release"
        assert (dest / "multi.txt").read_text() == "0123456789abcdefghij"
        assert (dest / "checksums.sfv").exists()
        assert not (dest / "release.rar").exists()
        assert not (dest / "release.r00").exists()

        deleted = await local_delete.load_deleted_archive_paths(db, queue_id)
        assert deleted == {"Release/release.rar", "Release/release.r00"}

        events = await (
            await db.execute("SELECT kind FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        assert "move" in [e["kind"] for e in events]
    finally:
        await db.close()


# --- The regression guard: a real scan must never read PARTIAL, and must survive a restart -----


class _FakeScanPool:
    """A fixed remote tree, handed back on every scan -- the same shape
    `tests/test_state_persistence.py._FakePool` uses, so `core/engine.py.scan_queue` runs for
    real (real `local_scan.scan_local` against the actual, post-cleanup filesystem; real
    `core/engine.py._persist` arbitration) without a live SSH connection.
    """

    def __init__(self, tree: dict[str, RemoteEntry]) -> None:
        self._tree = tree

    async def scan(self, host, remote_path):  # noqa: ARG002
        return self._tree, None


async def _setup_extracted_and_cleaned_release(tmp_path):
    """Runs the real pipeline (real `unrar`) to produce an `EXTRACTED` item whose archive
    volumes have already been deleted from disk -- the starting point every test below needs.
    Returns `(db, host_id, queue_id, item_id, local_root, remote_tree)` so a caller can drive
    `Engine.scan_queue` against exactly the database and filesystem state a real run leaves
    behind, with `remote_tree` describing what a `copy`-mode queue's seedbox still has (nothing
    ever deletes remote content in `copy` mode, so the archive volumes are still there).
    """
    db = await _make_db()
    local_root = tmp_path / "local"
    local_root.mkdir()
    write_if_needed(str(local_root))
    await _make_multivolume_rar_release(local_root)
    sfv_size = len(_sfv_bytes())

    host_id, queue_id = await _make_host_and_queue_rows(
        db, sync_mode="copy", auto_extract=1, auto_delete_archives=1
    )
    await db.execute(
        "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
    )
    await db.commit()
    total = len(_RAR_MULTIVOL_VOL1) + len(_RAR_MULTIVOL_VOL2) + sfv_size
    item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED", remote_size=total)
    await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
    await db.commit()

    pipeline = postprocess.PostprocessPipeline(
        db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
    )
    settings = postprocess.PostprocessSettings(
        extract_enabled=True, delete_archives_after_extract=True
    )
    await pipeline.process_item(item_id, settings)

    remote_tree = {
        "Release": RemoteEntry(rel_path="Release", is_dir=True, size=0, mtime=1.0),
        "Release/release.rar": RemoteEntry(
            rel_path="Release/release.rar", is_dir=False, size=len(_RAR_MULTIVOL_VOL1), mtime=1.0
        ),
        "Release/release.r00": RemoteEntry(
            rel_path="Release/release.r00", is_dir=False, size=len(_RAR_MULTIVOL_VOL2), mtime=1.0
        ),
        "Release/checksums.sfv": RemoteEntry(
            rel_path="Release/checksums.sfv", is_dir=False, size=sfv_size, mtime=1.0
        ),
    }
    return db, host_id, queue_id, item_id, local_root, remote_tree


def _queue_and_host(
    host_id: int, queue_id: int, local_root: Path
) -> tuple[QueueConfig, HostConfig]:
    q = QueueConfig(
        id=queue_id,
        host_id=host_id,
        name="q",
        remote_path="/data/pickup",
        local_path=str(local_root),
        staging_path=None,
        enabled=True,
        sync_mode="copy",
    )
    host = HostConfig(
        id=host_id,
        address="seedbox.invalid",
        port=22,
        username="u",
        auth_method="key",
        key_path="/k",
        known_hosts_policy="strict",
    )
    return q, host


@pytestmark_unrar
async def test_archive_cleanup_does_not_cause_a_partial_re_download_loop(tmp_path):
    """**The regression test that is the point of this whole feature.** Without the
    `deleted_archive` bookkeeping and `core/engine.py.build_scan_counts_predicate` fix, this
    item would read `PARTIAL` on the very first real scan after cleanup (local now short of
    remote by the two deleted rar volumes), `EXTRACTED` would not survive
    (`outcome_survives_rescan` only protects a structural `DOWNLOADED`), and the item would
    become eligible for auto-queue again (`REMOTE_ONLY`/`PARTIAL` are the only
    `ELIGIBLE_STATES`) -- re-fetching, re-extracting, and re-deleting the same archives forever.
    """
    (
        db,
        host_id,
        queue_id,
        item_id,
        local_root,
        remote_tree,
    ) = await _setup_extracted_and_cleaned_release(tmp_path)
    try:
        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED", "setup did not actually extract -- test is void"

        engine = Engine(db, str(tmp_path), EventBus())
        engine.pool = _FakeScanPool(remote_tree)
        q, host = _queue_and_host(host_id, queue_id, local_root)

        # Not just one pass -- the failure mode is a *periodic* re-computation, so several
        # scans in a row must all agree (same discipline as
        # tests/test_state_persistence.py::test_an_outcome_survives_repeated_scans_not_just_the_first).
        for _ in range(3):
            await engine.scan_queue(q, host)
            row = await (
                await db.execute(
                    "SELECT state, first_missing_at FROM item WHERE id = ?", (item_id,)
                )
            ).fetchone()
            assert row["state"] == "EXTRACTED", "the archive-delete trap: must never read PARTIAL"
            assert row["state"] not in ELIGIBLE_STATES, "must never become auto-queue eligible"
            assert row["first_missing_at"] is None, "content is not absent, nothing should start"

        # The two archive-volume nodes are EXCLUDED, not REMOTE_ONLY -- a real state, not an
        # absence (DESIGN.md §3.2 rule 8), the same mechanism `file_exclude` already uses for a
        # different cause.
        for rel_path in ("Release/release.rar", "Release/release.r00"):
            row = await (
                await db.execute(
                    "SELECT state FROM item WHERE queue_id = ? AND rel_path = ?",
                    (queue_id, rel_path),
                )
            ).fetchone()
            assert row["state"] == "EXCLUDED", rel_path
    finally:
        await db.close()


@pytestmark_unrar
async def test_archive_cleanup_reaches_the_same_conclusion_after_a_simulated_restart(tmp_path):
    """Cold start: the reconciler must reach the same conclusion from only the database and the
    filesystem, not from anything the `PostprocessPipeline` run happened to hold in memory (it
    holds nothing relevant -- `deleted_archive` is the only thing that has to survive, and it is
    a table, not a Python object). A brand-new `Engine` -- nothing carried over, the same shape
    a process restart would produce with this same on-disk database -- must land on `EXTRACTED`
    on its very first scan, not need a warm-up pass.
    """
    (
        db,
        host_id,
        queue_id,
        item_id,
        local_root,
        remote_tree,
    ) = await _setup_extracted_and_cleaned_release(tmp_path)
    try:
        fresh_engine = Engine(db, str(tmp_path), EventBus())
        fresh_engine.pool = _FakeScanPool(remote_tree)
        q, host = _queue_and_host(host_id, queue_id, local_root)

        await fresh_engine.scan_queue(q, host)

        row = await (await db.execute("SELECT state FROM item WHERE id = ?", (item_id,))).fetchone()
        assert row["state"] == "EXTRACTED"
    finally:
        await db.close()


# --- The rename off "folder prefix during transfer" moved here as the pipeline's last step -----
# (2026-08-14, prompts/done/2026-08-14-rename-after-postprocessing-not-before.md, reversing
# `core/queue.py._reap_one`'s earlier "rename before postprocess.trigger" decision --
# docs/decisions.md has both entries.) `core/queue.py._reap_one` no longer touches the physical
# directory at all; it leaves `item.pending_download_prefix` set and calls `trigger()` with the
# item still sitting at `<local_path>/<prefix><name>/`. Everything below exercises
# `PostprocessPipeline` directly, the same way the rest of this file does -- `local_root` is
# resolved from `item.pending_download_prefix` via `core/local_delete.py._physical_local_root`,
# reused rather than re-derived (this task's own instruction).


async def _make_prefixed_dir_item(
    db, queue_id, rel_path, *, local_root_dir: Path, prefix: str = ".downloading-", remote_size
):
    """A directory item whose bytes physically live under `<prefix><rel_path>`, matching what
    `core/queue.py._reap_one` now hands to post-processing: `state='DOWNLOADED'`,
    `is_dir=1`, `pending_download_prefix` set. Returns `(item_id, prefixed_dir)`.
    """
    prefixed_dir = local_root_dir / f"{prefix}{rel_path}"
    item_id = await _make_item_row(db, queue_id, rel_path, remote_size=remote_size)
    await db.execute(
        "UPDATE item SET is_dir = 1, pending_download_prefix = ? WHERE id = ?",
        (prefix, item_id),
    )
    await db.commit()
    return item_id, prefixed_dir


async def test_prefix_renamed_off_only_after_verify_succeeds(tmp_path):
    """The single most important behaviour this task adds: nothing renames a still-unverified
    release, and the rename *does* happen once verification comes back clean -- proving the
    pipeline resolves the physical (prefixed) path for every step, not just the last one.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        content = b"a real, complete release"
        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_verify=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(content)
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(verify_enabled=True, verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        final_dir = local_dir / rel_path
        assert final_dir.is_dir(), "verified -> renamed to its real name"
        assert (final_dir / "a.mkv").read_bytes() == content
        assert not prefixed_dir.exists()

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "VERIFIED"
        assert item["pending_download_prefix"] is None

        events = await (
            await db.execute(
                "SELECT kind, message FROM event WHERE item_id = ? AND kind = 'download_prefix_removed'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1
        assert str(prefixed_dir) in events[0]["message"]
        assert str(final_dir) in events[0]["message"]
    finally:
        await db.close()


async def test_rename_message_reflects_skipped_verify_and_no_archives_truthfully(tmp_path):
    """(2026-08-14, prompts/done/2026-08-14-skipped-verification-must-not-withhold-the-move-delete.md)
    The `download_prefix_removed` event used to hardcode "downloaded, verified, and extracted"
    regardless of what actually happened. Found live: a `move`-mode release with no `.sfv`/`.md5`
    sidecar (verify -> `SKIPPED`) and no archives to unpack (extract -> nothing to do) got that
    exact sentence in the same second its own `verify`/`extract` events said otherwise. The
    message must name the real outcome of each step, not a fixed claim of success.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        content = b"a loose file with no sidecar and nothing to extract"
        # sync_mode="move" forces verify_effective on regardless of the queue's own
        # auto_verify column (DESIGN.md §7.3) -- and with no .sfv/.md5 sidecar and
        # verify_hash_on_disk left at its off default, that verification comes back
        # SKIPPED, not VERIFIED.
        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="move", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(content)
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(extract_enabled=True)
        await pipeline.process_item(item_id, settings)

        final_dir = local_dir / rel_path
        assert final_dir.is_dir(), "SKIPPED verify and no-archives extract are not failures"
        assert not prefixed_dir.exists()

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "DOWNLOADED"  # SKIPPED verify never claims VERIFIED

        events = await (
            await db.execute(
                "SELECT kind, message FROM event WHERE item_id = ? AND kind = 'verify'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1 and events[0]["message"].startswith("SKIPPED")

        extract_events = await (
            await db.execute(
                "SELECT message FROM event WHERE item_id = ? AND kind = 'extract'", (item_id,)
            )
        ).fetchall()
        assert len(extract_events) == 1
        assert "no archives" in extract_events[0]["message"]

        rename_events = await (
            await db.execute(
                "SELECT message FROM event WHERE item_id = ? AND kind = 'download_prefix_removed'",
                (item_id,),
            )
        ).fetchall()
        assert len(rename_events) == 1
        message = rename_events[0]["message"]
        # The old hardcoded claim must be gone, and the real per-step outcomes present instead.
        assert "downloaded, verified, and extracted" not in message
        assert "verify='SKIPPED'" in message
        assert "extract=None" in message
    finally:
        await db.close()


async def test_corrupt_item_is_never_renamed_off_the_prefix(tmp_path):
    """The prompt's own recommendation, verified: an importer that skips the hidden folder must
    never find a `CORRUPT` release under its real name either -- bytes stay hidden.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        content = b"short"
        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_verify=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        # remote_size deliberately larger than what's on disk -> hash-on-disk fallback reports
        # CORRUPT (verify.py: total_read < expected_total_bytes).
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(content) + 1000
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(verify_enabled=True, verify_hash_on_disk=True)
        await pipeline.process_item(item_id, settings)

        assert prefixed_dir.is_dir(), "bytes must stay under the prefixed directory"
        assert not (local_dir / rel_path).exists(), "must never appear under its real name"

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "CORRUPT"
        assert item["pending_download_prefix"] == ".downloading-"

        events = await (
            await db.execute(
                "SELECT kind, message FROM event WHERE item_id = ? "
                "AND kind = 'download_prefix_rename_withheld'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1
        assert "CORRUPT" in events[0]["message"]
    finally:
        await db.close()


async def test_extract_failed_item_is_never_renamed_off_the_prefix(tmp_path):
    """Same guarantee as the `CORRUPT` case, for the other failure mode this task's `release_ok`
    gate covers -- a precondition failure (missing rar volume) discovered by extraction.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=100
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "Release.rar").write_bytes(b"volume 1")
        (prefixed_dir / "Release.r00").write_bytes(b"volume 2")
        # Release.r01 missing, Release.r02 present -> a named gap.

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        (prefixed_dir / "Release.r02").write_bytes(b"volume 4")
        settings = postprocess.PostprocessSettings(extract_enabled=True)
        await pipeline.process_item(item_id, settings)

        assert prefixed_dir.is_dir()
        assert not (local_dir / rel_path).exists()

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACT_FAILED"
        assert item["pending_download_prefix"] == ".downloading-"
        # No attempt was made -- caught before any staging directory was ever created, and its
        # name (if it existed) would itself carry the prefix -- confirm neither exists.
        assert not (local_dir / f"{extract.UNPACK_PREFIX}.downloading-{rel_path}").exists()
        assert not (local_dir / f"{extract.FAILED_PREFIX}.downloading-{rel_path}").exists()
    finally:
        await db.close()


async def test_rename_conflict_leaves_bytes_under_the_prefix_and_logs_an_event(tmp_path):
    """A destination collision (something already sitting under the real name) must never be
    silently clobbered -- `move_tree`'s `merge=False` refuses it. Unlike the old
    `core/queue.py._reap_one` version of this rename, there is no PARTIAL to downgrade to here
    (post-processing has already run); the item is simply left exactly where it is, loudly.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy")
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=5
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(b"hello")
        # The real name already exists -- a genuine conflict `move_tree` must refuse.
        (local_dir / rel_path).mkdir()
        (local_dir / rel_path / "old.mkv").write_bytes(b"stale")

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        # Everything off -- nothing to flag the release bad, so the pipeline reaches the rename.
        await pipeline.process_item(item_id, postprocess.PostprocessSettings())

        assert prefixed_dir.is_dir(), "the in-flight content must be preserved, not lost"
        assert (prefixed_dir / "a.mkv").read_bytes() == b"hello"
        assert (local_dir / rel_path / "old.mkv").read_bytes() == b"stale", "conflict untouched"

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["pending_download_prefix"] == ".downloading-"

        events = await (
            await db.execute(
                "SELECT kind, message FROM event WHERE item_id = ? "
                "AND kind = 'download_prefix_rename_failed'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1
        assert "FileExistsError" in events[0]["message"]
    finally:
        await db.close()


async def test_move_relocates_directly_from_the_prefixed_source_and_clears_the_prefix(tmp_path):
    """No redundant standalone rename when a staging move is also configured: `_do_move`'s
    destination is already built from the item's unprefixed `rel_path`, so relocating the
    still-prefixed source straight there both moves it and removes the prefix in one operation.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        rel_path = "Release"
        content = b"ready to relocate"
        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="copy", auto_move=1, staging_path=str(staging_dir)
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(content)
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(move_enabled=True)
        await pipeline.process_item(item_id, settings)

        final_dir = staging_dir / rel_path
        assert final_dir.is_dir()
        assert (final_dir / "a.mkv").read_bytes() == content
        assert not prefixed_dir.exists()
        assert not (local_dir / rel_path).exists(), "never created under the unprefixed local path"

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["pending_download_prefix"] is None

        events = await (
            await db.execute("SELECT kind FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "move" in kinds
        assert "download_prefix_removed" not in kinds, "the move already did the rename's job"
    finally:
        await db.close()


async def test_move_withheld_when_prefixed_item_is_corrupt(tmp_path):
    """The staging move must not un-hide a `CORRUPT` release either -- its destination is built
    from the unprefixed `rel_path`, so relocating there would itself be the un-hiding this
    feature exists to prevent.
    """
    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        rel_path = "Release"
        content = b"short"
        _, queue_id = await _make_host_and_queue_rows(
            db,
            sync_mode="copy",
            auto_verify=1,
            auto_move=1,
            staging_path=str(staging_dir),
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(content) + 1000
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "a.mkv").write_bytes(content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            verify_enabled=True, verify_hash_on_disk=True, move_enabled=True
        )
        await pipeline.process_item(item_id, settings)

        assert not (staging_dir / rel_path).exists(), "move withheld"
        assert prefixed_dir.is_dir()
        assert (prefixed_dir / "a.mkv").read_bytes() == content

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "CORRUPT"
        assert item["pending_download_prefix"] == ".downloading-"

        events = await (
            await db.execute(
                "SELECT message FROM event WHERE item_id = ? "
                "AND kind = 'download_prefix_rename_withheld'",
                (item_id,),
            )
        ).fetchall()
        assert len(events) == 1
        assert "Staging move withheld" in events[0]["message"]
    finally:
        await db.close()


@pytestmark_7z
async def test_extraction_stages_into_unpack_correctly_for_a_prefixed_item(tmp_path, monkeypatch):
    """The prompt's own required coverage: extraction must stage into `_UNPACK_` correctly when
    the item is still physically under its download-prefix directory -- proving `_process_item`
    hands `_do_extract` the *physical* root (`_physical_local_root`), not `local_path/rel_path`.
    """
    monkeypatch.setattr(
        extract, "extract_item", functools.partial(extract.extract_item, binary=_SEVEN_ZIP_BIN)
    )
    seen_roots: list[Path] = []
    real_find_archives = extract.find_archives

    def _spying_find_archives(root):
        seen_roots.append(root)
        return real_find_archives(root)

    monkeypatch.setattr(extract, "find_archives", _spying_find_archives)

    db = await _make_db()
    try:
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        rel_path = "Release"
        archive_buf = io.BytesIO()
        with zipfile.ZipFile(archive_buf, "w") as zf:
            zf.writestr("inner.txt", "extracted from inside the prefixed directory")
        archive_content = archive_buf.getvalue()

        _, queue_id = await _make_host_and_queue_rows(db, sync_mode="copy", auto_extract=1)
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_dir), queue_id)
        )
        await db.commit()
        item_id, prefixed_dir = await _make_prefixed_dir_item(
            db, queue_id, rel_path, local_root_dir=local_dir, remote_size=len(archive_content)
        )
        prefixed_dir.mkdir()
        (prefixed_dir / "payload.zip").write_bytes(archive_content)

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(extract_enabled=True)
        await pipeline.process_item(item_id, settings)

        # The physical root `find_archives` actually walked was the prefixed directory, every
        # time it was called (once by `_do_extract`'s own precheck, once more inside
        # `extract_item` itself) -- confirms the staging sibling (`_UNPACK_<prefix><name>`) was
        # computed relative to where the bytes actually were, not the logical, not-yet-real name.
        assert seen_roots, "find_archives was never called"
        assert all(r == prefixed_dir for r in seen_roots)

        final_dir = local_dir / rel_path
        assert final_dir.is_dir(), "extracted, then renamed off the prefix"
        assert (
            final_dir / "inner.txt"
        ).read_text() == "extracted from inside the prefixed directory"
        assert (
            final_dir / "payload.zip"
        ).exists(), "archive itself untouched (cleanup is separate)"
        assert not prefixed_dir.exists()

        # No leftover staging/failure directory anywhere under the queue's local root, prefixed
        # name or not.
        leftovers = [
            p
            for p in local_dir.rglob("*")
            if p.is_dir()
            and (
                p.name.startswith(extract.UNPACK_PREFIX) or p.name.startswith(extract.FAILED_PREFIX)
            )
        ]
        assert leftovers == []

        item = await (await db.execute("SELECT * FROM item WHERE id = ?", (item_id,))).fetchone()
        assert item["state"] == "EXTRACTED"
        assert item["pending_download_prefix"] is None
    finally:
        await db.close()


# --- Archive cleanup never runs after a failed verification (2026-08-14) ----------------------


@pytestmark_unrar
async def test_pipeline_withholds_archive_cleanup_when_verification_failed(tmp_path):
    """The user's own call, 2026-08-14: "I don't think we want to delete on a failed verification
    unless the user deletes."

    Found live. A release whose `.sfv` no longer matched its (renamed) files reported `CORRUPT`;
    extraction still succeeded, and cleanup then removed all twelve rar volumes -- 2.2 GB, the
    only re-extractable source for an item the pipeline had *just* declared corrupt, on a `move`
    queue where the remote copy is the only other one. Cleanup was gated on extraction succeeding
    and never saw the verify result at all.

    The bar here is deliberately one notch lower than `_maybe_delete_remote`'s: `CORRUPT`
    withholds, `SKIPPED`/never-ran does not -- see the gate's own comment for why the two
    deletions are not equivalent.
    """
    db = await _make_db()
    try:
        local_root = tmp_path / "local"
        local_root.mkdir()
        write_if_needed(str(local_root))
        item_dir = await _make_multivolume_rar_release(local_root)
        # Break the sidecar the same way the live incident did -- the CRC lines name files that
        # do not exist, so verification legitimately fails while extraction still succeeds.
        (item_dir / "checksums.sfv").write_text("some.other.release.rar 00000000\n")

        _, queue_id = await _make_host_and_queue_rows(
            db, sync_mode="copy", auto_verify=1, auto_extract=1, auto_delete_archives=1
        )
        await db.execute(
            "UPDATE path_queue SET local_path = ? WHERE id = ?", (str(local_root), queue_id)
        )
        await db.commit()
        item_id = await _make_item_row(db, queue_id, "Release", state="DOWNLOADED")
        await db.execute("UPDATE item SET is_dir = 1 WHERE id = ?", (item_id,))
        await db.commit()

        pipeline = postprocess.PostprocessPipeline(
            db=db, events=EventBus(), remote_pool=_FakeRemotePool(), host_provider=_async_host
        )
        settings = postprocess.PostprocessSettings(
            verify_enabled=True, extract_enabled=True, delete_archives_after_extract=True
        )
        await pipeline.process_item(item_id, settings)

        # The whole point: the archives are still there to re-extract from.
        assert (item_dir / "release.rar").exists(), "a CORRUPT item's archives must survive"
        assert (item_dir / "release.r00").exists()
        assert (
            await local_delete.load_deleted_archive_paths(db, queue_id) == set()
        ), "nothing may be recorded as cleaned up either"

        events = await (
            await db.execute("SELECT kind, message FROM event WHERE item_id = ?", (item_id,))
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "archive_cleanup_withheld" in kinds, kinds
        assert "archive_cleanup" not in kinds, kinds
        withheld = next(e for e in events if e["kind"] == "archive_cleanup_withheld")
        assert "CORRUPT" in withheld["message"]
    finally:
        await db.close()
