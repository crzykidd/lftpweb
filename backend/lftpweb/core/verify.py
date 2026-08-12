"""Verification (DESIGN.md §6). `.sfv` / `.md5` sidecars when present; otherwise optional
hash-on-disk. Pure filesystem I/O, no database, no asyncio -- `core/postprocess.py` runs this
off the event loop via `asyncio.to_thread`.

**This is load-bearing, not garnish, for a `move`/`sync` queue** (DESIGN.md §6, §7.3): it is
the one and only gate on an irreversible remote delete. `VerifyResult.state` is one of:

- `VERIFIED`   -- every referenced file's checksum matched (sidecar path), or every file was
                  fully readable end to end with no sidecar to compare against (the weaker
                  hash-on-disk fallback -- proves readability, not content correctness; see
                  `detail`).
- `CORRUPT`     -- a checksum mismatch, a sidecar-referenced file that's missing, or a file
                  that could not be fully read.
- `SKIPPED`     -- no sidecar found and hash-on-disk verification is disabled. Not a failure:
                  it means "we have no evidence either way." DESIGN.md §7.3: "verification
                  that never ran means no delete" -- callers must treat this the same as
                  `CORRUPT` for gating purposes, never the same as `VERIFIED`.
"""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    """`.sfv`/`.md5` files under `root` (or, for a loose top-level file item, its parent --
    a sidecar for a single file conventionally sits alongside it, not "inside" it).
    """
    search_root = root if root.is_dir() else root.parent
    if not search_root.is_dir():
        return []
    return sorted(
        p for p in search_root.rglob("*") if p.is_file() and p.suffix.lower() in (".sfv", ".md5")
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


def _verify_against_sidecars(sidecars: list[Path]) -> VerifyResult:
    mismatches: list[str] = []
    checked = 0
    for sidecar in sidecars:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
        base = sidecar.parent
        is_sfv = sidecar.suffix.lower() == ".sfv"
        expected_sfv = _parse_sfv(text) if is_sfv else {}
        expected_md5 = _parse_md5(text) if not is_sfv else {}
        expected: dict[str, int | str] = expected_sfv if is_sfv else expected_md5
        for name, expected_value in expected.items():
            target = base / name
            if not target.is_file():
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


def _verify_hash_on_disk(root: Path) -> VerifyResult:
    files = _iter_files(root)
    unreadable: list[str] = []
    for f in files:
        try:
            with f.open("rb") as fh:
                while fh.read(_CHUNK_SIZE):
                    pass
        except OSError as exc:
            unreadable.append(f"{f.name}: {exc}")
    if unreadable:
        shown = "; ".join(unreadable[:_MAX_DETAIL_ITEMS])
        return VerifyResult(
            state="CORRUPT", detail=f"{len(unreadable)} file(s) could not be fully read: {shown}"
        )
    return VerifyResult(
        state="VERIFIED",
        detail=(
            f"no .sfv/.md5 sidecar found; {len(files)} file(s) fully read on disk "
            "(hash-on-disk fallback -- confirms readability, not content correctness)"
        ),
    )


def verify_item(root: str | Path, *, hash_on_disk_fallback: bool = False) -> VerifyResult:
    """Verify one item's local files. See the module docstring for the three possible
    results. `root` is the item's own local path (a directory for a release, a single file
    for a loose top-level file).
    """
    root = Path(root)
    sidecars = _find_sidecars(root)
    if sidecars:
        return _verify_against_sidecars(sidecars)

    if not hash_on_disk_fallback:
        return VerifyResult(
            state="SKIPPED",
            detail="no .sfv/.md5 sidecar found and hash-on-disk verification is disabled",
        )

    return _verify_hash_on_disk(root)
