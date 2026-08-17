"""`POST /api/support-bundle` (Settings -> Logs, 2026-08-17): a downloadable diagnostic zip a
user can attach to an issue or send manually. DESIGN.md §10.1's log/backup precedents
(`api/logs.py`, `api/backup.py`) are the closest existing file-serving shapes.
`core/supportbundle.py` carries the framework-free half (zip building, per-instance *arr log
fetch, disk usage, `lftp`/Python version reads); this module is the thin assembly layer: it
reads exactly what the caller selected, reusing the *same* response-model conversion functions
the settings endpoints already return (`_host_out_from_row`, `_queue_out_from_row`,
`_instance_out_from_row`, `_pattern_out_from_row`) rather than hand-picking columns -- those
functions are already the one place a secret is kept off the wire, and a bundle that
hand-picked columns instead is exactly how a future field would leak into it
(docs/decisions.md).

**Bundle contents**, one part per checkbox:

- `logs/` -- every rotated app-log file, verbatim (`api/logs.py`'s own file list; the
  credential redactor already ran on the way in, `logsetup.py`). **Always included** -- the
  frontend shows this checkbox checked and disabled.
- `bundle/environment.json` -- version, build SHA/channel, migration level, the `/api/health`
  payload, `lftp`/Python versions, per-queue disk usage.
- `bundle/settings.json` -- host config, queues, patterns, transfer/postprocess/backup settings,
  auth *mode only*, *arr instances -- every secret excluded by construction (module docstring
  above), including postprocess's `extract_passwords` -- an archive extract password is a user
  secret too, so the bundle's own copy of that dict carries `extract_passwords_count` instead
  (2026-08-17 polish; the real `/api/settings/postprocess` response is untouched).
- `bundle/events.ndjson` -- the most recent `core.supportbundle.EVENTS_LIMIT` audit rows,
  newest first.
- `bundle/jobs.ndjson` -- the most recent `core.supportbundle.JOBS_LIMIT` jobs, including
  `error_class`/`output_tail` -- unlike the History API's own list endpoint, which omits
  `output_tail` from every row by design (`api/history.py`'s module docstring), a bundle *is*
  the on-demand case that endpoint's docstring carves out.
- `bundle/arr-<name>/` -- one directory per selected, still-*enabled* *arr instance, its own
  Sonarr/Radarr log files, fetched newest-first up to a per-instance byte budget (a `TRUNCATED
  .txt` marker names what didn't fit). An instance-level fetch failure never fails the whole
  bundle -- `FETCH-FAILED.txt` in that instance's own directory; one file's own fetch failing is
  a narrower `<filename>.FETCH-ERROR.txt` beside the files that did fetch (`core/supportbundle.
  py.fetch_arr_instance_logs`).

One `support_bundle_created` info event is written on success, naming the selected parts --
"when was this bundle made and what's in it" is the exact question the audit trail exists to
answer for every other irreversible/diagnostic action in this codebase.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

from lftpweb import __version__
from lftpweb.api import logs as logs_api
from lftpweb.api.health import health as _health_endpoint
from lftpweb.api.jobs import _settings_out as _transfer_settings_out
from lftpweb.api.settings_arr import _INSTANCE_COLUMNS, _instance_out_from_row
from lftpweb.api.settings_host import _get_host_row, _host_out_from_row
from lftpweb.api.settings_postprocess import _postprocess_out
from lftpweb.api.settings_queues import (
    _QUEUE_SELECT_COLUMNS,
    _pattern_out_from_row,
    _queue_out_from_row,
)
from lftpweb.api.history import _event_out, _events_where_clause, _jobs_where_clause
from lftpweb.config import settings as app_settings
from lftpweb.core import audit, supportbundle
from lftpweb.core.auth import load_auth_settings
from lftpweb.core.backup import load_backup_settings
from lftpweb.core.postprocess import load_postprocess_settings
from lftpweb.core.queue import load_transfer_settings
from lftpweb.models import BackupSettingsOut, SupportBundleRequest

router = APIRouter()


def _gather_logs_sync(config_dir: str) -> dict[str, bytes]:
    """`logs/` -- every rotated app-log file `api/logs.py` already lists, read verbatim (the
    credential redactor ran on the way *in*, `logsetup.py`, so nothing here needs to filter the
    content again). A file that vanishes between listing and reading (rotation racing this
    request) gets a marker instead of failing the whole bundle, same "per-part failure isolated"
    rule as the *arr log fetch.
    """
    files = logs_api._list_files_sync(config_dir)
    log_dir = logs_api._log_dir(config_dir)
    parts: dict[str, bytes] = {}
    for f in files:
        try:
            parts[f"logs/{f.name}"] = (log_dir / f.name).read_bytes()
        except OSError as exc:
            parts[f"logs/{f.name}.FETCH-FAILED.txt"] = f"{exc}\n".encode()
    return parts


async def _gather_environment(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    health_payload = await _health_endpoint(request)
    migration, lftp_v = await asyncio.gather(
        supportbundle.migration_level(db), supportbundle.lftp_version()
    )
    cursor = await db.execute("SELECT name, local_path, staging_path FROM path_queue ORDER BY id")
    rows = await cursor.fetchall()
    queue_paths = [
        supportbundle.QueuePaths(
            name=r["name"], local_path=r["local_path"], staging_path=r["staging_path"]
        )
        for r in rows
    ]
    disk_usage = await supportbundle.queue_disk_usage(queue_paths)
    return {
        "version": __version__,
        "build_sha": app_settings.build_sha,
        "build_channel": app_settings.build_channel,
        "migration_level": migration,
        "health": health_payload.model_dump(),
        "lftp_version": lftp_v,
        "python_version": supportbundle.python_version(),
        "queue_disk_usage": disk_usage,
    }


async def _gather_settings(db: Any) -> dict[str, Any]:
    host_row = await _get_host_row(db)
    host = _host_out_from_row(host_row).model_dump() if host_row is not None else None

    cursor = await db.execute(f"SELECT {_QUEUE_SELECT_COLUMNS} FROM path_queue ORDER BY id")
    queues = [_queue_out_from_row(r).model_dump() for r in await cursor.fetchall()]

    cursor = await db.execute("SELECT id, queue_id, kind, expr, enabled FROM pattern ORDER BY id")
    patterns = [_pattern_out_from_row(r).model_dump() for r in await cursor.fetchall()]

    transfer = _transfer_settings_out(await load_transfer_settings(db)).model_dump()

    # Bundle-only redaction (2026-08-17 polish): `_postprocess_out` -- correctly -- returns
    # `extract_passwords` verbatim to the authenticated Settings API, the same way `password`
    # would if `PostprocessSettingsOut` carried one. A support bundle is not that API: these are
    # the user's own archive passwords, secrets in their own right, so the bundle's own copy of
    # this dict swaps the list for a count and drops the key. The real `/api/settings/postprocess`
    # response is untouched -- this mutation happens only on the dict already `model_dump()`ed
    # for this bundle, never on the shared conversion function itself.
    postprocess = _postprocess_out(await load_postprocess_settings(db)).model_dump()
    postprocess["extract_passwords_count"] = len(postprocess.pop("extract_passwords"))

    auth_mode = (await load_auth_settings(db)).mode

    cursor = await db.execute(f"SELECT {_INSTANCE_COLUMNS} FROM arr_instance ORDER BY id")
    arr_instances = [_instance_out_from_row(r).model_dump() for r in await cursor.fetchall()]

    # The one `*Settings` group support-bundle-polish (2026-08-17) found missing from the dump
    # -- same api-module conversion (`BackupSettingsOut`) `api/backup.py`'s own GET uses; no
    # secret lives in this group (interval/keep-count only), so nothing to redact here.
    backup_settings = await load_backup_settings(db)
    backup = BackupSettingsOut(
        interval_days=backup_settings.interval_days, keep_count=backup_settings.keep_count
    ).model_dump()

    return {
        "host": host,
        "queues": queues,
        "patterns": patterns,
        "transfer": transfer,
        "postprocess": postprocess,
        "auth_mode": auth_mode,
        "arr_instances": arr_instances,
        "backup": backup,
    }


def _ndjson(rows: list[dict[str, Any]]) -> bytes:
    lines = [json.dumps(r) for r in rows]
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


async def _gather_events(db: Any) -> bytes:
    """`bundle/events.ndjson` -- the same `WHERE`/`JOIN` shape `api/history.py.
    list_history_events` uses (its `_events_where_clause` builder, reused directly here, plus
    an identical `SELECT`), just capped at `core.supportbundle.EVENTS_LIMIT` with no filter --
    "the most recent N", not a second query grammar.
    """
    where_sql, params = _events_where_clause(
        kind=None, level=None, item_id=None, queue_id=None, since=None, until=None
    )
    cursor = await db.execute(
        "SELECT event.id, event.ts, event.level, event.kind, event.message, "
        "event.item_id, event.job_id, item.queue_id AS queue_id, "
        "path_queue.name AS queue_name, item.rel_path AS rel_path "
        "FROM event "
        "LEFT JOIN item ON item.id = event.item_id "
        "LEFT JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE {where_sql} "
        "ORDER BY event.ts DESC, event.id DESC "
        "LIMIT ?",
        [*params, supportbundle.EVENTS_LIMIT],
    )
    rows = await cursor.fetchall()
    return _ndjson([_event_out(r).model_dump() for r in rows])


async def _gather_jobs(db: Any) -> bytes:
    """`bundle/jobs.ndjson` -- the same terminal-state `WHERE` `api/history.py.
    list_history_jobs` uses (its `_jobs_where_clause` builder, reused directly here, and the
    same joined `SELECT`), plus `output_tail` -- deliberately *not* routed through
    `HistoryJobOut`/`_job_out`, which excludes it from every row by design for the paginated
    History list (that module's own docstring). A support bundle is exactly the on-demand,
    bounded case that exclusion exists to make room for.
    """
    where_sql, params = _jobs_where_clause(
        item_id=None, queue_id=None, state=None, error_class=None, since=None, until=None
    )
    cursor = await db.execute(
        "SELECT job.id, job.item_id, item.queue_id, path_queue.name AS queue_name, "
        "item.rel_path, item.is_dir, job.kind, job.state, job.attempt, job.queued_at, "
        "job.started_at, job.finished_at, "
        "COALESCE(job.bytes_total, item.remote_size) AS bytes_total, job.bytes_done, "
        "job.exit_code, job.error_class, job.output_tail "
        "FROM job "
        "JOIN item ON item.id = job.item_id "
        "JOIN path_queue ON path_queue.id = item.queue_id "
        f"WHERE {where_sql} "
        "ORDER BY COALESCE(job.finished_at, job.queued_at) DESC, job.id DESC "
        "LIMIT ?",
        [*params, supportbundle.JOBS_LIMIT],
    )
    rows = await cursor.fetchall()
    return _ndjson(
        [
            {
                "id": r["id"],
                "item_id": r["item_id"],
                "queue_id": r["queue_id"],
                "queue_name": r["queue_name"],
                "rel_path": r["rel_path"],
                "is_dir": bool(r["is_dir"]),
                "kind": r["kind"],
                "state": r["state"],
                "attempt": r["attempt"],
                "queued_at": r["queued_at"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "bytes_total": r["bytes_total"],
                "bytes_done": r["bytes_done"],
                "exit_code": r["exit_code"],
                "error_class": r["error_class"],
                "output_tail": r["output_tail"],
            }
            for r in rows
        ]
    )


@router.post("/api/support-bundle")
async def create_support_bundle(
    request: Request, body: SupportBundleRequest | None = None
) -> Response:
    selection = body or SupportBundleRequest()
    db = request.app.state.db
    config_dir = request.app.state.config_dir

    parts: dict[str, bytes] = await asyncio.to_thread(_gather_logs_sync, config_dir)
    selected: list[str] = ["logs"]

    if selection.include_environment:
        parts["bundle/environment.json"] = json.dumps(
            await _gather_environment(request), indent=2
        ).encode()
        selected.append("environment")

    if selection.include_settings:
        parts["bundle/settings.json"] = json.dumps(await _gather_settings(db), indent=2).encode()
        selected.append("settings")

    if selection.include_events:
        parts["bundle/events.ndjson"] = await _gather_events(db)
        selected.append("events")

    if selection.include_jobs:
        parts["bundle/jobs.ndjson"] = await _gather_jobs(db)
        selected.append("jobs")

    if selection.arr_instance_ids:
        parts.update(
            await supportbundle.gather_arr_instance_logs(db, config_dir, selection.arr_instance_ids)
        )
        selected.append(f"arr({len(selection.arr_instance_ids)})")

    zip_bytes = supportbundle.build_zip(parts)
    filename = supportbundle.bundle_filename(__version__, datetime.now(UTC))

    await audit.record_event(
        db,
        level="info",
        kind="support_bundle_created",
        message=f"support bundle created: {', '.join(selected)} -> {filename}",
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
