"""Settings -> Queues and Patterns (DESIGN.md §3.1 `path_queue`/`pattern`, §4.7): queue
CRUD, the auto-queue mount-gate status read, pattern CRUD, and the live pattern preview.
Split out of the former monolithic `api/settings.py` (audit P2, docs/audit-v0.1.0.md);
shares the `/api/settings` prefix with its sibling routers."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from lftpweb.api.settings_host import _get_host_row  # the one shared read: "the" host row
from lftpweb.core import browse as browse_core
from lftpweb.core import patterns as patterns_core
from lftpweb.core.download_prefix import validate_prefix
from lftpweb.core.engine import load_host_config
from lftpweb.core.mount_sentinel import check as mount_ok_check
from lftpweb.models import (
    PathQueueIn,
    PathQueueOut,
    PatternIn,
    PatternOut,
    PatternPreviewFile,
    PatternPreviewItem,
    PatternPreviewRequest,
    PatternPreviewResponse,
    QueueAutoQueueStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")

# --- Queues --------------------------------------------------------------------------


_QUEUE_SELECT_COLUMNS = (
    "id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode, "
    "auto_queue_enabled, auto_queue_patterns_only, auto_verify, auto_extract, auto_move, "
    "auto_delete_archives, scan_interval_s, download_prefix_enabled, download_prefix, "
    "arr_instance_id, arr_delete_completed, arr_visible_path"
)


def _nullable_bool(value: int | None) -> bool | None:
    """`aiosqlite.Row` for a nullable `INTEGER ... CHECK (col IN (0, 1))` column (migration
    015): `None` really is `NULL` (inherit), never a value this function invents by defaulting
    a falsy int -- `bool(0)` and "column is NULL" must stay distinguishable all the way out to
    the API response, the same way `_effective` in `core/postprocess.py` needs them
    distinguishable coming in.
    """
    return None if value is None else bool(value)


def _queue_out_from_row(row) -> PathQueueOut:
    return PathQueueOut(
        id=row["id"],
        host_id=row["host_id"],
        name=row["name"],
        remote_path=row["remote_path"],
        local_path=row["local_path"],
        staging_path=row["staging_path"],
        enabled=bool(row["enabled"]),
        sync_mode=row["sync_mode"],
        auto_queue_enabled=bool(row["auto_queue_enabled"]),
        auto_queue_patterns_only=bool(row["auto_queue_patterns_only"]),
        auto_verify=_nullable_bool(row["auto_verify"]),
        auto_extract=_nullable_bool(row["auto_extract"]),
        auto_move=_nullable_bool(row["auto_move"]),
        auto_delete_archives=_nullable_bool(row["auto_delete_archives"]),
        scan_interval_s=row["scan_interval_s"],
        download_prefix_enabled=_nullable_bool(row["download_prefix_enabled"]),
        download_prefix=row["download_prefix"],
        arr_instance_id=row["arr_instance_id"],
        arr_delete_completed=bool(row["arr_delete_completed"]),
        arr_visible_path=row["arr_visible_path"],
    )


# `copy` and `move` are implemented (phase 5, DESIGN.md §13). `sync` (propagate *local*
# deletes to the remote) stays rejected -- it is explicitly not scheduled (DESIGN.md §7) and
# is not being built as a side effect of `move`. Accepting a mode that silently behaves as
# `copy` is worse than one that isn't offered: on a seedbox that means an operator believes
# their disk is being reclaimed while it quietly fills. Refusing with a clear reason beats a
# switch that lies. See docs/decisions.md for why `move` is safe to enable now.
IMPLEMENTED_SYNC_MODES = frozenset({"copy", "move"})

_UNIMPLEMENTED_REASON = {
    "sync": "local-delete propagation is not scheduled (DESIGN.md §7)",
}


def _sql_bool(value: bool | None) -> int | None:
    """`PathQueueIn`'s four post-processing fields are `bool | None` (migration 015: `None` =
    inherit); this is the reverse of `_nullable_bool` above, for writing one back to its
    `INTEGER` column. Never collapses `None` to `0` -- that would silently turn "inherit" into
    an explicit override the instant a queue is saved.
    """
    return None if value is None else (1 if value else 0)


def _effective_auto_verify(body: PathQueueIn) -> bool | None:
    """DESIGN.md §6: "For a queue in `move` or `sync` mode, `auto_verify` is forced on and
    cannot be turned off in the UI." For `move`/`sync` this returns an explicit `True`, never
    `None` (inherit) -- inheriting would let a later site-wide change silently turn off the
    sole gate on this mode's irreversible remote delete, exactly the failure mode inheritance
    exists to avoid everywhere else. Enforced here too, not only in the frontend form —
    a direct API call (curl, a script) must not be able to silently disable it for a `move`
    queue by omitting the field or sending `null`.
    """
    if body.sync_mode == "move":
        return True
    return body.auto_verify


def _reject_unimplemented_sync_mode(mode: str) -> None:
    if mode not in IMPLEMENTED_SYNC_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"sync_mode '{mode}' is not available: "
            f"{_UNIMPLEMENTED_REASON.get(mode, 'unknown mode')}. Use 'copy'.",
        )


def _reject_invalid_scan_interval(value: float | None) -> None:
    """The DB `CHECK` (migration 009) would refuse a negative value too, but as a raw
    `IntegrityError` -> 500, not the clean 400 every other rejected `path_queue` input in this
    module gets (`_reject_unimplemented_sync_mode` above is the existing precedent). `0` (on-
    demand only) and `None` (use the site default) are both valid -- only strictly negative is
    rejected, matching `core/engine.py.effective_scan_interval`'s own `<= 0` -> "none" reading:
    a negative number isn't a *stricter* "none," it's meaningless, so it's refused rather than
    silently folded into the same bucket as 0.
    """
    if value is not None and value < 0:
        raise HTTPException(
            status_code=400,
            detail="scan_interval_s must be null (site default), 0 (on-demand only), or positive",
        )


def _reject_invalid_download_prefix(body: PathQueueIn) -> None:
    """`core/download_prefix.py.validate_prefix`, applied to a queue's own override
    (`body.download_prefix`) -- `None` (inherit) needs no check at all, it carries no shape of
    its own to validate. `enabled=bool(body.download_prefix_enabled)` treats "inherit" (`None`)
    the same as an explicit `False` here: this check only ever gates the "must not be empty"
    rule (`validate_prefix`'s own docstring), and this endpoint cannot know what a *site-wide*
    toggle will resolve to later, so it validates only what it can see now -- shape and
    collision checks run unconditionally regardless, which is what actually protects against a
    garbage value reaching the database and taking effect once something (site or queue) does
    turn the toggle on.
    """
    if body.download_prefix is None:
        return
    error = validate_prefix(body.download_prefix, enabled=bool(body.download_prefix_enabled))
    if error is not None:
        raise HTTPException(status_code=400, detail=error)


def _reject_invalid_local_paths(body: PathQueueIn) -> None:
    """Mid-run scope addition to `prompts/done/2026-08-16-path-browse-dialog.md` -- a mistyped
    `local_path` used to save silently and surface only as a WARNING log line the next time
    auto-queue's mount gate refused to act (`core/autoqueue.py.on_scan`), found hours later.
    `local_path` is always checked; `staging_path` only when set (it's optional -- DESIGN.md
    §9.2's "Final destination"). Hard, not best-effort: unlike `remote_path` below, the
    container's own filesystem is always reachable from this process, so there is no
    "unconfigured/unreachable" case that would justify silently allowing a bad value through
    (docs/decisions.md).
    """
    error = browse_core.local_directory_error(body.local_path)
    if error is not None:
        raise HTTPException(status_code=400, detail=f"local_path: {error}")
    if body.staging_path:
        error = browse_core.local_directory_error(body.staging_path)
        if error is not None:
            raise HTTPException(status_code=400, detail=f"staging_path: {error}")


async def _reject_invalid_remote_path(request: Request, remote_path: str) -> None:
    """`remote_path`'s save-time check, deliberately **best-effort** -- the settled asymmetry
    (docs/decisions.md, mid-run scope addition): an unconfigured, unreachable, or
    `credentials_need_reentry` host must never block a Queues save, or a seedbox outage would
    lock the user out of editing settings entirely. Only a clean "no such directory" answer
    from the seedbox itself (`core/browse.py.RemotePathNotFoundError`) blocks the save; every
    other failure (no engine wired, no host, can't decrypt, can't connect, a stat that fails
    ambiguously) is swallowed and the save proceeds -- "cannot verify" is not "invalid."

    Reuses the browse endpoint's own `core/browse.py.remote_directory_error` over the engine's
    pooled SFTP connection (`app.state.engine.pool`, the same seam `api/browse.py`/
    `PostprocessPipeline`/`ArrSyncScheduler` already share) rather than a second SFTP-stat path.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return
    host = await load_host_config(request.app.state.db, request.app.state.config_dir)
    if host is None or host.credentials_need_reentry:
        return
    try:
        conn = await engine.pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            await browse_core.remote_directory_error(sftp, remote_path)
    except browse_core.RemotePathNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"remote_path: {exc}") from exc
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - best-effort: any other failure means "allow the save"
        return


async def _validate_arr_binding(
    db, arr_instance_id: int | None, arr_delete_completed: bool
) -> None:
    """migration 018 / docs/arr-integration-spec.md "API surface": `arr_instance_id` must
    reference an existing `arr_instance` row or be `None`, and `arr_delete_completed` -- the
    only destructive switch this feature has -- can never be `True` on a queue with no bound
    instance (spec "Defaults & safety": "gated behind a confirmed import event even when on,"
    which presupposes an instance to confirm an import *against*). Shared by create and update
    so there is exactly one place this pair of rules is enforced.
    """
    if arr_instance_id is not None:
        cursor = await db.execute("SELECT id FROM arr_instance WHERE id = ?", (arr_instance_id,))
        if await cursor.fetchone() is None:
            raise HTTPException(
                status_code=400, detail=f"arr_instance_id {arr_instance_id} does not exist"
            )
    if arr_delete_completed and arr_instance_id is None:
        raise HTTPException(
            status_code=400,
            detail="arr_delete_completed requires an arr_instance_id",
        )


@router.get("/queues", response_model=list[PathQueueOut])
async def list_queues(request: Request) -> list[PathQueueOut]:
    cursor = await request.app.state.db.execute(
        f"SELECT {_QUEUE_SELECT_COLUMNS} FROM path_queue ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [_queue_out_from_row(r) for r in rows]


@router.post("/queues", response_model=PathQueueOut, status_code=201)
async def create_queue(body: PathQueueIn, request: Request) -> PathQueueOut:
    _reject_unimplemented_sync_mode(body.sync_mode)
    _reject_invalid_scan_interval(body.scan_interval_s)
    _reject_invalid_download_prefix(body)
    _reject_invalid_local_paths(body)
    db = request.app.state.db
    await _validate_arr_binding(db, body.arr_instance_id, body.arr_delete_completed)
    host_row = await _get_host_row(db)
    if host_row is None:
        raise HTTPException(status_code=409, detail="configure a host before creating a queue")
    await _reject_invalid_remote_path(request, body.remote_path)

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode, auto_queue_enabled, auto_queue_patterns_only, "
        "auto_verify, auto_extract, auto_move, auto_delete_archives, scan_interval_s, "
        "download_prefix_enabled, download_prefix, "
        "arr_instance_id, arr_delete_completed, arr_visible_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            host_row["id"],
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
            1 if body.auto_queue_enabled else 0,
            1 if body.auto_queue_patterns_only else 0,
            _sql_bool(_effective_auto_verify(body)),
            _sql_bool(body.auto_extract),
            _sql_bool(body.auto_move),
            _sql_bool(body.auto_delete_archives),
            body.scan_interval_s,
            _sql_bool(body.download_prefix_enabled),
            body.download_prefix,
            body.arr_instance_id,
            1 if body.arr_delete_completed else 0,
            body.arr_visible_path,
        ),
    )
    await db.commit()

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()

    cursor = await db.execute(
        f"SELECT {_QUEUE_SELECT_COLUMNS} FROM path_queue WHERE id = ?",
        (cursor.lastrowid,),
    )
    row = await cursor.fetchone()
    return _queue_out_from_row(row)


