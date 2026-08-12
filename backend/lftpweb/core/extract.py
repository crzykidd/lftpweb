"""Archive extraction via `7zz` (DESIGN.md §6) -- the image's only archive tool. See `NOTICE`
and `docs/decisions.md`: 7-Zip 21.07+ reads rar/rar5/zip/7z/tar/gz/bz2/xz natively, so there is
no `unrar` anywhere in this project, deliberately.

`binary` defaults to `"7zz"` (the Alpine `7zip` package's binary name, matching the runtime
image) but is always an overridable parameter -- exactly `core/lftp.py.spawn`'s `lftp_bin`
pattern -- because the *development* host this project is built on names the same real 7-Zip
binary `7z` (Debian/Ubuntu's `7zip` package). Tests pass `binary="7z"` (or set
`LFTPWEB_7Z_BIN`) to run against a real local binary without needing the container image.

Every subprocess call passes `stdin=DEVNULL`: 7z prompts for a password on an encrypted
archive if none (or the wrong one) was given, and a prompt with no stdin attached must fail
fast, not hang the postprocessing worker forever.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BINARY = os.environ.get("LFTPWEB_7Z_BIN", "7zz")

# Extraction writes under these names, never the final one, until it is known to have
# succeeded (DESIGN.md §6). Same convention `core/lftp.py` already uses for in-flight
# downloads (`xfer:use-temp-file`) applied to the one step that didn't have it: an *arr
# watching the download tree must see nothing, then see a complete release, never a growing,
# importable-looking file mid-extraction.
UNPACK_PREFIX = "_UNPACK_"
FAILED_PREFIX = "_FAILED_"

# Extensions this module will try to extract as a *direct* 7zz target. Compound tar formats
# need two passes (see `_is_compound_tar`); rar multi-part sets are filtered in `find_archives`
# so only the first volume is ever a target (DESIGN.md §6: "extract from the first volume
# only" -- 7zz itself follows the rest of the set once given that one).
_SIMPLE_SUFFIXES = (".zip", ".7z", ".tar", ".gz", ".bz2", ".xz")
_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz")

_RAR_PART_RE = re.compile(r"\.part(?P<n>\d+)\.rar$", re.IGNORECASE)
_RAR_OLD_VOLUME_RE = re.compile(r"\.r\d{2,}$", re.IGNORECASE)  # .r00, .r01, ... (old-style)

_SUBPROCESS_TIMEOUT_S = 600


@dataclass(frozen=True)
class ExtractResult:
    ok: bool
    detail: str
    extracted_dirs: tuple[Path, ...] = ()


def _is_first_rar_volume(name_lower: str) -> bool:
    m = _RAR_PART_RE.search(name_lower)
    if m:
        return int(m.group("n")) == 1
    return True  # a bare .rar, or a name not using the .partNN.rar convention


def _is_compound_tar(name_lower: str) -> bool:
    return name_lower.endswith(_COMPOUND_SUFFIXES)


def find_archives(root: Path) -> list[Path]:
    """Every archive under `root` that should itself be handed to 7zz. Multi-part rar sets
    contribute only their first volume; old-style `.r00`/`.r01`/... continuation volumes are
    never a direct target at all (7zz reads them once given the `.rar` head of the set).
    """
    candidates = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())

    out: list[Path] = []
    for p in candidates:
        name_lower = p.name.lower()
        if _RAR_OLD_VOLUME_RE.search(name_lower) and not name_lower.endswith(".rar"):
            continue
        if name_lower.endswith(".rar"):
            if _is_first_rar_volume(name_lower):
                out.append(p)
            continue
        if _is_compound_tar(name_lower) or name_lower.endswith(_SIMPLE_SUFFIXES):
            out.append(p)
    return out


def _run_7z(binary: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def extract_archive(
    archive: Path,
    target_dir: Path,
    *,
    passwords: tuple[str, ...] = (),
    binary: str = DEFAULT_BINARY,
) -> ExtractResult:
    """Extract one archive into `target_dir` (created if needed).

    Compound tar formats (`.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`) need two
    7zz passes -- one to strip the outer compression, one to unpack the resulting `.tar` --
    because 7-Zip only peels one layer of a chained format per invocation. The intermediate
    `.tar` lives in a throwaway subdirectory of `target_dir` that is removed either way.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    attempts = list(passwords) if passwords else [None]
    last_error = ""

    for password in attempts:
        pw_args = [f"-p{password}"] if password else []
        if _is_compound_tar(archive.name.lower()):
            with tempfile.TemporaryDirectory(dir=target_dir) as tmp:
                tmp_path = Path(tmp)
                result = _run_7z(binary, ["x", str(archive), f"-o{tmp_path}", "-y", *pw_args])
                if result.returncode != 0:
                    last_error = (result.stderr or result.stdout).strip()
                    continue
                inner_tars = list(tmp_path.glob("*.tar"))
                if not inner_tars:
                    last_error = "expected an intermediate .tar after decompression, found none"
                    continue
                result2 = _run_7z(
                    binary, ["x", str(inner_tars[0]), f"-o{target_dir}", "-y", *pw_args]
                )
                if result2.returncode != 0:
                    last_error = (result2.stderr or result2.stdout).strip()
                    continue
                return ExtractResult(
                    ok=True,
                    detail=f"extracted {archive.name} (compound tar)",
                    extracted_dirs=(target_dir,),
                )
        else:
            result = _run_7z(binary, ["x", str(archive), f"-o{target_dir}", "-y", *pw_args])
            if result.returncode == 0:
                return ExtractResult(
                    ok=True, detail=f"extracted {archive.name}", extracted_dirs=(target_dir,)
                )
            last_error = (result.stderr or result.stdout).strip()

    return ExtractResult(ok=False, detail=f"{archive.name}: {last_error or 'extraction failed'}")


