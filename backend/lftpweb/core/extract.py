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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BINARY = os.environ.get("LFTPWEB_7Z_BIN", "7zz")

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


def extract_item(
    root: Path,
    *,
    target_dir: Path | None = None,
    passwords: tuple[str, ...] = (),
    binary: str = DEFAULT_BINARY,
) -> ExtractResult:
    """Extract every archive found under `root` (DESIGN.md §6). `target_dir=None` means "in
    place" -- each archive is extracted into its own containing directory. An item with no
    archives at all (most items) is a no-op success, not a failure -- extraction is opt-in
    per queue and most releases aren't archives to begin with.
    """
    archives = find_archives(root)
    if not archives:
        return ExtractResult(ok=True, detail="no archives found")

    failures: list[str] = []
    extracted_dirs: list[Path] = []
    for archive in archives:
        dest = target_dir if target_dir is not None else archive.parent
        result = extract_archive(archive, dest, passwords=passwords, binary=binary)
        if result.ok:
            extracted_dirs.extend(result.extracted_dirs)
        else:
            failures.append(result.detail)

    if failures:
        return ExtractResult(
            ok=False,
            detail=f"{len(failures)} of {len(archives)} archive(s) failed: " + "; ".join(failures),
            extracted_dirs=tuple(extracted_dirs),
        )
    return ExtractResult(
        ok=True,
        detail=f"extracted {len(archives)} of {len(archives)} archive(s)",
        extracted_dirs=tuple(extracted_dirs),
    )
