"""api/settings_queues.py's *arr extension (migration 018, docs/arr-integration-spec.md "API
surface"): `arr_instance_id`/`arr_delete_completed`/`arr_visible_path` on the queues endpoint.
Same `TestClient` + `isolated_config` idiom as `tests/test_settings_api.py::test_queue_crud`.
"""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from lftpweb.main import app


def _put_host(client: TestClient) -> None:
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


def _create_instance(client: TestClient) -> int:
    resp = client.post(
        "/api/settings/arr",
        json={
            "name": "Sonarr",
            "kind": "sonarr",
            "base_url": "http://sonarr.example.invalid",
            "api_key": "key",
            "enabled": True,
            "notify_on_complete": False,
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _queue_body(**overrides) -> dict:
    # A real, readable directory (mid-run scope addition to
    # `prompts/done/2026-08-16-path-browse-dialog.md`) -- `local_path` is now hard-validated
    # at save time; `remote_path` stays a fake literal since its own check is best-effort and
    # this file's host ("example.invalid") is unreachable.
    body = {"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()}
    body.update(overrides)
    return body


def test_new_queue_has_no_arr_integration_by_default(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body())
        assert resp.status_code == 201
        body = resp.json()
        assert body["arr_instance_id"] is None
        assert body["arr_delete_completed"] is False
        assert body["arr_visible_path"] is None


def test_create_queue_rejects_unknown_arr_instance_id(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(arr_instance_id=999))
        assert resp.status_code == 400
        assert "does not exist" in resp.text


def test_create_queue_rejects_delete_completed_without_instance(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(arr_delete_completed=True))
        assert resp.status_code == 400
        assert "arr_instance_id" in resp.text


def test_create_queue_accepts_a_bound_instance_and_delete_completed(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client)
        resp = client.post(
            "/api/settings/queues",
            json=_queue_body(
                arr_instance_id=instance_id,
                arr_delete_completed=True,
                arr_visible_path="/data/torrents/tv",
            ),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["arr_instance_id"] == instance_id
        assert body["arr_delete_completed"] is True
        assert body["arr_visible_path"] == "/data/torrents/tv"


def test_update_queue_rejects_unknown_arr_instance_id(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        queue_id = client.post("/api/settings/queues", json=_queue_body()).json()["id"]
        resp = client.put(f"/api/settings/queues/{queue_id}", json=_queue_body(arr_instance_id=999))
        assert resp.status_code == 400


def test_update_queue_rejects_delete_completed_without_instance(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        queue_id = client.post("/api/settings/queues", json=_queue_body()).json()["id"]
        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json=_queue_body(arr_delete_completed=True),
        )
        assert resp.status_code == 400


def test_update_queue_can_unbind_the_instance(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client)
        queue_id = client.post(
            "/api/settings/queues", json=_queue_body(arr_instance_id=instance_id)
        ).json()["id"]

        resp = client.put(f"/api/settings/queues/{queue_id}", json=_queue_body())
        assert resp.status_code == 200
        body = resp.json()
        assert body["arr_instance_id"] is None
        assert body["arr_delete_completed"] is False


def test_deleting_the_instance_unbinds_the_queue(isolated_config):
    """migration 018's `ON DELETE SET NULL` -- deleting the instance leaves the queue with no
    integration rather than orphaning it or failing the delete.
    """
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client)
        queue_id = client.post(
            "/api/settings/queues", json=_queue_body(arr_instance_id=instance_id)
        ).json()["id"]

        resp = client.delete(f"/api/settings/arr/{instance_id}")
        assert resp.status_code == 204

        resp = client.get("/api/settings/queues")
        queue = next(q for q in resp.json() if q["id"] == queue_id)
        assert queue["arr_instance_id"] is None
