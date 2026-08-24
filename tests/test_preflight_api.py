"""`GET /api/queue/preflight` (docs/transfers-redesign-spec.md §4, prefigured; this task's own
handoff prompt, prompts/done/2026-08-20-preflight-box.md, plus its follow-up
prompts/2026-08-20-preflight-waiting-sources.md) -- the live `source_configured` check
(no bound, enabled *arr instance anywhere, and no settle-eligible queue -> hide the row list)
and the "configured but nothing projected yet" shape. The projection itself (attribution, flap
tolerance, no-duplicate-at-handover) is `tests/test_arr_preflight.py`'s job for the *arr source
and `tests/test_autoqueue.py`'s for the settle source, each exercised directly against its own
scheduler/class; this file only covers what only the live app can answer -- whether the
endpoint's own config-aware gates and merge logic agree with the database. Same `TestClient` +
`isolated_config` idiom as `tests/test_settings_queues_arr.py`.
"""

from __future__ import annotations

import asyncio
import tempfile

from fastapi.testclient import TestClient

from lftpweb.api.jobs import _merge_preflight_rows
from lftpweb.core.preflight import PreflightRow
from lftpweb.main import app


def _row(source: str, queue_id: int, title: str, **overrides) -> PreflightRow:
    fields = {
        "queue_name": "q",
        "queue_short_name": None,
        "status_label": None,
        "source_label": "x",
        "source_kind": None,
        "size_bytes": None,
        "size_remaining_bytes": None,
        "remaining_s": None,
        "download_client": None,
        "wait_scans": None,
        "wait_since": None,
    }
    fields.update(overrides)
    return PreflightRow(source=source, queue_id=queue_id, title=title, **fields)


def _put_host(client: TestClient) -> None:
    client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "example.invalid",
            "username": "seeduser",
            "auth_method": "password",
            "password": "hunter2",
        },
    )


def _create_instance(client: TestClient, **overrides) -> int:
    body = {
        "name": "Sonarr",
        "kind": "sonarr",
        "base_url": "http://sonarr.example.invalid",
        "api_key": "key",
        "enabled": True,
        "notify_on_complete": False,
    }
    body.update(overrides)
    resp = client.post("/api/settings/arr", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_queue(client: TestClient, **overrides) -> int:
    body = {"name": "TV", "remote_path": "/data/tv", "local_path": tempfile.mkdtemp()}
    body.update(overrides)
    resp = client.post("/api/settings/queues", json=body)
    assert resp.status_code == 201
    return resp.json()["id"]


def test_no_instance_at_all_hides_the_box(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/queue/preflight")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "source_configured": False,
            "rows": [],
            "gated_queues": [],
            "unattributed_clients": [],
        }


def test_disabled_instance_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=False)
        _create_queue(client, arr_instance_id=instance_id)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {
            "source_configured": False,
            "rows": [],
            "gated_queues": [],
            "unattributed_clients": [],
        }


def test_enabled_instance_with_no_bound_queue_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        _create_instance(client, enabled=True)  # nothing bound to it

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {
            "source_configured": False,
            "rows": [],
            "gated_queues": [],
            "unattributed_clients": [],
        }


def test_enabled_instance_with_disabled_queue_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=True)
        _create_queue(client, arr_instance_id=instance_id, enabled=False)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {
            "source_configured": False,
            "rows": [],
            "gated_queues": [],
            "unattributed_clients": [],
        }


def test_enabled_bound_instance_with_nothing_projected_yet_shows_configured(isolated_config):
    """`source_configured=True` with an empty `rows` list -- the "Nothing in preflight" case,
    distinct from "hide the box entirely." The scheduler's own cache is empty here (it has never
    polled in this test), which is exactly the state a freshly-configured install is in before
    its first ~60s poll lands.
    """
    with TestClient(app) as client:
        _put_host(client)
        instance_id = _create_instance(client, enabled=True)
        _create_queue(client, arr_instance_id=instance_id, enabled=True)

        resp = client.get("/api/queue/preflight")
        assert resp.json() == {
            "source_configured": True,
            "rows": [],
            "gated_queues": [],
            "unattributed_clients": [],
        }


# --- The settle-gated source's own "is it configured" gate (this task) -----------------------


