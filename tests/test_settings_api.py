from __future__ import annotations

import asyncssh
from fastapi.testclient import TestClient

from lftpweb.core.itemview import ItemView, item_view
from lftpweb.main import app


def _generate_test_key_pem() -> str:
    key = asyncssh.generate_private_key("ssh-ed25519")
    return key.export_private_key().decode("ascii")


def _view(
    rel_path: str,
    *,
    is_dir: bool,
    remote_size: int | None = None,
    remote_mtime: float | None = None,
) -> ItemView:
    """One node as `Engine._project` would have read it back out of the `item` table -- built
    through `item_view` itself so the fixture can't drift from the real projection.
    """
    return item_view(
        {
            "id": abs(hash(rel_path)) % 100_000,
            "rel_path": rel_path,
            "is_dir": 1 if is_dir else 0,
            "state": "REMOTE_ONLY",
            "substate": None,
            "suppressed_reason": None,
            "remote_size": remote_size,
            "local_size": None,
            "remote_mtime": remote_mtime,
            "local_mtime": None,
            "state_changed_at": None,
            "first_seen_at": None,
            "downloaded_at": None,
            "verified_at": None,
            "extracted_at": None,
            "first_missing_at": None,
            "remote_deleted_at": None,
            "pending_download_prefix": None,
        }
    )


def test_get_host_returns_null_when_unconfigured(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/host")
        assert resp.status_code == 200
        assert resp.json() is None


def test_put_host_creates_and_never_returns_plaintext_password(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "port": 2222,
                "username": "seeduser",
                "auth_method": "password",
                "password": "hunter2",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_password"] is True
        assert "password" not in body
        assert body["credentials_need_reentry"] is False

        # Re-fetch: still no plaintext anywhere in the response.
        resp = client.get("/api/settings/host")
        assert "hunter2" not in resp.text


def test_put_host_without_new_password_keeps_previous_one(isolated_config):
    with TestClient(app) as client:
        client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "password",
                "password": "hunter2",
            },
        )
        # Update the address only; omit password entirely.
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example2.invalid",
                "username": "seeduser",
                "auth_method": "password",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["has_password"] is True
        assert resp.json()["address"] == "example2.invalid"


def test_put_host_key_auth_requires_key_path(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
            },
        )
        assert resp.status_code == 422


# --- migration 014: paste-a-key (DESIGN.md §8) ---------------------------------------------


def test_put_host_key_auth_accepts_a_pasted_key_without_key_path(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "ssh_key": _generate_test_key_pem(),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ssh_key"] is True
        assert body["active_key_source"] == "pasted"
        assert "ssh_key" not in body
        assert "PRIVATE KEY" not in resp.text


def test_put_host_never_returns_the_pasted_key_plaintext(isolated_config):
    key_pem = _generate_test_key_pem()
    with TestClient(app) as client:
        client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "ssh_key": key_pem,
            },
        )
        # Re-fetch: still no plaintext anywhere in the response, same guarantee as the
        # password test above.
        resp = client.get("/api/settings/host")
        assert key_pem not in resp.text
        assert "BEGIN OPENSSH PRIVATE KEY" not in resp.text


def test_put_host_rejects_an_unparseable_pasted_key(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "ssh_key": "not a real private key",
            },
        )
        assert resp.status_code == 422
        assert "does not parse" in resp.text


def test_put_host_rejects_a_passphrase_protected_pasted_key(isolated_config):
    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    encrypted_pem = key.export_private_key(format_name="pkcs8-pem", passphrase="hunter2").decode(
        "ascii"
    )
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "ssh_key": encrypted_pem,
            },
        )
        assert resp.status_code == 422
        assert "passphrase" in resp.text.lower()


def test_put_host_without_new_key_keeps_previous_one(isolated_config):
    key_pem = _generate_test_key_pem()
    with TestClient(app) as client:
        client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "ssh_key": key_pem,
            },
        )
        # Update the address only; omit ssh_key entirely -- "unchanged" must not mean
        # "cleared," same as the password case.
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example2.invalid",
                "username": "seeduser",
                "auth_method": "key",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ssh_key"] is True
        assert body["active_key_source"] == "pasted"
        assert body["address"] == "example2.invalid"


