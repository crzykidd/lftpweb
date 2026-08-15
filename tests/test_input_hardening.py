"""Audit S3 (input length caps + port bounds) and S4 (safe security response headers).

S3 closes an argon2/body-size DoS surface: an unauthenticated login could hand the server an
arbitrarily large password to hash. The caps are generous enough never to reject a real value,
so these tests assert only that clearly-absurd inputs are refused and a normal one still passes.
S4 asserts the three static defense-in-depth headers are present on responses (and that no CSP
is set -- that was deliberately deferred, so a future accidental CSP here should trip a test).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from lftpweb.main import app
from lftpweb.models import MAX_PORT, MAX_SECRET_LEN


def test_login_rejects_an_oversized_password_without_hashing_it(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "u", "password": "x" * (MAX_SECRET_LEN + 1)},
        )
        # 422 from validation, never a 401 (which would mean it was hashed and compared).
        assert resp.status_code == 422


def test_host_rejects_a_port_out_of_range(isolated_config):
    with TestClient(app) as client:
        for bad_port in (0, 70000, -1):
            resp = client.put(
                "/api/settings/host",
                json={
                    "name": "seedbox",
                    "address": "example.com",
                    "port": bad_port,
                    "username": "u",
                    "auth_method": "agent",
                },
            )
            assert resp.status_code == 422, f"port {bad_port} should be rejected"


def test_host_accepts_a_normal_port_and_name(isolated_config):
    with TestClient(app) as client:
        resp = client.put(
            "/api/settings/host",
            json={
                "name": "seedbox",
                "address": "example.com",
                "port": MAX_PORT,
                "username": "u",
                "auth_method": "agent",
            },
        )
        assert resp.status_code == 200


def test_security_headers_present_on_a_response(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
        assert resp.headers.get("referrer-policy") == "same-origin"
        # CSP/HSTS were deliberately NOT set (unverifiable without a browser). If a future
        # change adds one here, that should be a conscious, reviewed decision -- flag it.
        assert "content-security-policy" not in resp.headers
        assert "strict-transport-security" not in resp.headers
