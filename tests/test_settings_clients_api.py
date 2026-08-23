"""`api/settings_clients.py` (migration 027, docs/download-client-framework-spec.md, stage 1b of
#18): download-client instance CRUD, `GET /api/settings/client-types`, base-path save-time
validation (spec §8.2), and test-connection with the probed capability layer (spec §4.1) and the
redacted capture (spec §13.3). Same `TestClient` + `isolated_config` idiom as
`tests/test_settings_arr_api.py`, the shape this module mirrors.

A tiny test-only connector (`_ProbeClient`, registered as `"test-probe"`) drives the capability
degrade/no-degrade/reset assertions directly -- `SabnzbdClient.test_connection` never itself
raises `CapabilityUnavailable` (only `list_trackers`/`recheck` do, statically, spec §5), so a
connector whose failure mode is driven straight from its own `config` dict is what actually lets
a test exercise "only `CapabilityUnavailable` degrades, and exactly one key" against a real
`DownloadClient` subclass rather than by calling `core.clients.base.degrade_from_error` in
isolation (already covered by `tests/test_clients_framework.py`). Registered in `tests/`, not in
`core/clients/`, the same "test-only scaffolding never reaches the production registry"
convention `tests/fake_client.py` documents for `"fake"`.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lftpweb.core.clients import USENET_BASELINE, ConfigField, DownloadClient, register_client
from lftpweb.core.clients.errors import CapabilityUnavailable, ClientError, ClientUnreachable
from lftpweb.core.clients.models import (
    BasePath,
    ConnectionInfo,
    RemoveOutcome,
    SpaceInfo,
    Transfer,
    TrackerInfo,
    TransferPhase,
)
from lftpweb.main import app

SEEDBOX_HOST = "127.0.0.1"
SEEDBOX_PORT = 2222
SEEDBOX_USER = "seeduser"
SEEDBOX_PASSWORD = "testpass123"  # noqa: S105 - fake seedbox fixture credential, never real


def _seedbox_reachable() -> bool:
    try:
        with socket.create_connection((SEEDBOX_HOST, SEEDBOX_PORT), timeout=1.0):
            return True
    except OSError:
        return False


SEEDBOX_UP = _seedbox_reachable()


def _put_unreachable_host(client: TestClient) -> None:
    """`example.invalid` -- never reachable, proves the best-effort allow (same convention as
    `tests/test_settings_queues_path_validation.py`).
    """
    resp = client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "example.invalid",
            "username": SEEDBOX_USER,
            "auth_method": "password",
            "password": "hunter2",
        },
    )
    assert resp.status_code == 200, resp.text


def _put_live_seedbox_host(client: TestClient) -> None:
    resp = client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": SEEDBOX_HOST,
            "port": SEEDBOX_PORT,
            "username": SEEDBOX_USER,
            "auth_method": "password",
            "password": SEEDBOX_PASSWORD,
            "known_hosts_policy": "insecure",
        },
    )
    assert resp.status_code == 200, resp.text


def _sab_body(**overrides):
    """Defaults `enabled` to `False` -- most of this module's own tests use a `base_url` that
    was never meant to be dialled (`sabnzbd.example.invalid`, or a live seedbox host with no
    real SABnzbd behind it), and §3a's save-on-test now means an *enabled* save against a
    config like that would be rejected outright. Tests that actually want the enabled +
    connectivity-tested path pass `enabled=True` explicitly against a fixture that really
    answers (`fake_sabnzbd_server`) or a controllable test-only connector (`_ProbeClient`,
    below).
    """
    body = {
        "name": "SABnzbd",
        "client_type": "sabnzbd",
        "config": {"base_url": "http://sabnzbd.example.invalid:8080", "api_key": "hunter2-key"},
        "enabled": False,
    }
    body.update(overrides)
    return body


# --- Poll status (migration 029, finding #2, 2026-08-23) -------------------------------------


def test_a_freshly_created_instance_reports_never_polled(isolated_config):
    """`last_poll_at`/`last_poll_ok`/`last_poll_message`/`last_success_at` all `None` -- the
    poller (`core.clientsync.ClientSyncScheduler`) is what ever sets these, never this endpoint;
    a fresh instance must not render as falsely "healthy" before its first pass.
    """
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body())
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["last_poll_at"] is None
        assert created["last_poll_ok"] is None
        assert created["last_poll_message"] is None
        assert created["last_success_at"] is None


# --- CRUD round trip -----------------------------------------------------------------------


def test_create_list_update_delete_round_trip(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body())
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["has_secret"] is True
        assert created["config"] == {"base_url": "http://sabnzbd.example.invalid:8080"}
        assert "api_key" not in resp.text
        assert "hunter2-key" not in resp.text
        client_id = created["id"]

        resp = client.get("/api/settings/clients")
        assert resp.status_code == 200
        assert [c["id"] for c in resp.json()] == [client_id]
        assert "hunter2-key" not in resp.text

        # Update omitting the secret -- must keep it, never require resending it.
        resp = client.put(
            f"/api/settings/clients/{client_id}",
            json=_sab_body(
                name="SABnzbd Renamed",
                config={"base_url": "http://sabnzbd.example.invalid:8080"},
            ),
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["name"] == "SABnzbd Renamed"
        assert updated["has_secret"] is True

        resp = client.delete(f"/api/settings/clients/{client_id}")
        assert resp.status_code == 204
        resp = client.get("/api/settings/clients")
        assert resp.json() == []


def test_create_requires_declared_required_fields(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(config={"base_url": "http://sabnzbd.example.invalid:8080"}),
        )
        assert resp.status_code == 422
        assert "api_key" in resp.text


def test_create_rejects_unknown_client_type(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body(client_type="not-a-thing"))
        assert resp.status_code == 400
        assert "not-a-thing" in resp.text


def test_update_requires_existing_instance(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/clients/999", json=_sab_body())
        assert resp.status_code == 404


def test_delete_requires_existing_instance(isolated_config):
    with TestClient(app) as client:
        resp = client.delete("/api/settings/clients/999")
        assert resp.status_code == 404


def test_secret_encrypted_at_rest_and_never_echoed(isolated_config):
    plaintext = "hunter2-real-sabnzbd-key"
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(config={"base_url": "http://x.invalid", "api_key": plaintext}),
        )
        assert resp.status_code == 201
        assert plaintext not in resp.text
        resp = client.get("/api/settings/clients")
        assert plaintext not in resp.text

    db_path = Path(isolated_config) / "lftpweb.db"
    raw = sqlite3.connect(str(db_path))
    try:
        (stored,) = raw.execute("SELECT secret_enc FROM download_client").fetchone()
    finally:
        raw.close()
    assert stored != plaintext
    assert plaintext not in stored


def test_delete_cascades_base_paths_and_categories(isolated_config):
    with TestClient(app) as client:
        _put_unreachable_host(client)
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()},
        )
        queue_id = resp.json()["id"]

        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(
                base_paths=[{"path": "/downloads/complete"}],
                categories=[{"category": "tv", "queue_id": queue_id}],
            ),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert len(body["base_paths"]) == 1
        assert len(body["categories"]) == 1
        client_id = body["id"]

        # The delete itself, through the real app connection -- `core/db.py.connect()` sets
        # `PRAGMA foreign_keys = ON` for it, which is what actually makes `ON DELETE CASCADE`
        # fire (a bare `sqlite3.connect()` opened after the fact would not enforce it and would
        # give this test a false pass).
        resp = client.delete(f"/api/settings/clients/{client_id}")
        assert resp.status_code == 204

    db_path = Path(isolated_config) / "lftpweb.db"
    raw = sqlite3.connect(str(db_path))
    try:
        (bp_count,) = raw.execute(
            "SELECT COUNT(*) FROM download_client_base_path WHERE client_id = ?", (client_id,)
        ).fetchone()
        (cat_count,) = raw.execute(
            "SELECT COUNT(*) FROM download_client_category WHERE client_id = ?", (client_id,)
        ).fetchone()
    finally:
        raw.close()
    assert bp_count == 0
    assert cat_count == 0


def test_category_rejects_a_nonexistent_queue_id(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(categories=[{"category": "tv", "queue_id": 999}]),
        )
        assert resp.status_code == 400
        assert "999" in resp.text


def test_unbound_category_survives_a_save_and_a_re_edit(isolated_config):
    """The #11b regression test from prompts/2026-08-23-category-binding-redesign.md: a category
    left unbound (`queue_id: None`) must be stored, not silently dropped, and must still be there
    on a later read -- the round trip, not merely a 2xx from the save. The old free-text control
    dropped exactly this shape of row because its client-side filter treated an unfilled row as
    "nothing to save"; the redesigned UI never produces a blank category string in the first
    place, but the backend contract this asserts is unchanged and is the actual guarantee.
    """
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(categories=[{"category": "movies", "queue_id": None}]),
        )
        assert resp.status_code == 201, resp.text
        client_id = resp.json()["id"]
        assert resp.json()["categories"] == [
            {
                "id": resp.json()["categories"][0]["id"],
                "category": "movies",
                "queue_id": None,
                "source": "client",
            }
        ]

        # Re-fetch (simulating a page reload / re-opening Edit) -- the row must still be there.
        reread = client.get("/api/settings/clients").json()[0]
        assert reread["id"] == client_id
        assert len(reread["categories"]) == 1
        assert reread["categories"][0]["category"] == "movies"
        assert reread["categories"][0]["queue_id"] is None

        # Saving again (the "re-edit" half of the round trip) with the same unbound row present
        # must not lose it either.
        resp = client.put(
            f"/api/settings/clients/{client_id}",
            json=_sab_body(categories=[{"category": "movies", "queue_id": None}]),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["categories"]) == 1
        assert resp.json()["categories"][0]["queue_id"] is None


# --- GET /api/settings/client-types ----------------------------------------------------------


def test_client_types_lists_sabnzbd_with_its_declared_schema(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/client-types")
        assert resp.status_code == 200
        by_type = {entry["client_type"]: entry for entry in resp.json()}
        assert "sabnzbd" in by_type
        sab = by_type["sabnzbd"]
        assert sab["family"] == "usenet"
        keys = {f["key"] for f in sab["config_schema"]}
        assert {"base_url", "api_key"}.issubset(keys)
        secret_fields = [f for f in sab["config_schema"] if f["kind"] == "secret"]
        assert any(f["key"] == "api_key" for f in secret_fields)


# --- Base-path save-time validation (spec §8.2) ----------------------------------------------


def test_create_accepts_a_bogus_base_path_when_the_host_is_unreachable(isolated_config):
    with TestClient(app) as client:
        _put_unreachable_host(client)
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(base_paths=[{"path": "/this/does/not/exist/anywhere"}]),
        )
        assert resp.status_code == 201, resp.text  # cannot verify -- not invalid


def test_create_accepts_a_bogus_base_path_when_no_host_is_configured(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(base_paths=[{"path": "/whatever"}]),
        )
        assert resp.status_code == 201, resp.text


_SEEDBOX_SKIP_REASON = (
    "fake seedbox not reachable on 127.0.0.1:2222 -- "
    "`docker compose -f docker-compose.test.yml up --build -d`"
)


@pytest.mark.skipif(not SEEDBOX_UP, reason=_SEEDBOX_SKIP_REASON)
def test_create_rejects_a_base_path_the_live_seedbox_reports_missing(isolated_config):
    with TestClient(app) as client:
        _put_live_seedbox_host(client)
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(base_paths=[{"path": "/data/pickup/does-not-exist-anywhere"}]),
        )
        assert resp.status_code == 400, resp.text
        assert "base_paths" in resp.text
        assert "does not exist" in resp.text


@pytest.mark.skipif(not SEEDBOX_UP, reason=_SEEDBOX_SKIP_REASON)
def test_create_accepts_a_base_path_that_really_exists_on_the_live_seedbox(isolated_config):
    with TestClient(app) as client:
        _put_live_seedbox_host(client)
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(base_paths=[{"path": "/data/pickup"}]),
        )
        assert resp.status_code == 201, resp.text


# --- Test-connection against the real SABnzbd connector + fake server ------------------------


def _sab_body_for(server) -> dict:
    return _sab_body(config={"base_url": server.base_url, "api_key": server.state.api_key})


def test_test_connection_success_persists_capabilities_and_version(
    isolated_config, fake_sabnzbd_server
):
    fake_sabnzbd_server.state.version = "4.3.1-test"
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]
        assert resp.json()["capabilities"] is None  # never probed yet

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["version"] == "4.3.1-test"
        assert body["capabilities"]["operations"]["test_connection"]["support"] == "native"
        assert body["capabilities"]["fields"]["ratio"]["support"] == "none"  # usenet baseline

        resp = client.get("/api/settings/clients")
        stored = resp.json()[0]
        assert stored["version"] == "4.3.1-test"
        assert stored["capabilities"] is not None
        assert stored["capabilities_probed_at"] is not None


def test_test_connection_failure_leaves_a_previously_persisted_set_intact(
    isolated_config, fake_sabnzbd_server
):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.json()["ok"] is True
        first_caps = client.get("/api/settings/clients").json()[0]["capabilities"]

        # Now make the same stored config fail -- SABnzbd's own MEASURED "bad key" shape
        # (HTTP 403, `text/html`, body "API Key Incorrect", spec §13.4 #9) makes `sabnzbd.py`'s
        # `_get` raise `ClientAuthenticationFailed`, a `ClientError` subclass -- this endpoint's
        # own except-block ordering (`api/settings_clients.py`, out of scope for this
        # correction) reports any non-`ClientUnreachable`/`CapabilityUnavailable` `ClientError`
        # generically as `"ClientError"`, so that string is unchanged here even though the
        # underlying exception type is now more specific. Either way, neither is a
        # `CapabilityUnavailable`, so neither may ever degrade a capability.
        fake_sabnzbd_server.state.bad_api_key_mode = True
        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] == "ClientError"
        assert body["capabilities"] == first_caps  # untouched, not wiped

        stored = client.get("/api/settings/clients").json()[0]
        assert stored["capabilities"] == first_caps


def test_test_connection_reports_unreachable_for_a_closed_port(isolated_config):
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            # Port 1 -- reliably connection-refused on loopback, a genuine transport failure
            # rather than an application-level error, to prove `ClientUnreachable` specifically.
            json=_sab_body(config={"base_url": "http://127.0.0.1:1", "api_key": "whatever"}),
        )
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] == "ClientUnreachable"
        # Never probed before -- the static declaration is what "last known" falls back to,
        # and it must not have been persisted (still None on the stored row).
        assert body["capabilities"]["operations"]["test_connection"]["support"] == "native"

        stored = client.get("/api/settings/clients").json()[0]
        assert stored["capabilities"] is None
        assert stored["capabilities_probed_at"] is None


def test_test_connection_writes_a_redacted_capture_with_no_secret_in_the_log(
    isolated_config, fake_sabnzbd_server
):
    """spec §13.3: redaction happens at the point of capture, never later. The fake server is
    put in `echo_key_in_version_body` mode so the API key rides **both** the request URL (every
    real SABnzbd call, `apikey=...`) and the response body -- proving the connector's capture
    doesn't merely get lucky because a real vendor response never echoes it back.

    Asserts on the **actual on-disk log file** (`core/logsetup.py`'s rotating file handler)
    rather than `caplog`: `logsetup.setup_logging` -- invoked by the app's own lifespan inside
    `TestClient(app)`'s startup, same as a real deployment -- does `root.handlers.clear()`,
    which tears down pytest's own `caplog` handler on the root logger the instant the app
    starts. Reading the file this task's own capture is meant to land in is both more robust
    and more faithful to "does not reach the log" than fighting that teardown would be.

    `logger.debug(...)` calls are gated by this specific logger's own effective level, not
    `root`'s (`logging.Logger.isEnabledFor` walks its own ancestry once, independent of any
    later `root.setLevel` call `setup_logging` makes) -- raising just this one logger's level
    before the app starts is what makes the capture line actually get emitted at all, without
    needing `LFTPWEB_LOG_LEVEL=DEBUG` for the whole app.
    """
    fake_sabnzbd_server.state.echo_key_in_version_body = True
    sab_logger = logging.getLogger("lftpweb.core.clients.sabnzbd")
    original_level = sab_logger.level
    sab_logger.setLevel(logging.DEBUG)
    try:
        with TestClient(app) as client:
            resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
            client_id = resp.json()["id"]

            resp = client.post(f"/api/settings/clients/{client_id}/test")
            assert resp.json()["ok"] is True
    finally:
        sab_logger.setLevel(original_level)

    log_path = Path(isolated_config) / "logs" / "lftpweb.log"
    log_text = log_path.read_text()
    api_key = fake_sabnzbd_server.state.api_key
    assert "sabnzbd test_connection response" in log_text
    assert api_key not in log_text
    assert "REDACTED" in log_text


# --- Capability degrade / no-degrade / reset, via a test-only connector ----------------------
#
# `_ProbeClient` is driven directly by `config["mode"]` -- the one channel this endpoint passes
# a connector's config through -- so a test can force exactly the failure this task's three
# capability rules care about, against a real `DownloadClient` subclass and the real
# `settings_clients.py` routing, not `core.clients.base.degrade_from_error` in isolation.


@register_client("test-probe")
class _ProbeClient(DownloadClient):
    family = "usenet"
    capabilities = USENET_BASELINE
    config_schema = (
        ConfigField(key="mode", label="Mode", kind="str", required=False, default="ok"),
    )

    def __init__(self, *, config) -> None:
        super().__init__(config=config)
        self._mode = config.get("mode", "ok")

    @staticmethod
    def map_phase(raw_status: str) -> TransferPhase:
        return TransferPhase.UNKNOWN

    async def test_connection(self) -> ConnectionInfo:
        if self._mode == "unreachable":
            raise ClientUnreachable("probe: unreachable")
        if self._mode == "capability_unavailable":
            raise CapabilityUnavailable("probe: test_connection unavailable")
        if self._mode == "client_error":
            raise ClientError("probe: generic failure")
        return ConnectionInfo(version="9.9.9-probe")

    async def list_transfers(self, *, active_only: bool = False) -> list[Transfer]:
        raise NotImplementedError

    async def list_history(self) -> list[Transfer]:
        raise NotImplementedError

    async def get_transfer(self, client_id: str) -> Transfer | None:
        raise NotImplementedError

    async def list_trackers(self, client_id: str) -> list[TrackerInfo]:
        raise NotImplementedError

    async def list_files(self, client_id: str) -> list[str]:
        raise NotImplementedError

    async def list_base_paths(self) -> list[BasePath]:
        raise NotImplementedError

    async def list_categories(self) -> list[str]:
        raise NotImplementedError

    async def free_space(self, path: str) -> SpaceInfo:
        raise NotImplementedError

    async def pause(self, client_id: str) -> None:
        raise NotImplementedError

    async def resume(self, client_id: str) -> None:
        raise NotImplementedError

    async def remove(self, client_id: str) -> RemoveOutcome:
        raise NotImplementedError

    async def set_label(self, client_id: str, label: str) -> None:
        raise NotImplementedError

    async def recheck(self, client_id: str) -> None:
        raise NotImplementedError


def _probe_body(mode: str, **overrides):
    body = {"name": "Probe", "client_type": "test-probe", "config": {"mode": mode}}
    body.update(overrides)
    return body


def test_capability_unavailable_degrades_exactly_one_key(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok"))
        client_id = resp.json()["id"]

        resp = client.put(
            f"/api/settings/clients/{client_id}",
            json=_probe_body("capability_unavailable"),
        )
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] == "CapabilityUnavailable"
        ops = body["capabilities"]["operations"]
        assert ops["test_connection"]["support"] == "none"
        # Every other key is untouched from the static baseline -- only the one key degrades.
        assert ops["list_transfers"]["support"] == "native"
        assert body["capabilities"]["fields"]["size_bytes"]["support"] == "native"


def test_client_unreachable_degrades_nothing_on_top_of_a_prior_degrade(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("capability_unavailable"))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        degraded = resp.json()["capabilities"]
        assert degraded["operations"]["test_connection"]["support"] == "none"

        resp = client.put(f"/api/settings/clients/{client_id}", json=_probe_body("unreachable"))
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        body = resp.json()
        assert body["ok"] is False
        assert body["error_class"] == "ClientUnreachable"
        # Exactly the previously-degraded set -- unchanged, not wiped, not un-degraded either.
        assert body["capabilities"] == degraded


def test_a_fresh_success_resets_capabilities_to_the_static_declaration(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("capability_unavailable"))
        client_id = resp.json()["id"]
        client.post(f"/api/settings/clients/{client_id}/test")

        resp = client.put(f"/api/settings/clients/{client_id}", json=_probe_body("ok"))
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        body = resp.json()
        assert body["ok"] is True
        # Layer 3 (runtime-degraded) is cleared by the next successful probe (spec §4.1) --
        # test_connection is back to native, not left at the prior degrade.
        assert body["capabilities"]["operations"]["test_connection"]["support"] == "native"


# --- Save-on-test (spec §3a, this task's own handoff prompt) ---------------------------------
#
# "we should test at save and not save if enabled and test failed -- this is how the *arr
# clients handle it": `enabled: true` tests the *submitted* config before persisting and
# refuses to save anything on failure; `enabled: false` never tests and always saves -- the
# deliberate escape hatch. `_ProbeClient` (above) drives the failure modes directly, the same
# way the capability tests above do.


def test_create_rejects_an_enabled_instance_whose_test_fails_and_persists_nothing(
    isolated_config,
):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("unreachable", enabled=True))
        assert 400 <= resp.status_code < 500, resp.text
        detail = resp.json()["detail"]
        assert detail["error_class"] == "ClientUnreachable"

    # Not merely a 4xx -- nothing was persisted at all, checked against the real database file,
    # not just the API's own (possibly-wrong) idea of what it did.
    db_path = Path(isolated_config) / "lftpweb.db"
    raw = sqlite3.connect(str(db_path))
    try:
        (count,) = raw.execute("SELECT COUNT(*) FROM download_client").fetchone()
    finally:
        raw.close()
    assert count == 0


def test_create_saves_a_disabled_instance_even_when_its_test_would_fail(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("unreachable", enabled=False))
        assert resp.status_code == 201, resp.text
        assert resp.json()["enabled"] is False
        # Never tested -- no capability/version readout from a save alone.
        assert resp.json()["capabilities"] is None


def test_create_success_populates_capabilities_and_version_without_a_separate_test_call(
    isolated_config,
):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok", enabled=True))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["version"] == "9.9.9-probe"
        assert body["capabilities"] is not None
        assert body["capabilities"]["operations"]["test_connection"]["support"] == "native"
        assert body["capabilities_probed_at"] is not None


def test_update_rejects_an_enabled_instance_whose_test_fails_and_leaves_the_row_untouched(
    isolated_config,
):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok", enabled=True))
        client_id = resp.json()["id"]
        before = client.get("/api/settings/clients").json()[0]

        resp = client.put(
            f"/api/settings/clients/{client_id}",
            json=_probe_body("unreachable", enabled=True, name="Renamed"),
        )
        assert 400 <= resp.status_code < 500, resp.text
        assert resp.json()["detail"]["error_class"] == "ClientUnreachable"

        after = client.get("/api/settings/clients").json()[0]
        assert after == before  # completely untouched, name included


def test_update_that_disables_a_previously_enabled_broken_instance_succeeds(isolated_config):
    """The escape hatch: an enabled instance that has since gone bad can still be edited and
    saved, as long as the edit disables it. Its previously-probed capabilities are left exactly
    as they were -- not tested again (disabled never tests), not cleared either.
    """
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok", enabled=True))
        client_id = resp.json()["id"]
        probed_before = client.get("/api/settings/clients").json()[0]["capabilities"]
        assert probed_before is not None

        resp = client.put(
            f"/api/settings/clients/{client_id}",
            json=_probe_body("unreachable", enabled=False),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is False
        assert body["capabilities"] == probed_before


# --- Base-path detection on test-connection (spec §8.2 correction, migration 028) -------------


def test_a_detection_failure_does_not_fail_the_connection_test(isolated_config):
    """`_ProbeClient` declares `Operation.LIST_BASE_PATHS` (it inherits `USENET_BASELINE`
    unmodified) but its own `list_base_paths` raises `NotImplementedError` -- reachability and
    detection are different questions (spec §4.2's temperament): `test_connection` (mode "ok")
    still succeeds, and detection simply contributes nothing rather than failing the test.
    (`tests/test_clients_detection.py` separately covers the other "detects nothing" case: a
    connector that never declares the capability at all.)
    """
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok"))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["detected_base_paths"] == []


def test_test_connection_detects_sabnzbds_base_paths_as_unverified_with_no_host_configured(
    isolated_config, fake_sabnzbd_server
):
    """No host configured at all -- every detected path reports `unverified`, never
    `not_found`: not being able to look over SSH is not the seedbox saying no. Also proves
    detection proposes without saving: `base_paths` on the stored row stays empty.
    """
    fake_sabnzbd_server.state.misc_complete_dir = "/downloads/complete"
    fake_sabnzbd_server.state.misc_download_dir = "/downloads/incomplete"
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        by_path = {d["client_path"]: d for d in body["detected_base_paths"]}
        assert by_path["/downloads/complete"]["kind"] == "content"
        assert by_path["/downloads/complete"]["state"] == "unverified"
        assert by_path["/downloads/incomplete"]["kind"] == "working"
        assert by_path["/downloads/incomplete"]["state"] == "unverified"

        # Detection proposes; it never saves.
        stored = client.get("/api/settings/clients").json()[0]
        assert stored["base_paths"] == []


# --- Category detection on test-connection (spec §8.3, joined 2026-08-23) --------------------


def test_test_connection_reports_the_clients_own_categories(isolated_config, fake_sabnzbd_server):
    fake_sabnzbd_server.state.category_names = ["*", "movies", "tv"]
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert sorted(body["detected_categories"]) == ["movies", "tv"]

        # Detection proposes; it never saves a category -> queue mapping -- exactly like base
        # paths. It **does** now persist the raw detected list itself (migration 030, this task)
        # -- a different thing, the settings page's own "what did the last Test see" memory.
        stored = client.get("/api/settings/clients").json()[0]
        assert stored["categories"] == []
        assert sorted(stored["detected_categories"]) == ["movies", "tv"]
        assert stored["detected_categories_at"] is not None


def test_test_connection_reports_no_categories_when_the_client_has_none_configured(
    isolated_config, fake_sabnzbd_server
):
    fake_sabnzbd_server.state.category_names = ["*"]
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        assert resp.json()["detected_categories"] == []

        # A successful Test that finds nothing still persists `[]`, stamped with when -- distinct
        # from "never tested" (`None`), which the next test below covers.
        stored = client.get("/api/settings/clients").json()[0]
        assert stored["detected_categories"] == []
        assert stored["detected_categories_at"] is not None


def test_a_failed_test_reports_no_detected_categories(isolated_config):
    with TestClient(app) as client:
        resp = client.post("/api/settings/clients", json=_probe_body("ok"))
        client_id = resp.json()["id"]
        client.put(f"/api/settings/clients/{client_id}", json=_probe_body("unreachable"))

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert resp.json()["detected_categories"] == []

        # A failed test never persists anything -- this instance has never had a *successful*
        # Test, so `detected_categories`/`detected_categories_at` both read as "never tested",
        # not a false empty list.
        stored = client.get("/api/settings/clients").json()[0]
        assert stored["detected_categories"] is None
        assert stored["detected_categories_at"] is None


# --- The manual "Add category" escape hatch (2026-08-23,
# prompts/2026-08-23-path-attribution-and-category-escape-hatch.md) ---------------------------


def test_manual_category_persists_with_its_source(isolated_config):
    """rTorrent's `list_categories` is DERIVED -- it can only report labels currently in use, so
    a category that will exist later can never be detected. A hand-added row must round-trip
    with `source: "manual"`, distinguishing it from a detected/guessed one.
    """
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(
                categories=[{"category": "ar-movies", "queue_id": None, "source": "manual"}]
            ),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["categories"][0]["source"] == "manual"

        client_id = resp.json()["id"]
        reread = client.get("/api/settings/clients").json()[0]
        assert reread["id"] == client_id
        assert reread["categories"][0]["source"] == "manual"


def test_blank_category_is_rejected_visibly_not_silently_dropped(isolated_config):
    """The exact defect class findings #11b/#11c already eliminated for the detected/guessed
    rows must not reappear at the one place a blank string can still originate: the manual-add
    escape hatch. A blank `category` is a 422, never a 2xx with the row quietly missing.
    """
    with TestClient(app) as client:
        resp = client.post(
            "/api/settings/clients",
            json=_sab_body(categories=[{"category": "", "queue_id": None, "source": "manual"}]),
        )
        assert resp.status_code == 422


@pytest.mark.skipif(not SEEDBOX_UP, reason=_SEEDBOX_SKIP_REASON)
def test_test_connection_distinguishes_verified_from_not_found_against_the_live_seedbox(
    isolated_config, fake_sabnzbd_server
):
    fake_sabnzbd_server.state.misc_complete_dir = "/data/pickup"
    fake_sabnzbd_server.state.misc_download_dir = "/data/pickup/does-not-exist-anywhere"
    with TestClient(app) as client:
        _put_live_seedbox_host(client)
        resp = client.post("/api/settings/clients", json=_sab_body_for(fake_sabnzbd_server))
        client_id = resp.json()["id"]

        resp = client.post(f"/api/settings/clients/{client_id}/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        by_path = {d["client_path"]: d["state"] for d in body["detected_base_paths"]}
        assert by_path["/data/pickup"] == "verified"
        assert by_path["/data/pickup/does-not-exist-anywhere"] == "not_found"
