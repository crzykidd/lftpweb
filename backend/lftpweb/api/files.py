"""GET /api/files — the reconciled tree, grouped by queue (DESIGN.md §9.2 Files page).

**Reads from the database, not `core/engine.py`'s in-memory scan model — found wiring up
phase 3's queue/stop actions, not anticipated by the phase 2 build.** `engine.models` holds
only `core/reconcile.py`'s pure structural output (REMOTE_ONLY/LOCAL_ONLY/PARTIAL/DOWNLOADED,
recomputed from scratch on every scan); it has no notion of QUEUED/DOWNLOADING/STOPPED/FAILED
at all, because those are job-lifecycle states `core/queue.py` writes straight to the `item`
table (`core/engine.py._persist`'s "protected" rows, DESIGN.md §4.6). Serving `engine.models`
here — as phase 2 correctly did, since nothing but scanning existed yet — would mean a stopped
item's row looks `PARTIAL` again the instant this endpoint is called, silently discarding the
one state DESIGN.md §4.6 requires to stick. The `item` table is the merge of both; reading it
directly is both simpler than reproducing the merge in Python and the only place a stale
`engine.models` snapshot can't reintroduce the bug.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from lftpweb.models import FileNode, FilesResponse, QueueFiles

router = APIRouter(prefix="/api/files")


@router.get("", response_model=FilesResponse)
async def get_files(request: Request) -> FilesResponse:
    engine = request.app.state.engine
    db = request.app.state.db
    queues: list[QueueFiles] = []

    for queue_id, meta in engine.queue_meta.items():
        cursor = await db.execute(
            "SELECT id, rel_path, is_dir, remote_size, local_size, remote_mtime, state "
            "FROM item WHERE queue_id = ? ORDER BY rel_path",
            (queue_id,),
        )
        rows = await cursor.fetchall()
        queues.append(
            QueueFiles(
                queue_id=queue_id,
                queue_name=meta.name,
                scanned_at=engine.last_scan_at.get(queue_id),
                error=engine.scan_errors.get(queue_id),
                nodes=[
                    FileNode(
                        id=row["id"],
                        rel_path=row["rel_path"],
                        is_dir=bool(row["is_dir"]),
                        state=row["state"],
                        remote_size=row["remote_size"],
                        local_size=row["local_size"],
                        remote_mtime=float(row["remote_mtime"]) if row["remote_mtime"] is not None else None,
                    )
                    for row in rows
                ],
            )
        )

    queues.sort(key=lambda q: q.queue_id)
    return FilesResponse(queues=queues)


@router.post("/rescan", status_code=202)
async def rescan(request: Request) -> dict[str, bool]:
    request.app.state.engine.request_rescan()
    return {"triggered": True}
