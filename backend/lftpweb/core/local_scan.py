"""Local tree walk (DESIGN.md §5 cadence, §4.4 conventions).

`os.scandir`, recursive, plus the two lftp on-disk conventions that determine whether a
partial file reads as partial. Nothing writes these files yet — the transfer engine is phase
3 — so this module is unit-tested against fixtures constructed by hand, not against a live
transfer (per the phase 2 prompt).

Paths use `os.fsdecode`'s surrogateescape decoding throughout (DESIGN.md §15.10) — the same
convention `core/remote.py` uses for remote paths — so a local and a remote path for the same
odd-byte filename compare equal without either side needing special-casing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.mount_sentinel import SENTINEL_NAME

PGET_STATUS_SUFFIX = ".lftp-pget-status"
TEMP_FILE_SUFFIX = ".lftp"


@dataclass(frozen=True)
class LocalEntry:
    """One filesystem entry, keyed by its `rel_path` (POSIX-style, relative to the scan
    root). `size` is the *effective* size for files (§4.4a/b already applied) and is always
    0 for directories — the reconciler computes directory totals by summing children, the
    same way it does for the remote tree, so the two are computed identically.

    `mtime` (2026-08-13, prompts/2026-08-13-files-detail-inspector.md) is the file's own
    `st_mtime`, epoch seconds — the local-side counterpart to `core/remote.py.RemoteEntry.mtime`
    that never existed before this task (the gap the item drawer's "modified date, both sides"
    request exposed directly). Always `0.0` for a directory, mirroring `RemoteEntry` exactly:
    `core/reconcile.py` only ever reads a file's own mtime, never a directory's, on either side
    — see that module for why staying consistent with the existing (files-only) convention was
    the deliberate choice here rather than inventing a directory rule from scratch.
    """

    rel_path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


@dataclass(frozen=True)
class PgetStatus:
    """A parsed `.lftp-pget-status` sidecar (DESIGN.md §4.4a)."""

    size: int
    chunks: tuple[tuple[int, int], ...]  # (pos, limit) per chunk, in file order


def parse_pget_status(text: str) -> PgetStatus:
    """Parse a `.lftp-pget-status` sidecar's contents.

    Format (one `key=value` per line):
        size=<total>
        0.pos=<n>   0.limit=<n>
        1.pos=<n>   1.limit=<n>
        ...

    Unknown keys are ignored rather than rejected — lftp has added fields to this format
    before (e.g. `1.opos`) and a filename must never crash a scan because of one.
    """
    size = 0
    chunk_bounds: dict[int, dict[str, int]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        try:
            value = int(raw_value.strip())
        except ValueError:
            continue
        if key == "size":
            size = value
            continue
        chunk_key, _, field = key.partition(".")
        if field not in ("pos", "limit"):
            continue
        try:
            idx = int(chunk_key)
        except ValueError:
            continue
        chunk_bounds.setdefault(idx, {})[field] = value

    chunks = tuple(
        (bounds.get("pos", 0), bounds.get("limit", 0)) for _, bounds in sorted(chunk_bounds.items())
    )
    return PgetStatus(size=size, chunks=chunks)


def effective_size(status: PgetStatus) -> int:
    """size − Σ(limit − pos): the sidecar's total minus every chunk's still-outstanding
    range. Raw `st_size` reports the full sparse allocation immediately, so it lies; this is
    what's actually been written to disk.
    """
    outstanding = sum(max(limit - pos, 0) for pos, limit in status.chunks)
    return max(status.size - outstanding, 0)


def _read_sidecar(path: Path) -> PgetStatus | None:
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None
    return parse_pget_status(text)


def effective_file_size(path: str | Path) -> int:
    """The effective size of one file, applying §4.4a/b the same way `scan_local` does, but
    without a directory walk — for `core/progress.py`'s single-file (`pget`) active-set
    sampling, where stat-ing one known path beats walking its parent directory.

    Checks, in order: a live `.lftp` temp file (if `path` itself doesn't exist yet), then that
    file's own `.lftp-pget-status` sidecar (if present, its accounting wins over raw
    `st_size`), then plain `st_size`. Returns 0 for a path that doesn't exist in any of these
    forms — a file that hasn't started yet reads as 0 bytes done, not an error.
    """
    path = Path(path)
    candidate = path
    if not candidate.exists():
        temp_candidate = path.with_name(path.name + TEMP_FILE_SUFFIX)
        if temp_candidate.exists():
            candidate = temp_candidate
        else:
            return 0

    sidecar_path = candidate.with_name(candidate.name + PGET_STATUS_SUFFIX)
    status = _read_sidecar(sidecar_path)
    if status is not None:
        return effective_size(status)
    try:
        return candidate.stat().st_size
    except OSError:
        return 0


def scan_local(root: str | Path) -> dict[str, LocalEntry]:
    """Walk `root` and return every entry keyed by POSIX-style `rel_path`.

    `.lftp-pget-status` sidecars never appear as their own entries — they're consumed to
    correct the size of the file they describe. A `*.lftp` temp file is reported under its
    *final* name (suffix stripped), so it matches its remote counterpart directly; if the
    sidecar-adjusted size is also available for that temp file, it wins over raw `st_size`.
    """
    root = Path(root)
    entries: dict[str, LocalEntry] = {}
    if not root.is_dir():
        return entries

    def walk(dir_path: Path, rel_prefix: str) -> None:
        try:
            with os.scandir(dir_path) as it:
                raw_entries = list(it)
        except OSError:
            return

        sidecars: dict[str, PgetStatus] = {}
        for entry in raw_entries:
            if entry.name.endswith(PGET_STATUS_SUFFIX) and not entry.is_dir(follow_symlinks=False):
                carrier = entry.name[: -len(PGET_STATUS_SUFFIX)]
                status = _read_sidecar(Path(entry.path))
                if status is not None:
                    sidecars[carrier] = status

        for entry in raw_entries:
            name = entry.name
            rel_path = f"{rel_prefix}{name}" if rel_prefix == "" else f"{rel_prefix}/{name}"

            # `core/mount_sentinel.py` writes this at the queue's local root after every
            # successful scan (DESIGN.md §7.3). It is lftpweb's own bookkeeping, not content
            # the user asked for, and it exists only locally — so left in the walk it
            # reconciles to a permanent LOCAL_ONLY node and shows up in the Files tree as a
            # file the remote is "missing". Filtered here, at the source, rather than in the
            # UI: the reconciler, the item table, and every completeness count then all see
            # the same tree the user sees. Root only, since that is the only place the
            # sentinel is ever written — an identically-named file deeper in the tree came
            # from the remote and is real content.
            if (
                rel_prefix == ""
                and name == SENTINEL_NAME
                and not entry.is_dir(follow_symlinks=False)
            ):
                continue

            if entry.is_dir(follow_symlinks=False):
                # `core/extract.py`'s `_UNPACK_<name>`/`_FAILED_<name>` staging directories
                # (DESIGN.md §6) are lftpweb's own bookkeeping too, but unlike the sentinel
                # they're siblings of the item they belong to, not root-only, so an item can
                # carry one at any depth. Left in the walk, an in-progress `_UNPACK_` dir
                # would reconcile to a growing LOCAL_ONLY node while extraction runs, and a
                # `_FAILED_` dir would sit there forever as a permanent LOCAL_ONLY once
                # extraction stops touching it -- neither is content the user asked for.
                if name.startswith(UNPACK_PREFIX) or name.startswith(FAILED_PREFIX):
                    continue
                entries[rel_path] = LocalEntry(rel_path=rel_path, is_dir=True)
                walk(Path(entry.path), rel_path)
                continue

            if name.endswith(PGET_STATUS_SUFFIX):
                continue  # consumed above

            final_name = name[: -len(TEMP_FILE_SUFFIX)] if name.endswith(TEMP_FILE_SUFFIX) else name
            final_rel_path = (
                f"{rel_prefix}{final_name}" if rel_prefix == "" else f"{rel_prefix}/{final_name}"
            )

            # One `stat()`, used for both `mtime` (always wanted -- a sidecar has no opinion
            # on it) and `size` (only when there's no sidecar to prefer instead). Missing
            # entirely on a race (deleted between `scandir` and `stat`) reads as `mtime=0.0`,
            # the same "nothing better available" fallback `size` already had.
            try:
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                stat_result = None
            mtime = stat_result.st_mtime if stat_result is not None else 0.0

            status = sidecars.get(name)
            if status is not None:
                size = effective_size(status)
            else:
                size = stat_result.st_size if stat_result is not None else 0

            existing = entries.get(final_rel_path)
            if existing is not None and existing.size >= size:
                # A finished file (no suffix) and a stray `.lftp` temp of the same final name
                # should not both occur, but if they do, keep whichever reports more data
                # rather than letting scan order decide.
                continue
            entries[final_rel_path] = LocalEntry(
                rel_path=final_rel_path, is_dir=False, size=size, mtime=mtime
            )

    walk(root, "")
    return entries
