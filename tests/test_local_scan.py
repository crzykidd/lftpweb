from __future__ import annotations

from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.local_scan import (
    LocalEntry,
    PgetStatus,
    effective_file_size,
    effective_size,
    parse_pget_status,
    scan_local,
)
from lftpweb.core.mount_sentinel import SENTINEL_NAME


def test_parse_pget_status_basic():
    text = "size=1000\n0.pos=200\n0.limit=500\n1.pos=900\n1.limit=1000\n"
    status = parse_pget_status(text)
    assert status == PgetStatus(size=1000, chunks=((200, 500), (900, 1000)))


def test_effective_size_subtracts_outstanding_ranges():
    # size=1000, chunk 0 has 300 bytes outstanding (500-200), chunk 1 has 100 (1000-900)
    status = PgetStatus(size=1000, chunks=((200, 500), (900, 1000)))
    assert effective_size(status) == 1000 - 300 - 100


def test_effective_size_fully_written_equals_size():
    # Every chunk's pos has caught up to its limit -> nothing outstanding.
    status = PgetStatus(size=1000, chunks=((500, 500), (1000, 1000)))
    assert effective_size(status) == 1000


def test_effective_size_never_negative():
    # Malformed/adversarial sidecar shouldn't produce a negative size.
    status = PgetStatus(size=100, chunks=((0, 1000),))
    assert effective_size(status) == 0


def test_parse_pget_status_ignores_unknown_keys():
    # lftp has added fields to this format before; unknown keys must not raise.
    text = "size=500\n0.pos=0\n0.limit=500\n0.opos=0\nunexpected=whatever\n"
    status = parse_pget_status(text)
    assert status.size == 500
    assert status.chunks == ((0, 500),)


def test_scan_local_sparse_pget_file_uses_sidecar_not_raw_size(tmp_path):
    # A pget file mid-transfer: st_size reports the full sparse allocation, but only part of
    # it is actually written. The sidecar's accounting must win.
    target = tmp_path / "movie.mkv"
    with open(target, "wb") as f:
        f.truncate(10_000_000)  # sparse allocation, "raw" st_size would read 10_000_000
    sidecar = tmp_path / "movie.mkv.lftp-pget-status"
    sidecar.write_text("size=10000000\n0.pos=0\n0.limit=6000000\n1.pos=8000000\n1.limit=10000000\n")

    entries = scan_local(tmp_path)
    assert "movie.mkv" in entries
    # outstanding = (6000000-0) + (10000000-8000000) = 8000000; effective = 2000000
    assert entries["movie.mkv"].size == 2_000_000
    # The sidecar itself must never appear as its own tree entry.
    assert "movie.mkv.lftp-pget-status" not in entries


def test_scan_local_temp_suffix_matched_to_final_name(tmp_path):
    (tmp_path / "show.mkv.lftp").write_bytes(b"x" * 500)

    entries = scan_local(tmp_path)
    assert "show.mkv" in entries
    assert "show.mkv.lftp" not in entries
    assert entries["show.mkv"].size == 500


def test_scan_local_finished_file_has_no_suffix(tmp_path):
    (tmp_path / "done.mkv").write_bytes(b"y" * 42)
    entries = scan_local(tmp_path)
    assert entries["done.mkv"] == LocalEntry(rel_path="done.mkv", is_dir=False, size=42)


def test_scan_local_nested_directories_and_files(tmp_path):
    (tmp_path / "Release").mkdir()
    (tmp_path / "Release" / "Subs").mkdir()
    (tmp_path / "Release" / "movie.mkv").write_bytes(b"a" * 100)
    (tmp_path / "Release" / "Subs" / "eng.srt").write_bytes(b"b" * 10)

    entries = scan_local(tmp_path)
    assert entries["Release"].is_dir is True
    assert entries["Release/Subs"].is_dir is True
    assert entries["Release/movie.mkv"].size == 100
    assert entries["Release/Subs/eng.srt"].size == 10


def test_scan_local_missing_root_returns_empty(tmp_path):
    assert scan_local(tmp_path / "does-not-exist") == {}


def test_scan_local_zero_byte_file(tmp_path):
    (tmp_path / "empty.bin").touch()
    entries = scan_local(tmp_path)
    assert entries["empty.bin"].size == 0


# --- effective_file_size: core/progress.py's single-file (pget) sampling, no directory walk --


def test_effective_file_size_plain_file(tmp_path):
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 42)
    assert effective_file_size(path) == 42


def test_effective_file_size_missing_file_is_zero(tmp_path):
    assert effective_file_size(tmp_path / "not-there.mkv") == 0


def test_effective_file_size_uses_sidecar_over_sparse_st_size(tmp_path):
    path = tmp_path / "movie.mkv"
    with open(path, "wb") as f:
        f.truncate(10_000_000)
    (tmp_path / "movie.mkv.lftp-pget-status").write_text(
        "size=10000000\n0.pos=0\n0.limit=6000000\n1.pos=8000000\n1.limit=10000000\n"
    )
    assert effective_file_size(path) == 2_000_000


