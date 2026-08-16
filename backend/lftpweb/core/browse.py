"""Path-browse resolution for Settings -> Queues' `remote_path`/`local_path`/`staging_path`
fields (DESIGN.md §9.2, GitHub issue #4, `prompts/done/2026-08-16-path-browse-dialog.md`) and
the save-time path validation added mid-task (same prompt's "Scope addition" section). Two
concerns share this module because they share one primitive -- "can this path actually be
listed/stat'd right now" -- for both the container's own filesystem and the seedbox over SFTP:

- **Browsing** (`resolve_local_dir`/`resolve_remote_dir`) never fails on bad input -- a path
  that doesn't exist, isn't a directory, or can't be read walks up to the nearest listable
  ancestor instead, so a half-typed field still opens somewhere useful (see each function's own
  docstring for the exact algorithm).
- **Save-time validation** (`local_directory_error`/`remote_directory_error`) does the opposite
  on purpose: a real answer, not a graceful fallback, because the whole point is to catch a typo
  instead of silently accepting it. A mistyped `local_path` used to surface only as a WARNING
  log line from `core/autoqueue.py.on_scan`, discovered hours later (docs/decisions.md).

`api/browse.py` is the thin HTTP wrapper for the first; `api/settings_queues.py` calls the
second directly, inline with the rest of its save-time validation. Kept in `core/` rather than
either API module so the resolution logic is testable without a live SSH connection (local) or
with only the fake-seedbox harness (remote), the same split every other `core/*.py` module in
this project follows (DESIGN.md §12).
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass

import asyncssh

# Mirrors api/history.py's MAX_LIMIT precedent -- a browse listing is for a human to click
# through, not a bulk export; capping it (rather than paging it) keeps both endpoints' response
# shape simple and keeps a directory with 100k entries from becoming a slow request.
MAX_ENTRIES = 500


@dataclass(frozen=True)
class BrowseEntry:
    name: str


@dataclass(frozen=True)
class BrowseResult:
    path: str
    parent: str | None
    entries: list[BrowseEntry]
    truncated: bool
    fallback_from: str | None


class LocalRootUnlistableError(Exception):
    """`/` itself could not be listed -- the one 500-worthy case for the local browse endpoint
    (prompts/done/2026-08-16-path-browse-dialog.md); every other bad local input walks up
    instead of raising.
    """


class RemoteBrowseError(Exception):
    """The remote directory could not be listed at all -- connection lost mid-walk, or even the
    SSH user's home and `/` both refused. Surfaced by `api/browse.py` as a clean 502, never a
    bare 500 traceback.
    """


def _parent_of(path: str) -> str | None:
    """Absolute parent of an already-normalized absolute `path`, or `None` at `/`."""
    if path == "/":
        return None
    return os.path.dirname(path.rstrip("/")) or "/"


def _entries_from_names(names: list[str]) -> tuple[list[BrowseEntry], bool]:
    ordered = sorted(names)
    truncated = len(ordered) > MAX_ENTRIES
    return [BrowseEntry(name=n) for n in ordered[:MAX_ENTRIES]], truncated


# --- Local (os.scandir) ----------------------------------------------------------------------


def _try_list_local(path: str) -> list[str] | None:
    """Directory names only at `path` (a symlink that resolves to a directory counts as one),
    or `None` if `path` doesn't exist, isn't a directory, or can't be read. The one "is this
    listable" primitive both `resolve_local_dir`'s walk-up and `local_directory_error`'s hard
    check use, so the two can never disagree about what "listable" means.
    """
    try:
        with os.scandir(path) as it:
            names = []
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=True):
                        names.append(entry.name)
                except OSError:
                    continue  # a broken symlink or a stat race -- skip it, don't fail the pass
            return names
    except OSError:
        return None


def resolve_local_dir(raw_path: str | None) -> BrowseResult:
    """Empty or non-absolute input -- including `~`, which is meaningless in this container
    (the app user has no real home, DESIGN.md §11.2's numeric PUID/PGID identity) -- opens at
    `/` outright, with no `fallback_from` note; `/` is a deliberate, sane starting point, not a
    failure to apologize for. An absolute path that can't be listed as given (missing, not a
    directory, permission denied) walks up to the nearest listable ancestor instead, with
    `fallback_from` set to exactly what was asked for.

    Raises `LocalRootUnlistableError` only if `/` itself can't be listed -- everything else
    about a bad `raw_path` resolves to *something*, never an error.
    """
    requested = (raw_path or "").strip()
    if not requested or not requested.startswith("/"):
        names = _try_list_local("/")
        if names is None:
            raise LocalRootUnlistableError("/ could not be listed")
        entries, truncated = _entries_from_names(names)
        return BrowseResult(
            path="/", parent=None, entries=entries, truncated=truncated, fallback_from=None
        )

    # Normalizing first (collapsing "..", a trailing slash, etc.) is canonicalization, not a
    # fallback -- fallback_from must only fire when the walk-up loop below actually had to move
    # off the (normalized) requested path because it wasn't listable.
    normalized = os.path.normpath(requested)
    current = normalized
    while True:
        names = _try_list_local(current)
        if names is not None:
            break
        if current == "/":
            raise LocalRootUnlistableError("/ could not be listed")
        current = _parent_of(current) or "/"

    entries, truncated = _entries_from_names(names)
    fallback_from = requested if current != normalized else None
    return BrowseResult(
        path=current,
        parent=_parent_of(current),
        entries=entries,
        truncated=truncated,
        fallback_from=fallback_from,
    )


def local_directory_error(path: str) -> str | None:
    """`None` if `path` exists, is a directory, and can be listed; otherwise a short, specific
    reason naming what's wrong -- the save-time check behind Settings -> Queues' `local_path`
    and (when set) `staging_path` (mid-run scope addition to
    `prompts/done/2026-08-16-path-browse-dialog.md`).

    Deliberately distinct from `resolve_local_dir` above: that function exists to let a
    half-typed field still open a browse dialog somewhere useful and so never fails; this one
    exists specifically to give a real, blocking answer, because catching a typo at save time
    is the entire point.

    Deliberately does **not** require the mount sentinel (`core/mount_sentinel.py.check`) --
    a brand-new queue's local root has never been scanned yet, so demanding the sentinel here
    would refuse every legitimate first save. Never creates the directory (the caller's own
    instruction): a not-yet-mounted root must never earn trust just because something tried to
    write into it, the identical reasoning `mount_sentinel.write_if_needed`'s own docstring
    gives for the same restraint.
    """
    if not path.startswith("/"):
        return f"{path!r} is not an absolute path"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"{path!r} does not exist"
    except NotADirectoryError:
        return f"{path!r} is not a directory (a path component is a file)"
    except PermissionError:
        return f"{path!r} cannot be read (permission denied)"
    except OSError as exc:
        return f"{path!r} could not be checked: {exc}"
    if not stat_module.S_ISDIR(st.st_mode):
        return f"{path!r} exists but is not a directory"
    if not os.access(path, os.R_OK | os.X_OK):
        return f"{path!r} exists but is not readable"
    return None


# --- Remote (SFTP over the pooled connection) ---------------------------------------------


def _posix_join(parent: str, name: str) -> str:
    if parent == "/":
        return "/" + name
    return parent.rstrip("/") + "/" + name


async def _try_list_remote(sftp: asyncssh.SFTPClient, path: str) -> list[str] | None:
    """`_try_list_local`'s remote counterpart. A symlink entry needs one extra `stat` (which
    follows symlinks by default) to learn what it actually points at -- `scandir`/`readdir`
    attrs describe the link itself, not its target (DESIGN.md §9.2: "a symlink that resolves to
    a directory counts as one").
    """
    try:
        names: list[str] = []
        async for entry in sftp.scandir(path):
            filename = entry.filename
            if filename in (".", ".."):
                continue
            attrs = entry.attrs
            is_dir = attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
            if attrs.type == asyncssh.FILEXFER_TYPE_SYMLINK:
                try:
                    target_attrs = await sftp.stat(_posix_join(path, filename))
                    is_dir = target_attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY
                except asyncssh.SFTPError:
                    is_dir = False  # a broken symlink -- not a directory, skip it
            if is_dir:
                names.append(filename)
        return names
    except asyncssh.SFTPError:
        return None


async def resolve_remote_dir(sftp: asyncssh.SFTPClient, raw_path: str | None) -> BrowseResult:
    """`~` and relative paths resolve against the SSH user's home via SFTP `realpath` -- unlike
    the local endpoint, `~` is meaningful here. Same nearest-listable-ancestor walk-up as local
    once a starting point is known; the ultimate fallback (a genuinely degenerate seedbox --
    every ancestor of the resolved path, all the way to `/`, refuses to list) is the home
    directory once, then `/`.

    Raises `RemoteBrowseError` only if nothing at all could be listed -- home included.
    """
    requested = (raw_path or "").strip()
    try:
        home = await sftp.realpath(".")
    except asyncssh.SFTPError as exc:
        raise RemoteBrowseError(f"could not resolve the seedbox home directory: {exc}") from exc

    if not requested:
        base = home
    else:
        try:
            base = await sftp.realpath(requested)
        except asyncssh.SFTPError:
            # realpath itself refused (an unusual server) -- fall back to the literal absolute
            # input, or home for a relative/~ one, rather than giving up before the walk-up
            # even starts.
            base = requested if requested.startswith("/") else home

    current = base
    home_tried = False
    while True:
        names = await _try_list_remote(sftp, current)
        if names is not None:
            break
        if current == "/":
            if not home_tried and home != "/":
                home_tried = True
                current = home
                continue
            raise RemoteBrowseError(
                f"neither {base!r}, the home directory {home!r}, nor / could be listed "
                "on the seedbox"
            )
        current = _parent_of(current) or "/"

    entries, truncated = _entries_from_names(names)
    fallback_from = requested if requested and current != base else None
    return BrowseResult(
        path=current,
        parent=_parent_of(current),
        entries=entries,
        truncated=truncated,
        fallback_from=fallback_from,
    )


class RemotePathNotFoundError(Exception):
    """The remote SFTP `stat` cleanly reports `path` missing or not a directory -- a real
    answer, not a connectivity failure. `api/settings_queues.py` turns this into a 400; every
    other exception `remote_directory_error` can raise is left to propagate as whatever asyncssh
    raised, which the caller treats as "cannot verify right now" and allows the save (best-
    effort validation, mid-run scope addition -- docs/decisions.md has the full reasoning for
    why a seedbox outage must never lock a user out of editing Queues).
    """


async def remote_directory_error(sftp: asyncssh.SFTPClient, path: str) -> None:
    """Raises `RemotePathNotFoundError` iff the seedbox clearly reports `path` missing or not a
    directory. Any other failure (permission denied stat'ing an otherwise-real path, a protocol
    hiccup) is deliberately left to propagate as itself -- deciding that an ambiguous failure
    means "allow the save" is the caller's job, not this function's.
    """
    try:
        attrs = await sftp.stat(path)
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath) as exc:
        raise RemotePathNotFoundError(f"{path!r} does not exist on the seedbox") from exc
    if attrs.type != asyncssh.FILEXFER_TYPE_DIRECTORY:
        raise RemotePathNotFoundError(f"{path!r} exists on the seedbox but is not a directory")
