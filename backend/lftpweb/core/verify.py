"""Verification (DESIGN.md §6). `.sfv` / `.md5` sidecars when present; otherwise optional
hash-on-disk. Pure filesystem I/O, no database, no asyncio -- `core/postprocess.py` runs this
off the event loop via `asyncio.to_thread`.

**This is load-bearing, not garnish, for a `move`/`sync` queue** (DESIGN.md §6, §7.3): it is
the one and only gate on an irreversible remote delete. `VerifyResult.state` is one of:

- `VERIFIED`   -- every referenced file's checksum matched (sidecar path), or every file was
                  fully readable end to end *and* the total bytes on disk match the item's
                  known remote size, with no sidecar to compare against (the weaker
                  hash-on-disk fallback -- proves readability and completeness, not per-byte
                  content correctness; see `detail`). The size check (prompts/open-issues.md
                  #3) is what stops a truncated file from passing: reading a short file to EOF
                  raises nothing, so readability alone previously proved nothing about
                  completeness.
- `CORRUPT`     -- a checksum mismatch, a sidecar-referenced file that's missing, or a file
                  that could not be fully read. Exception: see the "all sidecar entries
                  absent" case below.
- `SKIPPED`     -- no sidecar found and hash-on-disk verification is disabled; or (fix,
                  2026-08-15, docs/decisions.md) a sidecar *was* found but every file it
                  references is absent while the item still has other, non-sidecar content.
                  Not a failure: it means "we have no evidence either way." DESIGN.md §7.3:
                  "verification that never ran means no delete" -- callers must treat this
                  the same as `CORRUPT` for gating purposes, never the same as `VERIFIED`.

**The "all sidecar entries absent" case (2026-08-15).** A release rar'd at origin and
extracted *upstream* -- the seedbox's own SABnzbd (or similar) unpacks it, deletes the rars,
and keeps the `.sfv` -- arrives locally as e.g. `movie.mkv` + `movie.sfv`, where the `.sfv`
lists rar volumes that were never local to begin with. Treating every sidecar-referenced
name as "missing" and returning `CORRUPT` is false: nothing was actually corrupted, there is
simply nothing left to check the sidecar against. The rule, narrowed on purpose because this
is the one gate ahead of an irreversible remote delete (`core/postprocess.py`'s
`_maybe_delete_remote`, DESIGN.md §7.3/§7.4):

1. Every sidecar-referenced file absent, **and** at least one non-sidecar, non-archive content
   file exists -- `SKIPPED`. Zero files were verified, so `SKIPPED` (not `VERIFIED`) is the
   honest state; it puts the release at exactly the trust level of a sidecar-less release,
   which is already `SKIPPED` today, and `SKIPPED` already permits the move-mode delete. The
   "non-archive" qualifier (`_has_non_sidecar_content`, using `core/extract.py`'s
   `is_archive_member`) is deliberate: a leftover archive volume is not evidence of an upstream
   extraction (it hasn't been extracted, it's still sitting there), so it must not count as the
   content that makes "every entry absent" look benign -- otherwise a `.sfv` whose one entry
   names a simply-renamed file, sitting beside the real, still-archived, still-unverified rar
   volumes, would also read `SKIPPED` and let an unrelated cleanup step discard them unchecked.
   That exact shape is guarded by a regression test tied to a real prior incident (2026-08-14,
   `tests/test_postprocess.py::test_pipeline_withholds_archive_cleanup_when_verification_failed`).
2. Any referenced file present -- unchanged. The present ones get checksummed as before, any
   mismatch is `CORRUPT`, and **missing-plus-present (a half-deleted archive set) stays
   `CORRUPT`** -- do not relax this. By the time extraction would notice a missing volume,
   the remote copy is already gone; widening the relaxation to partial presence is tracked as
   its own, deliberately separate question (`prompts/open-issues.md` #2 / G1: reordering the
   pipeline so extraction runs before the delete gate).
3. Degenerate case -- the item is the sidecar and nothing else -- stays `CORRUPT`. There is no
   content the sidecar could have been vouching for, so "all entries absent" here is not an
   upstream-extraction signal, it's an empty release.

**Why "missing vs. the sidecar" can only mean an upstream anomaly, never a partial transfer:**
verification runs from `core/postprocess.py`, which only fires after `core/queue.py`'s
local-vs-remote completeness check has already passed (local bytes >= the item's known remote
size, no leftover `.lftp`/temp files -- see that module's `DOWNLOADED` transition). So a file
this module can't find was *also* absent on the remote by the time completeness was measured --
never "still arriving." A sidecar entry missing here is upstream's anomaly to explain, not a
sign this download stalled partway through.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lftpweb.core.extract import is_archive_member

VerifyState = Literal["VERIFIED", "CORRUPT", "SKIPPED"]

_CHUNK_SIZE = 1024 * 1024
_MAX_DETAIL_ITEMS = 20


@dataclass(frozen=True)
class VerifyResult:
    state: VerifyState
    detail: str


def _iter_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file())


def _find_sidecars(root: Path) -> list[Path]:
    """`.sfv`/`.md5` files for this item.

    **A directory item searches its own subtree; a loose top-level file searches only the single
    directory it sits in, never recursively.** That asymmetry is the whole point, and getting it
    wrong is how this function shipped a real defect (2026-08-14, found during a live screenshot
    session): `root.parent` for a loose file at the queue root *is the queue's entire local root*,
    and `rglob` from there walked into every sibling release directory. A 4.3 GB single `.mkv` was
    verified against a twelve-volume rar `.sfv` belonging to an unrelated release two directories
    away, reported `CORRUPT: 12 of 12 checked file(s) failed ... missing`, and — this queue being
    `move` mode — correctly withheld the remote delete for entirely the wrong reason.

    That failure happened to land safe (a false `CORRUPT` withholds a delete). The mirror image
    does not: a loose file whose accidental sidecar happens to list names that *do* exist nearby
    would report `VERIFIED` on evidence about different bytes entirely, and verification is the
    only gate on an irreversible remote delete (§6, §7.3). So the fix is not "recurse less
    eagerly" -- a loose file's sidecar is, by the convention this function's own docstring already
    named, the one sitting *alongside* it.

    A directory item is unchanged: `rglob` over its own subtree is correct, since a release's
    sidecar routinely sits one level down (inside `Sample/`, `Subs/`, or beside the archives).
    """
    if root.is_dir():
        return sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".sfv", ".md5")
        )

    parent = root.parent
    if not parent.is_dir():
        return []
    # `iterdir`, not `rglob`: one directory deep, no descent into siblings.
    return sorted(
        p for p in parent.iterdir() if p.is_file() and p.suffix.lower() in (".sfv", ".md5")
    )


def _parse_sfv(text: str) -> dict[str, int]:
    """`filename -> expected CRC32`. SFV lines are `path CRC32HEX`; comments start with `;`.
    The checksum is always the last whitespace-delimited token, so a filename containing
    spaces still parses correctly.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name, crc_hex = parts
        try:
            out[name.strip()] = int(crc_hex, 16)
        except ValueError:
            continue
    return out


