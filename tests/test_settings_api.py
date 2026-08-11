from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.main import app


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
