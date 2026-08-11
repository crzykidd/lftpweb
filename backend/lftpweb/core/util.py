"""Small helpers shared across the scanning/reconciliation core.

DESIGN.md §15.10: filenames can contain bytes that are not valid UTF-8. Every filesystem
and SSH path in this codebase flows through Python's `str` using **surrogateescape** —
`os.scandir`/`os.fsdecode` already do this automatically on POSIX, and `core/remote.py`'s
parser decodes remote bytes the same way — so undecodable bytes round-trip internally as
lone surrogates rather than raising or silently mangling data.

The one place that breaks is anything that must itself be valid UTF-8: a SQLite TEXT column
or a JSON payload. `to_safe_text()` is the boundary conversion — call it exactly there, never
in the scanning/matching path, so comparisons between a local and a remote path keep working
on the original (surrogate-escaped) strings for as long as possible.
"""

from __future__ import annotations


def to_safe_text(value: str) -> str:
    """Make `value` safe to store as SQLite TEXT or serialize as JSON.

    A `str` produced via surrogateescape (e.g. `os.fsdecode` on a non-UTF-8 filename) round
    -trips fine as a Python object but cannot be encoded back to UTF-8 in strict mode — which
    is exactly what sqlite3's C driver and `json.dumps` both do. Re-encoding with
    `surrogateescape` and decoding with `backslashreplace` turns any lone surrogate into a
    visible `\\xNN` escape instead of raising. A filename must never crash a scan, a DB write,
    or a WebSocket frame — this is what keeps that true at the one place it matters.
    """
    return value.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="backslashreplace")