def test_active_key_source_is_path_when_only_key_path_is_set(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "key_path": "/config/keys/id_ed25519",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ssh_key"] is False
        assert body["active_key_source"] == "path"
        assert body["key_path"] == "/config/keys/id_ed25519"


def test_active_key_source_pasted_key_wins_when_both_are_set(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "key",
                "key_path": "/config/keys/id_ed25519",
                "ssh_key": _generate_test_key_pem(),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ssh_key"] is True
        # key_path is still stored and returned (it keeps working for anyone relying on it --
        # migration 014 is additive) but is not the one actually in use.
        assert body["key_path"] == "/config/keys/id_ed25519"
        assert body["active_key_source"] == "pasted"


def test_test_connection_without_any_host_returns_404(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/host/test")
        assert resp.status_code == 404


def test_test_connection_to_unreachable_host_reports_error_class(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/host/test",
            json={
                "address": "127.0.0.1",
                "port": 1,  # nothing listens here
                "username": "nobody",
                "auth_method": "password",
                "password": "x",
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] is not None


def test_queue_crud(isolated_config):
    with TestClient(app) as client:
        # Creating a queue before a host exists is rejected.
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": "/downloads/tv"},
        )
        assert resp.status_code == 409

        client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.invalid",
                "username": "seeduser",
                "auth_method": "password",
                "password": "hunter2",
            },
        )

        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": "/downloads/tv"},
        )
        assert resp.status_code == 201
        queue = resp.json()
        assert queue["name"] == "TV"
        assert queue["sync_mode"] == "copy"

        resp = client.get("/api/settings/queues")
        assert len(resp.json()) == 1

        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": "TV Shows",
                "remote_path": "/data/tv",
                "local_path": "/downloads/tv",
                "enabled": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "TV Shows"
        assert resp.json()["enabled"] is False

        resp = client.delete(f"/api/settings/queues/{queue['id']}")
        assert resp.status_code == 204
        assert client.get("/api/settings/queues").json() == []


def test_unimplemented_sync_mode_sync_is_rejected(isolated_config):
    """A mode that silently behaves as `copy` is worse than one that isn't offered.

    `sync_mode` accepts three values in the schema so `sync` (unscheduled, DESIGN.md §7) can
    drop in later without a migration, but it isn't implemented and must never be silently
    accepted as if it were. `move` *is* implemented as of phase 5 -- see
    `test_move_sync_mode_is_accepted` below -- so only `sync` is exercised here now.
    """
    body = {
        "name": "q",
        "remote_path": "/remote",
        "local_path": "/tmp/local",
        "enabled": True,
        "sync_mode": "sync",
    }
    with TestClient(app) as client:
        response = client.post("/api/settings/queues", json=body)
    assert response.status_code == 400
    assert "not available" in response.json()["detail"]


# --- Phase 5: `move` mode is implemented; `auto_verify` is forced on for it (DESIGN.md §6) --


def test_move_sync_mode_is_accepted(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client, sync_mode="move")
        assert queue["sync_mode"] == "move"


def test_move_sync_mode_forces_auto_verify_on_even_if_not_requested(isolated_config):
    """DESIGN.md §6: "For a queue in `move` or `sync` mode, `auto_verify` is forced on and
    cannot be turned off in the UI." Enforced server-side (not just in the frontend form) so
    a direct API call can't silently create a move queue that never verifies -- and therefore
    never explains why it never deletes anything.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(client, sync_mode="move", auto_verify=False)
        assert queue["auto_verify"] is True

        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": queue["name"],
                "remote_path": queue["remote_path"],
                "local_path": queue["local_path"],
                "sync_mode": "move",
                "auto_verify": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["auto_verify"] is True


def test_copy_sync_mode_leaves_auto_verify_as_requested(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client, sync_mode="copy", auto_verify=False)
        assert queue["auto_verify"] is False


def test_new_queue_defaults_postprocess_toggles_off(isolated_config):
    """Migration 015 (2026-08-13, `prompts/2026-08-13-postprocess-inherit-or-override.md`):
    every post-processing toggle defaults to inherit (`None`) now, including for a queue
    created without specifying these fields at all -- not `False`, since the AND that made
    `False` and "off" indistinguishable is gone. `PostprocessSettings` itself still defaults
    every flag off, so the *effective* value for a fresh install's queue is still off end to
    end (`core/postprocess.py._effective`, and its own unit test) -- this test only pins down
    what is actually stored.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)
        assert queue["auto_verify"] is None
        assert queue["auto_extract"] is None
        assert queue["auto_move"] is None
        # Migration 012, 2026-08-13: archive cleanup's per-queue half. Shipped site-only
        # originally (migration 010) -- this closes that gap, so it follows the same
        # inherit-by-default shape as its three siblings above for an existing/newly-created
        # queue alike.
        assert queue["auto_delete_archives"] is None


