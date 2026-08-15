from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from lftpweb.main import app


def test_log_files_lists_the_current_file_after_a_log_line_is_written(isolated_config):
    with TestClient(app) as client:
        logging.getLogger("lftpweb.test").warning("hello from a test")
        resp = client.get("/api/logs/files")
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert any(f["name"] == "lftpweb.log" and f["is_current"] for f in files)


def test_tail_returns_recently_written_lines(isolated_config):
    with TestClient(app) as client:
        logger = logging.getLogger("lftpweb.test")
        logger.warning("marker-line-one")
        logger.info("marker-line-two")

        resp = client.get("/api/logs/tail")
        assert resp.status_code == 200
        body = resp.json()
        joined = "\n".join(body["lines"])
        assert "marker-line-one" in joined
        assert "marker-line-two" in joined
        assert body["truncated"] is False


def test_tail_filters_by_level(isolated_config):
    with TestClient(app) as client:
        logger = logging.getLogger("lftpweb.test")
        logger.warning("marker-warning-line")
        logger.info("marker-info-line")

        resp = client.get("/api/logs/tail", params={"level": "WARNING"})
        assert resp.status_code == 200
        body = resp.json()
        joined = "\n".join(body["lines"])
        assert "marker-warning-line" in joined
        assert "marker-info-line" not in joined


def test_tail_rejects_an_invalid_level(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/logs/tail", params={"level": "NOPE"})
        assert resp.status_code == 422


def test_tail_lines_param_is_bounded(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/logs/tail", params={"lines": 999999})
        assert resp.status_code == 200  # clamped server-side, not rejected


def test_download_current_log_file(isolated_config):
    with TestClient(app) as client:
        logging.getLogger("lftpweb.test").warning("marker-for-download")
        resp = client.get("/api/logs/lftpweb.log/download")
        assert resp.status_code == 200
        assert b"marker-for-download" in resp.content


def test_download_unknown_log_file_is_404(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/logs/not-a-real-file.log/download")
        assert resp.status_code == 404


def test_download_rejects_path_traversal(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/logs/..%2F..%2Fetc%2Fpasswd/download")
        assert resp.status_code in (404, 400)


def test_credential_redaction_already_covers_what_the_endpoint_can_expose(isolated_config):
    """DESIGN.md §10.1: redaction happens on the way IN (logsetup.CredentialRedactor), before
    a line ever reaches disk -- this endpoint must never see an unredacted credential to leak
    in the first place, so there is nothing for it to redact a second time. Prove the
    redactor actually catches the shape credentials take in this codebase (a URL embedding
    user:pass@, per core/lftp.py's own connection strings) end to end through the real
    logging pipeline the tail endpoint reads from.
    """
    with TestClient(app) as client:
        logging.getLogger("lftpweb.test").warning(
            "connecting to sftp://seeduser:hunter2@example.invalid:22"
        )
        resp = client.get("/api/logs/tail")
        joined = "\n".join(resp.json()["lines"])
        assert "hunter2" not in joined
        assert "sftp://seeduser:***@example.invalid:22" in joined
