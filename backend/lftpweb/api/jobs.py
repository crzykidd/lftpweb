"""Queue/stop/retry/move-to-top/start-now, the active+pending job list, and the site-level
transfer settings (DESIGN.md §4.5, §9.2 Transfers, §9.3). Backend only this phase — the
Transfers page and item drawer are phase 3b; this is the API they'll consume, verified here
through the API itself and the fake seedbox rather than through a UI that doesn't exist yet.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core.queue import TransferSettings, load_transfer_settings, save_transfer_settings
from lftpweb.models import (
    JobOut,
    JobsResponse,
    QueueItemRequest,
    TransferSettingsIn,
    TransferSettingsOut,
)

router = APIRouter()


def _job_out(row: dict) -> JobOut:
    return JobOut(
        id=row["id"],
        item_id=row["item_id"],
        queue_id=row["queue_id"],
        rel_path=row["rel_path"],
        is_dir=bool(row["is_dir"]),
        kind=row["kind"],
        state=row["state"],
        lane=row["lane"],
        rank=row["rank"],
        attempt=row["attempt"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        pid=row["pid"],
        rate_limit_bps=row["rate_limit_bps"],
        forced_full_rate=bool(row["forced_full_rate"]),
        bytes_start=row["bytes_start"],
        bytes_done=row["bytes_done"],
        bytes_total=row["remote_size"],
        speed_bps=row.get("speed_bps"),
        eta_s=row.get("eta_s"),
        exit_code=row["exit_code"],
        error_class=row["error_class"],
        output_tail=row["output_tail"],
    )


@router.get("/api/jobs", response_model=JobsResponse)
async def list_jobs(request: Request) -> JobsResponse:
    rows = await request.app.state.queue.list_jobs()
    return JobsResponse(jobs=[_job_out(r) for r in rows])


@router.post("/api/jobs", response_model=JobOut, status_code=201)
async def queue_item(body: QueueItemRequest, request: Request) -> JobOut:
    q = request.app.state.queue
    try:
        job_id = await q.enqueue_item(body.item_id, forced_full_rate=body.start_now)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = await q.list_jobs()
    row = next((r for r in rows if r["id"] == job_id), None)
    if row is None:
        raise HTTPException(status_code=500, detail="job vanished immediately after being queued")
    return _job_out(row)


@router.post("/api/jobs/{job_id}/stop", status_code=204)
async def stop_job(job_id: int, request: Request) -> None:
    try:
        await request.app.state.queue.stop_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/jobs/{job_id}/move-to-top", status_code=204)
async def move_to_top(job_id: int, request: Request) -> None:
    await request.app.state.queue.move_to_top(job_id)


@router.post("/api/jobs/{job_id}/start-now", response_model=dict)
async def start_now(job_id: int, request: Request) -> dict:
    applied = await request.app.state.queue.start_now(job_id)
    return {"applied": applied}


@router.post("/api/items/{item_id}/stop", response_model=dict)
async def stop_item(item_id: int, request: Request) -> dict:
    applied = await request.app.state.queue.stop_item(item_id)
    return {"applied": applied}


@router.post("/api/items/{item_id}/retry", response_model=JobOut, status_code=201)
async def retry_item(item_id: int, request: Request) -> JobOut:
    q = request.app.state.queue
    try:
        job_id = await q.retry_item(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = await q.list_jobs()
    row = next((r for r in rows if r["id"] == job_id), None)
    if row is None:
        raise HTTPException(status_code=500, detail="job vanished immediately after being queued")
    return _job_out(row)


# --- Settings -> Transfer (DESIGN.md §4.5's table, §9.2/§9.3) ---------------------------


def _settings_out(s: TransferSettings) -> TransferSettingsOut:
    return TransferSettingsOut(**{f: getattr(s, f) for f in TransferSettings.__dataclass_fields__})


@router.get("/api/settings/transfer", response_model=TransferSettingsOut)
async def get_transfer_settings(request: Request) -> TransferSettingsOut:
    settings = await load_transfer_settings(request.app.state.db)
    return _settings_out(settings)


@router.put("/api/settings/transfer", response_model=TransferSettingsOut)
async def put_transfer_settings(body: TransferSettingsIn, request: Request) -> TransferSettingsOut:
    settings = TransferSettings(**body.model_dump())
    await save_transfer_settings(request.app.state.db, settings)
    q = getattr(request.app.state, "queue", None)
    if q is not None:
        q.request_tick()
    return _settings_out(settings)
