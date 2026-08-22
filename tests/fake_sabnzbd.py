"""A fake SABnzbd instance -- `tests/fake_arr.py`'s shape: a real FastAPI app on a real
listening `uvicorn` socket, driven through a mutable `FakeSabState` a test manipulates
in-process between calls, not a mocked transport. Exercises `core/clients/sabnzbd.py`'s real
HTTP request/response cycle (the `apikey` query parameter, `output=json`, real JSON parsing)
instead of a stand-in for it.

**Every response shape this fixture produces is authored from vendor documentation
(sabnzbd.org/wiki/configuration/5.1/api), 2026-08-22, and is UNVERIFIED against a live
SABnzbd instance.** This repo has already shipped a defect of exactly this shape: a fake *arr
fixture that encoded the same wrong numeric assumption the production code did, so every test
stayed green while two live Sonarr imports were misclassified `gone`
(`core/arrclient.py`'s own docstring; docs/download-client-framework-spec.md §13.2). **This
fixture is the first thing to go correct** once spec §13.3's redacted capture returns real
bytes from a live SABnzbd (stage 1b) -- nothing here should be read as confirmed behavior.
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
from fastapi.responses import JSONResponse

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
    # Every call answers SABnzbd's own documented "bad API key" shape: HTTP 200,
    # `{"status": false, "error": "..."}` -- vendor docs describe SABnzbd signalling this kind
    # of failure in the body rather than via a 401/403, which is exactly the shape this
    # connector's `_get` must detect itself rather than relying on `raise_for_status()` alone.
    bad_api_key_mode: bool = False


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
        if apikey != state.api_key or state.bad_api_key_mode:
            # Doc-derived, UNVERIFIED: SABnzbd answers a bad key with HTTP 200 and a body-level
            # failure, not a 401/403 -- this is the shape `SabnzbdClient._get` has to detect.
            return {"status": False, "error": "API Key Incorrect"}

        if mode == "version":
            return {"version": state.version}

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
