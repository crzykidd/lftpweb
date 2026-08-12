from __future__ import annotations

import socket
from pathlib import Path

import pytest

from lftpweb.core.remote import (
    HostConfig,
    RemoteConnectionPool,
    RemoteEntry,
    RemoteRecord,
    RemoteScanError,
    interpret_primary_scan_result,
    parse_find_records,
    records_to_entries,
)


def test_parse_find_records_basic():
    raw = (
        "f\t1024\t1699999999.123456789\t/data/pickup/Release/movie.mkv\n"
        "d\t4096\t1699999998.0\t/data/pickup/Release\n"
    )
    records = parse_find_records(raw)
    assert records == [
        RemoteRecord(
            type_char="f",
            size=1024,
            mtime=1699999999.123456789,
            path="/data/pickup/Release/movie.mkv",
        ),
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
    assert (
        records[0].path.encode("utf-8", errors="surrogateescape")
        == b"/data/pickup/bad-\xffname.bin"
    )


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
        "Release/movie.mkv": RemoteEntry(
            rel_path="Release/movie.mkv", is_dir=False, size=1024, mtime=2.0
        ),
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


# --- interpret_primary_scan_result: the scan-abort bug's fix (docs/decisions.md phase 2/3) --
#
# GNU `find -mindepth 1 -printf ...` exits 1 the instant it can't stat/read *one*
# subdirectory anywhere in the tree, even though it already printed every record it *could*
# reach to stdout and kept scanning the rest. The old code treated any nonzero exit that
# wasn't the "-printf unsupported" signature as a hard failure and discarded the whole
# queue's tree. These are unit tests, not a live SSH connection, the same way
# `parse_find_records`'s tests are -- see the live regression below for the real thing.


def test_interpret_primary_scan_result_success():
    stdout = b"f\t10\t1.0\t/data/pickup/a.txt\n"
    outcome = interpret_primary_scan_result(0, stdout, b"")
    assert outcome.raw == stdout.decode()
    assert outcome.warning is None


def test_interpret_primary_scan_result_unsupported_printf_signals_fallback():
    stderr = b"find: unrecognized: -printf\n"
    outcome = interpret_primary_scan_result(1, b"", stderr)
    assert outcome.raw is None
    assert outcome.warning is None


def test_interpret_primary_scan_result_hard_failure_no_output_still_raises():
    # A genuinely bad path (or the root itself unreadable): find produces *nothing*, so
    # there is nothing to salvage -- this must still surface as a real failure.
    stderr = b"find: '/no/such/path': No such file or directory\n"
    with pytest.raises(RemoteScanError):
        interpret_primary_scan_result(1, b"", stderr)


def test_interpret_primary_scan_result_permission_denied_subtree_is_a_partial_success():
    # The bug, reproduced directly: exit 1, but stdout is fully populated for everything
    # find *could* reach. Must not raise -- must return every record it read, plus a
    # human-readable warning naming what was skipped, so the queue's tree still renders.
    stdout = (
        b"d\t4096\t1.0\t/data/pickup/Some.Release\n"
        b"f\t10\t2.0\t/data/pickup/Some.Release/movie.mkv\n"
        b"d\t4096\t3.0\t/data/pickup/no-permission\n"
        b"f\t512\t4.0\t/data/pickup/loose-notes.txt\n"
    )
    stderr = b"find: '/data/pickup/no-permission': Permission denied\n"
    outcome = interpret_primary_scan_result(1, stdout, stderr)
    assert outcome.raw == stdout.decode()
    assert outcome.warning is not None
    assert "Permission denied" in outcome.warning
    assert "no-permission" in outcome.warning

    # And the rest of the pipeline still parses it into every reachable entry, including
    # the unreadable directory's *own* record (find could still see and stat it via its
    # readable parent -- it just couldn't descend into it).
    entries = records_to_entries(parse_find_records(outcome.raw), "/data/pickup")
    assert set(entries) == {
        "Some.Release",
        "Some.Release/movie.mkv",
        "no-permission",
        "loose-notes.txt",
    }


def test_interpret_primary_scan_result_multiple_skipped_paths_summarized():
    stdout = b"f\t1\t1.0\t/data/pickup/ok.txt\n"
    stderr = (
        b"find: '/data/pickup/a': Permission denied\n"
        b"find: '/data/pickup/b': Permission denied\n"
    )
    outcome = interpret_primary_scan_result(1, stdout, stderr)
    assert outcome.raw == stdout.decode()
    assert outcome.warning is not None
    assert "2 paths skipped" in outcome.warning


# --- Live regression against the real fake seedbox (GNU find) -----------------------------
# Skipped automatically if the seedbox isn't reachable, same convention as test_queue.py.
# `docker compose -f docker-compose.test.yml up --build -d` seeds a `chmod 000
# no-permission/` directory (docker/test-seedbox/seed_tree.sh) specifically for this test.

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


@pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- `docker compose -f docker-compose.test.yml up --build -d`",
)
async def test_live_scan_skips_unreadable_subdirectory_instead_of_aborting():
    host = HostConfig(
        id=1,
        address=SEEDBOX_HOST,
        port=SEEDBOX_PORT,
        username=SEEDBOX_USER,
        auth_method="password",
        password=SEEDBOX_PASSWORD,
        known_hosts_policy="insecure",
    )
    pool = RemoteConnectionPool(known_hosts_dir=Path("/tmp"))
    try:
        entries, warning = await pool.scan(host, "/data/pickup")
    finally:
        await pool.close()

    # The rest of the tree scanned normally -- the bug used to discard all of this.
    assert "Some.Release.S01E01.720p.WEB" in entries
    assert "Some.Release.S01E01.720p.WEB/Some.Release.S01E01.720p.WEB.mkv" in entries
    assert "loose-notes.txt" in entries

    # The unreadable directory itself is visible (find could stat it via its readable
    # parent)...
    assert "no-permission" in entries
    # ...but nothing beneath it, since find could never descend into it.
    assert not any(p.startswith("no-permission/") for p in entries)

    # And the queue-level warning says so, instead of the scan vanishing with no explanation.
    assert warning is not None
    assert "no-permission" in warning
