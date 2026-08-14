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
import re
import time
from dataclasses import dataclass
from pathlib import Path

from lftpweb.core.extract import FAILED_PREFIX, UNPACK_PREFIX
from lftpweb.core.mount_sentinel import SENTINEL_NAME

PGET_STATUS_SUFFIX = ".lftp-pget-status"
TEMP_FILE_SUFFIX = ".lftp"

# 2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md): lftp normally names an
# in-flight file `<final>.lftp` (`xfer:temp-file-name "*.lftp"`, `core/lftp.py.build_rc_text`),
# but when two lftp processes race the same target -- exactly the bug
# `core/queue.py.enqueue_item`'s active-job guard exists to prevent -- the loser can end up
# writing (or has previously written) to a *uniquified* variant instead:
# `foo.mkv.lftp~20260813154311~` (a real example from a user report the same day). Reproduced
# empirically against the fake seedbox by running two lftp processes concurrently against one
# target (see docs/decisions.md for what that reproduction actually showed -- the exact
# trigger for the renamed-variant form specifically, vs. two processes silently sharing the
# plain `.lftp` name, turned out to be a timing-dependent race inside lftp itself, not
# something this codebase controls). Both forms mean the same thing -- "not yet the real
# file" -- so both must be recognised identically everywhere `.lftp` already is, from one
# place both `core/local_scan.py` and `core/local_delete.py` import, per the task's own
# instruction not to hardcode `~` handling twice.
TEMP_FILE_RE = re.compile(r"^(?P<final>.+)\.lftp(?:~\d+~)?$")


def strip_temp_suffix(name: str) -> str:
    """`name` with any lftp temp-file suffix removed -- plain `.lftp`, or the
    `.lftp~<timestamp>~` variant lftp falls back to when it finds the plain name already
    spoken for. Returns `name` unchanged when neither suffix is present.
    """
    match = TEMP_FILE_RE.match(name)
    return match.group("final") if match else name


def is_temp_name(name: str) -> bool:
    """Whether `name` is an lftp temp-file name (either form) rather than a finished file's
    own name.
    """
    return TEMP_FILE_RE.match(name) is not None


def find_temp_variants(parent: Path, final_name: str) -> list[Path]:
    """Every on-disk temp-file variant of `final_name` inside `parent` -- the plain
    `<final_name>.lftp` (checked directly, cheap and exact) plus any
    `<final_name>.lftp~<timestamp>~` lftp chose instead (found by scanning `parent`, since the
    timestamp is lftp's own choice, never predictable by us). Used by `core/local_delete.py`
    (so a delete or a stopped-mid-transfer cleanup removes every variant, not just the plain
    one) and by orphan-reaping. Empty (not raising) when `parent` isn't a real directory --
    callers that need it to exist check that themselves.
    """
    if not parent.is_dir():
        return []
    variants: list[Path] = []
    plain = parent / f"{final_name}{TEMP_FILE_SUFFIX}"
    if plain.exists():
        variants.append(plain)
    try:
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name == plain.name or entry.is_dir(follow_symlinks=False):
                    continue
                if strip_temp_suffix(entry.name) == final_name and entry.name != final_name:
                    variants.append(Path(entry.path))
    except OSError:
        pass
    return variants


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

    `is_temp` (2026-08-13, prompts/2026-08-13-lftp-timestamped-temp-files.md) is `True` when
    this entry's *only* on-disk representation is still a temp-suffixed name (`.lftp` or
    `.lftp~<timestamp>~`, `TEMP_FILE_RE`) -- lftp has not yet performed the atomic rename onto
    the real name. `core/reconcile.py` refuses to call an entry with this flag set "complete"
    regardless of what `size` says, even if it happens to equal or exceed the remote size --
    a temp file's reported size can be wrong (a missing/mismatched sidecar falls back to a
    sparse `st_size`, or two processes racing the same target can leave one mid-write) in a way
    a real, already-renamed file's size cannot, and the one thing lftp's own rename-on-completion
    convention is *for* is that a name lftpweb hasn't seen renamed is not yet trustworthy. Always
    `False` for a directory (a directory is never itself temp-suffixed -- only files inside it
    are) and for a genuinely finished file.
    """

    rel_path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    is_temp: bool = False


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

    Checks, in order: a live `.lftp` temp file (if `path` itself doesn't exist yet), then any
    `.lftp~<timestamp>~` variant (`find_temp_variants`, 2026-08-13 -- the largest one, same
    "keep whichever reports more data" tie-break `scan_local` uses), then that candidate's own
    `.lftp-pget-status` sidecar (if present, its accounting wins over raw `st_size`), then plain
    `st_size`. Returns 0 for a path that doesn't exist in any of these forms — a file that
    hasn't started yet reads as 0 bytes done, not an error.
    """
    path = Path(path)
    candidate = path
    if not candidate.exists():
        temp_candidate = path.with_name(path.name + TEMP_FILE_SUFFIX)
        if temp_candidate.exists():
            candidate = temp_candidate
        else:
            variants = find_temp_variants(path.parent, path.name)
            if variants:
                candidate = max(variants, key=lambda p: p.stat().st_size if p.exists() else 0)
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

            is_temp = is_temp_name(name)
            final_name = strip_temp_suffix(name)
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
            if existing is not None:
                # A finished file (no suffix) and a stray temp variant (`.lftp` or
                # `.lftp~<timestamp>~`, 2026-08-13) of the same final name should not both
                # occur, but if they do: a real, already-renamed file always wins over a temp
                # one, regardless of size -- `is_temp` below is what makes that distinction
                # legible to `core/reconcile.py`, so an orphaned temp file must never be the
                # entry that survives once the genuine final file exists. Among two entries of
                # the *same* temp-ness (two temp variants, or -- shouldn't happen -- two real
                # files), keep whichever reports more data rather than letting scan order
                # decide, same rule as before this task.
                if not existing.is_temp and is_temp:
                    continue
                if existing.is_temp == is_temp and existing.size >= size:
                    continue
            entries[final_rel_path] = LocalEntry(
                rel_path=final_rel_path, is_dir=False, size=size, mtime=mtime, is_temp=is_temp
            )

    walk(root, "")
    return entries


