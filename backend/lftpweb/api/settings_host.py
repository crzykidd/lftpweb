"""Settings -> Connection: the single `host` row and its Test-connection endpoint
(DESIGN.md §3.1, §9.2). Split out of the former monolithic `api/settings.py` (audit P2,
docs/audit-v0.1.0.md); shares the `/api/settings` prefix with its sibling routers."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.remote import (
    HostConfig,
    InvalidPrivateKeyError,
    parse_connection_limit,
    validate_private_key,
)
from lftpweb.models import (
    HostIn,
    HostOut,
    HostTestRequest,
    TestConnectionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")


async def _get_host_row(db):
    cursor = await db.execute(
        "SELECT id, name, address, port, username, auth_method, key_path, password_enc, "
        "ssh_key_enc, known_hosts_policy, connection_overrides FROM host ORDER BY id LIMIT 1"
    )
    return await cursor.fetchone()


def _host_out_from_row(row, credentials_need_reentry: bool = False) -> HostOut:
    has_ssh_key = bool(row["ssh_key_enc"])
    # The coexistence rule (migration 014, DESIGN.md §8): a pasted key wins over `key_path`
    # when both are set. Computed once here rather than in the frontend so the UI can't drift
    # from what `core/remote.py._resolve_client_keys` / `core/lftp.py.spawn` actually do.
    active_key_source: Literal["pasted", "path"] | None = None
    if row["auth_method"] == "key":
        if has_ssh_key:
            active_key_source = "pasted"
        elif row["key_path"]:
            active_key_source = "path"
    return HostOut(
        id=row["id"],
        name=row["name"],
        address=row["address"],
        port=row["port"],
        username=row["username"],
        auth_method=row["auth_method"],
        key_path=row["key_path"],
        has_password=bool(row["password_enc"]),
        has_ssh_key=has_ssh_key,
        active_key_source=active_key_source,
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
    elif row["auth_method"] == "key" and row["ssh_key_enc"]:
        try:
            decrypt_secret(request.app.state.config_dir, row["ssh_key_enc"])
        except DecryptionError:
            needs_reentry = True
    return _host_out_from_row(row, credentials_need_reentry=needs_reentry)


@router.put("/host", response_model=HostOut)
async def put_host(body: HostIn, request: Request) -> HostOut:
    db = request.app.state.db
    config_dir = request.app.state.config_dir

    existing = await _get_host_row(db)

    # migration 014: `ssh_key` is an *additional* way to satisfy `auth_method = 'key'`,
    # alongside `key_path` -- so the requirement is "at least one," not "key_path
    # specifically." A bare "update the address" request must not be forced to re-supply
    # whichever one is already on file, same reasoning as the password check below.
    already_has_key_material = (
        existing is not None
        and existing["auth_method"] == "key"
        and (existing["key_path"] or existing["ssh_key_enc"])
    )
    if (
        body.auth_method == "key"
        and not body.key_path
        and not body.ssh_key
        and not already_has_key_material
    ):
        raise HTTPException(
            status_code=422, detail="auth_method 'key' requires key_path or a pasted key"
        )

    # Validate a pasted key at save time (DESIGN.md §8) -- parses as a private key, and isn't
    # passphrase-protected (lftpweb cannot supply a passphrase non-interactively). Rejecting
    # here means the failure surfaces immediately, not at the next scan or transfer attempt.
    if body.ssh_key:
        try:
            validate_private_key(body.ssh_key)
        except InvalidPrivateKeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A password is required for auth_method 'password' only when there isn't already one
    # on file — the UI never has the plaintext to send back (§9.2), so a bare "update the
    # address" request must not be forced to re-supply the password too.
    already_has_password = (
        existing is not None and existing["auth_method"] == "password" and existing["password_enc"]
    )
    if body.auth_method == "password" and not body.password and not already_has_password:
        raise HTTPException(status_code=422, detail="auth_method 'password' requires password")

    password_enc = encrypt_secret(config_dir, body.password) if body.password else None
    ssh_key_enc = encrypt_secret(config_dir, body.ssh_key) if body.ssh_key else None

    if existing is None:
        cursor = await db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, key_path, "
            "password_enc, ssh_key_enc, known_hosts_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.name,
                body.address,
                body.port,
                body.username,
                body.auth_method,
                body.key_path,
                password_enc,
                ssh_key_enc,
                body.known_hosts_policy,
            ),
        )
        host_id = cursor.lastrowid
    else:
        host_id = existing["id"]
        # Keep the previously stored password / key if the caller didn't supply a new one —
        # the UI never has the plaintext to send back (§9.2), so "unchanged" must not mean
        # "cleared."
        final_password_enc = password_enc if body.password else existing["password_enc"]
        final_ssh_key_enc = ssh_key_enc if body.ssh_key else existing["ssh_key_enc"]
        await db.execute(
            "UPDATE host SET name = ?, address = ?, port = ?, username = ?, auth_method = ?, "
            "key_path = ?, password_enc = ?, ssh_key_enc = ?, known_hosts_policy = ? WHERE id = ?",
            (
                body.name,
                body.address,
                body.port,
                body.username,
                body.auth_method,
                body.key_path,
                final_password_enc,
                final_ssh_key_enc,
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

    # migration 014: same "override, else stored, else nothing" resolution as password above,
    # so *Test connection* can validate a pasted-but-unsaved key too (§9.2's "test before
    # committing").
    ssh_key: str | None = None
    if override is not None and override.ssh_key is not None:
        ssh_key = override.ssh_key
    elif row is not None and row["ssh_key_enc"]:
        try:
            ssh_key = decrypt_secret(config_dir, row["ssh_key_enc"])
        except DecryptionError as exc:
            raise HTTPException(
                status_code=409,
                detail="stored credentials cannot be decrypted; re-enter the key",
            ) from exc

    return HostConfig(
        id=row["id"] if row is not None else 0,
        address=field("address", ""),
        port=field("port", 22),
        username=field("username", ""),
        auth_method=auth_method,
        key_path=field("key_path", None),
        password=password,
        ssh_key=ssh_key,
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
