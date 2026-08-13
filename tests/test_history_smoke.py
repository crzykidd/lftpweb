"""HTTP-level wiring for `api/history.py` -- confirms the routes are registered on the real
app (`main.py`) and return the documented JSON shape. Query-parameter/filter/pagination logic
itself is covered more thoroughly, without the HTTP layer, in `tests/test_history_api.py`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.main import app


def test_history_jobs_empty_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/history/jobs")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"jobs": [], "total": 0, "limit": 200, "offset": 0}


def test_history_events_empty_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/history/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"events": [], "total": 0, "limit": 200, "offset": 0}


def test_history_job_output_404_for_unknown_job(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/history/jobs/999999/output")
        assert resp.status_code == 404


def test_history_jobs_rejects_non_terminal_state_filter(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/history/jobs", params={"state": "running"})
        assert resp.status_code == 422


def test_history_jobs_limit_is_clamped_not_rejected(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/history/jobs", params={"limit": 100_000})
        assert resp.status_code == 200
        assert resp.json()["limit"] == 500


# --- Clearing (2026-08-13, prompts/2026-08-13-clear-history.md) -- HTTP wiring only; the
# actual filter/SQL behaviour is covered thoroughly in tests/test_history_api.py.


def test_history_clear_job_404_for_unknown_job(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/history/jobs/999999")
        assert resp.status_code == 404


def test_history_clear_event_404_for_unknown_event(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/history/events/999999")
        assert resp.status_code == 404


def test_history_clear_all_jobs_on_an_empty_db_deletes_nothing(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/history/jobs")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 0}


def test_history_clear_all_events_on_an_empty_db_deletes_nothing(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/history/events")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 0}


def test_history_clear_jobs_rejects_non_terminal_state_filter(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/history/jobs", params={"state": "running"})
        assert resp.status_code == 422
