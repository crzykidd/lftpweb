"""Archive extraction via two tools (DESIGN.md §6; the 2026-08-12 rar fix -- see NOTICE and
docs/decisions.md): `7zz` for zip/7z/tar/gz/bz2/xz, and `unrar` for rar/rar5.

**This project shipped nine phases believing 7zz alone covered rar too -- it never did.**
Alpine's `7zip` package (the one this image actually ships) is built without the RAR codec at
all: `7zz i` lists no `Rar`/`Rar5` handler, distros strip it because 7-Zip's RAR decoder derives
from unRAR source, whose licence they won't ship in `main`. `rar (unrar)` was DESIGN.md §6's
original wording all along; a 2026-08-11 decision (docs/decisions.md) replaced it with "7zz
covers everything" on the strength of upstream 7-Zip's own native RAR support, without verifying
that Alpine's *build* of 7zz carried it. It didn't, and no test ever built a real rar to catch
that. `unrar` is back, built from RARLAB source in the image's builder stage (`docker/
Dockerfile`) -- see docs/decisions.md for the licence position (freeware, redistribution
permitted, decompression only) and why `libarchive-tools`/`bsdtar` was rejected.

`binary` (7zz) defaults to `"7zz"` (the Alpine `7zip` package's binary name, matching the runtime
image) but is always an overridable parameter -- exactly `core/lftp.py.spawn`'s `lftp_bin`
pattern -- because the *development* host this project is built on names the same real 7-Zip
binary `7z` (Debian/Ubuntu's `7zip` package). Tests pass `binary="7z"` (or set
`LFTPWEB_7Z_BIN`) to run against a real local binary without needing the container image.
`rar_binary` (unrar) follows the identical pattern: defaults to `"unrar"`, overridable, and
`LFTPWEB_UNRAR_BIN` env-overridable for a dev host where it isn't on `PATH` under that name.

Every subprocess call passes `stdin=DEVNULL`: both tools prompt for a password on an encrypted
archive if none (or the wrong one) was given, and a prompt with no stdin attached must fail
fast, not hang the postprocessing worker forever. `unrar` additionally gets `-p-` (disable the
password prompt outright) when no password is being tried, rather than relying on the closed
stdin alone -- belt and suspenders, and `-p-` also short-circuits `unrar` faster than waiting on
a doomed read.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DEFAULT_BINARY = os.environ.get("LFTPWEB_7Z_BIN", "7zz")

# The rar decoder (2026-08-12 fix -- see module docstring and docs/decisions.md). Built from
# RARLAB unrar source in `docker/Dockerfile`'s builder stage; `unrar` is the binary name on
# both the runtime image and a dev host that built it the same way, so unlike `DEFAULT_BINARY`
# there is no dev-vs-container name split -- the env override exists for the same reason
# `LFTPWEB_7Z_BIN` does: a test or operator pointing at a differently-named binary.
DEFAULT_RAR_BINARY = os.environ.get("LFTPWEB_UNRAR_BIN", "unrar")

# `extract_item`'s "nothing to extract" outcome (fix, 2026-08-12 -- docs/decisions.md): named
# once here so `core/postprocess.py`'s own pre-check (which skips the step entirely rather than
# calling into this module at all -- see that module's `_do_extract`) and this module's own
# late-discovery fallback branch can never drift onto two different strings for the same event.
NO_ARCHIVES_DETAIL = "no archives found"

# `_FAILED_` staging directories are kept as diagnostic evidence on failure (see
# `_staging_dirs`) but nothing removed them until this fix -- see docs/decisions.md for why
# 14 days was chosen and why the sweep that acts on this default ships off.
FAILED_RETENTION_DEFAULT_DAYS = 14.0

# Extraction writes under these names, never the final one, until it is known to have
# succeeded (DESIGN.md §6). Same convention `core/lftp.py` already uses for in-flight
# downloads (`xfer:use-temp-file`) applied to the one step that didn't have it: an *arr
# watching the download tree must see nothing, then see a complete release, never a growing,
# importable-looking file mid-extraction.
UNPACK_PREFIX = "_UNPACK_"
FAILED_PREFIX = "_FAILED_"

# Extensions this module will try to extract as a *direct* 7zz target (rar goes to `unrar`
# instead -- see `extract_archive`). Compound tar formats need two passes (see
# `_is_compound_tar`); rar multi-part sets are filtered in `find_archives` so only the first
# volume is ever a target (DESIGN.md §6: "extract from the first volume only" -- `unrar`
# follows the rest of the set on its own once given that one).
_SIMPLE_SUFFIXES = (".zip", ".7z", ".tar", ".gz", ".bz2", ".xz")
_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz")

_RAR_PART_RE = re.compile(r"\.part(?P<n>\d+)\.rar$", re.IGNORECASE)
_RAR_OLD_VOLUME_RE = re.compile(r"\.r\d{2,}$", re.IGNORECASE)  # .r00, .r01, ... (old-style)

_SUBPROCESS_TIMEOUT_S = 600

ExtractState = Literal["EXTRACTED", "EXTRACT_FAILED", "SKIPPED"]


@dataclass(frozen=True)
class ExtractResult:
    """Three outcomes, the same shape `core/verify.py.VerifyResult` already uses for exactly
    this reason (fix, 2026-08-12 -- docs/decisions.md): a plain boolean `ok` conflated "nothing
    to extract" with "extraction succeeded", so an item with no archives at all -- most items,
    since extraction is opt-in per queue -- was stamped `EXTRACTED` with a real `extracted_at`
    timestamp for work that never happened.

    - `EXTRACTED`      -- every archive found under the item extracted and merged cleanly.
    - `EXTRACT_FAILED` -- an archive failed to extract, a precondition (`check_extract_
                          preconditions`) refused to hand one to 7zz, or the post-extraction
                          merge into the final directory failed.
    - `SKIPPED`         -- nothing to do: no archives were found. Not a failure and not a
                          success -- `core/postprocess.py` must not advance `item.state` off
                          this, the same way it never advances state off `VerifyResult.SKIPPED`.

    `ok` is kept as a derived convenience for callers (and the many existing tests) that only
    ever cared about "did extraction change anything on disk" -- true for `EXTRACTED` only.
    """

    state: ExtractState
    detail: str
    extracted_dirs: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state == "EXTRACTED"


def _is_first_rar_volume(name_lower: str) -> bool:
    m = _RAR_PART_RE.search(name_lower)
    if m:
        return int(m.group("n")) == 1
    return True  # a bare .rar, or a name not using the .partNN.rar convention


def _is_compound_tar(name_lower: str) -> bool:
    return name_lower.endswith(_COMPOUND_SUFFIXES)


def find_archives(root: Path) -> list[Path]:
    """Every archive under `root` that should itself be handed to a decoder (7zz for
    zip/7z/tar/gz/bz2/xz, `unrar` for rar -- see `extract_archive`). Multi-part rar sets
    contribute only their first volume; old-style `.r00`/`.r01`/... continuation volumes are
    never a direct target at all (`unrar` reads them once given the `.rar` head of the set).
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