def _merged_toggle(
    body: PathQueueIn, provided: set[str], field_name: str, current_stored: int | None
) -> int | None:
    """Merge one of the four post-processing toggle fields into `update_queue`: the field
    genuinely **absent** from the request body leaves the stored value (override *or*
    inherit) untouched; **explicitly sent** -- including `null` -- always overwrites, `null`
    clearing an existing override back to inherit. This is the one place on this endpoint
    that merges rather than replaces -- see `update_queue`'s own docstring for why -- and it
    is the same fix, for the same reason, as `put_postprocess_settings`/
    `put_retention_settings`'s own `model_fields_set` handling: getting this backwards means
    a queue form that doesn't happen to touch a toggle field silently clears whatever override
    was already saved for it.
    """
    if field_name not in provided:
        return current_stored
    return _sql_bool(getattr(body, field_name))


def _merged_field(body: PathQueueIn, provided: set[str], field_name: str, current_stored):
    """`_merged_toggle`'s identical absent-vs-null distinction, for a plain (non-boolean)
    nullable field -- `download_prefix` (migration 017): a field genuinely absent from the
    request body leaves the stored value untouched, while an explicitly sent `null` clears an
    existing override back to inherit. Kept separate from `_merged_toggle` rather than
    generalised into one function with a coercion callback -- one extra four-line function reads
    more plainly than a callback parameter for what is, today, exactly one caller.
    """
    if field_name not in provided:
        return current_stored
    return getattr(body, field_name)


