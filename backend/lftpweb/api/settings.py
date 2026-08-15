"""Settings → Connection and Settings → Queues (DESIGN.md §9.2).

v1 has exactly one `host` row (§3.1) — there is no host id in the URL; `GET/PUT
/api/settings/host` always operate on "the" host, creating it on first `PUT`. Path queues are
a normal collection under `/api/settings/queues`.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import local_delete
from lftpweb.core import patterns as patterns_core
from lftpweb.core.autoqueue import (
    AutoQueueSettings,
    load_autoqueue_settings,
    save_autoqueue_settings,
)
from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.download_prefix import (
    DownloadPrefixSettings,
    load_download_prefix_settings,
    save_download_prefix_settings,
    validate_prefix,
)
from lftpweb.core.mount_sentinel import COMPLETE_STATES, DEFAULT_GRACE_S
from lftpweb.core.mount_sentinel import check as mount_ok_check
from lftpweb.core.postprocess import (
    PostprocessSettings,
    load_postprocess_settings,
    save_postprocess_settings,
)
from lftpweb.core.remote import (
    HostConfig,
    InvalidPrivateKeyError,
    parse_connection_limit,
    validate_private_key,
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
    OrphanTempCleanupSettingsIn,
    OrphanTempCleanupSettingsOut,
    PostprocessSettingsIn,
    PostprocessSettingsOut,
    QueueAutoQueueStatus,
    RemovalGraceSettingsOut,
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


# --- Queues --------------------------------------------------------------------------


_QUEUE_SELECT_COLUMNS = (
    "id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode, "
    "auto_queue_enabled, auto_queue_patterns_only, auto_verify, auto_extract, auto_move, "
    "auto_delete_archives, scan_interval_s, download_prefix_enabled, download_prefix"
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
    db = request.app.state.db
    host_row = await _get_host_row(db)
    if host_row is None:
        raise HTTPException(status_code=409, detail="configure a host before creating a queue")

    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, staging_path, "
        "enabled, sync_mode, auto_queue_enabled, auto_queue_patterns_only, "
        "auto_verify, auto_extract, auto_move, auto_delete_archives, scan_interval_s, "
        "download_prefix_enabled, download_prefix) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    db = request.app.state.db

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
        "scan_interval_s = ?, download_prefix_enabled = ?, download_prefix = ? WHERE id = ?",
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
