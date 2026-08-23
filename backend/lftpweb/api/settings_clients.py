"""Settings -> download clients (migration 027, docs/download-client-framework-spec.md, stage
1b of #18 -- "the download-client instance row, its API, and test-connection"): instance CRUD,
`client-types` (the registry's declared config schemas, spec §8.1), and test-connection with the
probed capability layer (spec §4.1) and the redacted capture (spec §13.3). Shares the
`/api/settings` prefix with its sibling routers (`settings_arr.py`, the shape this task mirrors
closely).

**Stage 1b is backend only.** No Settings page, no generic connector form, no README write-up --
those are stage 1b-ii. **No poller, no scheduler changes, no Preflight source** -- those are
stage 2 (spec §14). `core/arrsync.py` is untouched, per the spec's explicit instruction.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import browse as browse_core
from lftpweb.core.clients import (
    Capability,
    CapabilitySet,
    DownloadClient,
    Field as ClientField,
    Operation,
    Support,
    degrade_from_error,
    get_client_class,
    registered_clients,
)
from lftpweb.core.clients.detection import (
    DetectedBasePath,
    report_base_paths,
    report_categories,
    verify_reported_paths,
)
from lftpweb.core.clients.errors import CapabilityUnavailable, ClientError, ClientUnreachable
from lftpweb.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from lftpweb.core.engine import load_host_config
from lftpweb.models import (
    ClientConfigFieldOut,
    ClientTypeOut,
    DetectedBasePathOut,
    DownloadClientBasePathIn,
    DownloadClientBasePathOut,
    DownloadClientCategoryIn,
    DownloadClientCategoryOut,
    DownloadClientIn,
    DownloadClientOut,
    DownloadClientTestResponse,
)

# `settings_arr.py`'s own `_now_iso` -- not imported from there (that module is a sibling, not a
# shared-utility module; a third settings router adding a dependency on a second purely for one
# four-line timestamp helper is not a reuse worth the coupling).
from datetime import UTC, datetime

router = APIRouter(prefix="/api/settings")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------------------------
# Capability (de)serialization -- `CapabilitySet` <-> the JSON stored in
# `download_client.capabilities_json` (spec §4.1). Kept here, in the API layer, rather than
# added to `core/clients/base.py`: `CapabilitySet` has no persistence concept of its own by
# design (stage 0 shipped no instance rows to persist onto at all), and this task's own
# instruction is to leave the stage 0/1a framework modules alone except where genuinely
# required -- adding a storage projection is not required there, only here, where the table
# that needed it is first introduced.
# --------------------------------------------------------------------------------------------


def _capabilities_to_json(caps: CapabilitySet) -> dict[str, Any]:
    return {
        "operations": {
            op.value: {"support": cap.support.value, "note": cap.note}
            for op, cap in caps.operations.items()
        },
        "fields": {
            f.value: {"support": cap.support.value, "note": cap.note}
            for f, cap in caps.fields.items()
        },
    }


def _capabilities_from_json(data: dict[str, Any]) -> CapabilitySet:
    operations = {
        Operation(k): Capability(support=Support(v["support"]), note=v.get("note"))
        for k, v in data.get("operations", {}).items()
    }
    fields = {
        ClientField(k): Capability(support=Support(v["support"]), note=v.get("note"))
        for k, v in data.get("fields", {}).items()
    }
    return CapabilitySet(operations=operations, fields=fields)


async def _persist_capabilities(
    db, client_id: int, caps: CapabilitySet, *, version: str | None
) -> None:
    now = _now_iso()
    await db.execute(
        "UPDATE download_client SET capabilities_json = ?, capabilities_probed_at = ?, "
        "version = ?, updated_at = ? WHERE id = ?",
        (json.dumps(_capabilities_to_json(caps)), now, version, now, client_id),
    )
    await db.commit()


# --------------------------------------------------------------------------------------------
# Config-schema-driven request validation (spec §8.1) -- each connector declares its own
# connection-config schema (`DownloadClient.config_schema`, a tuple of `ConfigField`), so there
# is no fixed `base_url`/`api_key` pair to validate the way `ArrInstanceIn` does; instead every
# submitted `config` dict is checked and split against whichever connector's schema the request
# names.
# --------------------------------------------------------------------------------------------


def _validate_and_split_non_secret(
    client_class: type[DownloadClient], config: dict[str, Any]
) -> dict[str, Any]:
    non_secret: dict[str, Any] = {}
    for entry in client_class.config_schema:
        if entry.kind == "secret":
            continue
        value = config.get(entry.key, entry.default)
        if entry.required and (value is None or value == ""):
            raise HTTPException(status_code=422, detail=f"config.{entry.key} is required")
        non_secret[entry.key] = value
    return non_secret


def _validate_and_build_secret(
    client_class: type[DownloadClient], config: dict[str, Any]
) -> dict[str, Any]:
    secret: dict[str, Any] = {}
    for entry in client_class.config_schema:
        if entry.kind != "secret":
            continue
        value = config.get(entry.key, entry.default)
        if entry.required and (value is None or value == ""):
            raise HTTPException(status_code=422, detail=f"config.{entry.key} is required")
        secret[entry.key] = value
    return secret


def _secret_provided(client_class: type[DownloadClient], config: dict[str, Any]) -> bool:
    """Whether the request body actually names any of this connector's declared secret keys --
    the one bit `update_client_instance` needs to decide "replace the stored secret" vs "keep it
    unchanged." Same `if body.api_key` test `settings_arr.py.update_arr_instance` uses for its
    one named field, generalized to however many secret keys a connector's schema declares:
    **all-or-nothing**, exactly like that precedent -- a caller either resends every secret
    field it wants to keep configured, or none at all and the entire previously-encrypted blob
    is left untouched, byte for byte, with no decrypt-merge-reencrypt round trip needed.
    """
    return any(config.get(f.key) for f in client_class.config_schema if f.kind == "secret")


async def _reject_invalid_base_paths(request: Request, paths: list[str]) -> None:
    """Every submitted base path is validated on save against `core/browse.py.
    remote_directory_error` (spec §8.2: "mandatory... never silently accepted") -- a wrong base
    path is a wrong safety boundary for the §10.2 containment check and the §11 scan roots read
    from this table.

    Deliberately reuses `api/settings_queues.py._reject_invalid_remote_path`'s own best-effort
    asymmetry for *unreachability* specifically, rather than inventing a stricter rule here: an
    unconfigured, unreachable, or `credentials_need_reentry` host must not lock the user out of
    saving Settings any more here than it already does for a queue's own `remote_path` --
    `core/browse.py.remote_directory_error`'s own docstring is explicit that "deciding that an
    ambiguous failure means 'allow the save' is the caller's job, not this function's." Only a
    clean, live "no such directory" answer from the seedbox blocks anything, which is exactly
    the case the spec's "mandatory... never silently accepted" language is actually about.
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
            for path in paths:
                await browse_core.remote_directory_error(sftp, path)
    except browse_core.RemotePathNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"base_paths: {exc}") from exc
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - best-effort: any other failure means "allow the save"
        return