@router.put("/queues/{queue_id}", response_model=PathQueueOut)
async def update_queue(queue_id: int, body: PathQueueIn, request: Request) -> PathQueueOut:
    """Every field **except** the four post-processing toggles is a full replace, same as this
    endpoint has always been -- `name`/`remote_path`/`sync_mode`/etc. are written from `body`
    unconditionally, and a caller that wants to change one field must still resend the rest
    (Settings -> Queues' edit form always submits the complete form state, so this has never
    been a real constraint for it).

    The four toggles (`auto_verify`/`auto_extract`/`auto_move`/`auto_delete_archives`) are the
    one deliberate exception, via `_merged_toggle` above: `null` and "field not sent" are now
    genuinely different for them (migration 015) -- `null` means inherit, a real value this
    endpoint must be able to write, while an *absent* field has to mean "leave whatever this
    queue already has," exactly like `put_postprocess_settings`'s own fields. A plain full
    replace can't tell those apart: `body.auto_verify` reads as Python `None` either way, so
    writing it unconditionally would silently reset an override to inherit any time a caller
    (a script, an older client, a future partial-update form) posts a body that just doesn't
    mention one of these four fields.
    """
    _reject_unimplemented_sync_mode(body.sync_mode)
    _reject_invalid_scan_interval(body.scan_interval_s)
    _reject_invalid_local_paths(body)
    db = request.app.state.db
    await _validate_arr_binding(db, body.arr_instance_id, body.arr_delete_completed)
    await _reject_invalid_remote_path(request, body.remote_path)

    cursor = await db.execute(
        "SELECT auto_verify, auto_extract, auto_move, auto_delete_archives, "
        "download_prefix_enabled, download_prefix "
        "FROM path_queue WHERE id = ?",
        (queue_id,),
    )
    existing = await cursor.fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="queue not found")

    provided = body.model_fields_set
    # `move`/`sync` forces verification on regardless of whether `auto_verify` was even sent
    # (DESIGN.md §6) -- checked before the merge, not folded into `_merged_toggle`, since this
    # override wins even over "leave it as it was."
    auto_verify_value = (
        1
        if body.sync_mode == "move"
        else _merged_toggle(body, provided, "auto_verify", existing["auto_verify"])
    )
    # `download_prefix` (migration 017), merged the same way -- validated against the
    # *resulting* `download_prefix_enabled` merge below, not `body`'s own possibly-absent
    # value, since a validate-then-merge body that only overrides one of the two fields must
    # still be checked against what the row will actually end up with.
    download_prefix_enabled_value = _merged_toggle(
        body, provided, "download_prefix_enabled", existing["download_prefix_enabled"]
    )
    download_prefix_value = _merged_field(
        body, provided, "download_prefix", existing["download_prefix"]
    )
    if download_prefix_value is not None:
        error = validate_prefix(download_prefix_value, enabled=bool(download_prefix_enabled_value))
        if error is not None:
            raise HTTPException(status_code=400, detail=error)

    await db.execute(
        "UPDATE path_queue SET name = ?, remote_path = ?, local_path = ?, staging_path = ?, "
        "enabled = ?, sync_mode = ?, auto_queue_enabled = ?, auto_queue_patterns_only = ?, "
        "auto_verify = ?, auto_extract = ?, auto_move = ?, auto_delete_archives = ?, "
        "scan_interval_s = ?, download_prefix_enabled = ?, download_prefix = ?, "
        "arr_instance_id = ?, arr_delete_completed = ?, arr_visible_path = ? WHERE id = ?",
        (
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
            1 if body.auto_queue_enabled else 0,
            1 if body.auto_queue_patterns_only else 0,
            auto_verify_value,
            _merged_toggle(body, provided, "auto_extract", existing["auto_extract"]),
            _merged_toggle(body, provided, "auto_move", existing["auto_move"]),
            _merged_toggle(
                body, provided, "auto_delete_archives", existing["auto_delete_archives"]
            ),
            body.scan_interval_s,
            download_prefix_enabled_value,
            download_prefix_value,
            body.arr_instance_id,
            1 if body.arr_delete_completed else 0,
            body.arr_visible_path,
            queue_id,
        ),
    )
    await db.commit()

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()

    cursor = await db.execute(
        f"SELECT {_QUEUE_SELECT_COLUMNS} FROM path_queue WHERE id = ?",
        (queue_id,),
    )
    row = await cursor.fetchone()
    return _queue_out_from_row(row)


