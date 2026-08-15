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