async def _validate_category_queue_ids(db, categories: list[DownloadClientCategoryIn]) -> None:
    for cat in categories:
        if cat.queue_id is None:
            continue
        cursor = await db.execute("SELECT id FROM path_queue WHERE id = ?", (cat.queue_id,))
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=400, detail=f"queue_id {cat.queue_id} does not exist")


async def _replace_base_paths(
    db, client_id: int, base_paths: list[DownloadClientBasePathIn]
) -> None:
    """Base paths and categories are **fully replaced** on every save (create starts from
    nothing; update deletes-then-reinserts) -- there is no stage 1b-ii client UI yet to depend
    on a stable child-row id surviving an edit, and a full replace is one statement shape
    shared by both create and update rather than two.
    """
    await db.execute("DELETE FROM download_client_base_path WHERE client_id = ?", (client_id,))
    for bp in base_paths:
        await db.execute(
            "INSERT INTO download_client_base_path (client_id, path, kind, client_path, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (client_id, bp.path, bp.kind, bp.client_path, bp.source),
        )


async def _replace_categories(
    db, client_id: int, categories: list[DownloadClientCategoryIn]
) -> None:
    await db.execute("DELETE FROM download_client_category WHERE client_id = ?", (client_id,))
    for cat in categories:
        await db.execute(
            "INSERT INTO download_client_category (client_id, category, queue_id) "
            "VALUES (?, ?, ?)",
            (client_id, cat.category, cat.queue_id),
        )


