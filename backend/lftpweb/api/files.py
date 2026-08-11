"""GET /api/files — the reconciled tree, grouped by queue (DESIGN.md §9.2 Files page).

Read-only this phase: no queue/stop/delete actions exist yet because nothing is behind them
(no job engine until phase 3). `POST /api/files/rescan` is the one on-demand action — force a
scan now rather than waiting for the engine's interval.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from lftpweb.core.util import to_safe_text
from lftpweb.models import FileNode, FilesResponse, QueueFiles

router = APIRouter(prefix="/api/files")


@router.get("", response_model=FilesResponse)
async def get_files(request: Request) -> FilesResponse:
    engine = request.app.state.engine
    queues: list[QueueFiles] = []
    for queue_id, nodes in engine.models.items():
        meta = engine.queue_meta.get(queue_id)
        queues.append(
            QueueFiles(
                queue_id=queue_id,
                queue_name=meta.name if meta else "",
                scanned_at=engine.last_scan_at.get(queue_id),
                error=engine.scan_errors.get(queue_id),
                nodes=[
                    FileNode(
                        rel_path=to_safe_text(n.rel_path),
                        is_dir=n.is_dir,
                        state=n.state,
                        remote_size=n.remote_size,
                        local_size=n.local_size,
                        remote_mtime=n.remote_mtime,
                    )
                    for n in nodes.values()
                ],
            )
        )
    # Queues that exist in config but haven't completed a first scan yet still show up (as
    # empty, possibly errored) rather than silently missing from the response.
    seen_ids = {q.queue_id for q in queues}
    for queue_id, meta in engine.queue_meta.items():
        if queue_id in seen_ids:
            continue
        queues.append(
            QueueFiles(
                queue_id=queue_id,
                queue_name=meta.name,
                scanned_at=None,
                error=engine.scan_errors.get(queue_id),
                nodes=[],
            )
        )
    queues.sort(key=lambda q: q.queue_id)
    return FilesResponse(queues=queues)


@router.post("/rescan", status_code=202)
async def rescan(request: Request) -> dict[str, bool]:
    request.app.state.engine.request_rescan()
    return {"triggered": True}
