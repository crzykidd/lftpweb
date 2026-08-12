"""Settings → Logs (DESIGN.md §10.1): list the rotated app-log files, tail the current one
with a bounded read and an optional level filter, and download any of them.

This is the **app log** stream only — `logsetup.py`'s rotating `lftpweb.log` (plus its
`.1`..`.N` rotations). It is deliberately not the per-job lftp output (`job.output_tail`,
served by `api/history.py`) or the `event` table (also History) — DESIGN.md §10.1 is explicit
that mixing the three streams "becomes a dumping ground nobody reads."
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from lftpweb.core.logtail import DEFAULT_MAX_LINES, MAX_LINES_CAP, line_level, tail_file
from lftpweb.logsetup import LOG_FILENAME
from lftpweb.models import LogFileOut, LogFilesResponse, LogTailResponse

router = APIRouter(prefix="/api/logs")

# lftpweb.log (current) or lftpweb.log.N (a rotation) -- RotatingFileHandler's own naming.
# Anchoring the download/tail endpoints to this exact pattern is what keeps a filename from
# a request from ever resolving outside the logs directory.
_ROTATED_RE = re.compile(r"^lftpweb\.log(?:\.(?P<n>[1-9][0-9]*))?$")


def _log_dir(config_dir: str) -> Path:
    return Path(config_dir) / "logs"


def _sort_key(name: str) -> int:
    match = _ROTATED_RE.match(name)
    n = match.group("n") if match else None
    return 0 if n is None else int(n)  # current file first, then .1, .2, ... oldest last


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _list_files_sync(config_dir: str) -> list[LogFileOut]:
    d = _log_dir(config_dir)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if p.is_file() and _ROTATED_RE.match(p.name):
            stat = p.stat()
            out.append(
                LogFileOut(
                    name=p.name,
                    size_bytes=stat.st_size,
                    modified_at=_iso(stat.st_mtime),
                    is_current=(p.name == LOG_FILENAME),
                )
            )
    out.sort(key=lambda f: _sort_key(f.name))
    return out


@router.get("/files", response_model=LogFilesResponse)
async def list_log_files(request: Request) -> LogFilesResponse:
    config_dir = request.app.state.config_dir
    files = await asyncio.to_thread(_list_files_sync, config_dir)
    return LogFilesResponse(files=files)


@router.get("/tail", response_model=LogTailResponse)
async def tail_log(
    request: Request, lines: int = DEFAULT_MAX_LINES, level: str | None = None
) -> LogTailResponse:
    """Tail the *current* log file only (DESIGN.md §10.1: "tail the current one") -- a
    bounded read (`core/logtail.py`), never the whole file, and never a rotated one (those
    are download-only; nothing is actively being appended to them).
    """
    lines = max(1, min(lines, MAX_LINES_CAP))
    level_filter: str | None = None
    if level is not None:
        level_filter = level.upper()
        if level_filter not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise HTTPException(status_code=422, detail=f"invalid level: {level}")

    path = _log_dir(request.app.state.config_dir) / LOG_FILENAME
    if not path.is_file():
        return LogTailResponse(lines=[], truncated=False)

    raw_lines, truncated = await asyncio.to_thread(tail_file, path, lines)
    if level_filter is not None:
        raw_lines = [line for line in raw_lines if line_level(line) == level_filter]
    return LogTailResponse(lines=raw_lines, truncated=truncated)


@router.get("/{filename}/download")
async def download_log(filename: str, request: Request) -> FileResponse:
    if not _ROTATED_RE.match(filename):
        raise HTTPException(status_code=404, detail="unknown log file")
    path = _log_dir(request.app.state.config_dir) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown log file")
    return FileResponse(path, filename=filename, media_type="text/plain")
