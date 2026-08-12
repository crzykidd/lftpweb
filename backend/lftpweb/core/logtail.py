"""Bounded log tailing (DESIGN.md §10.1) for `api/logs.py`.

A rotated app log can be up to `logsetup.MAX_BYTES` (5 MB) and there are up to
`logsetup.BACKUP_COUNT` (5) of them on disk. Reading a whole file into memory just to show
the last 200 lines is exactly the thing this module exists to avoid — `tail_lines` reads
backwards from the end of the file in fixed-size chunks, seeking further back only until it
either has enough lines or hits `max_bytes`, whichever comes first. Memory use is bounded by
`max_bytes` (plus one chunk) regardless of how large the file actually is.

Level filtering happens *after* the bounded read, over whatever was already pulled in — not
by re-scanning more of the file until enough matching lines are found. A `level=ERROR` filter
over a chatty file can therefore return fewer than the requested line count even though more
matching lines exist earlier in the file; `LogTailResponse.truncated` says so. Scanning
further back until the request is satisfied would reintroduce an unbounded read for exactly
the filter that's most likely to be used on a large, mostly-quiet file — the case this module
is supposed to protect against, not special-case around.

Every line here has already passed `logsetup.CredentialRedactor` *before* it reached disk
(DESIGN.md §10.1: "a secret that reaches disk has already leaked") — this module does not
redact anything a second time. See docs/decisions.md for why a second filter here would be
redundant defence-in-depth, not real depth: the one thing that matters (never letting a
credential reach disk unredacted) is already enforced at the only point that can enforce it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO

DEFAULT_MAX_LINES = 200
MAX_LINES_CAP = 2000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB -- the hard ceiling on what any one tail reads
_CHUNK_SIZE = 65536

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# logsetup._LOG_FORMAT: "%(asctime)s %(levelname)-8s %(name)s: %(message)s" -- asctime is
# itself two whitespace-separated tokens ("2026-08-11 12:00:00,000"), so a line that starts a
# new record looks like "<date> <time> LEVEL    name: message". A continuation line (a
# traceback frame) has no such prefix and is intentionally treated as "no level" below.
_LEVEL_RE = re.compile(r"^\S+ \S+ (" + "|".join(_LEVELS) + r")\b")


def line_level(line: str) -> str | None:
    match = _LEVEL_RE.match(line)
    return match.group(1) if match else None


def tail_lines(
    fileobj: BinaryIO,
    max_lines: int = DEFAULT_MAX_LINES,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    chunk_size: int = _CHUNK_SIZE,
) -> tuple[list[str], bool]:
    """Read up to `max_lines` lines from the end of a seekable binary file object, reading no
    more than `max_bytes` total (plus at most one final chunk) regardless of file size.

    Returns `(lines, truncated)` — `truncated` is True when `max_bytes` was hit before
    `max_lines` worth of newlines were found (i.e. there is more file above what was read).
    Operates on a file object rather than a path so tests can instrument exactly how many
    bytes were requested without needing a real multi-megabyte fixture on disk.
    """
    fileobj.seek(0, 2)
    file_size = fileobj.tell()

    data = b""
    pos = file_size
    truncated = False
    while pos > 0:
        if data.count(b"\n") > max_lines:
            break
        if file_size - pos >= max_bytes:
            truncated = True
            break
        read_size = min(chunk_size, pos)
        pos -= read_size
        fileobj.seek(pos)
        data = fileobj.read(read_size) + data

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if pos > 0 and lines:
        # Whenever the read didn't reach byte offset 0 (whichever reason it stopped for),
        # the chunk boundary landed mid-line -- drop that partial fragment rather than show
        # a truncated line as if it were whole.
        lines = lines[1:]
    return lines[-max_lines:], truncated


def tail_file(
    path: Path,
    max_lines: int = DEFAULT_MAX_LINES,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[str], bool]:
    with path.open("rb") as fh:
        return tail_lines(fh, max_lines, max_bytes=max_bytes)
