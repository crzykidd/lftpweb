"""Integration tests for the auth surface (DESIGN.md §8, phase 8) over the real HTTP app via
`TestClient` -- `middleware.py`, `api/auth.py`, and their effect on every other router.

The single most important test in this file is
`test_default_mode_is_none_every_endpoint_behaves_as_before`: `AUTH_MODE` defaults to `none`,
and an existing install pulling this phase must see zero behavioural change. Everything else
here is the "prove the other two modes actually gate things, and prove the lockout-recovery
routes actually work" half.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from lftpweb.config import settings as app_settings
from lftpweb.core import auth as auth_module
from lftpweb.db import db_path
from lftpweb.main import app

# --- Every protected route, as FastAPI's own path templates (so this list can be checked
# against the app's actual registered routes -- see
# test_protected_route_enumeration_has_no_drift below). "Protected" here means: not in
# middleware.py's PUBLIC_API_PATHS allowlist. -----------------------------------------------

PROTECTED_ROUTE_TEMPLATES: list[tuple[str, str]] = [
    ("GET", "/api/stats"),
    ("GET", "/api/settings/host"),
    ("PUT", "/api/settings/host"),
    ("POST", "/api/settings/host/test"),
    ("GET", "/api/settings/queues"),
    ("POST", "/api/settings/queues"),
    ("PUT", "/api/settings/queues/{queue_id}"),
    ("DELETE", "/api/settings/queues/{queue_id}"),
    ("GET", "/api/settings/queues/{queue_id}/autoqueue-status"),
    ("GET", "/api/settings/patterns"),
    ("POST", "/api/settings/patterns"),
    ("PUT", "/api/settings/patterns/{pattern_id}"),
    ("DELETE", "/api/settings/patterns/{pattern_id}"),
    ("POST", "/api/settings/queues/{queue_id}/pattern-preview"),
    ("GET", "/api/settings/postprocess"),
    ("PUT", "/api/settings/postprocess"),
    ("GET", "/api/settings/transfer"),
    ("PUT", "/api/settings/transfer"),
    ("GET", "/api/settings/backup"),
    ("PUT", "/api/settings/backup"),
    ("GET", "/api/settings/backup/list"),
    ("POST", "/api/settings/backup/now"),
    ("GET", "/api/settings/backup/{filename}/download"),
    ("GET", "/api/settings/auth"),
    ("PUT", "/api/settings/auth"),
    ("POST", "/api/settings/auth/password"),
    ("GET", "/api/settings/auth/api-keys"),
    ("POST", "/api/settings/auth/api-keys"),
    ("DELETE", "/api/settings/auth/api-keys/{key_id}"),
    ("GET", "/api/files"),
    ("POST", "/api/files/rescan"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("POST", "/api/jobs/{job_id}/stop"),
    ("POST", "/api/jobs/{job_id}/move-to-top"),
    ("POST", "/api/jobs/{job_id}/start-now"),
    ("POST", "/api/items/{item_id}/stop"),
    ("POST", "/api/items/{item_id}/retry"),
    ("GET", "/api/history/jobs"),
    ("GET", "/api/history/jobs/{job_id}/output"),
    ("GET", "/api/history/events"),
    ("GET", "/api/logs/files"),
    ("GET", "/api/logs/tail"),
    ("GET", "/api/logs/{filename}/download"),
]

PUBLIC_ROUTE_TEMPLATES: set[tuple[str, str]] = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/session"),
    ("POST", "/api/auth/logout"),
}


def _concrete(path: str) -> str:
    """`/api/settings/queues/{queue_id}` -> `/api/settings/queues/1`. The actual value never
    matters for a 401/403 test -- middleware denies before FastAPI's router ever resolves
    path params -- but a real value keeps the request shape honest.
    """
    return re.sub(r"\{[^}]+\}", "1", path)


def _enable_password_mode(
    client: TestClient, username: str = "admin", password: str = "hunter2"
) -> dict:
    resp = client.put(
        "/api/settings/auth",
        json={
            "mode": "password",
            "proxy_header": "Remote-User",
            "proxy_trusted_cidrs": [],
            "username": username,
            "new_password": password,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _login(client: TestClient, username: str = "admin", password: str = "hunter2") -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- The regression that matters most -----------------------------------------------------


def test_default_mode_is_none_every_endpoint_behaves_as_before(isolated_config):
    """`AUTH_MODE` defaults to `none` (module docstring). No env var, no stored settings row
    (a truly fresh install) -- every endpoint across every router must behave exactly as it
    did before this phase, with no cookie, no API key, nothing.
    """
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/stats").status_code == 200
        assert client.get("/api/settings/host").status_code == 200
        assert client.get("/api/settings/queues").status_code == 200
        assert client.get("/api/settings/postprocess").status_code == 200
        assert client.get("/api/settings/transfer").status_code == 200
        assert client.get("/api/settings/backup").status_code == 200
        assert client.get("/api/files").status_code == 200
        assert client.post("/api/files/rescan").status_code == 202
        assert client.get("/api/jobs").status_code == 200
        assert client.get("/api/history/jobs").status_code == 200
        assert client.get("/api/history/events").status_code == 200
        assert client.get("/api/logs/files").status_code == 200
        # Settings -> Auth itself is reachable too, unauthenticated, in `none` mode --
        # that's how a fresh install turns auth on in the first place.
        assert client.get("/api/settings/auth").status_code == 200
        assert client.get("/api/settings/auth").json()["mode"] == "none"


def test_health_reachable_unauthenticated_in_every_mode(tmp_path, monkeypatch):
    # Each mode gets its own on-disk database (a fresh subdirectory) so switching modes
    # doesn't itself get blocked by the *previous* mode's gate -- this test is about
    # `/api/health` specifically, not about the mode-switch flow (covered elsewhere).
    none_dir, password_dir, proxy_dir = (
        tmp_path / "none",
        tmp_path / "password",
        tmp_path / "proxy",
    )
    for d in (none_dir, password_dir, proxy_dir):
        d.mkdir()

    monkeypatch.setattr(app_settings, "config_dir", str(none_dir))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200  # mode=none

    monkeypatch.setattr(app_settings, "config_dir", str(password_dir))
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        assert client.get("/api/health").status_code == 200  # mode=password, no session

    monkeypatch.setattr(app_settings, "config_dir", str(proxy_dir))
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/auth",
            json={
                "mode": "proxy",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": ["203.0.113.0/24"],
            },
        )
        assert resp.status_code == 200
        # No header, and the default TestClient peer isn't in the trusted CIDR -- health
        # must still answer.
        assert client.get("/api/health").status_code == 200  # mode=proxy


def test_static_and_spa_shell_always_reachable_in_every_mode(isolated_config):
    """Non-negotiable #2: the login page itself has to load. Non-`/api/` paths are never
    gated by `middleware.py` -- see its module docstring.
    """
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        # No static_dir in this test environment (LFTPWEB_STATIC_DIR unset outside the
        # container -- see config.py), so main.py never registers the SPA fallback route at
        # all, and a non-/api/ path 404s. What matters for this test is that it is *not*
        # gated by auth (401/403) -- the absence of the SPA build is a test-environment
        # fact, not something this phase controls.
        resp = client.get("/some/spa/route")
        assert resp.status_code == 404
        assert resp.status_code not in (401, 403)


# --- Enumerate protected routes and assert the negative ------------------------------------


def test_protected_routes_return_401_unauthenticated_in_password_mode(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        failures = []
        for method, template in PROTECTED_ROUTE_TEMPLATES:
            resp = client.request(method, _concrete(template))
            if resp.status_code != 401:
                failures.append(f"{method} {template} -> {resp.status_code}")
        assert not failures, "routes NOT rejected unauthenticated:\n" + "\n".join(failures)


def test_protected_route_enumeration_has_no_drift(isolated_config):
    """Guards the enumeration itself against going stale: every HTTP route this app
    actually registers under /api/ (besides the public allowlist) must appear in
    PROTECTED_ROUTE_TEMPLATES above. A new router mounted later and never added here would
    otherwise go completely untested -- exactly the failure mode the phase 8 prompt names.
    """
    actual: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not path.startswith("/api/") or not methods:
            continue
        for m in methods:
            if m == "HEAD":
                continue
            actual.add((m, path))

    expected = set(PROTECTED_ROUTE_TEMPLATES) | PUBLIC_ROUTE_TEMPLATES
    missing = actual - expected
    assert not missing, f"routes registered but not covered by this test file: {missing}"


def test_ws_rejected_without_session_in_password_mode(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/ws"):
                pass


def test_ws_reachable_when_mode_is_none(isolated_config):
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as ws:
            message = ws.receive_json()
            assert message["type"] == "snapshot"


# --- Login / session / CSRF -----------------------------------------------------------------


def test_login_success_sets_httponly_samesite_lax_cookie_and_returns_csrf(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()

        resp = client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["username"] == "admin"
        assert body["csrf_token"]

        set_cookie = resp.headers.get("set-cookie", "")
        assert "lftpweb_session=" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()


def test_login_failure_wrong_password_401(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401


def test_login_failure_unknown_username_401_not_404(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        resp = client.post("/api/auth/login", json={"username": "nobody", "password": "hunter2"})
        assert resp.status_code == 401


def test_session_cookie_grants_access_to_protected_route(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        _login(client)
        assert client.get("/api/files").status_code == 200


def test_logout_invalidates_the_session(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        _login(client)
        assert client.get("/api/files").status_code == 200

        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/files").status_code == 401


def test_csrf_required_on_mutating_request_missing_or_wrong(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        _login(client)

        assert client.post("/api/files/rescan").status_code == 403
        assert (
            client.post("/api/files/rescan", headers={"X-CSRF-Token": "wrong"}).status_code == 403
        )


def test_csrf_correct_token_allows_mutating_request(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        session = _login(client)
        resp = client.post("/api/files/rescan", headers={"X-CSRF-Token": session["csrf_token"]})
        assert resp.status_code == 202


def test_csrf_not_required_on_get(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        _login(client)
        assert client.get("/api/files").status_code == 200


def test_login_rate_limited_after_repeated_failures(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        for _ in range(5):
            resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
            assert resp.status_code == 401
        blocked = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert blocked.status_code == 429
        assert "retry-after" in {h.lower() for h in blocked.headers}


def test_login_rate_limit_does_not_block_a_correct_password(isolated_config):
    """The limiter only ever counts failures (`core/auth.py.LoginRateLimiter`) -- a handful
    of typos must not lock a legitimate user out of their own account.
    """
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        for _ in range(3):
            client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
        assert resp.status_code == 200


# --- API key: independent of mode -----------------------------------------------------------


def test_api_key_works_in_every_mode(isolated_config):
    with TestClient(app) as client:
        create_resp = client.post("/api/settings/auth/api-keys", json={"name": "script"})
        assert create_resp.status_code == 201
        key = create_resp.json()["key"]

        # mode=none: works (and so does everything else).
        assert client.get("/api/files", headers={"X-API-Key": key}).status_code == 200

        _enable_password_mode(client)
        client.cookies.clear()
        # mode=password, no session at all: the API key alone is sufficient.
        assert client.get("/api/files", headers={"X-API-Key": key}).status_code == 200
        assert client.get("/api/files", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/api/files").status_code == 401  # no key, no session: still denied


def test_api_key_mutating_request_does_not_need_csrf(isolated_config):
    with TestClient(app) as client:
        create_resp = client.post("/api/settings/auth/api-keys", json={"name": "script"})
        key = create_resp.json()["key"]
        _enable_password_mode(client)
        client.cookies.clear()
        resp = client.post("/api/files/rescan", headers={"X-API-Key": key})
        assert resp.status_code == 202


def test_api_key_plaintext_never_returned_by_list_endpoint(isolated_config):
    with TestClient(app) as client:
        create_resp = client.post("/api/settings/auth/api-keys", json={"name": "script"})
        key = create_resp.json()["key"]
        list_resp = client.get("/api/settings/auth/api-keys")
        assert key not in list_resp.text


def test_api_key_revoke_stops_working(isolated_config):
    with TestClient(app) as client:
        create_resp = client.post("/api/settings/auth/api-keys", json={"name": "script"})
        key_id = create_resp.json()["id"]
        key = create_resp.json()["key"]
        assert client.delete(f"/api/settings/auth/api-keys/{key_id}").status_code == 204
        _enable_password_mode(client)
        client.cookies.clear()
        assert client.get("/api/files", headers={"X-API-Key": key}).status_code == 401


# --- proxy mode ------------------------------------------------------------------------------


def test_proxy_mode_refuses_to_enable_without_trusted_cidr(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/auth",
            json={"mode": "proxy", "proxy_header": "Remote-User", "proxy_trusted_cidrs": []},
        )
        assert resp.status_code == 400
        assert client.get("/api/settings/auth").json()["mode"] == "none"


def test_proxy_mode_rejects_request_outside_trusted_cidr(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/auth",
            json={
                "mode": "proxy",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": ["203.0.113.0/24"],
            },
        )
        assert resp.status_code == 200
        # TestClient's default peer ("testclient", 50000) isn't in 203.0.113.0/24 -- a
        # spoofed header alone must not be enough (DESIGN.md §8: "without the CIDR check
        # this mode is a bypass").
        resp = client.get("/api/files", headers={"Remote-User": "alice"})
        assert resp.status_code == 401


def test_proxy_mode_accepts_trusted_cidr_with_header(isolated_config):
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        resp = client.put(
            "/api/settings/auth",
            json={
                "mode": "proxy",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": ["127.0.0.1/32"],
            },
        )
        assert resp.status_code == 200
        resp = client.get("/api/files", headers={"Remote-User": "alice"})
        assert resp.status_code == 200


def test_proxy_mode_rejects_missing_identity_header_even_from_trusted_cidr(isolated_config):
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        client.put(
            "/api/settings/auth",
            json={
                "mode": "proxy",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": ["127.0.0.1/32"],
            },
        )
        resp = client.get("/api/files")
        assert resp.status_code == 401


# --- argon2id, and the hash never leaking ---------------------------------------------------


def test_stored_password_hash_is_argon2id_and_never_returned(isolated_config):
    with TestClient(app) as client:
        settings_resp = _enable_password_mode(client)
        assert "password_hash" not in settings_resp
        assert "hunter2" not in str(settings_resp)

        get_resp = client.get("/api/settings/auth")
        assert "password_hash" not in get_resp.text
        assert "hunter2" not in get_resp.text

    conn = sqlite3.connect(str(db_path(str(isolated_config))))
    try:
        row = conn.execute("SELECT password_hash FROM auth_user").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0].startswith("$argon2id$")


# --- Lockout-recovery routes, actually exercised, not just documented ----------------------


def test_lockout_recovery_env_var_override(isolated_config, monkeypatch):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        # Locked out: mode=password stored, no session.
        assert client.get("/api/files").status_code == 401

    # Recovery route 1 (README.md, core/auth.py's module docstring): force AUTH_MODE=none
    # via the env-var override and restart -- simulated here by setting the same module-level
    # `settings.auth_mode` attribute `config.py` documents, then opening a fresh app context
    # against the same on-disk database (same `isolated_config` dir -- same stored
    # `mode: "password"` row, untouched).
    monkeypatch.setattr(app_settings, "auth_mode", "none")
    with TestClient(app) as client:
        resp = client.get("/api/files")
        assert resp.status_code == 200, "the env-var override did not restore access"


# --- §10.1 redaction: nothing auth adds ever reaches the log file --------------------------


def test_login_never_writes_password_session_or_csrf_to_the_log_file(isolated_config):
    """The phase 8 prompt asks this explicitly: never log a token, cookie, or key, not even
    truncated. Nothing in `api/auth.py`/`core/auth.py`/`middleware.py` calls `logger.*` with
    a secret (reviewed directly -- see docs/decisions.md), and uvicorn's own access log line
    is method+path+status only, never headers or body -- this test proves that rather than
    just asserting it by inspection.
    """
    with TestClient(app) as client:
        _enable_password_mode(client, username="admin", password="hunter2-secret")
        client.cookies.clear()

        login_resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "hunter2-secret"}
        )
        assert login_resp.status_code == 200
        csrf_token = login_resp.json()["csrf_token"]
        session_cookie_value = login_resp.cookies.get(auth_module.SESSION_COOKIE_NAME)
        assert session_cookie_value

        create_resp = client.post(
            "/api/settings/auth/api-keys",
            json={"name": "script"},
            headers={"X-CSRF-Token": csrf_token},
        )
        api_key = create_resp.json()["key"]

    log_file = db_path(str(isolated_config)).parent / "logs" / "lftpweb.log"
    assert log_file.is_file()
    contents = log_file.read_text()

    assert "hunter2-secret" not in contents
    assert csrf_token not in contents
    assert api_key not in contents
    assert session_cookie_value not in contents


def test_lockout_recovery_delete_user_row(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        client.cookies.clear()
        assert client.get("/api/files").status_code == 401

        # Recovery route 2 (core/auth.py's module docstring): an operator with only DB/shell
        # access -- no ability or wish to restart the container -- runs the equivalent of
        #   sqlite3 /config/lftpweb.db "DELETE FROM auth_user"
        # as a second, independent connection to the same on-disk file. WAL mode (db.py's
        # own connect()) is exactly what makes this safe to do while the app itself holds the
        # file open.
        conn = sqlite3.connect(str(db_path(str(isolated_config))))
        conn.execute("DELETE FROM auth_user")
        conn.commit()
        conn.close()

        resp = client.get("/api/files")
        assert resp.status_code == 200, "deleting the auth_user row did not restore access"
