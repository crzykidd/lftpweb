from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.main import app


def test_files_empty_when_unconfigured(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/files")
        assert resp.status_code == 200
        assert resp.json() == {"queues": []}


def test_rescan_triggers_without_error(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/files/rescan")
        assert resp.status_code == 202
        assert resp.json() == {"triggered": True}