_TOGGLE_FIELDS = ("auto_verify", "auto_extract", "auto_move", "auto_delete_archives")


def test_queue_toggle_omitted_on_update_preserves_existing_override(isolated_config):
    """The API subtlety worth getting right
    (`prompts/2026-08-13-postprocess-inherit-or-override.md`): `null` and "field not sent" are
    different for the four post-processing toggles now that migration 015 makes them
    nullable-for-inherit. A PUT that omits one entirely must leave whatever was already stored
    (override or inherit) untouched -- `api/settings.py._merged_toggle` -- the same class of
    fix `put_postprocess_settings` already made for its own fields via `model_fields_set`.
    Covers all four columns in one pass, since they are separate and it is easy to wire three
    correctly and miss one.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(
            client,
            auto_verify=True,
            auto_extract=True,
            auto_move=True,
            auto_delete_archives=True,
        )
        for field in _TOGGLE_FIELDS:
            assert queue[field] is True, field

        # A PUT that only touches `name` -- the four toggle fields are absent entirely, not
        # sent as `null`.
        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": "renamed",
                "remote_path": queue["remote_path"],
                "local_path": queue["local_path"],
            },
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["name"] == "renamed"
        for field in _TOGGLE_FIELDS:
            assert saved[field] is True, field

        # Persisted, not just echoed back.
        fetched = client.get("/api/settings/queues").json()[0]
        for field in _TOGGLE_FIELDS:
            assert fetched[field] is True, field


def test_queue_toggle_explicit_null_clears_override_to_inherit(isolated_config):
    """The other half of the same fix: sending an explicit `null` for a toggle field DOES
    clear a stored override back to inherit -- unlike simply omitting it
    (`test_queue_toggle_omitted_on_update_preserves_existing_override` above). If
    `_merged_toggle` collapsed "omitted" and "sent null" into the same case, one of these two
    tests would fail.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(
            client,
            auto_verify=True,
            auto_extract=True,
            auto_move=True,
            auto_delete_archives=True,
        )

        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": queue["name"],
                "remote_path": queue["remote_path"],
                "local_path": queue["local_path"],
                "auto_verify": None,
                "auto_extract": None,
                "auto_move": None,
                "auto_delete_archives": None,
            },
        )
        assert resp.status_code == 200
        saved = resp.json()
        for field in _TOGGLE_FIELDS:
            assert saved[field] is None, field

        fetched = client.get("/api/settings/queues").json()[0]
        for field in _TOGGLE_FIELDS:
            assert fetched[field] is None, field


