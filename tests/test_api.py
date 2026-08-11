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


def test_stats_returns_documented_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json() == {
            "current_speed_bps": 0,
            "allocated_bps": 0,
            "ceiling_bps": 0,
            "queued_count": 0,
            "queued_bytes": 0,
            "transferred_24h_bytes": 0,
        }
