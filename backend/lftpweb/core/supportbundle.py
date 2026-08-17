"""Support bundle (Settings -> Logs, 2026-08-17): the framework-free, unit-testable half of
`POST /api/support-bundle` -- the zip builder, per-*arr-instance log fetch, per-queue disk
usage, and the `lftp`/Python version reads. `api/support_bundle.py` is the thin assembly layer
on top: it reads exactly what the caller selected from the database, using the *same*
response-model conversion functions the settings endpoints already return
(`_host_out_from_row`, `_queue_out_from_row`, `_instance_out_from_row`, `_pattern_out_from_row`)
rather than hand-picking columns -- those functions are already the one place a secret is kept
off the wire, and a bundle that hand-picked columns instead is exactly how a future field would
leak into it (docs/decisions.md).

**Deliberately excluded from every bundle:** the SQLite database itself (carries encrypted
secrets and the encryption landscape -- migration level + the settings dump below cover what
support actually needs), the `known_hosts` pins, and the install secret. No redaction pass is
attempted on fetched *arr log files -- they are the *arr's own logs, and including one is the
user's own opt-in choice per instance.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from lftpweb.core.arrclient import ArrClient, ArrClientError, ArrKind
from lftpweb.core.crypto import DecryptionError, decrypt_secret

# One page walk's worth of history/audit rows -- bounded, "recent," never "everything" (the
# plan's own numbers: 1000 events, 100 jobs).
EVENTS_LIMIT = 1000
JOBS_LIMIT = 100

# Per-*arr-instance log fetch budget -- the plan's own "cap per-instance fetch at a sane byte
# budget (~20 MB)". A single instance's log directory is rotation-bounded on the *arr's own
# side, so this is a safety cap against an unbounded or misbehaving response, not a number tuned
# against a real observed log size.
ARR_LOG_BYTE_BUDGET = 20 * 1024 * 1024


def bundle_filename(version: str, now: datetime) -> str:
    """`lftpweb-support-<version>-<UTC timestamp>.zip`, per the plan's own naming rule."""
    return f"lftpweb-support-{version}-{now.strftime('%Y%m%dT%H%M%SZ')}.zip"


