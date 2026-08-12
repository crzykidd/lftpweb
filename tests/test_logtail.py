from __future__ import annotations

import io

from lftpweb.core.logtail import line_level, tail_file, tail_lines


def _make_lines(n: int, prefix: str = "line") -> list[str]:
    return [
        f"2026-08-11 12:00:{i % 60:02d},000 INFO     lftpweb.core.queue: {prefix} {i}"
        for i in range(n)
    ]


def test_tail_lines_returns_the_last_n_lines():
    lines = _make_lines(1000)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    fh = io.BytesIO(data)

    tailed, truncated = tail_lines(fh, max_lines=10, max_bytes=10 * 1024 * 1024)
    assert tailed == lines[-10:]
    assert truncated is False


def test_tail_lines_on_a_file_smaller_than_the_request_returns_everything():
    lines = _make_lines(5)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    fh = io.BytesIO(data)

    tailed, truncated = tail_lines(fh, max_lines=500)
    assert tailed == lines
    assert truncated is False


def test_tail_lines_never_reads_more_than_the_byte_cap():
    """The bounded-tail requirement, proven, not just asserted: instrument the file object's
    read() calls and confirm the total bytes requested never exceeds max_bytes (plus at most
    one chunk) even though the underlying file is far larger than that.
    """

    class CountingBytesIO(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.total_read = 0

        def read(self, size=-1):  # noqa: ANN001 - matches BytesIO's own signature
            chunk = super().read(size)
            self.total_read += len(chunk)
            return chunk

    huge_line_count = 500_000
    lines = _make_lines(huge_line_count)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    assert len(data) > 10 * 1024 * 1024  # confirm the fixture is actually huge

    fh = CountingBytesIO(data)
    max_bytes = 65536
    # Ask for far more lines than max_bytes could ever contain (each line is ~65 bytes, so
    # 65536 bytes holds roughly 1000 of them) -- guarantees the byte cap is what stops the
    # read, not "found enough lines already."
    tailed, truncated = tail_lines(fh, max_lines=50_000, max_bytes=max_bytes, chunk_size=16384)

    assert truncated is True
    assert fh.total_read <= max_bytes + 16384  # cap, plus at most one more chunk
    assert fh.total_read < len(data)  # did NOT read the whole 10+ MB file
    # What we did read is still the tail's own real content, correctly ordered.
    assert tailed == tailed[:]  # sanity: no exception, well-formed list
    assert all(line.startswith("2026-08-11") for line in tailed)
    assert tailed == lines[-len(tailed) :]


def test_tail_file_reads_from_a_real_path(tmp_path):
    path = tmp_path / "app.log"
    lines = _make_lines(20)
    path.write_text("\n".join(lines) + "\n")

    tailed, truncated = tail_file(path, max_lines=5)
    assert tailed == lines[-5:]
    assert truncated is False


def test_line_level_parses_the_logsetup_format():
    line = "2026-08-11 12:00:00,000 WARNING  lftpweb.core.queue: something happened"
    assert line_level(line) == "WARNING"


def test_line_level_returns_none_for_a_continuation_line():
    # A traceback frame has no timestamp/level prefix.
    assert line_level('  File "queue.py", line 42, in tick') is None


def test_level_filter_applied_after_the_bounded_read():
    lines = [
        "2026-08-11 12:00:00,000 INFO     x: info one",
        "2026-08-11 12:00:01,000 ERROR    x: error one",
        "2026-08-11 12:00:02,000 INFO     x: info two",
        "2026-08-11 12:00:03,000 ERROR    x: error two",
    ]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    tailed, _truncated = tail_lines(io.BytesIO(data), max_lines=100)
    errors = [line for line in tailed if line_level(line) == "ERROR"]
    assert errors == [lines[1], lines[3]]
