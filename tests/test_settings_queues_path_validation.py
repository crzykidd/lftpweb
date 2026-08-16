"""Save-time path validation on `POST`/`PUT /api/settings/queues` -- a mid-run scope addition
to `prompts/done/2026-08-16-path-browse-dialog.md`, prompted by a real incident: a mistyped
`local_path` saved silently and only ever surfaced as a WARNING log line the next time
`core/autoqueue.py.on_scan`'s mount gate refused to act, discovered hours later.

`local_path`/`staging_path` are **hard** (block the save, `api/settings_queues.py.
_reject_invalid_local_paths` / `core/browse.py.local_directory_error`) -- the container's own
filesystem is always reachable from this process. `remote_path` is **best-effort**
(`_reject_invalid_remote_path` / `core/browse.py.remote_directory_error`) -- an unconfigured,
unreachable, or `credentials_need_reentry` host must never block a settings save, so only a
live, reachable seedbox that clearly reports "no such directory" blocks anything; everything
else (no host, unreachable, can't decrypt) is a silent allow, exercised here against the fake
seedbox (`docker-compose.test.yml`, same convention as `tests/test_remote.py`) for the one case
that actually needs a live connection to prove.
"""

from __future__ import annotations

import socket
import tempfile

import pytest
from fastapi.testclient import TestClient

from lftpweb.main import app

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _put_fake_host(client: TestClient) -> None:
    resp = client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "example.invalid",  # never reachable -- proves the best-effort allow
            "username": "seeduser",
            "auth_method": "password",
            "password": "hunter2",
        },
    )
    assert resp.status_code == 200, resp.text


# --- local_path / staging_path: hard, blocking ------------------------------------------------


def test_create_queue_rejects_a_missing_local_path(isolated_config):
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": "/does/not/exist"},
        )
        assert resp.status_code == 400
        assert "local_path" in resp.text
        assert "does not exist" in resp.text


def test_create_queue_rejects_a_local_path_that_is_a_file_not_a_directory(
    isolated_config, tmp_path
):
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x")
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": str(f)},
        )
        assert resp.status_code == 400
        assert "not a directory" in resp.text


def test_create_queue_accepts_a_real_local_path(isolated_config):
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()},
        )
        assert resp.status_code == 201, resp.text


def test_create_queue_rejects_a_missing_staging_path_when_set(isolated_config):
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={
                "name": "TV",
                "remote_path": "/data/tv",
                "local_path": tempfile.mkdtemp(),
                "staging_path": "/also/does/not/exist",
            },
        )
        assert resp.status_code == 400
        assert "staging_path" in resp.text


def test_create_queue_omitted_staging_path_is_never_validated(isolated_config):
    # staging_path is optional (DESIGN.md §9.2's "Final destination") -- only validated when set.
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["staging_path"] is None


def test_update_queue_rejects_a_bad_local_path(isolated_config):
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()},
        )
        queue_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": "/nope/nope/nope"},
        )
        assert resp.status_code == 400
        assert "local_path" in resp.text


def test_update_queue_with_unchanged_valid_paths_and_only_a_toggle_change_still_succeeds(
    isolated_config,
):
    # A save that only changes an unrelated field must still validate -- and pass -- against
    # the submitted (still-valid) paths, never skip validation just because "nothing about the
    # path changed."
    local_path = tempfile.mkdtemp()
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": local_path},
        )
        queue_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json={
                "name": "TV",
                "remote_path": "/data/tv",
                "local_path": local_path,
                "enabled": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False


# --- remote_path: best-effort ------------------------------------------------------------------


def test_create_queue_accepts_a_bogus_remote_path_when_no_host_is_configured(isolated_config):
    # No host at all -- create_queue's own pre-existing 409 fires first either way, but this
    # proves the *ordering* doesn't accidentally make a bad remote_path the reported reason.
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/whatever", "local_path": tempfile.mkdtemp()},
        )
        assert resp.status_code == 409
        assert "host" in resp.text


def test_create_queue_accepts_a_bogus_remote_path_when_the_host_is_unreachable(isolated_config):
    with TestClient(app) as client:
        _put_fake_host(client)  # example.invalid -- never reachable
        resp = client.post(
            "/api/settings/queues",
            json={
                "name": "TV",
                "remote_path": "/this/does/not/exist/on/any/real/host",
                "local_path": tempfile.mkdtemp(),
            },
        )
        assert resp.status_code == 201, resp.text  # allowed -- cannot verify, not invalid


def test_update_queue_accepts_a_bogus_remote_path_with_credentials_needing_reentry(
    isolated_config,
):
    # A host that decrypts to nothing (credentials_need_reentry) must not block a save either
    # -- same asymmetry as "unreachable," a different cause.
    with TestClient(app) as client:
        _put_fake_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()},
        )
        queue_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/queues/{queue_id}",
            json={
                "name": "TV",
                "remote_path": "/somewhere/that/does/not/exist",
                "local_path": tempfile.mkdtemp(),
            },
        )
        assert resp.status_code == 200, resp.text


@pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- "
    "`docker compose -f docker-compose.test.yml up --build -d`",
)
def test_create_queue_rejects_a_remote_path_the_live_seedbox_reports_missing(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": SEEDBOX_HOST,
                "port": SEEDBOX_PORT,
                "username": SEEDBOX_USER,
                "auth_method": "password",
                "password": SEEDBOX_PASSWORD,
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200, resp.text

        resp = client.post(
            "/api/settings/queues",
            json={
                "name": "TV",
                "remote_path": "/data/pickup/does-not-exist-anywhere",
                "local_path": tempfile.mkdtemp(),
            },
        )
        assert resp.status_code == 400
        assert "remote_path" in resp.text
        assert "does not exist" in resp.text


@pytest.mark.skipif(
    not _seedbox_reachable(),
    reason="fake seedbox not reachable on 127.0.0.1:2222 -- "
    "`docker compose -f docker-compose.test.yml up --build -d`",
)
def test_create_queue_accepts_a_remote_path_that_really_exists_on_the_live_seedbox(
    isolated_config,
):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": SEEDBOX_HOST,
                "port": SEEDBOX_PORT,
                "username": SEEDBOX_USER,
                "auth_method": "password",
                "password": SEEDBOX_PASSWORD,
                "known_hosts_policy": "insecure",
            },
        )
        assert resp.status_code == 200, resp.text

        resp = client.post(
            "/api/settings/queues",
            json={
                "name": "TV",
                "remote_path": "/data/pickup",
                "local_path": tempfile.mkdtemp(),
            },
        )
        assert resp.status_code == 201, resp.text