def _parse_md5(text: str) -> dict[str, str]:
    """`filename -> expected md5 hex digest`. GNU `md5sum` format: `HEX  filename` or the
    binary-mode `HEX *filename`.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name.strip()
        if name.startswith("*"):
            name = name[1:]
        out[name] = digest.lower()
    return out


def _crc32_of(path: Path) -> int:
    crc = 0
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            crc = zlib.crc32(chunk, crc)
    return crc


def _md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def _has_non_sidecar_content(root: Path) -> bool:
    """True if the item has any file besides the `.sfv`/`.md5` sidecars themselves and any
    leftover archive volumes -- distinguishes "sidecar plus nothing else" (rule 3, degenerate,
    stays `CORRUPT`) from a release that still has real content alongside a sidecar whose
    entries all point at upstream-extracted archives (rule 1, `SKIPPED`).

    The archive exclusion (`is_archive_member`, `core/extract.py`) matters for a case the
    headline rule's plain wording doesn't by itself rule out: a `.sfv` whose one entry names a
    file that was simply renamed, sitting alongside the *actual*, still-present, still-archived
    rar volumes under their real names. Those volumes are real content, but they are not
    evidence of an upstream extraction -- an archive that's still on disk hasn't been
    extracted, so its mere presence must not be read as "nothing left to verify." Counting it
    as content would flip that case from `CORRUPT` to `SKIPPED` and, on a `move` queue with
    `delete_archives_after_extract`, let the untouched archive_cleanup gate discard the very
    volumes verification never actually checked -- reopening the 2026-08-14 incident this
    project already fixed once (see the `test_pipeline_withholds_archive_cleanup_when_
    verification_failed` regression test in `tests/test_postprocess.py`, and `docs/decisions.md`
    2026-08-15 for the fuller account).
    """
    return any(
        p.suffix.lower() not in (".sfv", ".md5") and not is_archive_member(p)
        for p in _iter_files(root)
    )


def _verify_against_sidecars(root: Path, sidecars: list[Path]) -> VerifyResult:
    mismatches: list[str] = []
    checked = 0
    total_referenced = 0
    missing_count = 0
    for sidecar in sidecars:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
        base = sidecar.parent
        is_sfv = sidecar.suffix.lower() == ".sfv"
        expected_sfv = _parse_sfv(text) if is_sfv else {}
        expected_md5 = _parse_md5(text) if not is_sfv else {}
        expected: dict[str, int | str] = expected_sfv if is_sfv else expected_md5
        for name, expected_value in expected.items():
            total_referenced += 1
            target = base / name
            if not target.is_file():
                missing_count += 1
                mismatches.append(f"{name}: missing")
                continue
            checked += 1
            if is_sfv:
                actual_crc = _crc32_of(target)
                if actual_crc != expected_value:
                    mismatches.append(f"{name}: crc32 {actual_crc:08x} != {expected_value:08x}")
            else:
                actual_md5 = _md5_of(target)
                if actual_md5 != expected_value:
                    mismatches.append(f"{name}: md5 {actual_md5} != {expected_value}")

    # Rule 1: every sidecar-referenced entry is absent, and there's other content in the item
    # for the sidecar to have plausibly been vouching for before an upstream extraction removed
    # it. `total_referenced` guards the pathological empty-sidecar case (nothing referenced at
    # all) from tripping this; `missing_count == total_referenced` requires *zero* present
    # files, so any present-and-checked entry (rule 2, including a half-deleted archive set)
    # falls through to the ordinary mismatch handling below and stays `CORRUPT`.
    if total_referenced and missing_count == total_referenced and _has_non_sidecar_content(root):
        return VerifyResult(
            state="SKIPPED",
            detail=(
                f"sidecar references {total_referenced} file(s), all absent -- archives "
                "likely extracted upstream; nothing verifiable"
            ),
        )

    if mismatches:
        shown = "; ".join(mismatches[:_MAX_DETAIL_ITEMS])
        more = (
            f" (+{len(mismatches) - _MAX_DETAIL_ITEMS} more)"
            if len(mismatches) > _MAX_DETAIL_ITEMS
            else ""
        )
        return VerifyResult(
            state="CORRUPT",
            detail=f"{len(mismatches)} of {checked + len(mismatches)} checked file(s) failed: {shown}{more}",
        )
    return VerifyResult(state="VERIFIED", detail=f"{checked} file(s) matched sidecar checksum")


def _verify_hash_on_disk(root: Path, expected_total_bytes: int | None) -> VerifyResult:
    """The hash-on-disk fallback. `expected_total_bytes` -- the item's known remote size
    (`item.remote_size`, passed in by `core/postprocess.py`) -- is the fix for the bug
    recorded in `prompts/open-issues.md` #3: reading a file fully proves it's *readable*, not
    that it's *complete*. A file truncated mid-transfer reads to EOF without error; only a
    total-bytes comparison against what the remote side actually has catches that. `None`
    (size not known to the caller) skips the check rather than failing closed on missing
    information -- the same permissiveness this fallback already had before this fix, for the
    one case where no better answer is available.
    """
    files = _iter_files(root)
    unreadable: list[str] = []
    total_read = 0
    for f in files:
        try:
            with f.open("rb") as fh:
                while chunk := fh.read(_CHUNK_SIZE):
                    total_read += len(chunk)
        except OSError as exc:
            unreadable.append(f"{f.name}: {exc}")
    if unreadable:
        shown = "; ".join(unreadable[:_MAX_DETAIL_ITEMS])
        return VerifyResult(
            state="CORRUPT", detail=f"{len(unreadable)} file(s) could not be fully read: {shown}"
        )
    if expected_total_bytes is not None and total_read < expected_total_bytes:
        return VerifyResult(
            state="CORRUPT",
            detail=(
                f"hash-on-disk fallback: only {total_read} of {expected_total_bytes} expected "
                "bytes are present on disk -- truncated, still arriving, or the remote grew "
                "after this item was last measured"
            ),
        )
    return VerifyResult(
        state="VERIFIED",
        detail=(
            f"no .sfv/.md5 sidecar found; {len(files)} file(s) fully read on disk, "
            f"{total_read} bytes total"
            + (
                f" (matches the {expected_total_bytes}-byte remote total)"
                if expected_total_bytes is not None
                else ""
            )
            + " (hash-on-disk fallback -- confirms readability and total size, not per-byte "
            "content correctness)"
        ),
    )


def verify_item(
    root: str | Path,
    *,
    hash_on_disk_fallback: bool = False,
    expected_total_bytes: int | None = None,
) -> VerifyResult:
    """Verify one item's local files. See the module docstring for the three possible
    results. `root` is the item's own local path (a directory for a release, a single file
    for a loose top-level file).

    `expected_total_bytes` only matters for the hash-on-disk fallback (see
    `_verify_hash_on_disk`) -- a `.sfv`/`.md5` sidecar's own per-file checksums are already a
    strictly stronger completeness+correctness check and don't need it.
    """
    root = Path(root)
    sidecars = _find_sidecars(root)
    if sidecars:
        return _verify_against_sidecars(root, sidecars)

    if not hash_on_disk_fallback:
        return VerifyResult(
            state="SKIPPED",
            detail="no .sfv/.md5 sidecar found and hash-on-disk verification is disabled",
        )

    return _verify_hash_on_disk(root, expected_total_bytes)