def find_temp_files(root: str | Path) -> list[Path]:
    """Recursively, every lftp temp file (`.lftp` or `.lftp~<timestamp>~`, `is_temp_name`)
    anywhere under `root`, by its own real on-disk name.

    Unlike `scan_local`'s output -- which reports a still-temp file under its *final*, stripped
    name so it can be matched against its remote counterpart -- this is for a caller that needs
    the actual leftover path itself: an audit message naming exactly what is still sitting on
    disk (2026-08-14, prompts/2026-08-14-exit-zero-is-not-completion.md's completeness check,
    `core/queue.py._completeness_on_disk`), the row that would have explained a real incident
    at a glance instead of one that just says "500 MB short" with no filename.

    `_UNPACK_`/`_FAILED_` staging directories are skipped, matching every other walk in this
    module.
    """
    root = Path(root)
    found: list[Path] = []
    if not root.is_dir():
        return found

    def walk(dir_path: Path) -> None:
        try:
            with os.scandir(dir_path) as it:
                raw_entries = list(it)
        except OSError:
            return
        for entry in raw_entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name.startswith(UNPACK_PREFIX) or entry.name.startswith(FAILED_PREFIX):
                    continue
                walk(Path(entry.path))
                continue
            if is_temp_name(entry.name):
                found.append(Path(entry.path))

    walk(root)
    return found


def find_orphan_sidecars(root: str | Path) -> list[Path]:
    """Recursively, every `.lftp-pget-status` sidecar under `root` whose carrier file -- the
    same name with the suffix stripped -- is not present in that same directory listing.

    `scan_local`'s own walk only ever *consumes* a sidecar when its carrier is present
    alongside it (used to correct that carrier's reported size); a sidecar whose carrier was
    renamed away or removed without the sidecar being cleaned up alongside it never appears
    anywhere in `scan_local`'s output, so it is otherwise invisible bookkeeping. That is
    exactly the leftover DESIGN.md §4.3's completeness check (`core/queue.py._reap_one`,
    2026-08-14, prompts/2026-08-14-exit-zero-is-not-completion.md) needs to name explicitly:
    a job that exited 0 leaving a stray sidecar behind is evidence lftp's own bookkeeping
    disagrees with what's actually on disk, whatever the byte totals say.

    `_UNPACK_`/`_FAILED_` staging directories are skipped, matching every other walk in this
    module -- extraction hasn't run yet at the point this is called, but a stale one from a
    previous cycle must not be walked into.
    """
    root = Path(root)
    orphans: list[Path] = []
    if not root.is_dir():
        return orphans

    def walk(dir_path: Path) -> None:
        try:
            with os.scandir(dir_path) as it:
                raw_entries = list(it)
        except OSError:
            return
        names = {e.name for e in raw_entries}
        for entry in raw_entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name.startswith(UNPACK_PREFIX) or entry.name.startswith(FAILED_PREFIX):
                    continue
                walk(Path(entry.path))
                continue
            if not entry.name.endswith(PGET_STATUS_SUFFIX):
                continue
            carrier = entry.name[: -len(PGET_STATUS_SUFFIX)]
            if carrier not in names:
                orphans.append(Path(entry.path))

    walk(root)
    return orphans


