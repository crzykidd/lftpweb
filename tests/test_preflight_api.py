"""`GET /api/queue/preflight` (docs/transfers-redesign-spec.md §4, prefigured; this task's own
handoff prompt, prompts/done/2026-08-20-preflight-box.md) -- the live `source_configured` check
(no bound, enabled *arr instance anywhere -> hide the whole box) and the "configured but nothing
projected yet" shape. The projection itself (attribution, flap tolerance, no-duplicate-at-
handover) is `tests/test_arr_preflight.py`'s job, exercised directly against
`ArrSyncScheduler`; this file only covers what only the live app can answer -- whether the
endpoint's own config-aware gate agrees with the database. Same `TestClient` + `isolated_config`
idiom as `tests/test_settings_queues_arr.py`.
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


def _create_instance(client: TestClient, **overrides) -> int:
    body = {
        "name": "Sonarr",
        "kind": "sonarr",
        "base_url": "http://sonarr.example.invalid",
        "api_key": "key",
        "enabled": True,
        "notify_on_complete": False,
    }
    body.update(overrides)
    resp = client.post("/api/settings/arr", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_queue(client: TestClient, **overrides) -> int:
    body = {"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()}
    body.update(overrides)
    resp = client.post("/api/settings/queues", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


def test_no_instance_at_all_hides_the_box(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/queue/preflight")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"source_configured": False, "rows": []}


def test_disabled_instance_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=False)
        _create_queue(client, arr_instance_id=instance_id)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {"source_configured": False, "rows": []}


def test_enabled_instance_with_no_bound_queue_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        _create_instance(client, enabled=True)  # nothing bound to it

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {"source_configured": False, "rows": []}


def test_enabled_instance_with_disabled_queue_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=True)
        _create_queue(client, arr_instance_id=instance_id, enabled=False)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {"source_configured": False, "rows": []}


def test_enabled_bound_instance_with_nothing_projected_yet_shows_configured(isolated_config):
    """`source_configured=True` with an empty `rows` list -- the "Nothing in preflight" case,
    distinct from "hide the box entirely." The scheduler's own cache is empty here (it has never
    polled in this test), which is exactly the state a freshly-configured install is in before
    its first ~60s poll lands.
    """
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=True)
        _create_queue(client, arr_instance_id=instance_id, enabled=True)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {"source_configured": True, "rows": []}