def _rar_volume_number(name_lower: str, head_name_lower: str) -> int | None:
    """This file's 1-based position in `head`'s multi-volume set, or `None` if it isn't a
    member of the set at all. Shared numbering for both conventions so
    `check_extract_preconditions` only has to build one `{position: path}` map and look for
    gaps in it, rather than two separate gap-detection code paths that could disagree.

    - New-style (`.partNN.rar`): `head` is itself `<base>.part1.rar`; a sibling
      `<base>.partNN.rar` is volume `N`.
    - Old-style (bare `.rar` + `.r00`/`.r01`/...): `head` is volume 1; `<stem>.r00` is volume
      2, `<stem>.r01` is volume 3, and so on -- WinRAR's own off-by-one continuation numbering,
      not something this project invented.
    """
    if name_lower == head_name_lower:
        return 1
    m = _RAR_PART_RE.search(head_name_lower)
    if m:
        base = head_name_lower[: m.start()]
        pm = re.match(re.escape(base) + r"\.part(\d+)\.rar$", name_lower)
        return int(pm.group(1)) if pm else None
    if head_name_lower.endswith(".rar"):
        base = head_name_lower[: -len(".rar")]
        om = re.match(re.escape(base) + r"\.r(\d{2,})$", name_lower)
        return int(om.group(1)) + 2 if om else None
    return None


