from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb import __version__
from lftpweb.main import app


def test_health_returns_200_with_version_and_db_status(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["db"] is True
        assert body["uptime_s"] >= 0
        assert body["repo_url"] == ""
        # Phase 7, DESIGN.md §10.3: no host configured yet -> host_reachable is None (not
        # False -- "unreachable" would be a wrong claim about a host that doesn't exist), and
        # the scheduler loop (core/queue.py.TransferQueue) is running under TestClient's own
        # lifespan.
        assert body["host_reachable"] is None
        assert body["scheduler_alive"] is True
        # 2026-08-16: unbaked (local uv run / TestClient) -> None, not "" or a missing key --
        # the frontend's lib/versionBadge.ts degrades to today's plain rendering on exactly
        # this value.
        assert body["build_sha"] is None
        assert body["build_channel"] is None


def test_health_reports_build_sha_and_channel_when_baked(isolated_config, monkeypatch):
    """2026-08-16, docs/decisions.md: docker/Dockerfile bakes these into env vars at image
    build time; config.Settings reads them like any other LFTPWEB_* setting. Simulated here
    via monkeypatch on the settings singleton, same pattern as test_spa_fallback.py.
    """
    from lftpweb.config import settings

    monkeypatch.setattr(settings, "build_sha", "abc1234")
    monkeypatch.setattr(settings, "build_channel", "dev")
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["build_sha"] == "abc1234"
        assert body["build_channel"] == "dev"


def test_health_reports_host_unreachable_once_a_host_exists_and_a_connection_failed(
    isolated_config,
):
    """DESIGN.md §10.3: health must report host reachability, not just DB/scheduler. `None`
    (no host) is covered by the test above; this covers the `False` case -- a host row
    exists but the pooled connection last failed. Uses a closed local port so the failure is
    a fast ECONNREFUSED, not a multi-second timeout.
    """
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "unreachable",
                "address": "127.0.0.1",
                "port": 1,
                "username": "nobody",
                "auth_method": "key",
                "key_path": "/nonexistent",
            },
        )
        assert resp.status_code == 200

        test_resp = client.post("/api/settings/host/test")
        assert test_resp.json()["ok"] is False

        resp = client.get("/api/health")
        body = resp.json()
        assert body["host_reachable"] is False
        assert body["status"] == "degraded"


def test_settings_build_fields_normalize_blank_env_to_none():
    """2026-08-16: docker/Dockerfile's runtime stage always sets LFTPWEB_BUILD_SHA/_CHANNEL
    as ENV, even when the corresponding ARG was never passed with --build-arg -- Docker bakes
    an unset ARG's declared empty-string default in regardless. Settings must treat that blank
    string identically to the var being absent entirely, not as a third, spurious value.
    """
    from lftpweb.config import Settings

    assert Settings().build_sha is None
    assert Settings().build_channel is None
    blank = Settings(build_sha="", build_channel="")
    assert blank.build_sha is None
    assert blank.build_channel is None
    baked = Settings(build_sha="abc1234", build_channel="dev")
    assert baked.build_sha == "abc1234"
    assert baked.build_channel == "dev"


def test_stats_returns_documented_shape(isolated_config):
    # Phase 1 stubbed every field at 0; phase 3 (core/queue.py, core/scheduler.py) fills them
    # in for real — see tests/test_jobs_api.py for the queued/allocated/speed cases actually
    # moving. With nothing queued and default transfer settings, only `ceiling_bps` (the
    # configured max_bandwidth_bps default) is non-zero.
    with TestClient(app) as client:
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_speed_bps"] == 0
        assert body["allocated_bps"] == 0
        assert body["ceiling_bps"] > 0
        assert body["queued_count"] == 0
        assert body["queued_bytes"] == 0
        assert body["transferred_24h_bytes"] == 0