@router.delete("/queues/{queue_id}", status_code=204)
async def delete_queue(queue_id: int, request: Request) -> None:
    db = request.app.state.db
    cursor = await db.execute("DELETE FROM path_queue WHERE id = ?", (queue_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="queue not found")
    await db.commit()


@router.get("/queues/{queue_id}/autoqueue-status", response_model=QueueAutoQueueStatus)
async def get_autoqueue_status(queue_id: int, request: Request) -> QueueAutoQueueStatus:
    """Runtime read of the mount gate (DESIGN.md §7.3, required starting phase 4) for the
    Settings → Queues pattern editor -- distinct from the persisted `auto_queue_enabled`
    toggle, this is "would auto-queue actually act right now."
    """
    db = request.app.state.db
    cursor = await db.execute("SELECT local_path FROM path_queue WHERE id = ?", (queue_id,))
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="queue not found")

    engine = getattr(request.app.state, "engine", None)
    autoqueue = getattr(engine, "autoqueue", None) if engine is not None else None
    mount_ok = mount_ok_check(row["local_path"])
    gated_reason = autoqueue.gated.get(queue_id) if autoqueue is not None else None
    return QueueAutoQueueStatus(mount_ok=mount_ok, gated_reason=gated_reason)


# --- Patterns (DESIGN.md §3.1 `pattern`, §4.7) ------------------------------------------


