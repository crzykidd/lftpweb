"""Settings -> Post-processing and the transfer-shaping/retention settings that live
alongside it (DESIGN.md §6/§7.3): postprocess, the settle gate, the removal-grace read,
the download-prefix site-wide half, auto-queue re-download, local retention, and orphan
temp-file cleanup. Split out of the former monolithic `api/settings.py` (audit P2,
docs/audit-v0.1.0.md); shares the `/api/settings` prefix with its sibling routers."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import local_delete
from lftpweb.core.autoqueue import (
    AutoQueueSettings,
    load_autoqueue_settings,
    save_autoqueue_settings,
)
from lftpweb.core.download_prefix import (
    DownloadPrefixSettings,
    load_download_prefix_settings,
    save_download_prefix_settings,
    validate_prefix,
)
from lftpweb.core.mount_sentinel import COMPLETE_STATES, DEFAULT_GRACE_S
from lftpweb.core.postprocess import (
    PostprocessSettings,
    load_postprocess_settings,
    save_postprocess_settings,
)
from lftpweb.core.settle import (
    REQUIRED_SETTLE_SCANS,
    SETTLE_MIN_AGE_S,
    SettleSettings,
    load_settle_settings,
    save_settle_settings,
)
from lftpweb.models import (
    AutoQueueSettingsIn,
    AutoQueueSettingsOut,
    DownloadPrefixSettingsIn,
    DownloadPrefixSettingsOut,
    OrphanTempCleanupSettingsIn,
    OrphanTempCleanupSettingsOut,
    PostprocessSettingsIn,
    PostprocessSettingsOut,
    RemovalGraceSettingsOut,
    RetentionPreviewItem,
    RetentionPreviewRequest,
    RetentionPreviewResponse,
    RetentionSettingsIn,
    RetentionSettingsOut,
    SettleSettingsIn,
    SettleSettingsOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")

# --- Post-processing (DESIGN.md §6, phase 5) ----------------------------------------------


def _postprocess_out(s: PostprocessSettings) -> PostprocessSettingsOut:
    return PostprocessSettingsOut(
        verify_enabled=s.verify_enabled,
        verify_hash_on_disk=s.verify_hash_on_disk,
        extract_enabled=s.extract_enabled,
        extract_target_dir=s.extract_target_dir,
        extract_passwords=list(s.extract_passwords),
        failed_retention_enabled=s.failed_retention_enabled,
        failed_retention_days=s.failed_retention_days,
        delete_archives_after_extract=s.delete_archives_after_extract,
        move_enabled=s.move_enabled,
        concurrency=s.concurrency,
    )


@router.get("/postprocess", response_model=PostprocessSettingsOut)
async def get_postprocess_settings(request: Request) -> PostprocessSettingsOut:
    settings = await load_postprocess_settings(request.app.state.db)
    return _postprocess_out(settings)


@router.put("/postprocess", response_model=PostprocessSettingsOut)
async def put_postprocess_settings(
    body: PostprocessSettingsIn, request: Request
) -> PostprocessSettingsOut:
    """Merges over the previously-stored settings rather than replacing them wholesale --
    `PostprocessSettingsIn`'s own docstring for why. `body.model_fields_set` (pydantic v2) is
    which keys the *request JSON* actually carried; every field on this model except
    `failed_retention_enabled`/`_days`/`delete_archives_after_extract` is required (no default),
    so FastAPI itself 422s before this handler runs if any of those is missing -- the merge
    below only ever has real work to do for that trio, and it costs nothing for a request that
    (like the frontend's own) supplies every field: `provided` then contains all of them, and
    every `field()` call below just returns `getattr(body, name)` unchanged.
    """
    current = await load_postprocess_settings(request.app.state.db)
    provided = body.model_fields_set

    def field(name: str, current_value):
        return getattr(body, name) if name in provided else current_value

    settings = PostprocessSettings(
        verify_enabled=field("verify_enabled", current.verify_enabled),
        verify_hash_on_disk=field("verify_hash_on_disk", current.verify_hash_on_disk),
        extract_enabled=field("extract_enabled", current.extract_enabled),
        extract_target_dir=field("extract_target_dir", current.extract_target_dir),
        extract_passwords=tuple(field("extract_passwords", list(current.extract_passwords))),
        failed_retention_enabled=field(
            "failed_retention_enabled", current.failed_retention_enabled
        ),
        failed_retention_days=field("failed_retention_days", current.failed_retention_days),
        delete_archives_after_extract=field(
            "delete_archives_after_extract", current.delete_archives_after_extract
        ),
        move_enabled=field("move_enabled", current.move_enabled),
        concurrency=max(1, field("concurrency", current.concurrency)),
    )
    await save_postprocess_settings(request.app.state.db, settings)
    return _postprocess_out(settings)


# --- Settings -> the settle gate (prompts/open-issues.md #2, `core/settle.py`) -----------
#
# UI built in prompts/2026-08-12-settle-gate-followups.md, at Settings -> Transfer (the natural
# home -- see that page's own "the settle gate" section). Defaults **on** as of that task; see
# CHANGELOG.md's `### Changed` entry and `core/settle.py.SettleSettings`'s docstring for why.


@router.get("/settle", response_model=SettleSettingsOut)
async def get_settle_settings(request: Request) -> SettleSettingsOut:
    settings = await load_settle_settings(request.app.state.db)
    return SettleSettingsOut(
        enabled=settings.enabled,
        required_scans=REQUIRED_SETTLE_SCANS,
        min_age_s=SETTLE_MIN_AGE_S,
    )


@router.put("/settle", response_model=SettleSettingsOut)
async def put_settle_settings(body: SettleSettingsIn, request: Request) -> SettleSettingsOut:
    settings = SettleSettings(enabled=body.enabled)
    await save_settle_settings(request.app.state.db, settings)
    return SettleSettingsOut(
        enabled=settings.enabled,
        required_scans=REQUIRED_SETTLE_SCANS,
        min_age_s=SETTLE_MIN_AGE_S,
    )


# --- Settings -> the removal grace period (`core/mount_sentinel.py`, DESIGN.md §7.3) -----
#
# GET-only -- `RemovalGraceSettingsOut`'s own docstring: `DEFAULT_GRACE_S` isn't a per-install
# setting this phase, this endpoint exists purely so the frontend has a real number to build a
# countdown from instead of a hardcoded one (2026-08-14, prompts/2026-08-14-removal-grace-
# countdown.md).


@router.get("/removal-grace", response_model=RemovalGraceSettingsOut)
async def get_removal_grace_settings() -> RemovalGraceSettingsOut:
    # `COMPLETE_STATES` straight from `core/mount_sentinel.py`, never a list literal here: it is
    # the same set `resolve_absence` gates on, so the countdown can only ever be offered for a
    # row the backend would actually run the clock for. `tests/test_settings_api.py` pins the
    # equality so a new post-processing state added on the Python side cannot silently stop
    # being eligible in the UI.
    return RemovalGraceSettingsOut(grace_s=DEFAULT_GRACE_S, eligible_states=sorted(COMPLETE_STATES))


# --- Settings -> "folder prefix during transfer" (`core/download_prefix.py`) -----------
#
# Site-wide half lives at Settings -> Transfer, the same page as the settle gate above -- both
# are transfer-shaping settings that aren't part of DESIGN.md §4.5's bandwidth surface but don't
# have a page of their own either. The per-queue override half is on Settings -> Queues (the
# `_QUEUE_SELECT_COLUMNS`/`_queue_out_from_row`/`create_queue`/`update_queue` changes below).


@router.get("/download-prefix", response_model=DownloadPrefixSettingsOut)
async def get_download_prefix_settings(request: Request) -> DownloadPrefixSettingsOut:
    settings = await load_download_prefix_settings(request.app.state.db)
    return DownloadPrefixSettingsOut(enabled=settings.enabled, prefix=settings.prefix)


@router.put("/download-prefix", response_model=DownloadPrefixSettingsOut)
async def put_download_prefix_settings(
    body: DownloadPrefixSettingsIn, request: Request
) -> DownloadPrefixSettingsOut:
    error = validate_prefix(body.prefix, enabled=body.enabled)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    settings = DownloadPrefixSettings(enabled=body.enabled, prefix=body.prefix)
    await save_download_prefix_settings(request.app.state.db, settings)
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        # A changed prefix (or the toggle itself) changes what the very next scan's
        # `core/local_scan.py` filter must skip (`Engine._active_download_prefixes`) -- request
        # a fresh pass rather than waiting out the current interval, matching every other
        # setting write in this module that affects scan behaviour.
        engine.request_rescan()
    return DownloadPrefixSettingsOut(enabled=settings.enabled, prefix=settings.prefix)


# --- Settings -> auto-queue (`core/autoqueue.py.AutoQueueSettings`) ---------------------
#
# Settings -> Queues (the page that already owns every other auto-queue-related toggle: the
# per-queue enable, patterns-only, and the pattern editor) gained a self-contained "Re-download
# items removed outside lftpweb" section for this, mirroring `TransferTab.tsx`'s
# `SettleGateSection` idiom -- its own load/save cycle against this endpoint rather than folded
# into the per-queue form, since it's a site-level setting, not a `path_queue` column.


@router.get("/autoqueue", response_model=AutoQueueSettingsOut)
async def get_autoqueue_settings(request: Request) -> AutoQueueSettingsOut:
    settings = await load_autoqueue_settings(request.app.state.db)
    return AutoQueueSettingsOut(
        re_download_externally_removed=settings.re_download_externally_removed
    )


@router.put("/autoqueue", response_model=AutoQueueSettingsOut)
async def put_autoqueue_settings(
    body: AutoQueueSettingsIn, request: Request
) -> AutoQueueSettingsOut:
    settings = AutoQueueSettings(re_download_externally_removed=body.re_download_externally_removed)
    await save_autoqueue_settings(request.app.state.db, settings)
    return AutoQueueSettingsOut(
        re_download_externally_removed=settings.re_download_externally_removed
    )


# --- Settings -> local retention (prompts/open-issues.md "7 + 8", `core/local_delete.py`) -----
#
# No frontend page yet -- same accepted "backend first, UI catches up later" gap as the settle
# gate immediately above and Settings -> Transfer across several earlier phases. Defaults off
# either way, non-negotiably (this deletes the user's own data), so its absence from any
# settings screen changes nothing about how an existing install behaves.


@router.get("/retention", response_model=RetentionSettingsOut)
async def get_retention_settings(request: Request) -> RetentionSettingsOut:
    settings = await local_delete.load_retention_settings(request.app.state.db)
    return RetentionSettingsOut(enabled=settings.enabled, retention_days=settings.retention_days)


@router.put("/retention", response_model=RetentionSettingsOut)
async def put_retention_settings(
    body: RetentionSettingsIn, request: Request
) -> RetentionSettingsOut:
    """Merges over the previously-stored settings, same shape and same reason as
    `put_postprocess_settings` above -- **both** fields on `RetentionSettingsIn` default
    (`enabled: bool = False`, `retention_days: float = 30.0`), so unlike `postprocess` this
    endpoint's *entire* body could previously be omitted and still 200, silently turning off a
    deletion feature already flagged as this project's one non-negotiable "ships off." Found
    while auditing `*Settings` endpoints for the same shape as
    `prompts/2026-08-13-per-queue-archive-cleanup.md`'s item 3; fixed here too since the fix is
    identical and the risk (a destructive toggle silently reset) is the same class.
    """
    current = await local_delete.load_retention_settings(request.app.state.db)
    provided = body.model_fields_set
    settings = local_delete.RetentionSettings(
        enabled=body.enabled if "enabled" in provided else current.enabled,
        retention_days=(
            body.retention_days if "retention_days" in provided else current.retention_days
        ),
    )
    await local_delete.save_retention_settings(request.app.state.db, settings)
    return RetentionSettingsOut(enabled=settings.enabled, retention_days=settings.retention_days)


@router.post("/retention/preview", response_model=RetentionPreviewResponse)
async def retention_preview(
    body: RetentionPreviewRequest, request: Request
) -> RetentionPreviewResponse:
    """ "Here is exactly what would be deleted, and the total bytes" (prompts/open-issues.md
    "7 + 8"), mirroring `pattern_preview`'s idiom above: preview an unsaved value (or, when
    `retention_days` is omitted, the currently saved one) without writing anything.
    """
    db = request.app.state.db
    if body.retention_days is not None:
        retention_days = body.retention_days
    else:
        retention_days = (await local_delete.load_retention_settings(db)).retention_days

    postprocess = getattr(request.app.state, "postprocess", None)
    in_flight = postprocess.in_flight_item_ids() if postprocess is not None else frozenset()

    candidates = await local_delete.preview_retention(
        db, retention_days=retention_days, in_flight_item_ids=in_flight
    )
    items = [
        RetentionPreviewItem(
            item_id=c["item_id"],
            queue_id=c["queue_id"],
            queue_name=c["queue_name"],
            rel_path=c["rel_path"],
            local_size=c["local_size"],
            downloaded_at=c["downloaded_at"],
        )
        for c in candidates
    ]
    total_bytes = sum(c["local_size"] or 0 for c in candidates)
    return RetentionPreviewResponse(
        retention_days=retention_days, count=len(items), total_bytes=total_bytes, items=items
    )


# --- Settings -> orphan temp-file cleanup (2026-08-13,
# prompts/2026-08-13-lftp-timestamped-temp-files.md, `core/local_delete.py`) -----------------
#
# No frontend page yet -- same accepted "backend first, UI catches up later" gap `retention`
# above already has (its own comment). Defaults off either way, non-negotiably (this deletes
# files from disk), so its absence from any settings screen changes nothing about how an
# existing install behaves.


@router.get("/orphan-temp-cleanup", response_model=OrphanTempCleanupSettingsOut)
async def get_orphan_temp_cleanup_settings(request: Request) -> OrphanTempCleanupSettingsOut:
    settings = await local_delete.load_orphan_temp_cleanup_settings(request.app.state.db)
    return OrphanTempCleanupSettingsOut(
        enabled=settings.enabled, max_age_days=settings.max_age_days
    )


@router.put("/orphan-temp-cleanup", response_model=OrphanTempCleanupSettingsOut)
async def put_orphan_temp_cleanup_settings(
    body: OrphanTempCleanupSettingsIn, request: Request
) -> OrphanTempCleanupSettingsOut:
    """Merges over the previously-stored settings, same shape and same reason as
    `put_retention_settings` above -- both fields on `OrphanTempCleanupSettingsIn` default, so
    an omitted body (or a partial one) must not silently reset the other field.
    """
    db = request.app.state.db
    current = await local_delete.load_orphan_temp_cleanup_settings(db)
    provided = body.model_fields_set
    settings = local_delete.OrphanTempCleanupSettings(
        enabled=body.enabled if "enabled" in provided else current.enabled,
        max_age_days=body.max_age_days if "max_age_days" in provided else current.max_age_days,
    )
    await local_delete.save_orphan_temp_cleanup_settings(db, settings)
    return OrphanTempCleanupSettingsOut(
        enabled=settings.enabled, max_age_days=settings.max_age_days
    )
