from __future__ import annotations

from lftpweb.core.local_scan import (
    LocalEntry,
    PgetStatus,
    effective_size,
    parse_pget_status,
    scan_local,
)


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