def _pattern_out_from_row(row) -> PatternOut:
    return PatternOut(
        id=row["id"],
        queue_id=row["queue_id"],
        kind=row["kind"],
        expr=row["expr"],
        enabled=bool(row["enabled"]),
    )


@router.get("/patterns", response_model=list[PatternOut])
async def list_patterns(request: Request, queue_id: int | None = None) -> list[PatternOut]:
    """All patterns, or (with `?queue_id=`) a queue's own plus every global one -- the exact
    set `core/patterns.py.load_patterns` compiles for that queue.
    """
    db = request.app.state.db
    if queue_id is None:
        cursor = await db.execute(
            "SELECT id, queue_id, kind, expr, enabled FROM pattern ORDER BY id"
        )
    else:
        cursor = await db.execute(
            "SELECT id, queue_id, kind, expr, enabled FROM pattern "
            "WHERE queue_id IS NULL OR queue_id = ? ORDER BY id",
            (queue_id,),
        )
    rows = await cursor.fetchall()
    return [_pattern_out_from_row(r) for r in rows]


@router.post("/patterns", response_model=PatternOut, status_code=201)
async def create_pattern(body: PatternIn, request: Request) -> PatternOut:
    db = request.app.state.db
    cursor = await db.execute(
        "INSERT INTO pattern (queue_id, kind, expr, enabled) VALUES (?, ?, ?, ?)",
        (body.queue_id, body.kind, body.expr, 1 if body.enabled else 0),
    )
    await db.commit()
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        # Retroactive (DESIGN.md §4.7): a fresh pattern must re-evaluate the whole known
        # model, not just what shows up in the next natural scan cadence.
        engine.request_rescan()
    cursor = await db.execute(
        "SELECT id, queue_id, kind, expr, enabled FROM pattern WHERE id = ?", (cursor.lastrowid,)
    )
    row = await cursor.fetchone()
    return _pattern_out_from_row(row)


