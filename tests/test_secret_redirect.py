"""Audit batch C4 (F6): a stored write-only secret is never sent to a changed endpoint.

For each of the three places a stored secret was reused (download-client update/test-on-save,
*arr instance update, seedbox host save/test) the same four cases:

* identity changed, no new secret -> 422 and **no outbound call** (recorded fakes);
* identity changed, new secret    -> works, the new secret is stored;
* identity unchanged, blank       -> works, the stored secret is used (today's behaviour);
* trivial edit (slash, case, default port, whitespace), blank -> works, same as unchanged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lftpweb.core.crypto import decrypt_secret
from lftpweb.core.endpoint_identity import ssh_identity, url_identity
from lftpweb.core.clients.models import ConnectionInfo
from lftpweb.main import app

# --- the shared helper -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://sab.example:8080", "http://sab.example:8080/"),
        ("http://sab.example:8080", "  http://SAB.Example:8080  "),
        ("http://sab.example", "http://sab.example:80"),
        ("https://sab.example/", "HTTPS://sab.example:443"),
        ("http://sab.example/sabnzbd", "http://sab.example/sabnzbd///"),
    ],
)
def test_url_identity_ignores_trivial_edits(a, b):
    assert url_identity(a) == url_identity(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://sab.example", "http://evil.example"),
        ("http://sab.example", "https://sab.example"),
        ("http://sab.example", "http://sab.example:8080"),
        ("http://sab.example/sabnzbd", "http://sab.example/other"),
        ("http://sab.example", "http://user:pw@sab.example"),
        ("http://sab.example", "http://sab.example/"[:-1] + "?x=1"),
    ],
)
def test_url_identity_sees_real_changes(a, b):
    assert url_identity(a) != url_identity(b)


def test_url_identity_rpc_path_counts_and_unparseable_is_deterministic():
    assert url_identity("http://h", "/RPC2") == url_identity("http://h/", "RPC2/")
    assert url_identity("http://h", "/RPC2") != url_identity("http://h", "/other")
    assert url_identity("not a url") == url_identity(" not a url/ ")
    assert url_identity("not a url") != url_identity("another")
    assert url_identity("http://h:notaport") == url_identity("http://h:notaport/")


def test_ssh_identity():
    assert ssh_identity(" Box.Example ", 22, " u ") == ssh_identity("box.example", 22, "u")
    assert ssh_identity("box", 22, "u") != ssh_identity("box", 2222, "u")
    assert ssh_identity("box", 22, "u") != ssh_identity("box", 22, "U")
    assert ssh_identity("box", 22, "u") != ssh_identity("evil", 22, "u")


# --- download clients ------------------------------------------------------------------------


@pytest.fixture
def probe_calls(monkeypatch):
    """Record every connection test the update path would make (config as sent)."""
    calls: list[dict] = []

    async def fake_test(client_class, config):
        calls.append(dict(config))
        return ConnectionInfo(version="1.0")

    monkeypatch.setattr("lftpweb.api.settings_clients._test_submitted_config_or_raise", fake_test)
    return calls


def _sab(base_url="http://sab.example:8080", api_key="key-one", **kw):
    config = {"base_url": base_url}
    if api_key is not None:
        config["api_key"] = api_key
    body = {"name": "SAB", "client_type": "sabnzbd", "config": config, "enabled": True}
    body.update(kw)
    return body


def _stored_client_secret(config_dir, client_id):
    raw = sqlite3.connect(str(Path(config_dir) / "lftpweb.db"))
    try:
        (enc,) = raw.execute(
            "SELECT secret_enc FROM download_client WHERE id = ?", (client_id,)
        ).fetchone()
    finally:
        raw.close()
    return decrypt_secret(str(config_dir), enc)


def _make_client(client):
    resp = client.post("/api/settings/clients", json=_sab(enabled=False))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_client_endpoint_change_without_secret_is_refused_and_never_contacted(
    isolated_config, probe_calls
):
    with TestClient(app) as client:
        cid = _make_client(client)
        for enabled in (True, False):
            resp = client.put(
                f"/api/settings/clients/{cid}",
                json=_sab(base_url="http://evil.example", api_key=None, enabled=enabled),
            )
            assert resp.status_code == 422, resp.text
            assert "api_key" in resp.text and "re-enter" in resp.text
        assert probe_calls == []
        assert "key-one" in _stored_client_secret(isolated_config, cid)
        assert client.get("/api/settings/clients").json()[0]["config"]["base_url"] == (
            "http://sab.example:8080"
        )


def test_client_endpoint_change_with_secret_works_and_stores_it(isolated_config, probe_calls):
    with TestClient(app) as client:
        cid = _make_client(client)
        resp = client.put(
            f"/api/settings/clients/{cid}",
            json=_sab(base_url="http://other.example", api_key="key-two"),
        )
        assert resp.status_code == 200, resp.text
        assert probe_calls == [{"base_url": "http://other.example", "api_key": "key-two"}]
        assert "key-two" in _stored_client_secret(isolated_config, cid)


def test_client_unchanged_endpoint_blank_secret_uses_the_stored_one(isolated_config, probe_calls):
    with TestClient(app) as client:
        cid = _make_client(client)
        resp = client.put(f"/api/settings/clients/{cid}", json=_sab(api_key=None, name="Renamed"))
        assert resp.status_code == 200, resp.text
        assert probe_calls == [{"base_url": "http://sab.example:8080", "api_key": "key-one"}]


@pytest.mark.parametrize(
    "url",
    [
        "http://sab.example:8080/",
        "  http://SAB.EXAMPLE:8080  ",
    ],
)
def test_client_trivial_edit_blank_secret_still_works(isolated_config, probe_calls, url):
    with TestClient(app) as client:
        cid = _make_client(client)
        resp = client.put(f"/api/settings/clients/{cid}", json=_sab(base_url=url, api_key=None))
        assert resp.status_code == 200, resp.text
        assert probe_calls[0]["api_key"] == "key-one"


def test_client_default_port_edit_is_trivial(isolated_config, probe_calls):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients", json=_sab(base_url="http://sab.example", enabled=False)
        )
        cid = resp.json()["id"]
        resp = client.put(
            f"/api/settings/clients/{cid}",
            json=_sab(base_url="http://sab.example:80", api_key=None),
        )
        assert resp.status_code == 200, resp.text


def test_client_rtorrent_rpc_path_change_needs_the_password(isolated_config, probe_calls):
    body = {
        "name": "rt",
        "client_type": "rtorrent",
        "config": {"base_url": "http://rt.example", "username": "u", "password": "pw1"},
        "enabled": False,
    }
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=body)
        assert resp.status_code == 201, resp.text
        cid = resp.json()["id"]
        # An explicit default rpc_path is trivial.
        same = {**body, "config": {"base_url": "http://rt.example", "rpc_path": "/RPC2"}}
        assert client.put(f"/api/settings/clients/{cid}", json=same).status_code == 200
        moved = {**body, "config": {"base_url": "http://rt.example", "rpc_path": "/evil"}}
        resp = client.put(f"/api/settings/clients/{cid}", json=moved)
        assert resp.status_code == 422, resp.text
        assert "Password" in resp.text


def test_client_type_change_without_secret_is_refused(isolated_config, probe_calls):
    with TestClient(app) as client:
        cid = _make_client(client)
        body = {
            "name": "x",
            "client_type": "rtorrent",
            "config": {"base_url": "http://sab.example:8080"},
            "enabled": True,
        }
        assert client.put(f"/api/settings/clients/{cid}", json=body).status_code == 422
        assert probe_calls == []


def test_client_test_endpoint_only_ever_uses_the_stored_endpoint(isolated_config):
    """`POST /clients/{id}/test` takes no body: the stored secret can only go to the stored URL,
    so there is nothing to redirect (audit C4: checked, not changed).
    """
    with TestClient(app) as client:
        cid = _make_client(client)
        # No request body model exists; extra JSON is ignored, not used.
        resp = client.post(
            f"/api/settings/clients/{cid}/test", json={"config": {"base_url": "http://evil"}}
        )
        assert resp.status_code == 200
        assert "evil" not in resp.text


# --- *arr ------------------------------------------------------------------------------------


def _arr(base_url="http://sonarr.example:8989", api_key="arr-key-one", **kw):
    body = {
        "name": "Sonarr",
        "kind": "sonarr",
        "base_url": base_url,
        "api_key": api_key,
        "enabled": True,
        "notify_on_complete": False,
    }
    body.update(kw)
    return body


def _stored_arr_key(config_dir, iid):
    raw = sqlite3.connect(str(Path(config_dir) / "lftpweb.db"))
    try:
        (enc,) = raw.execute("SELECT api_key_enc FROM arr_instance WHERE id = ?", (iid,)).fetchone()
    finally:
        raw.close()
    return decrypt_secret(str(config_dir), enc)


def test_arr_url_change_without_key_is_refused(isolated_config):
    with TestClient(app) as client:
        iid = client.post("/api/settings/arr", json=_arr()).json()["id"]
        resp = client.put(
            f"/api/settings/arr/{iid}", json=_arr(base_url="http://evil.example", api_key=None)
        )
        assert resp.status_code == 422, resp.text
        assert "re-enter the API key" in resp.text
        assert _stored_arr_key(isolated_config, iid) == "arr-key-one"
        assert client.get("/api/settings/arr").json()[0]["base_url"] == (
            "http://sonarr.example:8989"
        )


def test_arr_url_change_with_key_works_and_stores_it(isolated_config):
    with TestClient(app) as client:
        iid = client.post("/api/settings/arr", json=_arr()).json()["id"]
        resp = client.put(
            f"/api/settings/arr/{iid}",
            json=_arr(base_url="http://new.example", api_key="arr-key-two"),
        )
        assert resp.status_code == 200, resp.text
        assert _stored_arr_key(isolated_config, iid) == "arr-key-two"


@pytest.mark.parametrize(
    "url",
    [
        "http://sonarr.example:8989",
        "http://sonarr.example:8989/",
        "  HTTP://Sonarr.Example:8989 ",
    ],
)
def test_arr_unchanged_or_trivial_edit_keeps_the_stored_key(isolated_config, url):
    with TestClient(app) as client:
        iid = client.post("/api/settings/arr", json=_arr()).json()["id"]
        resp = client.put(
            f"/api/settings/arr/{iid}", json=_arr(base_url=url, api_key=None, name="Renamed")
        )
        assert resp.status_code == 200, resp.text
        assert _stored_arr_key(isolated_config, iid) == "arr-key-one"


def test_arr_default_port_edit_is_trivial(isolated_config):
    with TestClient(app) as client:
        iid = client.post("/api/settings/arr", json=_arr(base_url="http://s.example")).json()["id"]
        resp = client.put(
            f"/api/settings/arr/{iid}", json=_arr(base_url="http://s.example:80", api_key=None)
        )
        assert resp.status_code == 200, resp.text


def test_arr_test_endpoint_only_uses_the_stored_endpoint(isolated_config):
    """`POST /arr/{id}/test` takes no override body (audit C4: checked, no flaw there)."""
    with TestClient(app) as client:
        iid = client.post("/api/settings/arr", json=_arr(base_url="http://x.invalid")).json()["id"]
        resp = client.post(f"/api/settings/arr/{iid}/test", json={"base_url": "http://evil.test"})
        assert resp.status_code == 200
        assert "evil" not in resp.text


# --- seedbox host ----------------------------------------------------------------------------


def _host(address="box.example", port=22, username="seeduser", password="pw-one", **kw):
    body = {
        "name": "seedbox",
        "address": address,
        "port": port,
        "username": username,
        "auth_method": "password",
        "known_hosts_policy": "insecure",
    }
    if password is not None:
        body["password"] = password
    body.update(kw)
    return body


def _stored_host_password(config_dir):
    raw = sqlite3.connect(str(Path(config_dir) / "lftpweb.db"))
    try:
        (enc,) = raw.execute("SELECT password_enc FROM host").fetchone()
    finally:
        raw.close()
    return decrypt_secret(str(config_dir), enc)


@pytest.mark.parametrize(
    "changes",
    [{"address": "evil.example"}, {"port": 2222}, {"username": "other"}],
)
def test_host_save_endpoint_change_without_password_is_refused(isolated_config, changes):
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        resp = client.put("/api/settings/host", json=_host(password=None, **changes))
        assert resp.status_code == 422, resp.text
        assert "re-enter the password" in resp.text
        assert _stored_host_password(isolated_config) == "pw-one"
        assert client.get("/api/settings/host").json()["address"] == "box.example"


def test_host_save_endpoint_change_with_password_works(isolated_config):
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        resp = client.put(
            "/api/settings/host", json=_host(address="new.example", password="pw-two")
        )
        assert resp.status_code == 200, resp.text
        assert _stored_host_password(isolated_config) == "pw-two"


def test_host_save_unchanged_or_trivial_blank_password_keeps_it(isolated_config):
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        for address in ("box.example", "BOX.Example"):
            resp = client.put("/api/settings/host", json=_host(address=address, password=None))
            assert resp.status_code == 200, resp.text
        assert _stored_host_password(isolated_config) == "pw-one"


def test_host_save_key_auth_is_not_affected(isolated_config):
    """Key auth sends no secret to the host, so an address change needs nothing extra."""
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        resp = client.put(
            "/api/settings/host",
            json=_host(address="new.example", password=None, auth_method="key", key_path="/k"),
        )
        assert resp.status_code == 200, resp.text


@pytest.fixture
def pool_calls(monkeypatch):
    calls: list = []

    class _Result:
        ok = True
        error_class = None
        message = "ok"

    def install(client):
        async def fake(host_config):
            calls.append(host_config)
            return _Result()

        monkeypatch.setattr(client.app.state.engine.pool, "test_connection", fake)

    return calls, install


@pytest.mark.parametrize(
    "override",
    [{"address": "evil.example"}, {"port": 2222}, {"username": "other"}],
)
def test_host_test_endpoint_change_without_password_is_refused_and_never_connects(
    isolated_config, pool_calls, override
):
    calls, install = pool_calls
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        install(client)
        resp = client.post("/api/settings/host/test", json=override)
        assert resp.status_code == 422, resp.text
        assert "re-enter the password" in resp.text
        assert calls == []


def test_host_test_endpoint_change_with_password_connects_with_it(isolated_config, pool_calls):
    calls, install = pool_calls
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        install(client)
        resp = client.post(
            "/api/settings/host/test", json={"address": "new.example", "password": "pw-two"}
        )
        assert resp.status_code == 200, resp.text
        assert [c.password for c in calls] == ["pw-two"]


@pytest.mark.parametrize(
    "override", [None, {}, {"address": "BOX.example "}, {"port": 22}, {"username": "seeduser"}]
)
def test_host_test_unchanged_or_trivial_uses_the_stored_password(
    isolated_config, pool_calls, override
):
    calls, install = pool_calls
    with TestClient(app) as client:
        assert client.put("/api/settings/host", json=_host()).status_code == 200
        install(client)
        resp = client.post("/api/settings/host/test", json=override)
        assert resp.status_code == 200, resp.text
        assert [c.password for c in calls] == ["pw-one"]
