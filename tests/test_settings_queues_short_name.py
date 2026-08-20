"""`path_queue.short_name` (migration 024, docs/transfers-redesign-spec.md §3.6, phase 1 stage
3, prompts/done/2026-08-19-queue-short-display-name.md) -- a short display label for the
compact per-row queue tag stage 4 renders once Transfers drops its per-queue grouping. Same
`TestClient` + `isolated_config` idiom as `tests/test_settings_queues_arr.py`.
"""

from __future__ import annotations

import tempfile

from fastapi.testclient import TestClient

from lftpweb.api.settings_queues import MAX_SHORT_NAME_LEN, resolve_queue_display_name
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


def _queue_body(**overrides) -> dict:
    # A real, readable directory -- local_path is hard-validated at save time (see
    # tests/test_settings_queues_path_validation.py); remote_path stays a fake literal since
    # its own check is best-effort and this file's host ("example.invalid") is unreachable.
    body = {"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()}
    body.update(overrides)
    return body


def test_new_queue_has_no_short_name_by_default(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body())
        assert resp.status_code == 201, resp.text
        assert resp.json()["short_name"] is None


def test_create_queue_round_trips_a_short_name(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name="MOV"))
        assert resp.status_code == 201, resp.text
        assert resp.json()["short_name"] == "MOV"


def test_create_queue_trims_whitespace_around_short_name(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name="  MOV  "))
        assert resp.status_code == 201, resp.text
        assert resp.json()["short_name"] == "MOV"


def test_create_queue_normalizes_whitespace_only_short_name_to_null(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name="   "))
        assert resp.status_code == 201, resp.text
        assert resp.json()["short_name"] is None


def test_create_queue_rejects_an_over_length_short_name(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        too_long = "X" * (MAX_SHORT_NAME_LEN + 1)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name=too_long))
        assert resp.status_code == 400
        assert "short_name" in resp.text


def test_create_queue_accepts_a_short_name_at_exactly_the_cap(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        exactly_at_cap = "X" * MAX_SHORT_NAME_LEN
        resp = client.post("/api/settings/queues", json=_queue_body(short_name=exactly_at_cap))
        assert resp.status_code == 201, resp.text
        assert resp.json()["short_name"] == exactly_at_cap


def test_create_queue_rejects_an_over_length_short_name_even_after_trimming(isolated_config):
    # The length cap applies to the *trimmed* value, not the raw one -- padding an
    # already-too-long name with extra whitespace must not somehow make it pass, and trimming
    # must not be skipped just because the raw string is long enough to reject outright either.
    too_long_untrimmed = f"  {'X' * (MAX_SHORT_NAME_LEN + 1)}  "
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name=too_long_untrimmed))
        assert resp.status_code == 400
        assert "short_name" in resp.text


def test_two_queues_may_share_the_same_short_name(isolated_config):
    # Display hint, not an identifier -- no uniqueness constraint (docs/decisions.md).
    with TestClient(app) as client:
        _put_host(client)
        resp1 = client.post(
            "/api/settings/queues", json=_queue_body(name="4K Movies", short_name="MOV")
        )
        assert resp1.status_code == 201, resp1.text
        resp2 = client.post(
            "/api/settings/queues", json=_queue_body(name="1080p Movies", short_name="MOV")
        )
        assert resp2.status_code == 201, resp2.text
        assert resp1.json()["short_name"] == resp2.json()["short_name"] == "MOV"


def test_update_queue_can_set_a_short_name(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body())
        queue_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json=_queue_body(short_name="TV4K"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["short_name"] == "TV4K"


def test_update_queue_can_clear_a_short_name_back_to_null(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body(short_name="TV4K"))
        queue_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json=_queue_body(short_name=None),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["short_name"] is None


def test_update_queue_rejects_an_over_length_short_name(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post("/api/settings/queues", json=_queue_body())
        queue_id = resp.json()["id"]

        too_long = "X" * (MAX_SHORT_NAME_LEN + 1)
        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json=_queue_body(short_name=too_long),
        )
        assert resp.status_code == 400
        assert "short_name" in resp.text


# --- resolve_queue_display_name -- the "what do we display for this queue" fallback -----------


def test_resolve_queue_display_name_falls_back_to_name_when_short_name_is_none():
    assert resolve_queue_display_name(None, "DC-Movies") == "DC-Movies"


def test_resolve_queue_display_name_prefers_short_name_when_set():
    assert resolve_queue_display_name("MOV", "DC-Movies") == "MOV"
