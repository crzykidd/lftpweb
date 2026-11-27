"""Audit batch C3 (DESIGN.md §8): cross-site and DNS-rebinding defences that hold in every auth
mode -- `middleware.py.RequestGuardMiddleware` and `core/netguard.py`.

`TestClient` sends no `X-Requested-With`, `Origin` or `Sec-Fetch-Site`, i.e. it looks like curl.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from lftpweb.config import settings as app_settings
from lftpweb.core import netguard
from lftpweb.main import app
from lftpweb.middleware import REQUIRED_HEADER

DELETE_ITEM = "/api/items/999999/delete"  # bodiless POST: a CORS "simple request" (audit F2)


def _enable_password_mode(client: TestClient) -> None:
    resp = client.put(
        "/api/settings/auth",
        json={
            "mode": "password",
            "proxy_header": "Remote-User",
            "proxy_trusted_cidrs": [],
            "username": "admin",
            "new_password": "hunter2",
        },
    )
    assert resp.status_code == 200, resp.text


def _enable_proxy_mode(client: TestClient) -> None:
    resp = client.put(
        "/api/settings/auth",
        json={
            "mode": "proxy",
            "proxy_header": "Remote-User",
            "proxy_trusted_cidrs": ["127.0.0.1/32"],
        },
    )
    assert resp.status_code == 200, resp.text


# --- 1. mutating API requests: refuse only what the browser says is cross-site --------------

XSITE = {"Sec-Fetch-Site": "cross-site"}


def _bare(client: TestClient) -> TestClient:
    """curl-style: TestClient sends no X-Requested-With, Origin or Sec-Fetch-* by default."""
    for name in (REQUIRED_HEADER, "Origin", "Sec-Fetch-Site"):
        client.headers.pop(name, None)
    return client


def test_curl_style_request_is_allowed_in_none_mode(isolated_config):
    with TestClient(app) as client:
        resp = _bare(client).post(DELETE_ITEM)
        assert resp.status_code == 404, resp.text  # the route answered; the guard didn't


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_curl_style_is_allowed_for_every_mutating_method(isolated_config, method):
    with TestClient(app) as client:
        resp = _bare(client).request(method, "/api/settings/queues/1")
        assert resp.status_code != 403, resp.text


def test_stale_tab_same_origin_without_the_header_is_allowed(isolated_config):
    with TestClient(app) as client:
        resp = _bare(client).post(DELETE_ITEM, headers={"Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 404, resp.text
        resp = client.post(DELETE_ITEM, headers={"Sec-Fetch-Site": "none"})
        assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_browser_declared_cross_site_is_refused(isolated_config, site):
    with TestClient(app) as client:
        resp = _bare(client).post(DELETE_ITEM, headers={"Sec-Fetch-Site": site})
        assert resp.status_code == 403
        assert "cross-site" in resp.json()["detail"]


def test_origin_mismatch_without_fetch_metadata_is_refused(isolated_config):
    with TestClient(app) as client:
        resp = _bare(client).post(DELETE_ITEM, headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403
        resp = client.post(DELETE_ITEM, headers={"Origin": "null"})
        assert resp.status_code == 403
        # A matching Origin (old browser, same-origin) passes. TestClient's Host is `testserver`.
        resp = client.post(DELETE_ITEM, headers={"Origin": "http://testserver"})
        assert resp.status_code == 404, resp.text
        # ...as does one matching X-Forwarded-Host (a Host-rewriting proxy).
        resp = client.post(
            DELETE_ITEM,
            headers={"Origin": "https://lftpweb.example", "X-Forwarded-Host": "lftpweb.example"},
        )
        assert resp.status_code == 404, resp.text


def test_origin_matching_the_allow_list_passes(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "testserver,lftpweb.example")
    with TestClient(app) as client:
        resp = _bare(client).post(DELETE_ITEM, headers={"Origin": "https://lftpweb.example"})
        assert resp.status_code == 404, resp.text


def test_the_header_present_is_always_allowed(isolated_config):
    with TestClient(app) as client:
        for extra in ({}, XSITE, {"Origin": "http://evil.example"}):
            resp = _bare(client).post(DELETE_ITEM, headers={REQUIRED_HEADER: "lftpweb", **extra})
            assert resp.status_code == 404, (extra, resp.text)


def test_safe_methods_and_the_healthcheck_are_never_guarded(isolated_config):
    with TestClient(app) as client:
        _bare(client)
        assert client.get("/api/health", headers=XSITE).status_code == 200
        assert client.get("/api/files", headers=XSITE).status_code == 200
        assert client.get("/api/auth/session", headers=XSITE).status_code == 200


def test_public_login_and_logout_routes_get_the_same_rule(isolated_config):
    with TestClient(app) as client:
        _bare(client)
        assert client.post("/api/auth/logout", headers=XSITE).status_code == 403
        resp = client.post(
            "/api/auth/login", json={"username": "a", "password": "b"}, headers=XSITE
        )
        assert resp.status_code == 403
        assert client.post("/api/auth/logout").status_code != 403


def test_an_api_key_script_needs_nothing_but_a_cross_site_browser_is_refused(isolated_config):
    with TestClient(app) as client:
        created = client.post("/api/settings/auth/api-keys", json={"name": "script"})
        assert created.status_code == 201, created.text
        key = created.json()["key"]
        resp = _bare(client).post(DELETE_ITEM, headers={"X-API-Key": key})
        assert resp.status_code == 404
        resp = client.post(DELETE_ITEM, headers={"X-API-Key": key, **XSITE})
        assert resp.status_code == 403


def test_proxy_mode_guard(isolated_config):
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        _enable_proxy_mode(client)
        ident = {"Remote-User": "alice"}
        assert _bare(client).post(DELETE_ITEM, headers=ident).status_code == 404
        assert client.post(DELETE_ITEM, headers={**ident, **XSITE}).status_code == 403
        assert client.post(DELETE_ITEM, headers={**ident, REQUIRED_HEADER: "x"}).status_code == 404


def test_password_mode_keeps_its_pre_c3_csrf_token_and_adds_nothing(isolated_config):
    with TestClient(app) as client:
        _enable_password_mode(client)
        login = client.post("/api/auth/login", json={"username": "admin", "password": "hunter2"})
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        _bare(client)
        # curl-style with a valid session + token: allowed, no header required.
        assert client.post(DELETE_ITEM, headers={"X-CSRF-Token": csrf}).status_code == 404
        # The CSRF token is still required.
        no_token = client.post(DELETE_ITEM)
        assert no_token.status_code == 403
        assert "CSRF" in no_token.json()["detail"]
        # The guard still refuses a browser-declared cross-site request that has the token.
        assert client.post(DELETE_ITEM, headers={"X-CSRF-Token": csrf, **XSITE}).status_code == 403


def test_no_cors_middleware_answers_a_preflight_permissively(isolated_config):
    """The header only defends if a cross-origin preflight fails: nothing may grant one."""
    with TestClient(app) as client:
        resp = client.options(
            DELETE_ITEM,
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": REQUIRED_HEADER,
            },
        )
        assert "access-control-allow-origin" not in resp.headers
        assert "access-control-allow-headers" not in resp.headers


# --- 2. Host allow-list ---------------------------------------------------------------------


def test_any_host_is_accepted_when_the_allow_list_is_unset(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", None)
    with TestClient(app, base_url="http://rebound.evil.example") as client:
        assert client.get("/api/files").status_code == 200
        session = client.get("/api/auth/session").json()
        assert session["allowed_hosts_set"] is False


def test_foreign_host_is_refused_when_the_allow_list_is_set(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "lftpweb.home.arpa,192.168.1.10:8087")
    with TestClient(app, base_url="http://rebound.evil.example") as client:
        for path in ("/api/files", "/api/health", "/", "/settings"):
            resp = client.get(path)
            assert resp.status_code == 403, path
            assert netguard.ENV_VAR in resp.json()["detail"]
        assert client.post(DELETE_ITEM).status_code == 403


def test_listed_host_is_accepted_and_reports_the_flag(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "lftpweb.home.arpa,192.168.1.10:8087")
    with TestClient(app, base_url="http://LFTPWEB.home.arpa:8087") as client:
        assert client.get("/api/files").status_code == 200
        assert client.get("/api/auth/session").json()["allowed_hosts_set"] is True
    with TestClient(app, base_url="http://192.168.1.10:8087") as client:
        assert client.get("/api/files").status_code == 200
    with TestClient(app, base_url="http://192.168.1.10:9999") as client:
        assert client.get("/api/files").status_code == 403


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8087", "http://localhost:8087"])
def test_loopback_host_is_always_accepted(isolated_config, monkeypatch, base_url):
    """The container HEALTHCHECK is `curl http://127.0.0.1:8087/api/health`."""
    monkeypatch.setattr(app_settings, "allowed_hosts", "lftpweb.home.arpa")
    with TestClient(app, base_url=base_url) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/files").status_code == 200
    # TestClient can't take an IPv6 base_url; the rule itself is what matters.
    assert netguard.parse_allowed_hosts("lftpweb.home.arpa").allows("[::1]:8087")


def test_wildcard_entry_matches_subdomains_only(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "*.example.com")
    with TestClient(app, base_url="http://lftpweb.lan.example.com") as client:
        assert client.get("/api/files").status_code == 200
    with TestClient(app, base_url="http://example.com") as client:
        assert client.get("/api/files").status_code == 403
    with TestClient(app, base_url="http://evilexample.com") as client:
        assert client.get("/api/files").status_code == 403


def test_allow_list_parsing():
    parsed = netguard.parse_allowed_hosts(
        " A.example , [fd00::1]:8087,10.0.0.2, *.lan.example, bad*entry, host:notaport ,"
    )
    assert parsed.enforced
    assert parsed.invalid == ("bad*entry", "host:notaport")
    assert parsed.allows("a.example:1234")
    assert parsed.allows("a.example.")  # trailing-dot FQDN
    assert parsed.allows("[fd00::1]:8087")
    assert not parsed.allows("[fd00::1]:8088")
    assert parsed.allows("10.0.0.2")
    assert parsed.allows("x.lan.example")
    assert not parsed.allows(None)
    assert not parsed.allows("")
    assert not netguard.parse_allowed_hosts(None).enforced
    assert not netguard.parse_allowed_hosts("  ").enforced
    # Set but entirely invalid still enforces: only loopback gets in, with a 403 naming the
    # env var for everyone else -- never silently "allow all".
    only_bad = netguard.parse_allowed_hosts("*")
    assert only_bad.enforced and not only_bad.allows("anything") and only_bad.allows("localhost")


def test_startup_warns_in_none_mode_when_unset(caplog):
    with caplog.at_level(logging.WARNING, logger="lftpweb.core.netguard"):
        netguard.log_startup_posture(None, "none")
    assert any("DNS-rebinding protection is off" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lftpweb.core.netguard"):
        netguard.log_startup_posture(None, "password")
        netguard.log_startup_posture("lftpweb.home.arpa", "none")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- 3. WebSocket Origin --------------------------------------------------------------------


def _ws_code(client, headers) -> int | None:
    try:
        with client.websocket_connect("/api/ws", headers=headers):
            return None
    except WebSocketDisconnect as exc:
        return exc.code


def test_ws_unset_host_rewriting_proxy_shape_connects(isolated_config, monkeypatch):
    """Mismatched Origin, no Sec-Fetch-Site (a Host-rewriting proxy): must keep working."""
    monkeypatch.setattr(app_settings, "allowed_hosts", None)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/ws", headers={"Origin": "https://lftpweb.example"}
        ) as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_ws_unset_same_origin_fetch_metadata_connects(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", None)
    with TestClient(app) as client:
        headers = {"Origin": "https://lftpweb.example", "Sec-Fetch-Site": "same-origin"}
        with client.websocket_connect("/api/ws", headers=headers) as ws:
            assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_ws_unset_with_cross_site_fetch_metadata_is_refused(isolated_config, monkeypatch, site):
    monkeypatch.setattr(app_settings, "allowed_hosts", None)
    with TestClient(app) as client:
        headers = {"Origin": "http://evil.example", "Sec-Fetch-Site": site}
        assert _ws_code(client, headers) == 4403


def test_ws_unset_warns_once_per_distinct_origin_on_mismatch(isolated_config, monkeypatch):
    """(Spies on the logger: app startup reconfigures logging, which drops caplog's handler.)"""
    from lftpweb import middleware

    monkeypatch.setattr(app_settings, "allowed_hosts", None)
    warnings: list[tuple] = []
    monkeypatch.setattr(middleware.logger, "warning", lambda *a, **k: warnings.append(a))
    with TestClient(app) as client:
        for _ in range(2):
            assert _ws_code(client, {"Origin": "https://proxy-name.example"}) is None
        assert _ws_code(client, {"Origin": "https://other-name.example"}) is None
    assert sum("https://proxy-name.example" in w for w in warnings) == 1
    assert len(warnings) == 2


def test_ws_set_with_a_foreign_origin_is_refused(isolated_config, monkeypatch):
    monkeypatch.setattr(app_settings, "allowed_hosts", "testserver")
    with TestClient(app) as client:
        assert _ws_code(client, {"Origin": "http://evil.example"}) == 4403


def test_ws_set_with_a_matching_origin_is_accepted(isolated_config, monkeypatch):
    # TestClient's WebSocket handshake always carries `Host: testserver`.
    monkeypatch.setattr(app_settings, "allowed_hosts", "testserver")
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws", headers={"Origin": "http://testserver"}) as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_ws_without_origin_is_accepted(isolated_config):
    """A non-browser client: no Origin at all (browsers always send one)."""
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_origin_matching_rules():
    unset = netguard.parse_allowed_hosts(None)
    ok = netguard.origin_allowed
    assert ok("https://lftpweb.example", "lftpweb.example", None, unset)
    assert ok("https://lftpweb.example", "lftpweb.example:443", None, unset)
    assert ok("http://10.0.0.2:8087", "10.0.0.2:8087", None, unset)
    assert not ok("http://10.0.0.2:8088", "10.0.0.2:8087", None, unset)
    assert not ok("http://evil.example", "10.0.0.2:8087", None, unset)
    assert not ok("null", "10.0.0.2:8087", None, unset)
    assert not ok("file://", "10.0.0.2:8087", None, unset)
    # A reverse proxy (or the Vite dev proxy) that rewrites Host but forwards the original.
    assert ok("https://lftpweb.example", "lftpweb:8087", "lftpweb.example", unset)
    assert not ok("https://evil.example", "lftpweb:8087", "lftpweb.example", unset)
    # ...or, when the allow-list is set, a listed name.
    listed = netguard.parse_allowed_hosts("lftpweb.example,lftpweb:8087")
    assert ok("https://lftpweb.example", "lftpweb:8087", None, listed)
    assert not ok("https://evil.example", "lftpweb:8087", None, listed)


# --- 5. HostIn.address at the API ------------------------------------------------------------


def _host_body(address: str) -> dict:
    return {
        "name": "seedbox",
        "address": address,
        "port": 22,
        "username": "u",
        "auth_method": "password",
        "password": "p",
        "known_hosts_policy": "insecure",
    }


@pytest.mark.parametrize(
    "address", ["-oProxyCommand=id", "seed box.example", "host;id", "'quoted'", ""]
)
def test_unsafe_host_address_is_a_422(isolated_config, address):
    with TestClient(app) as client:
        resp = client.put("/api/settings/host", json=_host_body(address))
        assert resp.status_code == 422, resp.text
        assert "address" in resp.text


# A bracketed `[fd00::1]` is refused, as C1's spawn-time rule already refused it.
@pytest.mark.parametrize("address", ["seedbox.example.com", "10.0.0.2", "fd00::1", "fe80::1%eth0"])
def test_ordinary_host_address_is_accepted(isolated_config, address):
    with TestClient(app) as client:
        resp = client.put("/api/settings/host", json=_host_body(address))
        assert resp.status_code == 200, resp.text
