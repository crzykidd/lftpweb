from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.main import app


def test_backup_settings_default_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/backup")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"interval_days": 1.0, "keep_count": 7}


def test_backup_settings_round_trip(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/backup", json={"interval_days": 2.0, "keep_count": 3})
        assert resp.status_code == 200
        assert resp.json() == {"interval_days": 2.0, "keep_count": 3}

        resp = client.get("/api/settings/backup")
        assert resp.json() == {"interval_days": 2.0, "keep_count": 3}


def test_backup_settings_rejects_invalid_values(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/backup", json={"interval_days": 0, "keep_count": 7})
        assert resp.status_code == 422
        resp = client.put("/api/settings/backup", json={"interval_days": 1, "keep_count": 0})
        assert resp.status_code == 422


def test_backup_now_creates_and_lists_and_downloads(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/backup/now")
        assert resp.status_code == 201
        info = resp.json()
        assert info["filename"].startswith("lftpweb-") and info["filename"].endswith(".db")
        assert info["size_bytes"] > 0

        resp = client.get("/api/settings/backup/list")
        assert resp.status_code == 200
        backups = resp.json()["backups"]
        assert any(b["filename"] == info["filename"] for b in backups)

        resp = client.get(f"/api/settings/backup/{info['filename']}/download")
        assert resp.status_code == 200
        assert resp.content[:16] == b"SQLite format 3\x00"


def test_backup_now_prunes_to_keep_count(isolated_config):
    with TestClient(app) as client:
        client.put("/api/settings/backup", json={"interval_days": 1, "keep_count": 2})
        for _ in range(4):
            resp = client.post("/api/settings/backup/now")
            assert resp.status_code == 201

        resp = client.get("/api/settings/backup/list")
        assert len(resp.json()["backups"]) <= 2


def test_download_unknown_backup_is_404(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/backup/lftpweb-20260101-000000.db/download")
        assert resp.status_code == 404


def test_download_rejects_path_traversal(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/backup/..%2F..%2Fetc%2Fpasswd/download")
        assert resp.status_code in (404, 400)