def check_rar_volume_set(head: Path) -> str | None:
    """Multi-volume rar completeness precondition (DESIGN.md §6; the 2026-08-12 production
    gating gap -- see docs/decisions.md). Covers **both** conventions via `_rar_volume_number`'s
    shared numbering, and detects *gaps* in the sequence, not just "some siblings exist": `.r00`,
    `.r01`, `.r03` with `.r02` missing must fail exactly like a wholly-absent set, not silently
    hand `unrar` the volumes that happen to be there and let it discover the gap mid-extraction
    -- which is the exact "Cannot open the file as archive" symptom this exists to pre-empt.

    A volume counts as present only if it exists *and* is non-zero-length: a zero-byte volume
    is exactly as useless to `unrar` as an absent one, and reporting it as "missing" gives a
    truer diagnosis ("volume 3 of 15 missing") than the symptom `unrar` would otherwise surface
    after the fact. `head` itself is assumed present -- the caller (`check_extract_preconditions`)
    already ran the zero-length check on it before this function is reached.

    **Cannot detect a wholly-absent final volume.** There is no filename evidence of the true
    total volume count without opening the archive, which is deliberately out of scope for a
    filesystem-only precondition -- see docs/decisions.md. Gaps *between* present volumes are
    still caught, which is what the production failure this was written for actually looked
    like (a mid-set volume, not the last one, went missing).
    """
    head_name_lower = head.name.lower()
    present: dict[int, Path] = {1: head}
    for sibling in head.parent.iterdir():
        if sibling == head or not sibling.is_file():
            continue
        n = _rar_volume_number(sibling.name.lower(), head_name_lower)
        if n is None:
            continue
        try:
            if sibling.stat().st_size > 0:
                present[n] = sibling
        except OSError:
            continue

    total = max(present)
    missing = [n for n in range(1, total + 1) if n not in present]
    if not missing:
        return None
    return (
        f"{head.name}: incomplete multi-volume set -- volume {missing[0]} of {total} "
        "missing or zero-length"
    )


def archive_volume_paths(head: Path) -> list[Path]:
    """Every file on disk that makes up `head`'s archive -- just `head` itself for every format
    `find_archives` hands to 7zz directly, but the **full** multi-volume set for a rar head:
    `find_archives` deliberately returns only the first volume (`unrar` follows the rest once
    given that one -- see that function's docstring), so a caller that needs to account for
    every byte on disk belonging to one archive (2026-08-13,
    `prompts/2026-08-13-delete-archives-after-extract.md`: deleting only the head after a
    successful extraction would leave every `.r00`/`.r01`/...`.partNN.rar` continuation volume
    behind) cannot use `find_archives`'s own output for that.

    Reuses `_rar_volume_number`'s sibling-scanning walk -- the same one `check_rar_volume_set`
    already does -- so this and the completeness precondition can never disagree about which
    files belong to one set. Unlike `check_rar_volume_set`, this does not fail on a gap or a
    zero-length volume; a caller here is walking a set that has *already* extracted
    successfully (the precondition already ran, earlier, before that happened), so every
    present, numbered sibling is returned as-is.
    """
    if not head.name.lower().endswith(".rar"):
        return [head]
    head_name_lower = head.name.lower()
    volumes: dict[int, Path] = {1: head}
    for sibling in head.parent.iterdir():
        if sibling == head or not sibling.is_file():
            continue
        n = _rar_volume_number(sibling.name.lower(), head_name_lower)
        if n is not None:
            volumes[n] = sibling
    return [volumes[n] for n in sorted(volumes)]


