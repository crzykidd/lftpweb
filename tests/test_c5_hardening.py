"""Audit batch C5 (DESIGN.md §8): docs behind auth (F11), opt-in `LFTPWEB_TRUSTED_PROXIES`
(F9/F10), and `current_password` on `PUT /api/settings/auth` (F14). Every change is non-breaking:
unset env / unchanged config behaves exactly as before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lftpweb.config import settings as app_settings
from lftpweb.core import netguard
from lftpweb.main import app

LOGIN = {"username": "admin", "password": "hunter2"}


def _put(client: TestClient, **fields):
    body = {"mode": "password", "proxy_header": "Remote-User", "proxy_trusted_cidrs": []}
    body.update(fields)
    return client.put("/api/settings/auth", json=body)


def _enable_password(client: TestClient) -> None:
    resp = _put(client, username="admin", new_password="hunter2")
    assert resp.status_code == 200, resp.text
    client.cookies.clear()


def _login(client: TestClient) -> dict:
    resp = client.post("/api/auth/login", json=LOGIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _logged_in(client: TestClient) -> dict[str, str]:
    _enable_password(client)
    return {"X-CSRF-Token": _login(client)["csrf_token"]}


# --- F11: docs pages ------------------------------------------------------------------------

DOC_PATHS = ["/docs", "/redoc", "/openapi.json"]


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_gated_in_password_mode(isolated_config, path):
    with TestClient(app) as client:
        _enable_password(client)
        assert client.get(path).status_code == 401
        _login(client)
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_open_in_none_mode(isolated_config, path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 200


def test_docs_still_subject_to_host_allow_list(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "lftpweb.example.com")
    with TestClient(app) as client:
        assert client.get("/docs", headers={"Host": "evil.example.net"}).status_code == 403
        assert client.get("/docs", headers={"Host": "lftpweb.example.com"}).status_code == 200


# --- F9 / F10: trusted proxies --------------------------------------------------------------


def _login_via(peer: str, headers: dict[str, str], password: str = "hunter2"):
    with TestClient(app, client=(peer, 50000)) as client:
        return client.post(
            "/api/auth/login", json={"username": "admin", "password": password}, headers=headers
        )


def test_trusted_proxies_unset_ignores_forwarded_headers(isolated_config):
    assert not app_settings.trusted_proxies
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        _enable_password(client)
        resp = client.post(
            "/api/auth/login",
            json=LOGIN,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"},
        )
        assert resp.status_code == 200
        assert "secure" not in resp.headers["set-cookie"].lower()
        # Every forwarded IP shares the one socket-peer bucket, as before.
        for xff in ("203.0.113.1", "203.0.113.2", "203.0.113.3", "203.0.113.4", "203.0.113.5"):
            bad = client.post(
                "/api/auth/login",
                json={**LOGIN, "password": "nope"},
                headers={"X-Forwarded-For": xff},
            )
            assert bad.status_code == 401
        blocked = client.post(
            "/api/auth/login",
            json={**LOGIN, "password": "nope"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert blocked.status_code == 429


def test_trusted_proxies_set_secure_cookie_from_forwarded_proto(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "trusted_proxies", "10.0.0.0/24")
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        _enable_password(client)
        https = client.post("/api/auth/login", json=LOGIN, headers={"X-Forwarded-Proto": "https"})
        assert "secure" in https.headers["set-cookie"].lower()
        plain = client.post("/api/auth/login", json=LOGIN)
        assert "secure" not in plain.headers["set-cookie"].lower()


def test_trusted_proxies_set_limiter_buckets_per_real_client(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "trusted_proxies", "10.0.0.5")
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        _enable_password(client)
        bad = {**LOGIN, "password": "nope"}
        for _ in range(5):
            r = client.post(
                "/api/auth/login", json=bad, headers={"X-Forwarded-For": "198.51.100.1"}
            )
            assert r.status_code == 401
        assert (
            client.post(
                "/api/auth/login", json=bad, headers={"X-Forwarded-For": "198.51.100.1"}
            ).status_code
            == 429
        )
        # A different real client behind the same proxy is not locked out.
        other = client.post(
            "/api/auth/login", json=LOGIN, headers={"X-Forwarded-For": "198.51.100.2"}
        )
        assert other.status_code == 200


def test_trusted_proxies_untrusted_peer_headers_ignored(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "trusted_proxies", "10.0.0.5")
    with TestClient(app, client=("192.0.2.9", 50000)) as client:
        _enable_password(client)
        resp = client.post(
            "/api/auth/login",
            json=LOGIN,
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.1"},
        )
        assert "secure" not in resp.headers["set-cookie"].lower()
        bad = {**LOGIN, "password": "nope"}
        for i in range(5):
            client.post("/api/auth/login", json=bad, headers={"X-Forwarded-For": f"198.51.100.{i}"})
        # Forged, ever-changing X-Forwarded-For did not dodge the limiter.
        assert (
            client.post(
                "/api/auth/login", json=bad, headers={"X-Forwarded-For": "198.51.100.200"}
            ).status_code
            == 429
        )


def test_trusted_proxies_does_not_change_proxy_mode_peer_check(isolated_config, monkeypatch):
    """Proxy auth mode trusts the connection's peer (the proxy), never the forwarded client."""
    monkeypatch.setattr(app_settings, "trusted_proxies", "10.0.0.5")
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        resp = client.put(
            "/api/settings/auth",
            json={
                "mode": "proxy",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": ["10.0.0.5/32"],
            },
        )
        assert resp.status_code == 200
        headers = {"Remote-User": "alice", "X-Forwarded-For": "198.51.100.1"}
        assert client.get("/api/files", headers=headers).status_code == 200
    with TestClient(app, client=("192.0.2.9", 50000)) as client:
        assert client.get("/api/files", headers={"Remote-User": "alice"}).status_code == 401


