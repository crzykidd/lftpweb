"""Settings → Connection and Settings → Queues (DESIGN.md §9.2).

v1 has exactly one `host` row (§3.1) — there is no host id in the URL; `GET/PUT
/api/settings/host` always operate on "the" host, creating it on first `PUT`. Path queues are
a normal collection under `/api/settings/queues`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import patterns as patterns_core
from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.mount_sentinel import check as mount_ok_check
from lftpweb.core.remote import HostConfig
from lftpweb.models import (
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
    QueueAutoQueueStatus,
    TestConnectionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")


async def _get_host_row(db):
    cursor = await db.execute(
        "SELECT id, name, address, port, username, auth_method, key_path, password_enc, "
        "known_hosts_policy FROM host ORDER BY id LIMIT 1"
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
    "auto_queue_enabled, auto_queue_patterns_only"
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
    )


# Only `copy` actually does anything today. `move` (delete the remote after a verified
# transfer) is DESIGN.md §13 phase 5, and `sync` (propagate local deletes) is §7, unscheduled.
# Both are valid values in the schema, and the column exists so those phases can drop in — but
# accepting one now stores a setting that silently behaves as `copy`. On a seedbox that means
# an operator believes their disk is being reclaimed while it quietly fills. Refusing with a
# clear reason beats a switch that lies.
IMPLEMENTED_SYNC_MODES = frozenset({"copy"})

_UNIMPLEMENTED_REASON = {
    "move": "delete-after-download is build phase 5 (DESIGN.md §13); not implemented yet",
    "sync": "local-delete propagation is not scheduled (DESIGN.md §7)",
}


def _reject_unimplemented_sync_mode(mode: str) -> None:
    if mode not in IMPLEMENTED_SYNC_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"sync_mode '{mode}' is not available: "
            f"{_UNIMPLEMENTED_REASON.get(mode, 'unknown mode')}. Use 'copy'.",
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
    db = request.app.state.db
    host_row = await _get_host_row(db)
    if host_row is None:
        raise HTTPException(status_code=409, detail="configure a host before creating a queue")

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode, auto_queue_enabled, auto_queue_patterns_only) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    db = request.app.state.db
    cursor = await db.execute(
        "UPDATE path_queue SET name = ?, remote_path = ?, local_path = ?, staging_path = ?, "
        "enabled = ?, sync_mode = ?, auto_queue_enabled = ?, auto_queue_patterns_only = ? "
        "WHERE id = ?",
        (
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
            1 if body.auto_queue_enabled else 0,
            1 if body.auto_queue_patterns_only else 0,
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
        matched = compiled.item_matches(rel_path, is_file=not node.is_dir)
        items.append(PatternPreviewItem(rel_path=rel_path, is_dir=node.is_dir, matched=matched))
        if node.is_dir:
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
            if node.is_dir or not rel_path.startswith(prefix):
                continue
            basename = rel_path.rsplit("/", 1)[-1]
            sample_files.append(
                PatternPreviewFile(rel_path=rel_path, excluded=compiled.file_excluded(basename))
            )

    return PatternPreviewResponse(items=items, sample_item=sample_item, sample_files=sample_files)
