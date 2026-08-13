from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.core.itemview import ItemView, item_view
from lftpweb.main import app


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
            "remote_size": remote_size,
            "local_size": None,
            "remote_mtime": remote_mtime,
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
    """DESIGN.md §6 / this phase's non-negotiable: every post-processing step defaults off,
    including for a queue created without specifying these fields at all.
    """
    with TestClient(app) as client:
        queue = _make_host_and_queue(client)
        assert queue["auto_verify"] is False
        assert queue["auto_extract"] is False
        assert queue["auto_move"] is False


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