def test_parse_trusted_proxies():
    assert netguard.parse_trusted_proxies(None) == []
    assert netguard.parse_trusted_proxies("") == []
    assert netguard.parse_trusted_proxies("10.0.0.5, 172.18.0.0/16") == [
        "10.0.0.5",
        "172.18.0.0/16",
    ]
    assert netguard.parse_trusted_proxies("10.0.0.5, *") == "*"
    # An invalid entry is dropped (fewer trusted peers), never fatal.
    assert netguard.parse_trusted_proxies("nonsense, 10.0.0.5") == ["10.0.0.5"]


def test_healthcheck_and_forwarded_host_unaffected_when_set(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "trusted_proxies", "10.0.0.5")
    monkeypatch.setattr(app_settings, "allowed_hosts", "lftpweb.example.com")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/health", headers={"Host": "127.0.0.1:8087"}).status_code == 200
    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        resp = client.post(
            "/api/files/rescan",
            headers={
                "Host": "lftpweb.example.com",
                "Origin": "https://lftpweb.example.com",
                "X-Forwarded-Host": "lftpweb.example.com",
            },
        )
        assert resp.status_code == 202


# --- F14: current_password on PUT /settings/auth --------------------------------------------


def test_put_replace_password_requires_current_password(isolated_config):
    with TestClient(app) as client:
        h = _logged_in(client)
        resp = client.put(
            "/api/settings/auth",
            json={
                "mode": "password",
                "proxy_header": "Remote-User",
                "proxy_trusted_cidrs": [],
                "new_password": "brandnew",
            },
            headers=h,
        )
        assert resp.status_code == 422
        assert "current_password" in resp.json()["detail"]
        # The password was not changed.
        client.cookies.clear()
        assert client.post("/api/auth/login", json=LOGIN).status_code == 200


def _put_h(client, headers, **fields):
    body = {"mode": "password", "proxy_header": "Remote-User", "proxy_trusted_cidrs": []}
    body.update(fields)
    return client.put("/api/settings/auth", json=body, headers=headers)


def test_put_wrong_current_password_refused_and_counted(isolated_config):
    with TestClient(app) as client:
        h = _logged_in(client)
        for _ in range(5):
            r = _put_h(client, h, new_password="brandnew", current_password="wrong")
            assert r.status_code == 401
        # Same bucket as login: now blocked, even for the right password.
        r = _put_h(client, h, new_password="brandnew", current_password="hunter2")
        assert r.status_code == 429
        assert client.post("/api/auth/login", json={**LOGIN, "password": "x"}).status_code == 429


def test_put_right_current_password_changes_it(isolated_config):
    with TestClient(app) as client:
        h = _logged_in(client)
        r = _put_h(client, h, new_password="brandnew", current_password="hunter2")
        assert r.status_code == 200, r.text
        client.cookies.clear()
        assert client.post("/api/auth/login", json=LOGIN).status_code == 401
        ok = client.post("/api/auth/login", json={**LOGIN, "password": "brandnew"})
        assert ok.status_code == 200


def test_put_leaving_password_mode_requires_current_password(isolated_config):
    with TestClient(app) as client:
        h = _logged_in(client)
        r = _put_h(client, h, mode="none")
        assert r.status_code == 422
        r = _put_h(client, h, mode="none", current_password="wrong")
        assert r.status_code == 401
        r = _put_h(client, h, mode="none", current_password="hunter2")
        assert r.status_code == 200
        assert r.json()["mode"] == "none"


def test_put_first_enable_needs_no_current_password(isolated_config):
    with TestClient(app) as client:
        assert _put(client, username="admin", new_password="hunter2").status_code == 200


def test_put_none_to_password_with_existing_user_unaffected(isolated_config):
    """Mode `none` protects nothing, so re-enabling an existing user asks for nothing."""
    with TestClient(app) as client:
        h = _logged_in(client)
        assert _put_h(client, h, mode="none", current_password="hunter2").status_code == 200
        assert _put(client).status_code == 200


def test_put_non_password_changes_unaffected(isolated_config):
    with TestClient(app) as client:
        h = _logged_in(client)
        assert _put_h(client, h, proxy_header="X-User").status_code == 200
        assert _put_h(client, h, username="root").status_code == 200
        assert client.post("/api/auth/logout", headers=h).status_code == 200


def test_put_env_lockout_recovery_still_works(isolated_config, monkeypatch):
    """`LFTPWEB_AUTH_MODE=none` is the forgot-password lever: while it is set no password
    protects anything, so setting a new one asks for no current password."""
    with TestClient(app) as client:
        _enable_password(client)
        monkeypatch.setattr(app_settings, "auth_mode", "none")
        r = _put(client, new_password="fresh-start")
        assert r.status_code == 200, r.text
        monkeypatch.setattr(app_settings, "auth_mode", None)
        client.cookies.clear()
        ok = client.post("/api/auth/login", json={**LOGIN, "password": "fresh-start"})
        assert ok.status_code == 200