# --------------------------------------------------------------------------------------------
# Test-on-save (spec §3a, this task's own handoff prompt: "we should test at save and not save
# if enabled and test failed -- this is how the *arr clients handle it"). `enabled: true` tests
# the *submitted* config before anything is persisted; `enabled: false` never tests and always
# saves -- the deliberate escape hatch for a client that is temporarily unreachable, so a user
# is never locked out of editing (or disabling) their own broken instance.
# --------------------------------------------------------------------------------------------


async def _test_submitted_config_or_raise(
    client_class: type[DownloadClient], config: dict[str, Any]
):
    """Construct the connector from the *submitted* config (never a stored row -- on create
    there is no row yet) and call `test_connection()`. On success, returns the `ConnectionInfo`
    for the caller to persist alongside the row it's about to write. On any failure --
    `ClientUnreachable`, the base `ClientError`, or `CapabilityUnavailable` (all three subclass
    `ClientError`, spec §4.2's taxonomy) -- raises an `HTTPException(400)` carrying the real
    error class and message, **and never persists anything**: the caller must not have written
    the row yet when this raises.

    The message states the escape hatch explicitly (this task's own instruction: "the UI must
    make it obvious") rather than leaving a user to discover that unchecking Enabled is how to
    save a temporarily-broken instance.
    """
    client = client_class(config=config)
    try:
        return await client.test_connection()
    except ClientError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_class": type(exc).__name__,
                "message": (
                    f"connection test failed, so nothing was saved: {exc}. Uncheck Enabled to "
                    "save anyway and fix it later, or fix the settings above and try again."
                ),
            },
        ) from exc
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


def _resolve_secret_for_test(
    client_class: type[DownloadClient],
    config_dir,
    submitted_config: dict[str, Any],
    existing_secret_enc: str | None,
) -> dict[str, Any]:
    """The secret half of the config to actually test against on an update: the submitted
    secret if the request named one (`_secret_provided`'s all-or-nothing rule), otherwise the
    existing encrypted secret decrypted -- an update that omits every secret field is keeping
    the stored value, and testing anything else would mean testing a config that isn't the one
    about to be saved.
    """
    if existing_secret_enc is None or _secret_provided(client_class, submitted_config):
        return _validate_and_build_secret(client_class, submitted_config)
    try:
        return json.loads(decrypt_secret(config_dir, existing_secret_enc))
    except DecryptionError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_class": "DecryptionError",
                "message": (
                    "the stored secret cannot be decrypted, so an enabled save cannot be "
                    "tested; re-enter it above, or uncheck Enabled to save without testing."
                ),
            },
        ) from exc


# --------------------------------------------------------------------------------------------
# Row -> API projection.
# --------------------------------------------------------------------------------------------

_CLIENT_COLUMNS = (
    "id, name, client_type, config_json, secret_enc, enabled, "
    "capabilities_json, capabilities_probed_at, version, created_at, updated_at, "
    # The poller's own last-pass status (migration 029, finding #2, 2026-08-23) -- see
    # `DownloadClientOut`'s own docstring for why this is distinct from `capabilities_probed_at`.
    "last_poll_at, last_poll_ok, last_poll_message, last_success_at"
)


async def _get_client_row(db, client_id: int):
    cursor = await db.execute(
        f"SELECT {_CLIENT_COLUMNS} FROM download_client WHERE id = ?", (client_id,)
    )
    return await cursor.fetchone()


async def _get_base_paths(db, client_id: int) -> list[DownloadClientBasePathOut]:
    cursor = await db.execute(
        "SELECT id, path, kind, client_path, source FROM download_client_base_path "
        "WHERE client_id = ? ORDER BY id",
        (client_id,),
    )
    rows = await cursor.fetchall()
    return [
        DownloadClientBasePathOut(
            id=r["id"],
            path=r["path"],
            kind=r["kind"],
            client_path=r["client_path"],
            source=r["source"],
        )
        for r in rows
    ]


