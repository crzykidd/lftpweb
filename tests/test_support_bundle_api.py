"""`POST /api/support-bundle` (`api/support_bundle.py`, `core/supportbundle.py`) --
prompts/done/2026-08-17-support-bundle.md. Same `TestClient` + `isolated_config` idiom as
`tests/test_settings_arr_api.py`; the *arr fetch tests reuse `fake_arr_server`
(`tests/fake_arr.py`, auto-discovered via `conftest.py`) the same way `tests/test_arrsync.py`
does.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import zipfile

from fastapi.testclient import TestClient

from lftpweb.main import app

SEEDBOX_PASSWORD = "hunter2-seedbox-password-do-not-leak"  # noqa: S105 - test fixture secret
ARR_API_KEY = "arr-api-key-do-not-leak-either"  # noqa: S105 - test fixture secret


def _put_host(client: TestClient) -> None:
    resp = client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "example.invalid",
            "username": "seeduser",
            "auth_method": "password",
            "password": SEEDBOX_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text


def _make_queue(client: TestClient, *, name: str = "TV") -> int:
    resp = client.post(
        "/api/settings/queues",
        json={"name": name, "remote_path": "/remote", "local_path": tempfile.mkdtemp()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_arr_instance(client: TestClient, *, base_url: str, api_key: str = ARR_API_KEY) -> int:
    resp = client.post(
        "/api/settings/arr",
        json={
            "name": "Sonarr",
            "kind": "sonarr",
            "base_url": base_url,
            "api_key": api_key,
            "enabled": True,
            "notify_on_complete": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _zip_from(resp) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(resp.content))


# --- Selection: exactly the requested parts, always plus lftpweb's own logs ----------------


def test_bundle_always_includes_logs_regardless_of_selection(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/support-bundle",
            json={
                "include_environment": False,
                "include_settings": False,
                "include_events": False,
                "include_jobs": False,
                "arr_instance_ids": [],
            },
        )
        zf = _zip_from(resp)
        names = zf.namelist()
        assert any(n.startswith("logs/") for n in names)
        assert not any(n.startswith("bundle/") for n in names)


def test_bundle_contains_exactly_the_selected_parts(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        _make_queue(client)

        resp = client.post(
            "/api/support-bundle",
            json={
                "include_environment": True,
                "include_settings": False,
                "include_events": False,
                "include_jobs": False,
                "arr_instance_ids": [],
            },
        )
        names = _zip_from(resp).namelist()
        assert "bundle/environment.json" in names
        assert "bundle/settings.json" not in names
        assert "bundle/events.ndjson" not in names
        assert "bundle/jobs.ndjson" not in names

        resp = client.post(
            "/api/support-bundle",
            json={
                "include_environment": False,
                "include_settings": True,
                "include_events": True,
                "include_jobs": True,
                "arr_instance_ids": [],
            },
        )
        names = _zip_from(resp).namelist()
        assert "bundle/environment.json" not in names
        assert {"bundle/settings.json", "bundle/events.ndjson", "bundle/jobs.ndjson"} <= set(names)


def test_bundle_filename_and_content_disposition(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/support-bundle", json={})
        assert resp.status_code == 200
        disposition = resp.headers["content-disposition"]
        match = re.search(r'filename="([^"]+)"', disposition)
        assert match is not None
        filename = match.group(1)
        assert re.match(r"^lftpweb-support-.+-\d{8}T\d{6}Z\.zip$", filename)


def test_environment_snapshot_shape(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        queue_id = _make_queue(client)

        resp = client.post(
            "/api/support-bundle",
            json={"include_environment": True, "include_settings": False},
        )
        zf = _zip_from(resp)
        env = json.loads(zf.read("bundle/environment.json"))
        assert "version" in env
        assert "migration_level" in env and env["migration_level"] is not None
        assert "health" in env and env["health"]["status"] in ("ok", "degraded")
        assert "lftp_version" in env
        assert "python_version" in env
        usage_by_queue = {u["queue"]: u for u in env["queue_disk_usage"]}
        assert "TV" in usage_by_queue
        assert queue_id  # the queue really was created


def test_settings_dump_never_contains_secrets(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        _make_queue(client)
        _create_arr_instance(client, base_url="http://sonarr.example.invalid")

        resp = client.post(
            "/api/support-bundle",
            json={"include_environment": True, "include_settings": True},
        )
        # Coarse, defense-in-depth check over every byte of the zip response...
        assert SEEDBOX_PASSWORD.encode() not in resp.content
        assert ARR_API_KEY.encode() not in resp.content

        # ...and a precise one over the decompressed settings dump specifically, the part the
        # plan calls out by name.
        zf = _zip_from(resp)
        settings_text = zf.read("bundle/settings.json").decode()
        assert SEEDBOX_PASSWORD not in settings_text
        assert ARR_API_KEY not in settings_text

        settings_json = json.loads(settings_text)
        assert settings_json["host"]["has_password"] is True
        assert "password" not in settings_json["host"]
        assert settings_json["arr_instances"][0]["has_api_key"] is True
        assert "api_key" not in settings_json["arr_instances"][0]
        assert settings_json["auth_mode"] == "none"


# --- *arr log fetch: success and per-instance failure isolation ----------------------------


async def test_arr_instance_logs_are_fetched_and_bundled(isolated_config, fake_arr_server):
    fake_arr_server.state.api_key = ARR_API_KEY
    fake_arr_server.state.log_files = {"sonarr.txt": b"a real sonarr log line\n"}
    with TestClient(app) as client:
        instance_id = _create_arr_instance(client, base_url=fake_arr_server.base_url)

        resp = client.post(
            "/api/support-bundle",
            json={
                "include_environment": False,
                "include_settings": False,
                "include_events": False,
                "include_jobs": False,
                "arr_instance_ids": [instance_id],
            },
        )
        zf = _zip_from(resp)
        names = zf.namelist()
        arr_files = [n for n in names if n.startswith("arr-Sonarr/")]
        assert "arr-Sonarr/sonarr.txt" in arr_files
        assert zf.read("arr-Sonarr/sonarr.txt") == b"a real sonarr log line\n"


async def test_arr_instance_fetch_failure_is_a_marker_not_a_500(isolated_config, fake_arr_server):
    fake_arr_server.state.fail_all = True
    with TestClient(app) as client:
        instance_id = _create_arr_instance(client, base_url=fake_arr_server.base_url)

        resp = client.post(
            "/api/support-bundle",
            json={
                "include_environment": False,
                "include_settings": False,
                "include_events": False,
                "include_jobs": False,
                "arr_instance_ids": [instance_id],
            },
        )
        assert resp.status_code == 200
        zf = _zip_from(resp)
        assert "arr-Sonarr/FETCH-FAILED.txt" in zf.namelist()


# --- The audit trail (the plan's own "when was this bundle made and what's in it") ---------


def test_bundle_creation_writes_one_audit_event(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/support-bundle", json={"include_environment": True})
        assert resp.status_code == 200

        events = client.get(
            "/api/history/events", params={"kind": "support_bundle_created"}
        ).json()["events"]
        assert len(events) == 1
        assert "environment" in events[0]["message"]