@router.put("/patterns/{pattern_id}", response_model=PatternOut)
async def update_pattern(pattern_id: int, body: PatternIn, request: Request) -> PatternOut:
    db = request.app.state.db
    cursor = await db.execute(
        "UPDATE pattern SET queue_id = ?, kind = ?, expr = ?, enabled = ? WHERE id = ?",
        (body.queue_id, body.kind, body.expr, 1 if body.enabled else 0, pattern_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="pattern not found")
    await db.commit()
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()
    cursor = await db.execute(
        "SELECT id, queue_id, kind, expr, enabled FROM pattern WHERE id = ?", (pattern_id,)
    )
    row = await cursor.fetchone()
    return _pattern_out_from_row(row)


@router.delete("/patterns/{pattern_id}", status_code=204)
async def delete_pattern(pattern_id: int, request: Request) -> None:
    db = request.app.state.db
    cursor = await db.execute("DELETE FROM pattern WHERE id = ?", (pattern_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="pattern not found")
    await db.commit()
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()


@router.post("/queues/{queue_id}/pattern-preview", response_model=PatternPreviewResponse)
async def pattern_preview(
    queue_id: int, body: PatternPreviewRequest, request: Request
) -> PatternPreviewResponse:
    """The live "what would this match" preview (DESIGN.md §4.7, §9.2) -- evaluates the
    *unsaved* pattern set in the request body against the queue's current remote tree
    (`engine.models`), never against what's actually stored in the `pattern` table. Patterns
    are the feature most likely to be subtly wrong; this is the cheap fix.

    `engine.models` holds `core/itemview.py` projections (plain dicts) rather than
    `ReconciledNode` objects -- see that module for why the engine caches what it persisted
    instead of what it reconciled. Only `rel_path` and `is_dir` are read here, and neither is
    affected by which of the two the model happens to be.
    """
    engine = request.app.state.engine
    nodes = engine.models.get(queue_id)
    if nodes is None:
        raise HTTPException(status_code=404, detail="queue not found or not yet scanned")

    compiled = patterns_core.CompiledPatterns.compile(
        (patterns_core.Pattern(kind=p.kind, expr=p.expr, enabled=p.enabled) for p in body.patterns),
        patterns_only=body.patterns_only,
    )

    items: list[PatternPreviewItem] = []
    sample_item: str | None = None
    fallback_sample_item: str | None = None
    for rel_path, node in sorted(nodes.items()):
        if "/" in rel_path:
            continue  # only top-level entries are "items" (DESIGN.md §4.7)
        matched = compiled.item_matches(rel_path, is_file=not node["is_dir"])
        items.append(PatternPreviewItem(rel_path=rel_path, is_dir=node["is_dir"], matched=matched))
        if node["is_dir"]:
            # Prefer sampling a directory that would actually be picked up -- that's the one
            # a user editing patterns wants to see the file-level effect on. Fall back to any
            # directory item if nothing matched, so the sample panel isn't just empty.
            if matched and sample_item is None:
                sample_item = rel_path
            if fallback_sample_item is None:
                fallback_sample_item = rel_path
    if sample_item is None:
        sample_item = fallback_sample_item

    sample_files: list[PatternPreviewFile] = []
    if sample_item is not None:
        prefix = f"{sample_item}/"
        for rel_path, node in sorted(nodes.items()):
            if node["is_dir"] or not rel_path.startswith(prefix):
                continue
            basename = rel_path.rsplit("/", 1)[-1]
            sample_files.append(
                PatternPreviewFile(rel_path=rel_path, excluded=compiled.file_excluded(basename))
            )

    return PatternPreviewResponse(items=items, sample_item=sample_item, sample_files=sample_files)