# --- Orphaned temp-file reaping (2026-08-13, prompts/2026-08-13-lftp-timestamped-temp-files.md) --

# Deliberately several days, not hours: the guard is age alone (see `sweep_orphan_temp_files`'s
# own docstring for why that's safe), and this is the margin against a slow-but-genuinely-alive
# transfer, not a guess. Shorter than `core/extract.py.FAILED_RETENTION_DEFAULT_DAYS` (14 days)
# on purpose -- a `_FAILED_` directory is kept as diagnostic evidence someone might want to
# look at; an orphaned temp file has no diagnostic value at all, just wasted bytes.
ORPHAN_TEMP_FILE_DEFAULT_MAX_AGE_DAYS = 2.0


def sweep_orphan_temp_files(
    queue_root: str | Path, *, max_age_days: float, now: float | None = None
) -> list[tuple[Path, float]]:
    """Remove every stale lftp temp file (`.lftp` or `.lftp~<timestamp>~`, `TEMP_FILE_RE`)
    found anywhere under `queue_root` whose own mtime is older than `max_age_days` -- the
    disk-hygiene half of this task (the root cause -- duplicate concurrent processes -- is
    fixed in `core/queue.py.enqueue_item`/`_admit`; this is for what the bug already left
    behind, plus anything a future crash still manages to orphan).

    **Age-gated, not job-state-gated, deliberately** -- the same shape
    `core/extract.py.sweep_failed_dirs` already uses for its own stale-leftover problem. A temp
    file a live lftp process is actively writing has its mtime refreshed on every write, so it
    can never look older than a few seconds; `net:timeout`/`net:max-retries`
    (`core/lftp.py.build_rc_text`) already make a genuinely stalled connection fail within
    minutes, not days. A multi-day default threshold is therefore a safety margin against a
    slow-but-alive transfer, not a guess, and this function itself has no DB access (matching
    every other pure function in this module) -- a caller wanting to additionally cross-check
    "no active job" can do so with the paths this returns before acting on them.

    Sidecars are removed alongside their own temp file, never independently swept -- an
    orphaned sidecar with no temp file left is already invisible (`scan_local` only ever reads
    one via a `TEMP_FILE_RE`-matched carrier name) and harmless dead weight of at most a few
    hundred bytes. `_UNPACK_`/`_FAILED_` staging directories are skipped, same as `scan_local`'s
    own walk, so this never reaches into another feature's own bookkeeping; the mount sentinel
    is never temp-suffixed so it needs no special-casing here.

    Returns `(path, age_days)` for every file actually removed, so the caller can write one
    `event` row per removal -- the same audit shape `core/extract.py.sweep_failed_dirs` uses.
    """
    root = Path(queue_root)
    if not root.is_dir():
        return []
    now_ts = now if now is not None else time.time()
    removed: list[tuple[Path, float]] = []

    def walk(dir_path: Path) -> None:
        try:
            with os.scandir(dir_path) as it:
                raw_entries = list(it)
        except OSError:
            return
        for entry in raw_entries:
            name = entry.name
            if entry.is_dir(follow_symlinks=False):
                if name.startswith(UNPACK_PREFIX) or name.startswith(FAILED_PREFIX):
                    continue
                walk(Path(entry.path))
                continue
            if name.endswith(PGET_STATUS_SUFFIX) or not is_temp_name(name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            age_days = (now_ts - st.st_mtime) / 86400
            if age_days < max_age_days:
                continue
            path = Path(entry.path)
            path.with_name(path.name + PGET_STATUS_SUFFIX).unlink(missing_ok=True)
            try:
                path.unlink()
            except OSError:
                continue
            removed.append((path, age_days))

    walk(root)
    return removed
