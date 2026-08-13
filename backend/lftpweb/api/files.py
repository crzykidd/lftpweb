"""GET /api/files — the reconciled tree, grouped by queue (DESIGN.md §9.2 Files page).

**Reads from the database, not `core/engine.py`'s in-memory scan model — found wiring up
phase 3's queue/stop actions, not anticipated by the phase 2 build.** Serving the scan model
here — as phase 2 correctly did, since nothing but scanning existed yet — would mean a stopped
item's row looks `PARTIAL` again the instant this endpoint is called, silently discarding the
one state DESIGN.md §4.6 requires to stick, because `core/reconcile.py`'s structural output
has no notion of QUEUED/DOWNLOADING/STOPPED/FAILED at all. The `item` table is the merge of
every owner's writes; reading it directly is both simpler than reproducing the merge in Python
and the only place a stale in-memory snapshot can't reintroduce the bug.

**This endpoint used to be a *second* implementation of that read** — its own SELECT, its own
row-to-JSON conversion — living alongside the engine's WebSocket serializer, which is how the
two ended up disagreeing about the same item. Both now go through `core/itemview.py`: one
column list, one projection, one code path regardless of whether the rows leave over HTTP or
over the socket. `FileNode`'s fields are exactly `item_view`'s keys, so it takes them as
kwargs rather than restating them.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from lftpweb.core.itemview import ITEM_VIEW_COLUMNS_QUALIFIED, item_view
from lftpweb.models import FileNode, FilesResponse, QueueFiles

router = APIRouter(prefix="/api/files")


@router.get("", response_model=FilesResponse)
async def get_files(request: Request) -> FilesResponse:
    engine = request.app.state.engine
    db = request.app.state.db
    queues: list[QueueFiles] = []

    for queue_id, meta in engine.queue_meta.items():
        # `LEFT JOIN item_settle` (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 3): the
        # same join and reasoning as `core/engine.py._project`, which this endpoint otherwise
        # duplicates by design (this module's own docstring) -- both must agree on what a row's
        # settle countdown looks like.
        cursor = await db.execute(
            f"SELECT {ITEM_VIEW_COLUMNS_QUALIFIED}, "  # noqa: S608 - a module constant, not user input
            "settle.matched_scans AS settle_matched_scans, "
            "settle.updated_at AS settle_first_matched_at "
            "FROM item "
            "LEFT JOIN item_settle AS settle "
            "ON settle.queue_id = item.queue_id AND settle.rel_path = item.rel_path "
            "WHERE item.queue_id = ? ORDER BY item.rel_path",
            (queue_id,),
        )
        rows = await cursor.fetchall()
        queues.append(
            QueueFiles(
                queue_id=queue_id,
                queue_name=meta.name,
                scanned_at=engine.last_scan_at.get(queue_id),
                error=engine.scan_errors.get(queue_id),
                warning=engine.scan_warnings.get(queue_id),
                mount_ok=engine.mount_ok.get(queue_id),
                nodes=[FileNode(**item_view(row)) for row in rows],
            )
        )

    queues.sort(key=lambda q: q.queue_id)
    return FilesResponse(queues=queues)


@router.post("/rescan", status_code=202)
async def rescan(request: Request) -> dict[str, bool]:
    request.app.state.engine.request_rescan()
    return {"triggered": True}
