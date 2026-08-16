"""Settings -> Integrations (migration 018, docs/arr-integration-spec.md "API surface"):
Sonarr/Radarr instance CRUD and the Test-connection round trip. Shares the `/api/settings`
prefix with its sibling routers (`settings_host.py`, `settings_queues.py`,
`settings_postprocess.py`), same split-router convention (audit P2, docs/audit-v0.1.0.md).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core.arrclient import ArrClient, ArrClientError
from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.models import ArrInstanceIn, ArrInstanceOut, ArrTestResponse

router = APIRouter(prefix="/api/settings")

_INSTANCE_COLUMNS = (
    "id, name, kind, base_url, api_key_enc, enabled, notify_on_complete, created_at, updated_at"
)


def _instance_out_from_row(row) -> ArrInstanceOut:
    return ArrInstanceOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        base_url=row["base_url"],
        has_api_key=bool(row["api_key_enc"]),
        enabled=bool(row["enabled"]),
        notify_on_complete=bool(row["notify_on_complete"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@router.get("/arr", response_model=list[ArrInstanceOut])
async def list_arr_instances(request: Request) -> list[ArrInstanceOut]:
    cursor = await request.app.state.db.execute(
        f"SELECT {_INSTANCE_COLUMNS} FROM arr_instance ORDER BY id"
    )
    rows = await cursor.fetchall()
    return [_instance_out_from_row(r) for r in rows]


@router.post("/arr", response_model=ArrInstanceOut, status_code=201)
async def create_arr_instance(body: ArrInstanceIn, request: Request) -> ArrInstanceOut:
    if not body.api_key:
        raise HTTPException(status_code=422, detail="api_key is required")
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    api_key_enc = encrypt_secret(config_dir, body.api_key)
    now = _now_iso()
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, "
        "notify_on_complete, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.name,
            body.kind,
            body.base_url,
            api_key_enc,
            1 if body.enabled else 0,
            1 if body.notify_on_complete else 0,
            now,
            now,
        ),
    )
    await db.commit()
    cursor = await db.execute(
        f"SELECT {_INSTANCE_COLUMNS} FROM arr_instance WHERE id = ?", (cursor.lastrowid,)
    )
    row = await cursor.fetchone()
    return _instance_out_from_row(row)


async def _get_instance_row(db, instance_id: int):
    cursor = await db.execute(
        f"SELECT {_INSTANCE_COLUMNS} FROM arr_instance WHERE id = ?", (instance_id,)
    )
    return await cursor.fetchone()


@router.put("/arr/{instance_id}", response_model=ArrInstanceOut)
async def update_arr_instance(
    instance_id: int, body: ArrInstanceIn, request: Request
) -> ArrInstanceOut:
    """`api_key` omitted keeps the previously stored key -- the UI never has the plaintext to
    send back (same "unchanged must not mean cleared" rule `settings_host.py.put_host` follows
    for the seedbox password).
    """
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    existing = await _get_instance_row(db, instance_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="arr instance not found")

    api_key_enc = (
        encrypt_secret(config_dir, body.api_key) if body.api_key else existing["api_key_enc"]
    )
    await db.execute(
        "UPDATE arr_instance SET name = ?, kind = ?, base_url = ?, api_key_enc = ?, "
        "enabled = ?, notify_on_complete = ?, updated_at = ? WHERE id = ?",
        (
            body.name,
            body.kind,
            body.base_url,
            api_key_enc,
            1 if body.enabled else 0,
            1 if body.notify_on_complete else 0,
            _now_iso(),
            instance_id,
        ),
    )
    await db.commit()
    row = await _get_instance_row(db, instance_id)
    return _instance_out_from_row(row)


@router.delete("/arr/{instance_id}", status_code=204)
async def delete_arr_instance(instance_id: int, request: Request) -> None:
    """Deleting an instance un-binds every queue that referenced it -- migration 018's
    `path_queue.arr_instance_id` is `ON DELETE SET NULL`, so this is a plain delete with no
    extra bookkeeping here; a queue that was bound simply goes back to "no integration".
    """
    db = request.app.state.db
    cursor = await db.execute("DELETE FROM arr_instance WHERE id = ?", (instance_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="arr instance not found")
    await db.commit()


@router.post("/arr/{instance_id}/test", response_model=ArrTestResponse)
async def test_arr_instance(instance_id: int, request: Request) -> ArrTestResponse:
    """`GET /api/v3/system/status` round trip (docs/arr-integration-spec.md "API surface") --
    the Settings UI's Test button: reachability plus the instance's own reported version.
    Never raises for a reachable-but-erroring instance; the failure is reported in the body,
    the same "test tells you what's wrong, doesn't 500" shape `settings_host.py.test_host`
    already uses for the seedbox.
    """
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    row = await _get_instance_row(db, instance_id)
    if row is None:
        raise HTTPException(status_code=404, detail="arr instance not found")

    try:
        api_key = decrypt_secret(config_dir, row["api_key_enc"])
    except DecryptionError:
        return ArrTestResponse(
            ok=False,
            error_class="DecryptionError",
            message="stored API key cannot be decrypted; re-enter it",
        )

    async with ArrClient(kind=row["kind"], base_url=row["base_url"], api_key=api_key) as client:
        try:
            status = await client.system_status()
        except ArrClientError as exc:
            return ArrTestResponse(ok=False, error_class="ArrClientError", message=str(exc))

    return ArrTestResponse(
        ok=True, error_class=None, message="connected", version=status.get("version")
    )
