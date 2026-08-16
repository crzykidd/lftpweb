"""Settings -> Queues' path-browse dialog (DESIGN.md §9.2, GitHub issue #4,
`prompts/done/2026-08-16-path-browse-dialog.md`): two `GET`-only, read-only directory listings
-- one over the container's own filesystem, one over the seedbox via the pooled SFTP connection
(`core/remote.py.RemoteConnectionPool`, the same seam `PostprocessPipeline`/`ArrSyncScheduler`
already share). `core/browse.py` does the actual resolution; this module is the thin HTTP
wrapper (`main.py` registers it the same way as every other settings router).

**The local endpoint deliberately exposes the container's whole filesystem tree to any
authenticated user -- that is the feature**, not an oversight: a queue's `local_path`/
`staging_path` can be mounted anywhere, and the browse dialog has to be able to reach it.
Auth-gating comes free from `middleware.py.AuthMiddleware`'s default-deny (neither route is in
`PUBLIC_API_PATHS`); nothing here re-implements that.

Both endpoints return the identical shape (`BrowseResponse`) regardless of side, so
`PathBrowseDialog.tsx` has exactly one response shape to render.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import browse as browse_core
from lftpweb.core.engine import load_host_config
from lftpweb.models import MAX_PATH_LEN, BrowseEntryOut, BrowseResponse

router = APIRouter(prefix="/api/browse")


def _reject_overlong_path(path: str | None) -> None:
    # S3 audit standard (docs/audit-v0.1.0.md): cap every string input, generous enough that
    # no legitimate path is ever rejected. `path` is a query param, not a Pydantic body field,
    # so there is no `Field(max_length=...)` to lean on -- this is the query-string equivalent.
    if path is not None and len(path) > MAX_PATH_LEN:
        raise HTTPException(
            status_code=400, detail=f"path must be at most {MAX_PATH_LEN} characters"
        )


def _to_response(result: browse_core.BrowseResult) -> BrowseResponse:
    return BrowseResponse(
        path=result.path,
        parent=result.parent,
        entries=[BrowseEntryOut(name=e.name) for e in result.entries],
        truncated=result.truncated,
        fallback_from=result.fallback_from,
    )


@router.get("/local", response_model=BrowseResponse)
async def browse_local(path: str | None = None) -> BrowseResponse:
    _reject_overlong_path(path)
    try:
        result = browse_core.resolve_local_dir(path)
    except browse_core.LocalRootUnlistableError as exc:
        # The one 500-worthy case (core/browse.py's own docstring) -- every other bad `path`
        # resolves to something instead of raising.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _to_response(result)


@router.get("/remote", response_model=BrowseResponse)
async def browse_remote(request: Request, path: str | None = None) -> BrowseResponse:
    _reject_overlong_path(path)
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    host = await load_host_config(db, config_dir)
    if host is None:
        raise HTTPException(
            status_code=409, detail="configure a host before browsing the remote filesystem"
        )
    if host.credentials_need_reentry:
        raise HTTPException(
            status_code=409,
            detail="stored credentials cannot be decrypted; re-enter them in Settings -> Connection",
        )

    engine = request.app.state.engine
    try:
        conn = await engine.pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            result = await browse_core.resolve_remote_dir(sftp, path)
    except browse_core.RemoteBrowseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - connection/listing failure, never a bare 500
        raise HTTPException(
            status_code=502, detail=f"could not browse the remote filesystem: {exc}"
        ) from exc
    return _to_response(result)
