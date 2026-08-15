"""A fake Sonarr/Radarr instance -- small FastAPI app speaking the four v3 endpoints this
codebase touches (`/api/v3/system/status`, `/api/v3/queue`, `/api/v3/history`,
`/api/v3/command`), served over a real, listening `uvicorn` socket -- same philosophy as the
fake seedbox (`docker-compose.test.yml`'s sshd containers): exercise `core/arrclient.py`'s real
HTTP request/response cycle (headers, query-string encoding, JSON parsing, pagination), not a
mocked transport.

`FakeArrState` is the shared, mutable store a test manipulates directly (in-process, no HTTP)
between poller passes -- this is what lets `test_arrsync.py` model a slow multi-file import: a
queue record stays present with `trackedDownloadState: importing` while `history_events`
accretes one entry per file, exactly the shape docs/arr-integration-spec.md's "Failure modes"
section asks the test fixture to model.
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

DEFAULT_API_KEY = "test-arr-key"  # noqa: S105 - test-only fixture credential, never real


@dataclass
class FakeArrState:
    api_key: str = DEFAULT_API_KEY
    version: str = "4.0.0.0"
    queue_records: list[dict[str, Any]] = field(default_factory=list)
    history_events: list[dict[str, Any]] = field(default_factory=list)
    command_calls: list[dict[str, Any]] = field(default_factory=list)
    # Force a small effective page size regardless of what the client requests -- the one
    # knob the pagination test needs to make one small queue/history split across pages
    # without needing 250+ fixture records to do it honestly.
    page_size_override: int | None = None
    # When True, every request 500s -- the "unreachable instance" scenario
    # (`core/arrsync.py._handle_failure`) without actually tearing the server down.
    fail_all: bool = False


def create_fake_arr_app(state: FakeArrState) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if state.fail_all:
            return JSONResponse(status_code=503, content={"message": "simulated outage"})
        if request.headers.get("x-api-key") != state.api_key:
            return JSONResponse(status_code=401, content={"message": "Unauthorized"})
        return await call_next(request)

    def _paginate(
        records: list[dict[str, Any]], page: int, requested_page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        page_size = state.page_size_override or requested_page_size
        start = (page - 1) * page_size
        return records[start : start + page_size], page_size

    @app.get("/api/v3/system/status")
    async def system_status() -> dict[str, Any]:
        return {"version": state.version, "appName": "Fake*arr"}

    @app.get("/api/v3/queue")
    async def queue(page: int = 1, pageSize: int = 20) -> dict[str, Any]:
        sliced, effective_size = _paginate(state.queue_records, page, pageSize)
        return {
            "page": page,
            "pageSize": effective_size,
            "totalRecords": len(state.queue_records),
            "records": sliced,
        }

    @app.get("/api/v3/history")
    async def history(
        page: int = 1,
        pageSize: int = 20,
        downloadId: str | None = None,
        sourceTitle: str | None = None,
    ) -> dict[str, Any]:
        filtered = state.history_events
        if downloadId is not None:
            filtered = [e for e in filtered if e.get("downloadId") == downloadId]
        elif sourceTitle is not None:
            filtered = [e for e in filtered if e.get("sourceTitle") == sourceTitle]
        sliced, effective_size = _paginate(filtered, page, pageSize)
        return {
            "page": page,
            "pageSize": effective_size,
            "totalRecords": len(filtered),
            "records": sliced,
        }

    @app.post("/api/v3/command")
    async def command(body: dict[str, Any]) -> dict[str, Any]:
        state.command_calls.append(body)
        return {"id": len(state.command_calls), "name": body.get("name"), "status": "queued"}

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeArrServer:
    state: FakeArrState
    base_url: str


@asynccontextmanager
async def run_fake_arr_server() -> AsyncIterator[FakeArrServer]:
    """A real, listening `uvicorn` server for `create_fake_arr_app`, on its **own thread with
    its own event loop** -- torn down on exit. The `fake_arr_server` fixture below is the
    one-server-per-test wrapper around this; a test that needs *two* independent instances
    (the per-instance failure-isolation case: one unreachable instance must never block
    another) opens a second one directly with this.

    **Must be a separate thread, not `asyncio.create_task` on the caller's own loop.** Several
    of this feature's tests drive the app through `fastapi.testclient.TestClient`, whose
    `client.post(...)` is a *synchronous*, blocking call from the calling coroutine's point of
    view -- while it blocks, the event loop that call is running on is not pumped at all (a
    coroutine mid-synchronous-call yields nothing back to its own loop), so a fake server task
    scheduled on that same loop would never get to read the incoming request or write a
    response, and every request would hang until `core/arrclient.py`'s own 10s timeout fired.
    A dedicated OS thread with `asyncio.run` decouples the fake server's own scheduling from
    whatever the calling test happens to be blocked on, which is what a real out-of-process
    fake seedbox gets for free from being a separate container in the first place.

    A random free port is picked up front rather than letting uvicorn bind port 0 -- `Server`
    doesn't hand the bound port back directly, and reading it out of `server.servers[0].
    sockets[0]` after startup is one more moving part than pre-picking a free one and binding
    it directly (a small TOCTOU race against another process is accepted here the same way
    `docker-compose.test.yml`'s fixed host ports already accept it for the fake seedbox).
    """
    state = FakeArrState()
    app = create_fake_arr_app(state)
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
            raise RuntimeError("fake *arr server did not start in time")
        yield FakeArrServer(state=state, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
async def fake_arr_server() -> AsyncIterator[FakeArrServer]:
    async with run_fake_arr_server() as server:
        yield server