def build_zip(parts: Mapping[str, bytes]) -> bytes:
    """One in-memory zip from a flat `{path-within-zip: content}` mapping. Bundles are small
    (the plan's own "bundles are small; the logs budget dominates and is already bounded by
    rotation") -- an in-memory `BytesIO` is plenty; there is no case here that benefits from a
    spooled temp file.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in parts.items():
            zf.writestr(path, content)
    return buf.getvalue()


async def migration_level(db: Any) -> int | None:
    """The highest applied `schema_version.version` -- `None` only for a database that has
    never run `db.migrate()` at all, which cannot happen for a live app (the lifespan runs it
    before anything else can touch the database), but is handled rather than assumed away for
    the sake of a bundle built against a bare test connection.
    """
    cursor = await db.execute("SELECT MAX(version) AS v FROM schema_version")
    row = await cursor.fetchone()
    return row["v"] if row is not None else None


async def lftp_version(*, lftp_bin: str = "lftp") -> str:
    """`lftp --version`'s first line -- never raises: a missing binary or a non-zero exit is
    itself diagnostic information for a support bundle, not a reason to fail building one, so
    both are captured as a string rather than propagated (matches the plan's own "errors
    captured as strings -- a missing mount is itself diagnostic" for disk usage below).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            lftp_bin,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
    except OSError as exc:
        return f"error: {exc}"
    text = stdout.decode(errors="replace").strip()
    first_line = text.splitlines()[0] if text else ""
    return first_line or f"lftp --version exited {proc.returncode} with no output"


def python_version() -> str:
    return sys.version


@dataclass(frozen=True)
class QueuePaths:
    """The three fields `queue_disk_usage` needs from one `path_queue` row -- deliberately not
    the full `PathQueueOut` (this is an internal disk-usage-only projection, not part of the
    settings dump).
    """

    name: str
    local_path: str
    staging_path: str | None


def _disk_usage_or_error(path: str) -> dict[str, Any]:
    """`shutil.disk_usage` on one path, error captured as a string rather than raised -- "a
    missing mount is itself diagnostic" (the plan's own words): a `FileNotFoundError` here
    *is* the finding, for exactly the class of incident (`docs/decisions.md`'s "second writer"
    postmortem, a queue's `local_path` silently pointing at nothing) a support bundle exists to
    surface.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"path": path, "error": str(exc)}
    return {
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


async def queue_disk_usage(queues: Sequence[QueuePaths]) -> list[dict[str, Any]]:
    """One entry per queue: `local_path`'s usage always, `staging_path`'s too when the queue
    has one. Runs the blocking `os.statvfs` calls off the event loop in one `asyncio.to_thread`
    hop rather than one hop per queue -- a handful of queues at most, no reason for N context
    switches.
    """

    def _all() -> list[dict[str, Any]]:
        out = []
        for q in queues:
            entry: dict[str, Any] = {
                "queue": q.name,
                "local_path": _disk_usage_or_error(q.local_path),
            }
            if q.staging_path:
                entry["staging_path"] = _disk_usage_or_error(q.staging_path)
            out.append(entry)
        return out

    return await asyncio.to_thread(_all)


def _safe_zip_component(name: str, *, fallback: str) -> str:
    """Reduce a name that ultimately came from a remote HTTP response (an *arr instance's own
    name, or a log filename it reports) to a single path component with no `/`, no `..`, and no
    leading dot -- a defensive floor so a misbehaving or compromised *arr can never place a file
    outside its own `bundle/arr-<name>/` directory via `zipfile.ZipFile.writestr`, which (unlike
    `extractall`) does not sanitize the path it's given.
    """
    component = PurePosixPath(name).name  # strips any directory components, "..", trailing "/"
    component = component.lstrip(".").strip()
    return component or fallback


async def fetch_arr_instance_logs(
    *, kind: ArrKind, base_url: str, api_key: str, instance_name: str
) -> dict[str, bytes]:
    """One enabled *arr instance's own Sonarr/Radarr log files, keyed under
    `bundle/arr-<name>/...`. Per-instance failure (unreachable, bad key, a 5xx) never raises --
    the plan's own "must not fail the bundle" rule -- it writes a single `FETCH-FAILED.txt`
    marker in that instance's own directory instead, so one broken instance can never take the
    rest of the bundle down with it. Each file is capped at `ARR_LOG_BYTE_BUDGET`
    (`ArrClient.download_log_file`); a truncated file gets a trailing marker appended, not a
    silently cut-off file.
    """
    prefix = f"arr-{_safe_zip_component(instance_name, fallback='instance')}"
    parts: dict[str, bytes] = {}
    try:
        async with ArrClient(kind=kind, base_url=base_url, api_key=api_key) as client:
            files = await client.log_files()
            if not files:
                parts[f"{prefix}/NO-LOG-FILES.txt"] = b"the instance reported no log files\n"
            for raw in files:
                filename = raw.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                content, truncated = await client.download_log_file(
                    filename, max_bytes=ARR_LOG_BYTE_BUDGET
                )
                if truncated:
                    content += (
                        f"\n\n[lftpweb: truncated at {ARR_LOG_BYTE_BUDGET} bytes]\n"
                    ).encode()
                safe_name = _safe_zip_component(filename, fallback="log.txt")
                parts[f"{prefix}/{safe_name}"] = content
    except ArrClientError as exc:
        parts[f"{prefix}/FETCH-FAILED.txt"] = f"{exc}\n".encode()
    return parts


async def gather_arr_instance_logs(
    db: Any, config_dir: str, instance_ids: Sequence[int]
) -> dict[str, bytes]:
    """The orchestration `api/support_bundle.py` calls for the *arr-logs part: decrypt each
    selected, still-*enabled* instance's stored API key and fetch its log files
    (`fetch_arr_instance_logs` above). A stored key that fails to decrypt is treated exactly
    like an unreachable instance -- one `FETCH-FAILED.txt` marker, not a bundle-wide failure
    (module docstring's "per-instance failure ... must not fail the bundle"). An id naming a
    disabled or since-deleted instance is silently skipped -- the frontend only ever offers a
    checkbox for a currently-enabled instance, so this can only happen for a stale selection
    from a client that raced a settings change, not a real "your files are here" request.
    """
    if not instance_ids:
        return {}
    placeholders = ",".join("?" for _ in instance_ids)
    cursor = await db.execute(
        "SELECT id, name, kind, base_url, api_key_enc FROM arr_instance "
        f"WHERE id IN ({placeholders}) AND enabled = 1",
        list(instance_ids),
    )
    rows = await cursor.fetchall()
    parts: dict[str, bytes] = {}
    for row in rows:
        prefix = f"arr-{_safe_zip_component(row['name'], fallback='instance')}"
        try:
            api_key = decrypt_secret(config_dir, row["api_key_enc"])
        except DecryptionError:
            parts[f"{prefix}/FETCH-FAILED.txt"] = (
                b"stored API key cannot be decrypted; re-enter it\n"
            )
            continue
        parts.update(
            await fetch_arr_instance_logs(
                kind=row["kind"],
                base_url=row["base_url"],
                api_key=api_key,
                instance_name=row["name"],
            )
        )
    return parts
