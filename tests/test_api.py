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