def test_queue_round_trips_auto_delete_archives(isolated_config):
    """The per-queue half of archive cleanup (migration 012) round-trips through create and
    update, the same as `auto_verify`/`auto_extract`/`auto_move`.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(client, auto_extract=True, auto_delete_archives=True)
        assert queue["auto_delete_archives"] is True

        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": queue["name"],
                "remote_path": queue["remote_path"],
                "local_path": queue["local_path"],
                "auto_extract": True,
                "auto_delete_archives": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["auto_delete_archives"] is False

        # Persisted, not just echoed back.
        resp = client.get("/api/settings/queues")
        assert resp.json()[0]["auto_delete_archives"] is False


def test_postprocess_settings_round_trip_and_default_off(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/postprocess")
        assert resp.status_code == 200
        defaults = resp.json()
        assert defaults["verify_enabled"] is False
        assert defaults["extract_enabled"] is False
        assert defaults["move_enabled"] is False
        assert defaults["concurrency"] == 1
        # `_FAILED_` retention (fix, 2026-08-12): a new capability, so off by default even
        # though it's the more conservative choice of the pair -- see docs/decisions.md.
        assert defaults["failed_retention_enabled"] is False
        assert defaults["failed_retention_days"] == 14.0

        resp = client.put(
            "/api/settings/postprocess",
            json={
                "verify_enabled": True,
                "verify_hash_on_disk": True,
                "extract_enabled": True,
                "extract_target_dir": "/config/extracted",
                "extract_passwords": ["hunter2", "letmein"],
                "failed_retention_enabled": True,
                "failed_retention_days": 21,
                "move_enabled": True,
                "concurrency": 3,
            },
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["verify_enabled"] is True
        assert saved["extract_passwords"] == ["hunter2", "letmein"]
        assert saved["failed_retention_enabled"] is True
        assert saved["failed_retention_days"] == 21
        assert saved["concurrency"] == 3

        # Persisted, not just echoed back.
        resp = client.get("/api/settings/postprocess")
        assert resp.json() == saved


def test_postprocess_settings_put_omitting_failed_retention_fields_keeps_them_off(
    isolated_config,
):
    """A PUT body written before this fix existed (this endpoint's own contract: every field
    required except this pair -- see `PostprocessSettingsOut`) must not 422, and must still
    default the new capability off rather than raising or silently guessing on.
    """
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/postprocess",
            json={
                "verify_enabled": False,
                "verify_hash_on_disk": False,
                "extract_enabled": True,
                "extract_target_dir": None,
                "extract_passwords": [],
                "move_enabled": False,
                "concurrency": 1,
            },
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["failed_retention_enabled"] is False
        assert saved["failed_retention_days"] == 14.0


def test_postprocess_settings_put_omitting_a_field_preserves_its_previous_value(
    isolated_config,
):
    """The actual footgun (`prompts/2026-08-13-per-queue-archive-cleanup.md` item 3): once a
    real, non-default value is stored, a later PUT that omits one of the three optional-default
    fields must not silently reset it -- `api/settings.py.put_postprocess_settings` merges over
    the stored settings via `body.model_fields_set` rather than replacing wholesale. Unlike
    `test_postprocess_settings_put_omitting_failed_retention_fields_keeps_them_off` above (whose
    "omitted -> off" assertion happens to coincide with the model default because nothing had
    ever been saved yet), this test proves the merge, not the default.
    """
    with TestClient(app) as client:
        # First, save real, non-default values for all three optional fields.
        resp = client.put(
            "/api/settings/postprocess",
            json={
                "verify_enabled": True,
                "verify_hash_on_disk": False,
                "extract_enabled": True,
                "extract_target_dir": None,
                "extract_passwords": [],
                "failed_retention_enabled": True,
                "failed_retention_days": 45.0,
                "delete_archives_after_extract": True,
                "move_enabled": False,
                "concurrency": 1,
            },
        )
        assert resp.status_code == 200

        # Then PUT a body that omits every one of the three optional fields entirely -- the
        # exact shape a pre-this-fix client (or the frontend, for the two fields it has never
        # had a UI for) sends.
        resp = client.put(
            "/api/settings/postprocess",
            json={
                "verify_enabled": True,
                "verify_hash_on_disk": True,  # also prove a *changed* required field applies
                "extract_enabled": True,
                "extract_target_dir": "/config/extracted",
                "extract_passwords": ["hunter2"],
                "move_enabled": True,
                "concurrency": 2,
            },
        )
        assert resp.status_code == 200
        saved = resp.json()
        # The omitted fields survived, not reset to their model defaults (False / 14.0 / False).
        assert saved["failed_retention_enabled"] is True
        assert saved["failed_retention_days"] == 45.0
        assert saved["delete_archives_after_extract"] is True
        # The fields the second PUT did supply took effect -- this is a merge, not "ignore the
        # whole request."
        assert saved["verify_hash_on_disk"] is True
        assert saved["extract_target_dir"] == "/config/extracted"
        assert saved["extract_passwords"] == ["hunter2"]
        assert saved["move_enabled"] is True
        assert saved["concurrency"] == 2

        # Persisted, not just echoed back.
        resp = client.get("/api/settings/postprocess")
        assert resp.json() == saved


def test_retention_settings_put_omitting_a_field_preserves_its_previous_value(isolated_config):
    """`RetentionSettingsIn` is a worse instance of the same shape: *both* fields default, so
    the whole body could previously be omitted and still 200, silently turning off local-data
    retention. Found auditing other `*Settings` endpoints for the postprocess fix above; same
    merge fix applied here (`api/settings.py.put_retention_settings`).
    """
    with TestClient(app) as client:
        resp = client.put("/api/settings/retention", json={"enabled": True, "retention_days": 45.0})
        assert resp.status_code == 200

        # Omit `retention_days` entirely -- only `enabled` is in this request.
        resp = client.put("/api/settings/retention", json={"enabled": True})
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["enabled"] is True
        assert saved["retention_days"] == 45.0  # preserved, not reset to the 30.0 default

        resp = client.get("/api/settings/retention")
        assert resp.json() == saved


def test_orphan_temp_cleanup_settings_default_off_and_round_trip(isolated_config):
    """2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md, `core/local_delete.py.
    OrphanTempCleanupSettings`) -- same shape as retention above: default off, and `PUT` merges
    over the previously-stored value for any field genuinely absent from the request body.
    """
    with TestClient(app) as client:
        resp = client.get("/api/settings/orphan-temp-cleanup")
        assert resp.status_code == 200
        defaults = resp.json()
        assert defaults["enabled"] is False
        assert defaults["max_age_days"] == 2.0

        resp = client.put(
            "/api/settings/orphan-temp-cleanup", json={"enabled": True, "max_age_days": 5.0}
        )
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["enabled"] is True
        assert saved["max_age_days"] == 5.0

        # Omit `max_age_days` entirely -- only `enabled` is in this request.
        resp = client.put("/api/settings/orphan-temp-cleanup", json={"enabled": False})
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["enabled"] is False
        assert saved["max_age_days"] == 5.0  # preserved, not reset to the 2.0 default

        resp = client.get("/api/settings/orphan-temp-cleanup")
        assert resp.json() == saved


# --- The settle gate (prompts/open-issues.md #2, `core/settle.py`) -----------------------


def test_settle_settings_default_on_and_round_trip(isolated_config):
    """prompts/2026-08-12-settle-gate-followups.md item 3: unlike every other capability in
    this file, this one now defaults **on** -- the third reasoned exception to the "ships off"
    rule (docs/decisions.md). `required_scans`/`min_age_s` are read-only, always the module's
    own constants, never a stored value.
    """
    from lftpweb.core.settle import REQUIRED_SETTLE_SCANS, SETTLE_MIN_AGE_S

    with TestClient(app) as client:
        resp = client.get("/api/settings/settle")
        assert resp.status_code == 200
        defaults = resp.json()
        assert defaults["enabled"] is True
        assert defaults["required_scans"] == REQUIRED_SETTLE_SCANS
        assert defaults["min_age_s"] == SETTLE_MIN_AGE_S

        resp = client.put("/api/settings/settle", json={"enabled": False})
        assert resp.status_code == 200
        saved = resp.json()
        assert saved["enabled"] is False
        # Still surfaced, and still the constants, even with the toggle off.
        assert saved["required_scans"] == REQUIRED_SETTLE_SCANS
        assert saved["min_age_s"] == SETTLE_MIN_AGE_S

        # Persisted, not just echoed back.
        resp = client.get("/api/settings/settle")
        assert resp.json() == saved

        resp = client.put("/api/settings/settle", json={"enabled": True})
        assert resp.json()["enabled"] is True


# --- Phase 4: queues carry auto-queue toggles, default off (DESIGN.md §4.7) ---------------


def _make_host_and_queue(client, **queue_overrides):
    client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "example.invalid",
            "username": "seeduser",
            "auth_method": "password",
            "password": "hunter2",
        },
    )
    body = {"name": "TV", "remote_path": "/data/tv", "local_path": "/downloads/tv"}
    body.update(queue_overrides)
    resp = client.post("/api/settings/queues", json=body)
    assert resp.status_code == 201
    return resp.json()


def test_new_queue_defaults_auto_queue_off(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)
        assert queue["auto_queue_enabled"] is False
        assert queue["auto_queue_patterns_only"] is False


def test_auto_queue_toggle_is_an_explicit_opt_in(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)
        resp = client.put(
            f"/api/settings/queues/{queue['id']}",
            json={
                "name": queue["name"],
                "remote_path": queue["remote_path"],
                "local_path": queue["local_path"],
                "auto_queue_enabled": True,
                "auto_queue_patterns_only": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["auto_queue_enabled"] is True
        assert resp.json()["auto_queue_patterns_only"] is True


# --- Phase 4: pattern CRUD, global and per-queue (DESIGN.md §3.1 `pattern`, §4.7) ---------


def test_pattern_crud_and_global_vs_queue_scope(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)

        resp = client.post(
            "/api/settings/patterns",
            json={"queue_id": queue["id"], "kind": "file_exclude", "expr": "*.nfo"},
        )
        assert resp.status_code == 201
        pattern = resp.json()
        assert pattern["kind"] == "file_exclude"

        resp = client.post(
            "/api/settings/patterns", json={"queue_id": None, "kind": "skip", "expr": "*SAMPLE*"}
        )
        assert resp.status_code == 201
        global_pattern = resp.json()
        assert global_pattern["queue_id"] is None

        # Scoped listing returns both the queue's own and the global one.
        resp = client.get(f"/api/settings/patterns?queue_id={queue['id']}")
        assert {p["id"] for p in resp.json()} == {pattern["id"], global_pattern["id"]}

        resp = client.put(
            f"/api/settings/patterns/{pattern['id']}",
            json={
                "queue_id": queue["id"],
                "kind": "file_exclude",
                "expr": "*.sfv",
                "enabled": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["expr"] == "*.sfv"
        assert resp.json()["enabled"] is False

        resp = client.delete(f"/api/settings/patterns/{pattern['id']}")
        assert resp.status_code == 204
        resp = client.get(f"/api/settings/patterns?queue_id={queue['id']}")
        assert {p["id"] for p in resp.json()} == {global_pattern["id"]}


def test_pattern_not_found_returns_404(isolated_config):
    with TestClient(app) as client:
        assert (
            client.put(
                "/api/settings/patterns/9999", json={"kind": "select", "expr": "*"}
            ).status_code
            == 404
        )
        assert client.delete("/api/settings/patterns/9999").status_code == 404


# --- Phase 4: the live pattern preview (DESIGN.md §4.7, §9.2) -----------------------------


def test_pattern_preview_shows_selected_skipped_and_excluded_without_saving(isolated_config):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)

        # Seed the engine's in-memory model directly -- what a real scan would populate --
        # so the preview can run without a live seedbox connection. The model holds
        # `core/itemview.py` projections of persisted `item` rows, so these are the same
        # dicts `_project` would have read back.
        app.state.engine.models[queue["id"]] = {
            "Wanted.Release": _view("Wanted.Release", is_dir=True, remote_size=1500),
            "Wanted.Release/movie.mkv": _view(
                "Wanted.Release/movie.mkv", is_dir=False, remote_size=1000, remote_mtime=1.0
            ),
            "Wanted.Release/notes.nfo": _view(
                "Wanted.Release/notes.nfo", is_dir=False, remote_size=5, remote_mtime=1.0
            ),
            "Unwanted.Sample": _view("Unwanted.Sample", is_dir=True, remote_size=10),
        }

        resp = client.post(
            f"/api/settings/queues/{queue['id']}/pattern-preview",
            json={
                "patterns": [
                    {"kind": "select", "expr": "Wanted*"},
                    {"kind": "skip", "expr": "*Sample*"},
                    {"kind": "file_exclude", "expr": "*.nfo"},
                ],
                "patterns_only": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        by_path = {item["rel_path"]: item["matched"] for item in body["items"]}
        assert by_path["Wanted.Release"] is True
        assert by_path["Unwanted.Sample"] is False  # skip beats select

        assert body["sample_item"] == "Wanted.Release"
        excluded_by_path = {f["rel_path"]: f["excluded"] for f in body["sample_files"]}
        assert excluded_by_path["Wanted.Release/movie.mkv"] is False
        assert excluded_by_path["Wanted.Release/notes.nfo"] is True


def test_pattern_preview_unknown_queue_is_404(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/queues/9999/pattern-preview", json={"patterns": []})
        assert resp.status_code == 404


# --- Phase 4: the mount-gate status read (DESIGN.md §7.3) --------------------------------


def test_autoqueue_status_reports_mount_not_ok_before_any_scan(isolated_config, tmp_path):
    with TestClient(app) as client:
        queue = _make_host_and_queue(client, local_path=str(tmp_path / "never-scanned"))
        resp = client.get(f"/api/settings/queues/{queue['id']}/autoqueue-status")
        assert resp.status_code == 200
        assert resp.json()["mount_ok"] is False


def test_autoqueue_status_unknown_queue_is_404(isolated_config):
    with TestClient(app) as client:
        assert client.get("/api/settings/queues/9999/autoqueue-status").status_code == 404
