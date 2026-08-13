"""Settings → Connection and Settings → Queues (DESIGN.md §9.2).

v1 has exactly one `host` row (§3.1) — there is no host id in the URL; `GET/PUT
/api/settings/host` always operate on "the" host, creating it on first `PUT`. Path queues are
a normal collection under `/api/settings/queues`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import local_delete
from lftpweb.core import patterns as patterns_core
from lftpweb.core.autoqueue import (
    AutoQueueSettings,
    load_autoqueue_settings,
    save_autoqueue_settings,
)
from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.mount_sentinel import check as mount_ok_check
from lftpweb.core.postprocess import (
    PostprocessSettings,
    load_postprocess_settings,
    save_postprocess_settings,
)
from lftpweb.core.remote import HostConfig, parse_connection_limit
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
    HostIn,
    HostOut,
    HostTestRequest,
    PathQueueIn,
    PathQueueOut,
    PatternIn,
    PatternOut,
    PatternPreviewFile,
    PatternPreviewItem,
    PatternPreviewRequest,
    PatternPreviewResponse,
    PostprocessSettingsIn,
    PostprocessSettingsOut,
    QueueAutoQueueStatus,
    RetentionPreviewItem,
    RetentionPreviewRequest,
    RetentionPreviewResponse,
    RetentionSettingsIn,
    RetentionSettingsOut,
    SettleSettingsIn,
    SettleSettingsOut,
    TestConnectionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")


async def _get_host_row(db):
    cursor = await db.execute(
        "SELECT id, name, address, port, username, auth_method, key_path, password_enc, "
        "known_hosts_policy, connection_overrides FROM host ORDER BY id LIMIT 1"
    )
    return await cursor.fetchone()


def _host_out_from_row(row, credentials_need_reentry: bool = False) -> HostOut:
    return HostOut(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        port=row["port"],
        username=row["username"],
        auth_method=row["auth_method"],
        key_path=row["key_path"],
        has_password=bool(row["password_enc"]),
        known_hosts_policy=row["known_hosts_policy"],
        credentials_need_reentry=credentials_need_reentry,
        # Read-only surfacing of DESIGN.md §4.5/§9.3's "first-class, host-level"
        # net:connection-limit -- see core/remote.py.parse_connection_limit's docstring for
        # why this is dug out of the `connection_overrides` JSON blob rather than a real
        # column, and docs/decisions.md (2026-08-12) for the divergence this papers over.
        # There is still no `HostIn` field to *set* it -- only Settings → Transfer's
        # connection-count warning reads this, and it reads only what happens to already be
        # in the blob (nothing today writes to it via the UI).
        net_connection_limit=parse_connection_limit(row["connection_overrides"]),
    )


@router.get("/host", response_model=HostOut | None)
async def get_host(request: Request) -> HostOut | None:
    row = await _get_host_row(request.app.state.db)
    if row is None:
        return None
    needs_reentry = False
    if row["auth_method"] == "password" and row["password_enc"]:
        try:
            decrypt_secret(request.app.state.config_dir, row["password_enc"])
        except DecryptionError:
            needs_reentry = True
    return _host_out_from_row(row, credentials_need_reentry=needs_reentry)


@router.put("/host", response_model=HostOut)
async def put_host(body: HostIn, request: Request) -> HostOut:
    db = request.app.state.db
    config_dir = request.app.state.config_dir

    if body.auth_method == "key" and not body.key_path:
        raise HTTPException(status_code=422, detail="auth_method 'key' requires key_path")

    existing = await _get_host_row(db)

    # A password is required for auth_method 'password' only when there isn't already one
    # on file — the UI never has the plaintext to send back (§9.2), so a bare "update the
    # address" request must not be forced to re-supply the password too.
    already_has_password = (
        existing is not None and existing["auth_method"] == "password" and existing["password_enc"]
    )
    if body.auth_method == "password" and not body.password and not already_has_password:
        raise HTTPException(status_code=422, detail="auth_method 'password' requires password")

    password_enc = encrypt_secret(config_dir, body.password) if body.password else None

    if existing is None:
        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, key_path, "
            "password_enc, known_hosts_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.name,
                body.address,
                body.port,
                body.username,
                body.auth_method,
                body.key_path,
                password_enc,
                body.known_hosts_policy,
            ),
        )
        host_id = cursor.lastrowid
    else:
        host_id = existing["id"]
        # Keep the previously stored password if the caller didn't supply a new one — the
        # UI never has the plaintext to send back (§9.2), so "unchanged" must not mean
        # "cleared."
        final_password_enc = password_enc if body.password else existing["password_enc"]
        await db.execute(
            "UPDATE host SET name = ?, address = ?, port = ?, username = ?, auth_method = ?, "
            "key_path = ?, password_enc = ?, known_hosts_policy = ? WHERE id = ?",
            (
                body.name,
                body.address,
                body.port,
                body.username,
                body.auth_method,
                body.key_path,
                final_password_enc,
                body.known_hosts_policy,
                host_id,
            ),
        )
    await db.commit()

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()

    row = await _get_host_row(db)
    return _host_out_from_row(row)


async def _resolve_host_config(request: Request, override: HostTestRequest | None) -> HostConfig:
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    row = await _get_host_row(db)

    if row is None and override is None:
        raise HTTPException(status_code=404, detail="no host configured")

    def field(name: str, default):
        if override is not None and getattr(override, name, None) is not None:
            return getattr(override, name)
        return row[name] if row is not None else default

    auth_method = field("auth_method", "key")
    password: str | None = None
    if override is not None and override.password is not None:
        password = override.password
    elif row is not None and row["password_enc"]:
        try:
            password = decrypt_secret(config_dir, row["password_enc"])
        except DecryptionError as exc:
            raise HTTPException(
                status_code=409,
                detail="stored credentials cannot be decrypted; re-enter the password",
            ) from exc

    return HostConfig(
        id=row["id"] if row is not None else 0,
        address=field("address", ""),
        port=field("port", 22),
        username=field("username", ""),
        auth_method=auth_method,
        key_path=field("key_path", None),
        password=password,
        known_hosts_policy=field("known_hosts_policy", "accept-and-pin"),
    )


@router.post("/host/test", response_model=TestConnectionResponse)
async def test_host(
    request: Request, body: HostTestRequest | None = None
) -> TestConnectionResponse:
    engine = request.app.state.engine
    host_config = await _resolve_host_config(request, body)
    result = await engine.pool.test_connection(host_config)
    return TestConnectionResponse(
        ok=result.ok, error_class=result.error_class, message=result.message
    )


# --- Queues --------------------------------------------------------------------------


_QUEUE_SELECT_COLUMNS = (
    "id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode, "
    "auto_queue_enabled, auto_queue_patterns_only, auto_verify, auto_extract, auto_move, "
    "scan_interval_s"
)


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
        auto_verify=bool(row["auto_verify"]),
        auto_extract=bool(row["auto_extract"]),
        auto_move=bool(row["auto_move"]),
        scan_interval_s=row["scan_interval_s"],
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


def _effective_auto_verify(body: PathQueueIn) -> bool:
    """DESIGN.md §6: "For a queue in `move` or `sync` mode, `auto_verify` is forced on and
    cannot be turned off in the UI." Enforced here too, not only in the frontend form —
    verification is the sole gate on an irreversible remote delete (§7.3), so a direct API
    call (curl, a script) must not be able to silently disable it for a `move` queue.
    """
    return body.auto_verify or body.sync_mode == "move"


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
    db = request.app.state.db
    host_row = await _get_host_row(db)
    if host_row is None:
        raise HTTPException(status_code=409, detail="configure a host before creating a queue")

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode, auto_queue_enabled, auto_queue_patterns_only, "
        "auto_verify, auto_extract, auto_move, scan_interval_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            1 if _effective_auto_verify(body) else 0,
            1 if body.auto_extract else 0,
            1 if body.auto_move else 0,
            body.scan_interval_s,
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


@router.put("/queues/{queue_id}", response_model=PathQueueOut)
async def update_queue(queue_id: int, body: PathQueueIn, request: Request) -> PathQueueOut:
    _reject_unimplemented_sync_mode(body.sync_mode)
    _reject_invalid_scan_interval(body.scan_interval_s)
    db = request.app.state.db
    cursor = await db.execute(
        "UPDATE path_queue SET name = ?, remote_path = ?, local_path = ?, staging_path = ?, "
        "enabled = ?, sync_mode = ?, auto_queue_enabled = ?, auto_queue_patterns_only = ?, "
        "auto_verify = ?, auto_extract = ?, auto_move = ?, scan_interval_s = ? WHERE id = ?",
        (
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
            1 if body.auto_queue_enabled else 0,
            1 if body.auto_queue_patterns_only else 0,
            1 if _effective_auto_verify(body) else 0,
            1 if body.auto_extract else 0,
            1 if body.auto_move else 0,
            body.scan_interval_s,
            queue_id,
        ),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="queue not found")
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
    settings = PostprocessSettings(
        verify_enabled=body.verify_enabled,
        verify_hash_on_disk=body.verify_hash_on_disk,
        extract_enabled=body.extract_enabled,
        extract_target_dir=body.extract_target_dir,
        extract_passwords=tuple(body.extract_passwords),
        failed_retention_enabled=body.failed_retention_enabled,
        failed_retention_days=body.failed_retention_days,
        delete_archives_after_extract=body.delete_archives_after_extract,
        move_enabled=body.move_enabled,
        concurrency=max(1, body.concurrency),
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
    settings = local_delete.RetentionSettings(
        enabled=body.enabled, retention_days=body.retention_days
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