def test_settle_off_and_no_auto_queue_queue_hides_the_box(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/queue/preflight")
        body = resp.json()
        assert body["source_configured"] is False
        assert body["rows"] == []


def test_settle_on_but_no_auto_queue_enabled_queue_still_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        client.put("/api/settings/settle", json={"enabled": True})
        _create_queue(client, auto_queue_enabled=False)

        resp = client.get("/api/queue/preflight")
        assert resp.json()["source_configured"] is False


def test_settle_on_with_an_auto_queue_enabled_queue_shows_configured_with_nothing_projected(
    isolated_config,
):
    """Mirrors `test_enabled_bound_instance_with_nothing_projected_yet_shows_configured` above
    for the settle source: `source_configured=True` with an empty `rows` list, since nothing has
    ever scanned in this test.
    """
    with TestClient(app) as client:
        _put_host(client)
        client.put("/api/settings/settle", json={"enabled": True})
        _create_queue(client, auto_queue_enabled=True)

        resp = client.get("/api/queue/preflight")
        body = resp.json()
        assert body["source_configured"] is True
        assert body["rows"] == []


def test_settle_enabled_queue_disabled_hides_the_box(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        client.put("/api/settings/settle", json={"enabled": True})
        _create_queue(client, auto_queue_enabled=True, enabled=False)

        resp = client.get("/api/queue/preflight")
        assert resp.json()["source_configured"] is False


# --- The mount-gate banner (this task, decided with the user: a banner, never rows) -----------


def test_mount_gated_queue_shows_a_banner_line_with_its_reason(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        queue_id = _create_queue(client, auto_queue_enabled=True)
        app.state.engine.autoqueue.gated[queue_id] = "local root is missing"

        resp = client.get("/api/queue/preflight")
        body = resp.json()
        assert body["gated_queues"] == [{"queue_name": "TV", "reason": "local root is missing"}]


def test_gated_banner_is_independent_of_source_configured(isolated_config):
    """A queue can be mount-gated whether or not either row source is configured -- the box must
    still show the banner even with `source_configured=False` (no *arr, settle explicitly off --
    it otherwise defaults on, `core/settle.py.SettleSettings`).
    """
    with TestClient(app) as client:
        _put_host(client)
        client.put("/api/settings/settle", json={"enabled": False})
        queue_id = _create_queue(client, auto_queue_enabled=True)
        app.state.engine.autoqueue.gated[queue_id] = "local root is missing"

        resp = client.get("/api/queue/preflight")
        body = resp.json()
        assert body["source_configured"] is False
        assert len(body["gated_queues"]) == 1


def test_gated_queue_disabled_since_no_longer_appears_in_the_banner(isolated_config):
    """Defensive: `AutoQueue.gated` is only ever cleared by a later `on_scan` pass for that same
    queue, which never runs once a queue is disabled -- the endpoint's own live `path_queue`
    query is what actually drops a since-disabled queue from the banner, not the in-memory dict.
    """
    with TestClient(app) as client:
        _put_host(client)
        queue_id = _create_queue(client, auto_queue_enabled=True)
        app.state.engine.autoqueue.gated[queue_id] = "local root is missing"

        client.put(
            f"/api/settings/queues/{queue_id}",
            json={
                "name": "TV",
                "remote_path": "/data/tv",
                "local_path": tempfile.mkdtemp(),
                "enabled": False,
                "auto_queue_enabled": True,
            },
        )

        resp = client.get("/api/queue/preflight")
        assert resp.json()["gated_queues"] == []


# --- The unattributed-clients banner (finding #2, 2026-08-23) ------------------------------


def test_unattributed_client_shows_a_banner_line_with_its_count(isolated_config):
    """An enabled download-client instance whose most recent pass saw unattributable items --
    the exact live scenario finding #2 measured -- surfaces as a banner line, the mount-gate
    banner's own shape. `core.clientsync.ClientSyncScheduler`'s own count is asserted directly
    in `tests/test_clientsync.py`; this covers only this endpoint's own wiring (the live
    `enabled` column, the response model's field mapping, sorting).
    """
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post(
            "/api/settings/clients",
            json={
                "name": "SABnzbd",
                "client_type": "sabnzbd",
                # `enabled: False` -- creating an *enabled* instance tests connectivity (spec
                # §3a) against a real server this test has none of; `enabled` is flipped directly
                # below instead, since the banner only reads the stored column and the
                # scheduler's own in-memory cache, not whether Test was ever clicked.
                "config": {"base_url": "http://sab.example.invalid", "api_key": "k"},
                "enabled": False,
            },
        )
        assert resp.status_code == 201
        instance_id = resp.json()["id"]

        async def _enable() -> None:
            await app.state.db.execute(
                "UPDATE download_client SET enabled = 1 WHERE id = ?", (instance_id,)
            )
            await app.state.db.commit()

        asyncio.run(_enable())
        # Poked directly, exactly as `test_mount_gated_queue_shows_a_banner_line_with_its_reason`
        # above pokes `app.state.engine.autoqueue.gated` -- this test isn't exercising the poller
        # itself, only this endpoint's read of its cache.
        app.state.client_sync._instance_names[instance_id] = "SABnzbd"
        # Round 4's own category breakdown (`core.clientsync.UnattributedClientInfo`) -- poked
        # directly, so this test also covers this endpoint's own field mapping for it. `count`
        # (3) is no longer a separate poked field (2026-08-23 fix) -- `unattributed_clients` now
        # derives it fresh from this raw breakdown, filtered against a live exclusion read, every
        # call, rather than trusting a second, possibly-stale cached number.
        app.state.client_sync._unattributed_categories[instance_id] = {
            "ar-movies": 2,
            None: 1,
        }

        resp = client.get("/api/queue/preflight")
        body = resp.json()
        assert body["unattributed_clients"] == [
            {
                "client_id": instance_id,
                "client_name": "SABnzbd",
                "count": 3,
                "categories": ["ar-movies"],
                "no_category_count": 1,
            }
        ]


def test_a_quiet_client_never_appears_in_the_unattributed_banner(isolated_config):
    with TestClient(app) as client:
        _put_host(client)
        resp = client.post(
            "/api/settings/clients",
            json={
                "name": "SABnzbd",
                "client_type": "sabnzbd",
                "config": {"base_url": "http://sab.example.invalid", "api_key": "k"},
                "enabled": False,
            },
        )
        instance_id = resp.json()["id"]

        async def _enable() -> None:
            await app.state.db.execute(
                "UPDATE download_client SET enabled = 1 WHERE id = ?", (instance_id,)
            )
            await app.state.db.commit()

        asyncio.run(_enable())
        # Never touched `_unattributed_categories` -- a quiet client has nothing to say.

        resp = client.get("/api/queue/preflight")
        assert resp.json()["unattributed_clients"] == []


# --- Cross-source precedence + ordering (this task, decided with the user) --------------------
# `_merge_preflight_rows` is the one place allowed to know all three sources exist -- exercised
# directly, no app/db needed, since it's a pure function over `PreflightRow`. `client_rows` is
# always `[]` here -- these tests predate the download-client source (2026-08-23,
# `tests/test_preflight_client_merge.py` is where that source's own merge behaviour, including
# its interaction with the settle-precedence rule below, is actually exercised) and exist to
# prove the *arr/settle precedence untouched by that addition.


def test_settle_row_wins_over_an_arr_row_for_the_same_release():
    arr_row = _row("arr", 1, "Show.S01E01", source_label="Sonarr", source_kind="sonarr")
    settle_row = _row("settle", 1, "Show.S01E01", source_label="TV", size_bytes=100)

    assert _merge_preflight_rows([arr_row], [], [settle_row]) == [settle_row]


def test_different_releases_both_survive_the_merge():
    arr_row = _row("arr", 1, "Alpha.Release")
    settle_row = _row("settle", 1, "Beta.Release")

    assert _merge_preflight_rows([arr_row], [], [settle_row]) == [arr_row, settle_row]


def test_same_title_different_queue_is_not_deduplicated():
    arr_row = _row("arr", 1, "Show.S01E01")
    settle_row = _row("settle", 2, "Show.S01E01")

    merged = _merge_preflight_rows([arr_row], [], [settle_row])
    assert set(merged) == {arr_row, settle_row}


def test_merge_sorts_alphabetically_by_title_across_sources_case_insensitively():
    arr_row = _row("arr", 1, "banana")
    settle_row = _row("settle", 1, "Apple")

    assert _merge_preflight_rows([arr_row], [], [settle_row]) == [settle_row, arr_row]


def test_merge_with_no_rows_from_either_source_is_empty():
    assert _merge_preflight_rows([], [], []) == []
