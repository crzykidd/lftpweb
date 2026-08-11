from __future__ import annotations

from lftpweb.core.remote import (
    RemoteEntry,
    RemoteRecord,
    parse_find_records,
    records_to_entries,
)


def test_parse_find_records_basic():
    raw = "f\t1024\t1699999999.123456789\t/data/pickup/Release/movie.mkv\n" "d\t4096\t1699999998.0\t/data/pickup/Release\n"
    records = parse_find_records(raw)
    assert records == [
        RemoteRecord(type_char="f", size=1024, mtime=1699999999.123456789, path="/data/pickup/Release/movie.mkv"),
        RemoteRecord(type_char="d", size=4096, mtime=1699999998.0, path="/data/pickup/Release"),
    ]


def test_parse_find_records_no_trailing_newline_on_last_record():
    raw = "f\t10\t1.0\t/data/pickup/a.txt"
    records = parse_find_records(raw)
    assert records == [RemoteRecord(type_char="f", size=10, mtime=1.0, path="/data/pickup/a.txt")]


def test_parse_find_records_path_with_embedded_tab():
    raw = "f\t5\t1.0\t/data/pickup/weird\tname.txt\n"
    records = parse_find_records(raw)
    assert len(records) == 1
    assert records[0].path == "/data/pickup/weird\tname.txt"


def test_parse_find_records_path_with_embedded_newline():
    # The path itself contains a literal newline. Naive line-splitting would fragment this
    # into two "records"; the header-anchored parser must keep it as one, because the next
    # line does not itself start with a valid `<type>\t<size>\t<mtime>\t` header.
    raw = "f\t5\t1.0\t/data/pickup/weird\nname.txt\n" "f\t6\t2.0\t/data/pickup/next.txt\n"
    records = parse_find_records(raw)
    assert len(records) == 2
    assert records[0].path == "/data/pickup/weird\nname.txt"
    assert records[1].path == "/data/pickup/next.txt"


def test_parse_find_records_path_with_spaces_and_non_ascii():
    raw = "f\t5\t1.0\t/data/pickup/file with spaces.txt\n" "f\t7\t2.0\t/data/pickup/日本語.txt\n"
    records = parse_find_records(raw)
    assert records[0].path == "/data/pickup/file with spaces.txt"
    assert records[1].path == "/data/pickup/日本語.txt"


def test_parse_find_records_non_utf8_bytes_survive_via_surrogateescape():
    # A filename with a byte sequence that isn't valid UTF-8. The caller decodes the raw
    # process output with errors="surrogateescape" before parsing; simulate that here.
    raw_bytes = b"f\t3\t1.0\t/data/pickup/bad-\xffname.bin\n"
    raw = raw_bytes.decode("utf-8", errors="surrogateescape")
    records = parse_find_records(raw)
    assert len(records) == 1
    # Round-trips back to the original bytes when re-encoded the same way.
    assert records[0].path.encode("utf-8", errors="surrogateescape") == b"/data/pickup/bad-\xffname.bin"


def test_parse_find_records_empty_input():
    assert parse_find_records("") == []


def test_records_to_entries_strips_root_and_maps_types():
    records = [
        RemoteRecord(type_char="d", size=4096, mtime=1.0, path="/data/pickup/Release"),
        RemoteRecord(type_char="f", size=1024, mtime=2.0, path="/data/pickup/Release/movie.mkv"),
        RemoteRecord(type_char="f", size=0, mtime=3.0, path="/data/pickup/loose.txt"),
    ]
    entries = records_to_entries(records, "/data/pickup")
    assert entries == {
        "Release": RemoteEntry(rel_path="Release", is_dir=True, size=0, mtime=1.0),
        "Release/movie.mkv": RemoteEntry(rel_path="Release/movie.mkv", is_dir=False, size=1024, mtime=2.0),
        "loose.txt": RemoteEntry(rel_path="loose.txt", is_dir=False, size=0, mtime=3.0),
    }


def test_records_to_entries_skips_non_file_dir_types():
    records = [
        RemoteRecord(type_char="l", size=0, mtime=1.0, path="/data/pickup/symlink"),
        RemoteRecord(type_char="f", size=1, mtime=1.0, path="/data/pickup/real.txt"),
    ]
    entries = records_to_entries(records, "/data/pickup")
    assert list(entries) == ["real.txt"]


def test_records_to_entries_handles_root_with_trailing_slash():
    records = [RemoteRecord(type_char="f", size=1, mtime=1.0, path="/data/pickup/a.txt")]
    entries = records_to_entries(records, "/data/pickup/")
    assert list(entries) == ["a.txt"]