async def _get_categories(db, client_id: int) -> list[DownloadClientCategoryOut]:
    cursor = await db.execute(
        "SELECT id, category, queue_id FROM download_client_category "
        "WHERE client_id = ? ORDER BY id",
        (client_id,),
    )
    rows = await cursor.fetchall()
    return [
        DownloadClientCategoryOut(id=r["id"], category=r["category"], queue_id=r["queue_id"])
        for r in rows
    ]


async def _client_out_from_row(db, row) -> DownloadClientOut:
    return DownloadClientOut(
        id=row["id"],
        name=row["name"],
        client_type=row["client_type"],
        config=json.loads(row["config_json"]) if row["config_json"] else {},
        # Never the secret itself, in any form (mirrors `ArrInstanceOut.has_api_key`) --
        # whether one is on file, generalized from one named field to however many secret keys
        # a connector's schema declares.
        has_secret=bool(row["secret_enc"]),
        enabled=bool(row["enabled"]),
        capabilities=json.loads(row["capabilities_json"]) if row["capabilities_json"] else None,
        capabilities_probed_at=row["capabilities_probed_at"],
        version=row["version"],
        base_paths=await _get_base_paths(db, row["id"]),
        categories=await _get_categories(db, row["id"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_poll_at=row["last_poll_at"],
        last_poll_ok=bool(row["last_poll_ok"]) if row["last_poll_ok"] is not None else None,
        last_poll_message=row["last_poll_message"],
        last_success_at=row["last_success_at"],
    )


# --------------------------------------------------------------------------------------------
# `GET /api/settings/client-types` -- the registry's available connectors (spec §6), each with
# its declared config schema, so stage 1b-ii can render one generic form for all of them
# (spec §8.1) instead of one hand-authored form per connector.
# --------------------------------------------------------------------------------------------


@router.get("/client-types", response_model=list[ClientTypeOut])
async def list_client_types() -> list[ClientTypeOut]:
    return [
        ClientTypeOut(
            client_type=client_type,
            family=cls.family,
            config_schema=[
                ClientConfigFieldOut(
                    key=f.key,
                    label=f.label,
                    kind=f.kind,
                    required=f.required,
                    default=f.default,
                    help_text=f.help_text,
                )
                for f in cls.config_schema
            ],
        )
        for client_type, cls in sorted(registered_clients().items())
    ]


# --------------------------------------------------------------------------------------------
# Instance CRUD.
# --------------------------------------------------------------------------------------------


@router.get("/clients", response_model=list[DownloadClientOut])
async def list_client_instances(request: Request) -> list[DownloadClientOut]:
    db = request.app.state.db
    cursor = await db.execute(f"SELECT {_CLIENT_COLUMNS} FROM download_client ORDER BY id")
    rows = await cursor.fetchall()
    return [await _client_out_from_row(db, r) for r in rows]


def _get_client_class_or_400(client_type: str) -> type[DownloadClient]:
    try:
        return get_client_class(client_type)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"unknown client_type {client_type!r}"
        ) from None


@router.post("/clients", response_model=DownloadClientOut, status_code=201)
async def create_client_instance(body: DownloadClientIn, request: Request) -> DownloadClientOut:
    """`body.enabled` gates a connectivity test against the *submitted* config before anything
    is written (spec §3a) -- on create there is no row yet, so the connector is built straight
    from the request body, never persist-then-test-then-rollback. A failing test raises before
    the `INSERT` runs at all; a passing one persists the probed capabilities and version from
    that same call, so a fresh instance never needs a separate Test click to show them.
    """
    client_class = _get_client_class_or_400(body.client_type)
    non_secret = _validate_and_split_non_secret(client_class, body.config)
    secret = _validate_and_build_secret(client_class, body.config)

    db = request.app.state.db
    await _validate_category_queue_ids(db, body.categories)
    await _reject_invalid_base_paths(request, [bp.path for bp in body.base_paths])

    info = None
    if body.enabled:
        info = await _test_submitted_config_or_raise(client_class, {**non_secret, **secret})

    config_dir = request.app.state.config_dir
    secret_enc = encrypt_secret(config_dir, json.dumps(secret)) if secret else None
    now = _now_iso()
    caps_json = json.dumps(_capabilities_to_json(client_class.capabilities)) if info else None
    probed_at = now if info else None
    version = info.version if info else None
    cursor = await db.execute(
        "INSERT INTO download_client (name, client_type, config_json, secret_enc, enabled, "
        "capabilities_json, capabilities_probed_at, version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            body.name,
            body.client_type,
            json.dumps(non_secret),
            secret_enc,
            1 if body.enabled else 0,
            caps_json,
            probed_at,
            version,
            now,
            now,
        ),
    )
    client_id = cursor.lastrowid
    await _replace_base_paths(db, client_id, body.base_paths)
    await _replace_categories(db, client_id, body.categories)
    await db.commit()

    row = await _get_client_row(db, client_id)
    return await _client_out_from_row(db, row)


