"""Settings → Backup (DESIGN.md §10.2): manual "Backup now," list, download, and the
scheduled-backup settings. The pre-migration backup itself is *not* here — it fires
unconditionally from `db.py`'s `migrate()` (see `core/backup.py`'s module docstring), not
through this router, and is not something a client can trigger, skip, or configure.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from lftpweb.core.backup import (
    BackupSettings,
    backup_file_path,
    create_backup,
    list_backups,
    load_backup_settings,
    prune_backups,
    save_backup_settings,
)
from lftpweb.models import BackupInfoOut, BackupListResponse, BackupSettingsIn, BackupSettingsOut

router = APIRouter(prefix="/api/settings/backup")


def _info_out(info) -> BackupInfoOut:
    return BackupInfoOut(
        filename=info.filename, size_bytes=info.size_bytes, created_at=info.created_at
    )


@router.get("", response_model=BackupSettingsOut)
async def get_backup_settings(request: Request) -> BackupSettingsOut:
    settings = await load_backup_settings(request.app.state.db)
    return BackupSettingsOut(interval_days=settings.interval_days, keep_count=settings.keep_count)


@router.put("", response_model=BackupSettingsOut)
async def put_backup_settings(body: BackupSettingsIn, request: Request) -> BackupSettingsOut:
    if body.interval_days <= 0:
        raise HTTPException(status_code=422, detail="interval_days must be > 0")
    if body.keep_count < 1:
        raise HTTPException(status_code=422, detail="keep_count must be >= 1")
    settings = BackupSettings(interval_days=body.interval_days, keep_count=body.keep_count)
    await save_backup_settings(request.app.state.db, settings)
    return BackupSettingsOut(interval_days=settings.interval_days, keep_count=settings.keep_count)


@router.get("/list", response_model=BackupListResponse)
async def list_backup_files(request: Request) -> BackupListResponse:
    infos = await list_backups(request.app.state.config_dir)
    return BackupListResponse(backups=[_info_out(i) for i in infos])


@router.post("/now", response_model=BackupInfoOut, status_code=201)
async def backup_now(request: Request) -> BackupInfoOut:
    """DESIGN.md §10.2's "Backup now" button -- always takes one, regardless of whether the
    schedule considers one due yet, then prunes to the configured keep count so a burst of
    manual clicks doesn't defeat retention.
    """
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    info = await create_backup(db, config_dir, reason="manual")
    settings = await load_backup_settings(db)
    await prune_backups(config_dir, settings.keep_count)
    return _info_out(info)


@router.get("/{filename}/download")
async def download_backup(filename: str, request: Request) -> FileResponse:
    try:
        path = backup_file_path(request.app.state.config_dir, filename)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="unknown backup file") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown backup file")
    return FileResponse(path, filename=filename, media_type="application/vnd.sqlite3")