def check_extract_preconditions(archive: Path) -> str | None:
    """Cheap, filesystem-only gates run before an archive is ever handed to a decoder (7zz or
    `unrar`, DESIGN.md §6; the 2026-08-12 production failure and gating gap -- see
    docs/decisions.md). Returns `None`
    when it's safe to proceed, or a clean, named failure reason ("volume 3 of 15 missing", not
    "Cannot open the file as archive") when it isn't. Exported at module level, unit-testable
    without going through `extract_item` or the postprocessing pipeline at all.

    Deliberately **not** a remote-vs-local byte comparison -- that belongs with the settle-gate
    work, a separate task, and duplicating a weaker version of it here would just be the wrong
    place for it (see docs/decisions.md).
    """
    try:
        size = archive.stat().st_size
    except OSError as exc:
        return f"{archive.name}: cannot stat ({exc})"
    if size == 0:
        return f"{archive.name}: zero-length file, refusing to extract"
    if archive.name.lower().endswith(".rar"):
        return check_rar_volume_set(archive)
    return None


def _run_7z(binary: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _run_unrar(binary: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def _extract_rar(
    archive: Path, target_dir: Path, *, password: str | None, binary: str
) -> subprocess.CompletedProcess:
    """One `unrar x` attempt. `-y` assumes yes on every query (matching 7zz's `-y` above);
    `-p<password>` / `-p-` (no password) matches this module's password-list retry loop, which
    is shared with the 7zz branch in `extract_archive` -- rar just plugs a different subprocess
    into the same per-attempt loop.

    The trailing `os.sep` on the destination is required, not cosmetic: `unrar x archive dest`
    with no trailing separator is ambiguous with "extract this one member as a file named
    `dest`" for a single-file archive, exactly the shape every fixture and most real releases
    have.
    """
    pw_arg = f"-p{password}" if password else "-p-"
    return _run_unrar(binary, ["x", "-y", pw_arg, str(archive), f"{target_dir}{os.sep}"])


def extract_archive(
    archive: Path,
    target_dir: Path,
    *,
    passwords: tuple[str, ...] = (),
    binary: str = DEFAULT_BINARY,
    rar_binary: str = DEFAULT_RAR_BINARY,
) -> ExtractResult:
    """Extract one archive into `target_dir` (created if needed).

    Compound tar formats (`.tar.gz`/`.tgz`, `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`) need two
    7zz passes -- one to strip the outer compression, one to unpack the resulting `.tar` --
    because 7-Zip only peels one layer of a chained format per invocation. The intermediate
    `.tar` lives in a throwaway subdirectory of `target_dir` that is removed either way.

    `.rar` (any volume count -- `find_archives` already filtered to the first volume only)
    goes to `unrar` instead, handed just the head file: `unrar`, like the 7zz-native RAR
    support this project mistakenly relied on for nine phases (2026-08-12, docs/decisions.md),
    follows the rest of a multi-volume set on its own once given the first volume, using
    whichever naming convention (`.r00`/`.r01`/... or `.partNN.rar`) the sibling volumes use.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    attempts = list(passwords) if passwords else [None]
    last_error = ""
    is_rar = archive.name.lower().endswith(".rar")

    for password in attempts:
        if is_rar:
            result = _extract_rar(archive, target_dir, password=password, binary=rar_binary)
            if result.returncode == 0:
                return ExtractResult(
                    state="EXTRACTED",
                    detail=f"extracted {archive.name}",
                    extracted_dirs=(target_dir,),
                )
            last_error = (result.stderr or result.stdout).strip()
            continue
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
                    state="EXTRACTED",
                    detail=f"extracted {archive.name} (compound tar)",
                    extracted_dirs=(target_dir,),
                )
        else:
            result = _run_7z(binary, ["x", str(archive), f"-o{target_dir}", "-y", *pw_args])
            if result.returncode == 0:
                return ExtractResult(
                    state="EXTRACTED",
                    detail=f"extracted {archive.name}",
                    extracted_dirs=(target_dir,),
                )
            last_error = (result.stderr or result.stdout).strip()

    return ExtractResult(
        state="EXTRACT_FAILED", detail=f"{archive.name}: {last_error or 'extraction failed'}"
    )


def resolve_within_root(candidate: Path, root: Path) -> Path | None:
    """Resolve `candidate` and confirm it is `root` itself or a descendant of it, refusing to
    follow a symlink (anywhere along `candidate`'s own path, or at `candidate` itself) that
    would place it outside `root`. Returns the resolved path when contained, `None` on any
    escape.

    The one containment check this codebase deletes disk content through -- shared by
    `sweep_failed_dirs` below (which additionally requires a *direct* child: see its own
    check) and `core/local_delete.py.delete_local` (which allows any depth, since an item's
    `rel_path` can itself contain `/`). `prompts/open-issues.md` "7 + 8": "two different
    containment checks guarding deletion is how one of them ends up subtly weaker" -- so this
    is the only one, both a `LOCAL_ONLY` `_FAILED_` staging directory and a `LOCAL_ONLY` item
    can be a symlink, and `rm -rf`/`shutil.rmtree` through one pointing outside the queue's
    `local_path` is the worst possible outcome either deleting feature could produce.

    `root` not existing is not this function's concern -- callers that need `root` to be a
    real directory check that themselves (this project's queues can have a `local_path` that
    hasn't mounted yet, and *that* failure has its own, more specific message).
    """
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        return None
    return resolved_candidate


def find_escaping_path(staging_dir: Path) -> Path | None:
    """Return the first entry under `staging_dir` that resolves *outside* it, or `None` when
    every entry is contained (audit S2, docs/audit-v0.1.0.md).

    The realistic residual archive-escape after 7zz/unrar are handed `-o<staging>` is a
    **symlink member** pointing out of the extraction root (7zz and unrar both strip literal
    `../` traversal members themselves, but a symlink entry is content, not a path they
    rewrite): the symlink lands inside staging, and anything written *through* it, or a later
    move that follows it, escapes. `resolve_within_root` resolves symlinks, so walking staging
    and rejecting any entry that resolves out of it catches exactly that case -- before a single
    byte of un-vetted archive content is merged into the published tree. `rglob` does not recurse
    into a symlinked directory, so a malicious symlink is inspected, never followed, here.
    """
    root = staging_dir.resolve()
    for entry in staging_dir.rglob("*"):
        if resolve_within_root(entry, root) is None:
            return entry
    return None


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
    rar_binary: str = DEFAULT_RAR_BINARY,
) -> ExtractResult:
    """Extract every archive found under `root` (DESIGN.md §6). `target_dir=None` means "in
    place" -- the final directory is `root` itself. An item with no archives at all (most
    items) is a no-op, `SKIPPED` -- not `EXTRACTED` -- because extraction is opt-in per queue
    and most releases aren't archives to begin with; claiming `EXTRACTED` (and stamping
    `extracted_at`) for work that never happened is exactly the bug this fix (2026-08-12,
    docs/decisions.md) removes. A no-op leaves no `_UNPACK_` litter behind either way, for an
    item that was never touched.

    Before anything is staged, every candidate archive is run through
    `check_extract_preconditions` (2026-08-12, docs/decisions.md: the production gating gap --
    extraction had no completeness precondition of its own, so a `copy`-mode queue with
    verification off, the default, gated extraction on nothing but a stale size rollup). A
    precondition failure is reported as `EXTRACT_FAILED` **without ever creating a staging or
    `_FAILED_` directory** -- nothing was actually attempted, so there is no partial output to
    keep as evidence, and a `_FAILED_` directory implying otherwise would be its own kind of
    dishonesty (the same complaint fix 1, above, exists to fix in the first place).

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
        return ExtractResult(state="SKIPPED", detail=NO_ARCHIVES_DETAIL)

    precondition_failures = [
        msg for msg in (check_extract_preconditions(a) for a in archives) if msg
    ]
    if precondition_failures:
        return ExtractResult(
            state="EXTRACT_FAILED",
            detail=(
                f"{len(precondition_failures)} of {len(archives)} archive(s) failed a "
                "precondition check before extraction began: " + "; ".join(precondition_failures)
            ),
        )

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
        result = extract_archive(
            archive, staging_dir / rel, passwords=passwords, binary=binary, rar_binary=rar_binary
        )
        if not result.ok:
            failures.append(result.detail)

    if failures:
        staging_dir.rename(failed_dir)
        return ExtractResult(
            state="EXTRACT_FAILED",
            detail=(
                f"{len(failures)} of {len(archives)} archive(s) failed: "
                + "; ".join(failures)
                + f" -- partial output kept at {failed_dir}"
            ),
        )

    # Audit S2: never merge un-vetted archive output that escapes the staging root (a symlink
    # member pointing outside it) into the published tree. Checked here, on the whole staged
    # result, before the merge -- refusing is `EXTRACT_FAILED` (which withholds the rename and
    # is surfaced/audited exactly like any other extraction failure), and the offending tree is
    # kept as `_FAILED_` evidence rather than silently discarded.
    escaping = find_escaping_path(staging_dir)
    if escaping is not None:
        if failed_dir.exists():
            shutil.rmtree(failed_dir)
        staging_dir.rename(failed_dir)
        return ExtractResult(
            state="EXTRACT_FAILED",
            detail=(
                f"extraction produced a path escaping the staging root ({escaping.name!r}) "
                f"-- refusing to publish it; kept at {failed_dir}"
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
            state="EXTRACT_FAILED",
            detail=(
                f"extracted {len(archives)} archive(s) but placing them under {final_dir} "
                f"failed: {exc} -- partial output kept at {failed_dir}"
            ),
        )

    return ExtractResult(
        state="EXTRACTED",
        detail=f"extracted {len(archives)} of {len(archives)} archive(s)",
        extracted_dirs=(final_dir,),
    )


def sweep_failed_dirs(
    queue_root: Path, *, max_age_days: float, now: float | None = None
) -> list[tuple[Path, float]]:
    """Remove `_FAILED_<name>` staging directories older than `max_age_days` directly under
    `queue_root` (a path queue's `local_path`) -- fix for the third defect in this task
    (2026-08-12, docs/decisions.md): `_FAILED_` is deliberately never cleaned up by
    `extract_item` itself (kept as diagnostic evidence, see its docstring), but nothing ever
    removed it either, and `core/local_scan.py` filters the prefix out of every scan -- so it
    was invisible in the UI while consuming disk indefinitely. This is the bounded-lifetime
    half of that decision, run as its own pass so a directory a user is actively inspecting
    survives at least `max_age_days` before it can be swept.

    **Containment is re-verified here, not assumed from the caller.** This is one of two places
    in the codebase allowed to remove disk content with nobody in the loop (the other is
    `core/local_delete.py.delete_local`), so it re-checks its own precondition rather than
    trusting a caller that got the naming right by construction: a candidate is only ever
    removed if `resolve_within_root` confirms containment *and* it is a direct child of
    `queue_root` (rules out a symlink escaping the queue root one level deeper than its own
    entry) whose basename starts with `FAILED_PREFIX`.

    Returns `(path, age_days)` for every directory actually removed, so the caller
    (`core/postprocess.py`) can write one `event` row per removal -- DESIGN.md §3.1's audit
    trail must cover this the same as any other thing that deletes content on disk.
    """
    resolved_root = queue_root.resolve()
    if not resolved_root.is_dir():
        return []
    now_ts = now if now is not None else time.time()
    removed: list[tuple[Path, float]] = []
    for child in sorted(resolved_root.iterdir()):
        if not child.is_dir() or not child.name.startswith(FAILED_PREFIX):
            continue
        resolved_child = resolve_within_root(child, resolved_root)
        if resolved_child is None or resolved_child.parent != resolved_root:
            continue  # symlink (or similar) escaping the queue root -- refuse
        age_days = (now_ts - child.stat().st_mtime) / 86400
        if age_days < max_age_days:
            continue
        removed.append((resolved_child, age_days))
        shutil.rmtree(resolved_child)
    return removed