def _staging_dirs(final_dir: Path) -> tuple[Path, Path]:
    """The `_UNPACK_`/`_FAILED_` siblings of `final_dir`. Always siblings, never children --
    a child would sit inside the tree `core/local_scan.py` walks (and inside anything a later
    post-processing move relocates), which would defeat the point of staging at all.
    """
    return (
        final_dir.parent / f"{UNPACK_PREFIX}{final_dir.name}",
        final_dir.parent / f"{FAILED_PREFIX}{final_dir.name}",
    )


def extract_item(
    root: Path,
    *,
    target_dir: Path | None = None,
    passwords: tuple[str, ...] = (),
    binary: str = DEFAULT_BINARY,
) -> ExtractResult:
    """Extract every archive found under `root` (DESIGN.md §6). `target_dir=None` means "in
    place" -- the final directory is `root` itself. An item with no archives at all (most
    items) is a no-op success, not a failure -- extraction is opt-in per queue and most
    releases aren't archives to begin with, and a no-op must leave no `_UNPACK_` litter
    behind for an item that was never touched.

    Every archive extracts into a `_UNPACK_<name>` directory staged as a *sibling* of the
    final directory (see `_staging_dirs`), never in place under the final name. Once every
    archive under the item has extracted cleanly, the staging directory's contents are merged
    into the final directory -- reusing `core/postprocess.py.move_tree`'s cross-device-safe
    `merge=True` mode, since the final directory routinely already exists (it already holds
    the source archives) -- and the now-empty staging directory is gone by the time
    `move_tree` returns. On any archive failure, the staging directory is renamed to
    `_FAILED_<name>` and left in place as diagnostic evidence rather than deleted; a
    `_FAILED_` directory left over from an earlier attempt is replaced, not treated as a
    conflict blocking a retry.
    """
    archives = find_archives(root)
    if not archives:
        return ExtractResult(ok=True, detail="no archives found")

    # `root` is a directory for the common case (a release), but §4.7's loose top-level file
    # can make `root` the archive itself -- "its own directory" is then `root.parent`, the
    # same containing directory the pre-staging code used as `archive.parent` for that case.
    in_place_dir = root.parent if root.is_file() else root
    final_dir = target_dir if target_dir is not None else in_place_dir
    staging_dir, failed_dir = _staging_dirs(final_dir)
    # Leftovers from a killed prior attempt are stale, not this run's output -- clear both
    # before starting so neither can be mistaken for this attempt's result.
    if failed_dir.exists():
        shutil.rmtree(failed_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for archive in archives:
        # An archive nested under a subdirectory of `root` (a multi-CD release, say) extracts
        # into the matching subdirectory of staging, reproducing the layout `target_dir=None`
        # would have produced extracting in place. A configured `extract_target_dir` keeps
        # its existing flat behavior (one shared destination for every archive in the item;
        # see docs/decisions.md) -- staging just interposes ahead of it.
        rel = archive.parent.relative_to(in_place_dir) if target_dir is None else Path()
        result = extract_archive(archive, staging_dir / rel, passwords=passwords, binary=binary)
        if not result.ok:
            failures.append(result.detail)

    if failures:
        staging_dir.rename(failed_dir)
        return ExtractResult(
            ok=False,
            detail=(
                f"{len(failures)} of {len(archives)} archive(s) failed: "
                + "; ".join(failures)
                + f" -- partial output kept at {failed_dir}"
            ),
        )

    # Imported locally: `core/postprocess.py` imports this module at the top level, so a
    # top-level import here would be a circular import.
    from lftpweb.core.postprocess import move_tree

    try:
        move_tree(staging_dir, final_dir, merge=True)
    except Exception as exc:  # noqa: BLE001 - reported as EXTRACT_FAILED, never raised
        if staging_dir.exists():
            if failed_dir.exists():
                shutil.rmtree(failed_dir)
            staging_dir.rename(failed_dir)
        return ExtractResult(
            ok=False,
            detail=(
                f"extracted {len(archives)} archive(s) but placing them under {final_dir} "
                f"failed: {exc} -- partial output kept at {failed_dir}"
            ),
        )

    return ExtractResult(
        ok=True,
        detail=f"extracted {len(archives)} of {len(archives)} archive(s)",
        extracted_dirs=(final_dir,),
    )
