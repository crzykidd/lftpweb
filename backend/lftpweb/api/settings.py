"""Settings → Connection and Settings → Queues (DESIGN.md §9.2).

v1 has exactly one `host` row (§3.1) — there is no host id in the URL; `GET/PUT
/api/settings/host` always operate on "the" host, creating it on first `PUT`. Path queues are
a normal collection under `/api/settings/queues`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.remote import HostConfig, RemoteScanError
from lftpweb.models import (
    HostIn,
    HostOut,
    HostTestRequest,
    PathQueueIn,
    PathQueueOut,
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
    already_has_password = existing is not None and existing["auth_method"] == "password" and existing["password_enc"]
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
async def test_host(request: Request, body: HostTestRequest | None = None) -> TestConnectionResponse:
    engine = request.app.state.engine
    host_config = await _resolve_host_config(request, body)
    result = await engine.pool.test_connection(host_config)
    return TestConnectionResponse(ok=result.ok, error_class=result.error_class, message=result.message)


# --- Queues --------------------------------------------------------------------------


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
    )


@router.get("/queues", response_model=list[PathQueueOut])
async def list_queues(request: Request) -> list[PathQueueOut]:
    cursor = await request.app.state.db.execute(
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode "
        "FROM path_queue ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [_queue_out_from_row(r) for r in rows]


@router.post("/queues", response_model=PathQueueOut, status_code=201)
async def create_queue(body: PathQueueIn, request: Request) -> PathQueueOut:
    db = request.app.state.db
    host_row = await _get_host_row(db)
    if host_row is None:
        raise HTTPException(status_code=409, detail="configure a host before creating a queue")

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            host_row["id"],
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
        ),
    )
    await db.commit()

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        engine.request_rescan()

    cursor = await db.execute(
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode "
        "FROM path_queue WHERE id = ?",
        (cursor.lastrowid,),
    )
    row = await cursor.fetchone()
    return _queue_out_from_row(row)


@router.put("/queues/{queue_id}", response_model=PathQueueOut)
async def update_queue(queue_id: int, body: PathQueueIn, request: Request) -> PathQueueOut:
    db = request.app.state.db
    cursor = await db.execute(
        "UPDATE path_queue SET name = ?, remote_path = ?, local_path = ?, staging_path = ?, "
        "enabled = ?, sync_mode = ? WHERE id = ?",
        (
            body.name,
            body.remote_path,
            body.local_path,
            body.staging_path,
            1 if body.enabled else 0,
            body.sync_mode,
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
        "SELECT id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode "
        "FROM path_queue WHERE id = ?",
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
