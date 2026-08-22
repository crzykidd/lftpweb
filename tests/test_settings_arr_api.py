"""api/settings_arr.py -- Sonarr/Radarr instance CRUD + the Test-connection round trip
(docs/arr-integration-spec.md "API surface"). Same `TestClient` + `isolated_config` idiom as
`tests/test_settings_api.py`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from lftpweb.main import app


def _instance_body(**overrides):
    body = {
        "name": "Sonarr",
        "kind": "sonarr",
        "base_url": "http://sonarr.example.invalid",
        "api_key": "supersecretkey",
        "enabled": True,
        "notify_on_complete": False,
    }
    body.update(overrides)
    return body


def test_create_list_update_delete_round_trip(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/arr", json=_instance_body())
        assert resp.status_code == 201
        created = resp.json()
        assert created["has_api_key"] is True
        assert "api_key" not in created
        instance_id = created["id"]

        resp = client.get("/api/settings/arr")
        assert resp.status_code == 200
        assert [i["id"] for i in resp.json()] == [instance_id]

        resp = client.put(
            f"/api/settings/arr/{instance_id}",
            json=_instance_body(name="Sonarr 4K", api_key=None, enabled=False),
        )
        assert resp.status_code == 200
        updated = resp.json()
        assert updated["name"] == "Sonarr 4K"
        assert updated["enabled"] is False
        assert updated["has_api_key"] is True  # kept, since api_key was omitted

        resp = client.delete(f"/api/settings/arr/{instance_id}")
        assert resp.status_code == 204
        resp = client.get("/api/settings/arr")
        assert resp.json() == []


def test_create_requires_api_key(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/arr", json=_instance_body(api_key=None))
        assert resp.status_code == 422


def test_update_requires_existing_instance(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/arr/999", json=_instance_body())
        assert resp.status_code == 404


def test_delete_requires_existing_instance(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/settings/arr/999")
        assert resp.status_code == 404


def test_api_key_encrypted_at_rest_and_never_echoed(isolated_config):
    plaintext = "hunter2-arr-api-key"
    with TestClient(app) as client:
        resp = client.post("/api/settings/arr", json=_instance_body(api_key=plaintext))
        assert resp.status_code == 201
        assert plaintext not in resp.text

        resp = client.get("/api/settings/arr")
        assert plaintext not in resp.text

    # Byte-search the actual database file, not just the HTTP responses -- the stored value
    # must not be the plaintext (or contain it), the same discipline
    # `test_backup.py::test_backup_never_contains_the_encryption_secret` uses for the install
    # secret.
    db_path = Path(isolated_config) / "lftpweb.db"
    raw = sqlite3.connect(str(db_path))
    try:
        (stored,) = raw.execute("SELECT api_key_enc FROM arr_instance").fetchone()
    finally:
        raw.close()
    assert stored != plaintext
    assert plaintext not in stored


def test_test_endpoint_reports_version_on_success(isolated_config, fake_arr_server):
    fake_arr_server.state.version = "4.0.10.2544"
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/arr",
            json=_instance_body(
                base_url=fake_arr_server.base_url, api_key=fake_arr_server.state.api_key
            ),
        )
        instance_id = resp.json()["id"]

        resp = client.post(f"/api/settings/arr/{instance_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["version"] == "4.0.10.2544"


def test_test_endpoint_reports_failure_for_unreachable_instance(isolated_config, fake_arr_server):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/arr",
            json=_instance_body(base_url=fake_arr_server.base_url, api_key="wrong-key"),
        )
        instance_id = resp.json()["id"]

        resp = client.post(f"/api/settings/arr/{instance_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] == "ArrClientError"


def test_test_endpoint_requires_existing_instance(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/arr/999/test")
        assert resp.status_code == 404


# --- Poll cadence (2026-08-21, issue #16, prompts/done/2026-08-21-arr-poll-cadence.md) ---------


def test_poll_interval_default_and_round_trip(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/arr/poll-interval")
        assert resp.status_code == 200
        assert resp.json() == {"poll_interval_s": 10.0}  # down from 60.0, issue #16

        resp = client.put("/api/settings/arr/poll-interval", json={"poll_interval_s": 30.0})
        assert resp.status_code == 200
        assert resp.json() == {"poll_interval_s": 30.0}

        resp = client.get("/api/settings/arr/poll-interval")
        assert resp.json() == {"poll_interval_s": 30.0}


def test_poll_interval_rejects_below_the_floor(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/arr/poll-interval", json={"poll_interval_s": 1.0})
        assert resp.status_code == 422

        # Rejected -- the previously stored default must be untouched.
        resp = client.get("/api/settings/arr/poll-interval")
        assert resp.json() == {"poll_interval_s": 10.0}


def test_poll_interval_rejects_above_the_ceiling(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/arr/poll-interval", json={"poll_interval_s": 999999.0})
        assert resp.status_code == 422

        resp = client.get("/api/settings/arr/poll-interval")
        assert resp.json() == {"poll_interval_s": 10.0}


def test_poll_interval_accepts_the_floor_and_ceiling_boundaries(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/arr/poll-interval", json={"poll_interval_s": 5.0})
        assert resp.status_code == 200
        assert resp.json() == {"poll_interval_s": 5.0}

        resp = client.put("/api/settings/arr/poll-interval", json={"poll_interval_s": 3600.0})
        assert resp.status_code == 200
        assert resp.json() == {"poll_interval_s": 3600.0}