def test_effective_file_size_finds_temp_suffixed_file_by_final_name(tmp_path):
    (tmp_path / "movie.mkv.lftp").write_bytes(b"y" * 777)
    assert effective_file_size(tmp_path / "movie.mkv") == 777


def test_effective_file_size_temp_suffixed_file_with_sidecar(tmp_path):
    temp_path = tmp_path / "movie.mkv.lftp"
    with open(temp_path, "wb") as f:
        f.truncate(1_000)
    (tmp_path / "movie.mkv.lftp.lftp-pget-status").write_text(
        "size=1000\n0.pos=250\n0.limit=1000\n"
    )
    assert effective_file_size(tmp_path / "movie.mkv") == 250


def test_scan_local_skips_mount_sentinel_at_root(tmp_path):
    """The mount sentinel is lftpweb's own bookkeeping (DESIGN.md §7.3), not content.

    Left in the walk it reconciles to a permanent LOCAL_ONLY node and renders in the Files
    tree as a file the remote is missing.
    """
    (tmp_path / SENTINEL_NAME).write_text("")
    (tmp_path / "movie.mkv").write_bytes(b"z" * 100)

    entries = scan_local(tmp_path)

    assert SENTINEL_NAME not in entries
    assert entries["movie.mkv"].size == 100


def test_scan_local_keeps_sentinel_named_file_below_root(tmp_path):
    """Only the root copy is ours. The same name deeper in the tree arrived from the
    remote and is real content — hiding it would make the item permanently PARTIAL.
    """
    nested = tmp_path / "Some.Release"
    nested.mkdir()
    (nested / SENTINEL_NAME).write_bytes(b"x" * 5)

    entries = scan_local(tmp_path)

    assert f"Some.Release/{SENTINEL_NAME}" in entries


# --- core/extract.py's _UNPACK_/_FAILED_ staging dirs (DESIGN.md §6, this task) --------------


def test_scan_local_skips_unpack_staging_dir_at_root(tmp_path):
    """An in-progress (or crashed) extraction's staging dir is lftpweb's own bookkeeping, not
    content — left in the walk it would reconcile to a growing LOCAL_ONLY node while
    extraction runs.
    """
    (tmp_path / f"{UNPACK_PREFIX}Release").mkdir()
    (tmp_path / f"{UNPACK_PREFIX}Release" / "movie.mkv").write_bytes(b"x" * 10)
    (tmp_path / "movie.mkv").write_bytes(b"z" * 100)

    entries = scan_local(tmp_path)

    assert f"{UNPACK_PREFIX}Release" not in entries
    assert f"{UNPACK_PREFIX}Release/movie.mkv" not in entries
    assert entries["movie.mkv"].size == 100


def test_scan_local_skips_failed_extraction_dir_at_root(tmp_path):
    """A `_FAILED_` dir is left forever as diagnostic evidence (DESIGN.md §6) — it must never
    render as a permanent LOCAL_ONLY node in the Files tree.
    """
    (tmp_path / f"{FAILED_PREFIX}Release").mkdir()
    (tmp_path / f"{FAILED_PREFIX}Release" / "partial.mkv").write_bytes(b"x" * 10)

    entries = scan_local(tmp_path)

    assert f"{FAILED_PREFIX}Release" not in entries
    assert f"{FAILED_PREFIX}Release/partial.mkv" not in entries


def test_scan_local_skips_unpack_and_failed_dirs_at_any_depth(tmp_path):
    """Unlike the mount sentinel, these can appear next to *any* item, not just the queue
    root — an item can be nested arbitrarily deep under the scan root.
    """
    nested = tmp_path / "Show" / "Season 01"
    nested.mkdir(parents=True)
    (nested / f"{UNPACK_PREFIX}Episode.01").mkdir()
    (nested / f"{UNPACK_PREFIX}Episode.01" / "ep.mkv").write_bytes(b"a" * 5)
    (nested / f"{FAILED_PREFIX}Episode.02").mkdir()
    (nested / "Episode.03.mkv").write_bytes(b"b" * 5)

    entries = scan_local(tmp_path)

    assert f"Show/Season 01/{UNPACK_PREFIX}Episode.01" not in entries
    assert f"Show/Season 01/{FAILED_PREFIX}Episode.02" not in entries
    assert "Show/Season 01/Episode.03.mkv" in entries


def test_scan_local_unpack_prefix_only_hides_directories_not_files(tmp_path):
    """The prefix match is directories-only (extract.py never produces a file by these names,
    and a remote release could coincidentally start with the same text) — a file happening to
    share the prefix is real content and must not be swallowed by the filter.
    """
    (tmp_path / f"{UNPACK_PREFIX}notes.txt").write_bytes(b"real remote content")

    entries = scan_local(tmp_path)

    assert f"{UNPACK_PREFIX}notes.txt" in entries