@router.put("/clients/{client_id}", response_model=DownloadClientOut)
async def update_client_instance(
    client_id: int, body: DownloadClientIn, request: Request
) -> DownloadClientOut:
    """Every field is a full replace, same as `settings_arr.py.update_arr_instance` and
    `settings_queues.py.update_queue`'s own non-toggle fields -- **except** the connector's
    declared secret key(s), which follow `_secret_provided`'s all-or-nothing rule above: omit
    every secret key from `config` to keep the stored secret exactly as it was, name any one of
    them to replace the whole encrypted blob.

    `body.enabled` gates the same test-before-persist rule as create (spec §3a): tested against
    the submitted config (the new secret if one was sent, otherwise the existing one decrypted --
    never a stripped-down stand-in for what will actually be saved), and a failing test raises
    before the `UPDATE` runs, leaving the existing row completely untouched. **Disabling an
    instance is the escape hatch and always succeeds**, even one that is currently broken --
    `body.enabled = False` never tests at all, so unchecking Enabled is how a user recovers a
    broken instance to fix it. A disabling save leaves any previously-probed capabilities/version
    exactly as they were (not cleared -- they're still the last known truth); only a fresh
    successful enabled test resets them, same "layer 3 cleared by the next successful probe"
    rule the standalone `/test` endpoint follows.
    """
    db = request.app.state.db
    existing = await _get_client_row(db, client_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="download client not found")

    client_class = _get_client_class_or_400(body.client_type)
    non_secret = _validate_and_split_non_secret(client_class, body.config)

    config_dir = request.app.state.config_dir
    if _secret_provided(client_class, body.config):
        secret = _validate_and_build_secret(client_class, body.config)
        secret_enc = encrypt_secret(config_dir, json.dumps(secret)) if secret else None
    else:
        secret_enc = existing["secret_enc"]

    await _validate_category_queue_ids(db, body.categories)
    await _reject_invalid_base_paths(request, [bp.path for bp in body.base_paths])

    info = None
    if body.enabled:
        test_secret = _resolve_secret_for_test(
            client_class, config_dir, body.config, existing["secret_enc"]
        )
        info = await _test_submitted_config_or_raise(client_class, {**non_secret, **test_secret})

    now = _now_iso()
    if info is not None:
        await db.execute(
            "UPDATE download_client SET name = ?, client_type = ?, config_json = ?, "
            "secret_enc = ?, enabled = ?, capabilities_json = ?, capabilities_probed_at = ?, "
            "version = ?, updated_at = ? WHERE id = ?",
            (
                body.name,
                body.client_type,
                json.dumps(non_secret),
                secret_enc,
                1 if body.enabled else 0,
                json.dumps(_capabilities_to_json(client_class.capabilities)),
                now,
                info.version,
                now,
                client_id,
            ),
        )
    else:
        # Not tested this time (disabled) -- previously-probed capabilities/version, if any,
        # are left exactly as they were; only `name`/`client_type`/`config`/`secret`/`enabled`
        # change.
        await db.execute(
            "UPDATE download_client SET name = ?, client_type = ?, config_json = ?, "
            "secret_enc = ?, enabled = ?, updated_at = ? WHERE id = ?",
            (
                body.name,
                body.client_type,
                json.dumps(non_secret),
                secret_enc,
                1 if body.enabled else 0,
                now,
                client_id,
            ),
        )
    await _replace_base_paths(db, client_id, body.base_paths)
    await _replace_categories(db, client_id, body.categories)
    await db.commit()

    row = await _get_client_row(db, client_id)
    return await _client_out_from_row(db, row)


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client_instance(client_id: int, request: Request) -> None:
    """Deleting an instance cascades to its own base paths and categories (migration 027:
    `ON DELETE CASCADE` on `client_id` in both child tables -- they are genuine child records of
    the instance, not a soft cross-reference), and un-binds (never deletes) any `path_queue` row
    a category mapping pointed at only via that queue's own deletion, never this one.
    """
    db = request.app.state.db
    cursor = await db.execute("DELETE FROM download_client WHERE id = ?", (client_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="download client not found")
    await db.commit()


# --------------------------------------------------------------------------------------------
# Test-connection, the probed capability layer, the capture (spec §4.1, §13.3), and base-path
# detection (spec §8.2 correction, migration 028).
# --------------------------------------------------------------------------------------------


def _detected_to_out(detected: DetectedBasePath) -> DetectedBasePathOut:
    return DetectedBasePathOut(
        client_path=detected.client_path,
        kind=detected.kind,
        state=detected.state,
        resolved_candidate=detected.resolved_candidate,
    )


async def _detect_and_verify_base_paths(
    request: Request, client: DownloadClient, capabilities: CapabilitySet
) -> list[DetectedBasePath]:
    """The full detect -> verify pass (spec §8.2 correction): `client.list_base_paths()`
    (never treated as exhaustive or authoritative), each result checked over SSH via
    `core.browse.remote_directory_error`. Reuses `api/browse.py`'s own pattern for obtaining an
    SFTP client against the configured host -- never opens its own connection.

    **A detection failure must never fail the connection test** -- reachability and detection
    are different questions (spec §4.2's temperament). No host configured, undecryptable
    credentials, or the connection attempt itself failing all fall back to `verify_reported_
    paths(reported, sftp=None)`, which reports every entry `unverified`, never `not_found`: not
    being able to look is not the seedbox saying no.
    """
    reported = await report_base_paths(client, capabilities)
    if not reported:
        return []
    engine = getattr(request.app.state, "engine", None)
    host = None
    if engine is not None:
        host = await load_host_config(request.app.state.db, request.app.state.config_dir)
    if host is None or host.credentials_need_reentry:
        return await verify_reported_paths(reported, None)
    try:
        conn = await engine.pool.get_connection(host)
        async with conn.start_sftp_client() as sftp:
            return await verify_reported_paths(reported, sftp)
    except Exception:  # noqa: BLE001 - detection must never fail the connection test
        return await verify_reported_paths(reported, None)


@router.post("/clients/{client_id}/test", response_model=DownloadClientTestResponse)
async def test_client_instance(client_id: int, request: Request) -> DownloadClientTestResponse:
    """Construct the registered connector, call `test_connection()`, and persist the resolved
    capability set (spec §4.1) plus the client's own reported version. On a fresh success, also
    detects and SSH-verifies this connector's own base paths (spec §8.2 correction) and detects
    its own categories (spec §8.3, joined 2026-08-23) -- **only** on success, since a connector
    that could not even be reached has nothing to detect from.

    **The redacted capture (spec §13.3) is not duplicated here.** `SabnzbdClient.test_connection`
    already writes it via `core/clients/capture.py` before doing anything else with the
    response (that module's own docstring: "test_connection is the one method that actually
    exercises the capture helper end to end"); this endpoint's job is only to actually call
    through to a real `test_connection()`, which is what makes that capture fire in the first
    place with real bytes from a real instance once this deploys.

    Three rules this endpoint must never get wrong (spec §4.2, this task's own handoff prompt):

    - **Only `CapabilityUnavailable` degrades a capability** -- routed exclusively through
      `core.clients.base.degrade_from_error`; `ClientUnreachable` and the base `ClientError`
      change no capability at all, ever.
    - **A failed test never wipes a previously probed set** -- on anything other than a fresh
      success, whatever was already persisted (or the connector's static declaration, if this
      instance was never successfully probed) is exactly what is returned and re-persisted.
    - **A fresh success resets capabilities to the connector's static declaration** -- the
      probed/runtime-degraded layers are refined *from* a success, and layer 3 is explicitly
      cleared by the next successful probe (spec §4.1). This stage's SABnzbd connector performs
      no version-based narrowing of its own during `test_connection`, so "resolved" and
      "static" coincide for now; a later connector that does narrow by version would return
      something already-narrowed from its own `test_connection` for this endpoint to persist
      instead.
    """
    db = request.app.state.db
    config_dir = request.app.state.config_dir
    row = await _get_client_row(db, client_id)
    if row is None:
        raise HTTPException(status_code=404, detail="download client not found")

    last_known_json = json.loads(row["capabilities_json"]) if row["capabilities_json"] else None

    try:
        client_class = get_client_class(row["client_type"])
    except KeyError:
        return DownloadClientTestResponse(
            ok=False,
            error_class="UnknownClientType",
            message=f"unregistered client_type {row['client_type']!r}",
            version=row["version"],
            capabilities=last_known_json,
        )

    last_known = (
        _capabilities_from_json(last_known_json)
        if last_known_json is not None
        else client_class.capabilities
    )

    non_secret = json.loads(row["config_json"]) if row["config_json"] else {}
    secret: dict[str, Any] = {}
    if row["secret_enc"]:
        try:
            secret = json.loads(decrypt_secret(config_dir, row["secret_enc"]))
        except DecryptionError:
            return DownloadClientTestResponse(
                ok=False,
                error_class="DecryptionError",
                message="stored secret cannot be decrypted; re-enter it",
                version=row["version"],
                capabilities=_capabilities_to_json(last_known),
            )
    config = {**non_secret, **secret}

    client = client_class(config=config)
    try:
        try:
            info = await client.test_connection()
        except CapabilityUnavailable as exc:
            degraded = degrade_from_error(last_known, Operation.TEST_CONNECTION, exc)
            await _persist_capabilities(db, client_id, degraded, version=row["version"])
            return DownloadClientTestResponse(
                ok=False,
                error_class="CapabilityUnavailable",
                message=str(exc),
                version=row["version"],
                capabilities=_capabilities_to_json(degraded),
                detected_base_paths=[],
                detected_categories=[],
            )
        except ClientUnreachable as exc:
            return DownloadClientTestResponse(
                ok=False,
                error_class="ClientUnreachable",
                message=str(exc),
                version=row["version"],
                capabilities=_capabilities_to_json(last_known),
                detected_base_paths=[],
                detected_categories=[],
            )
        except ClientError as exc:
            return DownloadClientTestResponse(
                ok=False,
                error_class="ClientError",
                message=str(exc),
                version=row["version"],
                capabilities=_capabilities_to_json(last_known),
                detected_base_paths=[],
                detected_categories=[],
            )
        # Success -- detect base paths and categories while the connector is still open (its own
        # HTTP session is torn down in `finally`, below).
        detected = await _detect_and_verify_base_paths(request, client, client_class.capabilities)
        detected_categories = await report_categories(client, client_class.capabilities)
    finally:
        # Not part of the `DownloadClient` ABC (only `SabnzbdClient` itself declares
        # `aclose`/`__aenter__`/`__aexit__`) -- see this task's own report for why that is a
        # spec gap rather than an oversight here. `getattr` degrades gracefully for a future
        # connector that manages its own transport lifetime differently.
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()

    await _persist_capabilities(db, client_id, client_class.capabilities, version=info.version)
    return DownloadClientTestResponse(
        ok=True,
        error_class=None,
        message="connected",
        version=info.version,
        capabilities=_capabilities_to_json(client_class.capabilities),
        detected_base_paths=[_detected_to_out(d) for d in detected],
        detected_categories=detected_categories,
    )
