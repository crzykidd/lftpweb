"""A fake SABnzbd instance -- `tests/fake_arr.py`'s shape: a real FastAPI app on a real
listening `uvicorn` socket, driven through a mutable `FakeSabState` a test manipulates
in-process between calls, not a mocked transport. Exercises `core/clients/sabnzbd.py`'s real
HTTP request/response cycle (the `apikey` query parameter, `output=json`, real JSON parsing)
instead of a stand-in for it.

**Most response shapes this fixture produces are still authored from vendor documentation
(sabnzbd.org/wiki/configuration/5.1/api), 2026-08-22, and remain UNVERIFIED against a live
SABnzbd instance** -- see `sabnzbd.py`'s own module docstring for the full list. **The
authentication shape is the one exception: it is MEASURED against a live SABnzbd 5.1.1,
2026-08-22** (docs/download-client-framework-spec.md §13.4 #9/#10, GitHub #23) and encoded
below accordingly:

- `mode=version` is **unauthenticated** -- it answers 200/JSON for any API key, including no
  key at all. It proves reachability, never a credential.
- Every other mode answers a bad key with **HTTP 403, `Content-Type: text/html`, plain-text
  body `"API Key Incorrect"`** -- not the `{"status": false, "error": ...}` JSON envelope on a
  200 this fixture used to encode. That was the same wrong assumption the connector's own
  first draft made (this module's own history, and
  docs/download-client-framework-spec.md §13.2's `IMPORT_EVENT_TYPES = {3}` precedent): a
  fixture that only ever matches the connector it is meant to falsify cannot catch anything.

This repo has already shipped a defect of exactly this shape once before: a fake *arr fixture
that encoded the same wrong numeric assumption the production code did, so every test stayed
green while two live Sonarr imports were misclassified `gone` (`core/arrclient.py`'s own
docstring; docs/download-client-framework-spec.md §13.2). The auth shape above is this
fixture's second occurrence of that trap, now corrected against measured bytes rather than a
second guess.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

DEFAULT_API_KEY = "test-sab-key"  # noqa: S105 - test-only fixture credential, never real


@dataclass
class FakeSabState:
    api_key: str = DEFAULT_API_KEY
    version: str = "4.3.0"
    # One dict per queue slot / history slot, in SABnzbd's own doc-derived field names
    # (`nzo_id`, `filename`/`name`, `status`, `cat`/`category`, `mb`/`mbleft`, `timeleft`,
    # `storage`, `bytes`, `fail_message`, `completed`) -- a test builds these directly rather
    # than through a builder API, the same shape `FakeArrState.queue_records`/`history_events`
    # use.
    queue_slots: list[dict[str, Any]] = field(default_factory=list)
    history_slots: list[dict[str, Any]] = field(default_factory=list)
    # `diskspace1`/`diskspace2` (+ `diskspacetotal1`/`diskspacetotal2`) ride the queue response
    # itself (spec's own survey note: "the cheapest free-space source of the five") --
    # GB-denominated numeric strings per vendor docs, UNVERIFIED. `diskspace2` is the one this
    # connector reads (doc-derived guess: the "complete" folder pairing).
    diskspace1: str = "50.00"
    diskspace2: str = "100.00"
    diskspacetotal1: str = "500.00"
    diskspacetotal2: str = "1000.00"
    # The v0.2.4-shaped production incident fixture (spec §1, §4.2) -- `fake_arr.py`'s
    # `queue_empty_for_requests` precedent, copied here for the identical reason: a download
    # client's own transient blank queue response must never read as "everything vanished."
    # When > 0, `mode=queue` ignores `queue_slots` entirely and answers with an empty slot
    # list, decrementing this counter by one per request.
    queue_empty_for_requests: int = 0
    misc_complete_dir: str = "/downloads/complete"
    misc_download_dir: str = "/downloads/incomplete"
    # `mode=get_config&section=categories` -- doc-derived, UNVERIFIED, 2026-08-23 (spec §8.3,
    # §13.4 new row). SAB always carries the special `"*"` "Default" pseudo-category alongside
    # whatever real ones a user configured; the connector's own `list_categories` excludes it on
    # purpose (see its own docstring), so a test seeding the real-category half of this list
    # doesn't also need to remember to add "*" every time.
    category_names: list[str] = field(default_factory=lambda: ["*", "movies", "tv"])
    # get_files: `nzo_id -> list[filename]`, modeling `mode=get_files&value=<nzo_id>`.
    files_by_nzo_id: dict[str, list[str]] = field(default_factory=dict)
    # Every action call (`name=pause`/`resume`/`delete`/`change_cat`) records its own params
    # here, keyed by `name`, appended in call order -- lets a test assert e.g. that `remove`
    # actually sent `del_files=0` rather than trusting the connector's own claim to.
    action_calls: list[dict[str, Any]] = field(default_factory=list)
    # `client_id`s the fake will report `name=delete` succeeding for -- a test seeds this to
    # model "this nzo_id is really there" for the queue-then-history fallback `remove` tries.
    removable_from_queue: set[str] = field(default_factory=set)
    removable_from_history: set[str] = field(default_factory=set)
    # Every call 500s -- a reachable-but-erroring instance (`ClientError`, not `ClientUnreachable`
    # -- the fake server answered, just with a failure).
    fail_all: bool = False
    # Forces every *authenticated* call (everything except `mode=version`, per the measured
    # table above) to answer the real SABnzbd 403 auth-failure shape regardless of whether
    # `apikey` actually matches `state.api_key` -- lets a test simulate "this stored key has
    # gone bad" without also having to construct a client pointed at the wrong key string.
    # `mode=version` is deliberately exempt even when this is set: measured behaviour is that
    # it accepts any key unconditionally, so a fixture that let this flag override it would be
    # re-authoring the same wrong assumption the connector used to make.
    bad_api_key_mode: bool = False
    # A 403 the fixture produces on purpose, but whose body is *not* SABnzbd's own recognisable
    # auth-failure text -- models an unrelated 403 (a reverse proxy, a WAF) that must still read
    # as a plain `ClientError`, never misreported as a bad API key. Independent of
    # `bad_api_key_mode`; when set, it wins over even a correct `apikey`.
    unrecognized_403_mode: bool = False
    # Stage 1b addition (docs/download-client-framework-spec.md §13.3, tests for
    # `api/settings_clients.py`'s test-connection capture): the real vendor `mode=version`
    # response never echoes the API key back in its body, so this fixture cannot otherwise
    # exercise "a secret present in *both* the request URL and the response body must not reach
    # the log" -- a test-only knob, off by default, so every other test's `mode=version` response
    # is unaffected.
    echo_key_in_version_body: bool = False
    # `core/clientsync.py`'s own tests (2026-08-23, "the two cadences firing independently") --
    # every request's `mode` param, recorded unconditionally regardless of auth/branch outcome,
    # so a test can distinguish "the fast cadence's `list_transfers(active_only=True)`" (one
    # `queue` call) from "the slow cadence's `active_only=False`" (`queue` *and* `history`)
    # without a connector-level change. Deliberately separate from `action_calls` above, which
    # only ever records the action sub-calls (`name=pause`/etc), not every request.
    mode_calls: list[str] = field(default_factory=list)


def _queue_response(state: FakeSabState) -> dict[str, Any]:
    if state.queue_empty_for_requests > 0:
        state.queue_empty_for_requests -= 1
        slots: list[dict[str, Any]] = []
    else:
        slots = state.queue_slots
    return {
        "queue": {
            "status": "Downloading" if slots else "Idle",
            "slots": slots,
            "diskspace1": state.diskspace1,
            "diskspace2": state.diskspace2,
            "diskspacetotal1": state.diskspacetotal1,
            "diskspacetotal2": state.diskspacetotal2,
        }
    }


def _history_response(state: FakeSabState) -> dict[str, Any]:
    return {"history": {"slots": state.history_slots}}


def create_fake_sabnzbd_app(state: FakeSabState) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if state.fail_all:
            return JSONResponse(status_code=500, content={"error": "simulated outage"})
        return await call_next(request)

    @app.get("/api")
    async def api(request: Request) -> Any:
        params = request.query_params
        mode = params.get("mode", "")
        apikey = params.get("apikey", "")
        state.mode_calls.append(mode)

        # `mode=version` is unauthenticated -- MEASURED against a live SABnzbd 5.1.1,
        # 2026-08-22 (spec §13.4 #10): it answers for any key, including no key at all, and
        # must therefore be checked *before* any of the auth-failure branches below, not after.
        if mode == "version":
            body: dict[str, Any] = {"version": state.version}
            if state.echo_key_in_version_body:
                body["echoed_apikey"] = apikey  # test-only: see FakeSabState field docstring
            return body

        if state.unrecognized_403_mode:
            # A 403 that is *not* SABnzbd's own recognisable auth-failure text -- see the field
            # docstring above.
            return PlainTextResponse("Forbidden", status_code=403, media_type="text/html")

        if apikey != state.api_key or state.bad_api_key_mode:
            # MEASURED against a live SABnzbd 5.1.1, 2026-08-22 (spec §13.4 #9, GitHub #23):
            # every authenticated mode answers a bad key with HTTP 403, `text/html`, plain-text
            # body "API Key Incorrect" -- not a 200 with a `{"status": false}` JSON envelope.
            return PlainTextResponse("API Key Incorrect", status_code=403, media_type="text/html")

        if mode == "queue":
            name = params.get("name")
            if name is None:
                return _queue_response(state)
            value = params.get("value", "")
            state.action_calls.append(
                {"mode": "queue", "name": name, "value": value, "value2": params.get("value2")}
            )
            if name == "delete":
                del_files = params.get("del_files")
                state.action_calls[-1]["del_files"] = del_files
                return {"status": value in state.removable_from_queue}
            return {"status": True}

        if mode == "history":
            name = params.get("name")
            if name is None:
                return _history_response(state)
            value = params.get("value", "")
            state.action_calls.append(
                {
                    "mode": "history",
                    "name": name,
                    "value": value,
                    "del_files": params.get("del_files"),
                }
            )
            if name == "delete":
                return {"status": value in state.removable_from_history}
            return {"status": True}

        if mode == "get_config":
            section = params.get("section")
            if section == "categories":
                return {"config": {"categories": [{"name": n} for n in state.category_names]}}
            return {
                "config": {
                    "misc": {
                        "complete_dir": state.misc_complete_dir,
                        "download_dir": state.misc_download_dir,
                    }
                }
            }

        if mode == "get_files":
            nzo_id = params.get("value", "")
            filenames = state.files_by_nzo_id.get(nzo_id, [])
            return {"files": [{"filename": name} for name in filenames]}

        return {"status": False, "error": f"unrecognized mode {mode!r}"}

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeSabServer:
    state: FakeSabState
    base_url: str


@asynccontextmanager
async def run_fake_sabnzbd_server() -> AsyncIterator[FakeSabServer]:
    """A real, listening `uvicorn` server for `create_fake_sabnzbd_app`, on its own thread with
    its own event loop -- same reasoning as `fake_arr.py.run_fake_arr_server`: a test driving
    this through a synchronous/blocking call on the same loop the fake server would run on
    would starve the fake server of scheduling entirely.
    """
    state = FakeSabState()
    app = create_fake_sabnzbd_app(state)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("fake SABnzbd server did not start in time")
        yield FakeSabServer(state=state, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
async def fake_sabnzbd_server() -> AsyncIterator[FakeSabServer]:
    async with run_fake_sabnzbd_server() as server:
        yield server
