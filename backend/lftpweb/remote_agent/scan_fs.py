#!/usr/bin/env python3
"""Stdlib-only fallback remote scanner (DESIGN.md §5, §15.7).

Uploaded over SFTP and run with the remote `python3` **only** when `find -printf` is
detected to be unsupported (busybox/BSD `find`, e.g. Alpine seedboxes). Not deployed
permanently and not md5-compared/reinstalled the way SeedSync's `scan_fs.py` was — see
`core/remote.py`'s module docstring for why that whole class of machinery goes away once the
script itself is this small.

Emits the identical wire format as `find <path> -mindepth 1 -printf '%y\\t%s\\t%T@\\t%p\\n'`,
so `core/remote.py`'s `parse_find_records` is the single parser for both paths. No third-party
imports — this has to run with whatever stock `python3` happens to be on the seedbox.

**`--inodes`** (docs/download-client-framework-spec.md §11.1b, `core/remote.py.
RemoteConnectionPool.scan_with_inodes`): emits the five-field wire format
`%y\\t%i\\t%n\\t%s\\t%T@\\t%p\\n` instead -- inode and link count, from `os.lstat`'s own
`st_ino`/`st_nlink`, already captured for every entry below and simply never read before this
flag existed. Stdlib-only and requires nothing BusyBox `find` would need to support, which is
the whole point: the fallback path can supply inode data exactly as completely as GNU `find
-printf` can, so there is no "BusyBox can't do this" case to degrade from.
"""

from __future__ import annotations

import os
import stat as stat_module
import sys


def emit(path: str, is_dir: bool, size: int, mtime: float) -> None:
    type_char = "d" if is_dir else "f"
    # %T@ is seconds.nanoseconds; %.9f matches GNU find's own precision closely enough for
    # our purposes (mtime is advisory here, not used for completeness).
    sys.stdout.write(f"{type_char}\t{size}\t{mtime:.9f}\t{path}\n")


def emit_with_inode(
    path: str, is_dir: bool, inode: int, nlink: int, size: int, mtime: float
) -> None:
    type_char = "d" if is_dir else "f"
    sys.stdout.write(f"{type_char}\t{inode}\t{nlink}\t{size}\t{mtime:.9f}\t{path}\n")


def walk(root: str, *, with_inodes: bool = False) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # os.walk itself already skips descending into symlinked directories when
        # followlinks=False, matching GNU find's default (§5) — but it still lists the
        # symlink as a dirname; that's fine, we just don't recurse into it further below,
        # and its own type isn't reported (matching this module's `f`/`d`-only scope, same
        # as records_to_entries on the parsing side).
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat_module.S_ISDIR(st.st_mode):
                continue  # a symlink masquerading as a dirname; not modeled, skip
            if with_inodes:
                emit_with_inode(full, True, st.st_ino, st.st_nlink, 0, st.st_mtime)
            else:
                emit(full, True, 0, st.st_mtime)

        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat_module.S_ISREG(st.st_mode):
                continue  # symlink, device, fifo, etc. — not modeled, skip (matches remote.py)
            if with_inodes:
                emit_with_inode(full, False, st.st_ino, st.st_nlink, st.st_size, st.st_mtime)
            else:
                emit(full, False, st.st_size, st.st_mtime)


def main() -> int:
    args = sys.argv[1:]
    with_inodes = "--inodes" in args
    if with_inodes:
        args = [a for a in args if a != "--inodes"]
    if len(args) != 1:
        sys.stderr.write("usage: scan_fs.py [--inodes] <path>\n")
        return 2

    root = args[0]

    # DESIGN.md §15.10: filenames can contain bytes that aren't valid UTF-8. `os.fsdecode`
    # (which os.walk/os.listdir use internally) already applies `surrogateescape` on POSIX,
    # so paths round-trip as Python str; the only remaining risk is *writing* them back out,
    # since stdout defaults to strict UTF-8 and would raise on a lone surrogate. Reconfigure
    # it to use the matching error handler so a filename can never crash this script.
    sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")

    if not os.path.isdir(root):
        sys.stderr.write(f"scan_fs.py: not a directory: {root}\n")
        return 1

    walk(root, with_inodes=with_inodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
