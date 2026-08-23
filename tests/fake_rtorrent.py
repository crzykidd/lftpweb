"""A fake rTorrent instance -- `tests/fake_arr.py`/`tests/fake_sabnzbd.py`'s shape: a real
FastAPI app on a real listening `uvicorn` socket, driven through a mutable `FakeRtorrentState` a
test manipulates in-process between calls, not a mocked transport. Exercises `core/clients/
rtorrent.py`'s real HTTP request/response cycle -- a real XML-RPC POST body, real `xmlrpc.client.
loads()` parsing on the way in, a real XML-RPC response on the way out -- instead of a stand-in
for it.

**Every response shape and every dispatched method this fixture answers is authored from vendor
documentation (rtorrent-docs.readthedocs.io, docs/download-client-api-survey.md) and remains
UNVERIFIED against a live rTorrent, 2026-08-22** -- see `core/clients/rtorrent.py`'s own module
docstring for the full accounting, and `docs/download-client-framework-spec.md` §13.6 for the
risk-ranked list. **This fixture inherits every one of those guesses.** A green suite against it
proves `core/clients/rtorrent.py` is internally consistent with this module's own reading of the
vendor docs -- it proves nothing about whether that reading matches a real rTorrent. This repo
has already shipped two defects of exactly this shape (`core/arrclient.py`'s
`IMPORT_EVENT_TYPES = {3}`, and SABnzbd's auth shape, GitHub #23 -- see
`docs/download-client-framework-spec.md` §13.2/§13.4); this fixture is deliberately flagged
rather than trusted as ground truth for the vocabulary it drives.

**One exception, and it is the one thing this fixture actively enforces rather than merely
guesses at**: the fixture keys every torrent by an **uppercase** hash and does an exact,
case-sensitive lookup on every per-item call (`d.stop`, `d.erase`, `d.pause`, `d.resume`,
`t.multicall`, `f.multicall2`, `d.free_diskspace`, `d.custom1.set`). A connector that forgot to
uppercase a lowercase-normalized `client_id` before calling back into rTorrent (spec §7.1 vs.
`core/clients/rtorrent.py._to_rtorrent_hash`) would silently 404-equivalent against this fixture
exactly as it would risk doing against a real, case-sensitive rTorrent -- this is a deliberate
design choice, not an oversight, so the regression this connector's own high-risk correction-list
entry names is actually exercisable by a test.

**The other deliberate enforcement**: any RPC method this fixture does not explicitly recognise
-- including, on purpose, `d.custom5.set` and `d.delete_tied`, the `erasedata` hook sequence
spec §10.1 removed from this design -- answers an XML-RPC fault reading "Could not find command".
`remove()` must never provoke that fault; a test asserts on the exact set of methods `remove()`
called, not merely that it "worked."
"""

from __future__ import annotations

import asyncio
import base64
import socket
import threading
import xmlrpc.client
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

DEFAULT_USERNAME = "test-user"
DEFAULT_PASSWORD = "test-pass"  # noqa: S105 - test-only fixture credential, never real


@dataclass
class FakeRtorrentTorrent:
    """One `d.multicall2` row's worth of state, in rTorrent's own doc-derived field shape --
    a test builds these directly, the same "no builder API" convention `FakeSabState`'s queue/
    history slots use. `hash` is always stored **uppercase** here (see module docstring).
    """

    torrent_hash: str
    name: str = ""
    size_bytes: int = 0
    completed_bytes: int = 0
    left_bytes: int = 0
    down_rate: int = 0
    up_total: int = 0
    ratio: int = 0  # per-mille, MEASURED-adjacent per the survey; 1000 == a 1.0 ratio
    state: int = 1  # 1 == started
    complete: int = 0
    is_active: int = 1
    hashing: int = 0
    message: str = ""
    base_path: str = ""
    custom1: str = ""
    timestamp_started: int = 0
    timestamp_finished: int = 0
    free_diskspace: int = 0
    trackers: list[str] = field(default_factory=list)  # full announce URLs, never redacted here
    files: list[str] = field(default_factory=list)


@dataclass
class FakeRtorrentState:
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    client_version: str = "0.9.8"
    directory_default: str = "/downloads/rtorrent"
    # Keyed by uppercase hash -- see module docstring's case-sensitivity discipline.
    torrents: dict[str, FakeRtorrentTorrent] = field(default_factory=dict)
    # Every dispatched RPC call, in order: `{"method": ..., "params": [...]}`. A test's one
    # ground-truth source for "what did the connector actually send", the same role
    # `FakeSabState.action_calls` plays.
    action_calls: list[dict[str, Any]] = field(default_factory=list)
    # A reachable-but-erroring instance -- HTTP 500, no XML-RPC body at all.
    fail_all: bool = False
    # Forces every request to 401, regardless of the Authorization header presented -- models
    # "the stored credential has gone bad" without constructing a client pointed at a wrong one.
    force_unauthorized: bool = False
    # When False, the fixture skips the Basic-auth check entirely -- models a deployment with no
    # auth configured at all (not the probed live system, but a legitimate one to test against).
    require_auth: bool = True

    def add(self, torrent: FakeRtorrentTorrent) -> None:
        self.torrents[torrent.torrent_hash] = torrent


def _expected_auth_header(state: FakeRtorrentState) -> str:
    token = base64.b64encode(f"{state.username}:{state.password}".encode()).decode()
    return f"Basic {token}"


def _fault_response(code: int, message: str) -> bytes:
    return xmlrpc.client.dumps(xmlrpc.client.Fault(code, message), methodresponse=True).encode(
        "utf-8"
    )


def _ok_response(value: Any) -> bytes:
    return xmlrpc.client.dumps((value,), methodresponse=True).encode("utf-8")


_LISTING_COMMAND_TO_ATTR: dict[str, str] = {
    "d.hash=": "torrent_hash",
    "d.name=": "name",
    "d.size_bytes=": "size_bytes",
    "d.completed_bytes=": "completed_bytes",
    "d.left_bytes=": "left_bytes",
    "d.down.rate=": "down_rate",
    "d.up.total=": "up_total",
    "d.ratio=": "ratio",
    "d.state=": "state",
    "d.complete=": "complete",
    "d.is_active=": "is_active",
    "d.hashing=": "hashing",
    "d.message=": "message",
    "d.base_path=": "base_path",
    "d.custom1=": "custom1",
    "d.timestamp.started=": "timestamp_started",
    "d.timestamp.finished=": "timestamp_finished",
}


def _dispatch(state: FakeRtorrentState, method: str, params: tuple[Any, ...]) -> Any:
    """Returns either a plain value (wrapped as a normal XML-RPC response) or an
    `xmlrpc.client.Fault` instance the caller wraps as a fault response. Never raises for an
    unrecognised method -- it answers the same "Could not find command" fault a real bare
    rTorrent is documented to give for a command it does not have (module docstring's
    enforcement half: `d.custom5.set`/`d.delete_tied` land here, on purpose).
    """
    if method == "system.client_version":
        return state.client_version

    if method == "directory.default":
        return state.directory_default

    if method == "d.multicall2":
        # ("", "main", *commands) -- see core/clients/rtorrent.py's own comment on the leading
        # empty "call id" argument.
        commands = list(params[2:]) if len(params) > 2 else []
        rows = []
        for torrent in state.torrents.values():
            row = []
            for cmd in commands:
                attr = _LISTING_COMMAND_TO_ATTR.get(cmd)
                row.append(getattr(torrent, attr) if attr else "")
            rows.append(row)
        return rows

    if method in ("d.stop", "d.erase", "d.pause", "d.resume", "d.free_diskspace"):
        torrent_hash = params[0] if params else ""
        torrent = state.torrents.get(torrent_hash)
        if torrent is None:
            return xmlrpc.client.Fault(-501, f"Could not find info-hash: {torrent_hash!r}")
        if method == "d.stop":
            torrent.state = 0
            torrent.is_active = 0
        elif method == "d.erase":
            del state.torrents[torrent_hash]
        elif method == "d.pause":
            torrent.is_active = 0
        elif method == "d.resume":
            torrent.is_active = 1
        elif method == "d.free_diskspace":
            return torrent.free_diskspace
        return 0

    if method == "d.custom1.set":
        torrent_hash, value = (params + (None, None))[:2]
        torrent = state.torrents.get(torrent_hash)
        if torrent is None:
            return xmlrpc.client.Fault(-501, f"Could not find info-hash: {torrent_hash!r}")
        torrent.custom1 = str(value)
        return 0

    if method == "d.check_hash":
        torrent_hash = params[0] if params else ""
        torrent = state.torrents.get(torrent_hash)
        if torrent is None:
            return xmlrpc.client.Fault(-501, f"Could not find info-hash: {torrent_hash!r}")
        return 0

    if method == "t.multicall":
        torrent_hash = params[0] if params else ""
        torrent = state.torrents.get(torrent_hash)
        if torrent is None:
            return xmlrpc.client.Fault(-501, f"Could not find info-hash: {torrent_hash!r}")
        return [[url] for url in torrent.trackers]

    if method == "f.multicall2":
        torrent_hash = params[0] if params else ""
        torrent = state.torrents.get(torrent_hash)
        if torrent is None:
            return xmlrpc.client.Fault(-501, f"Could not find info-hash: {torrent_hash!r}")
        return [[path] for path in torrent.files]

    # The enforcement half of the module docstring: `d.custom5.set`, `d.delete_tied`, and any
    # other method this fixture was never taught land here -- the same fault shape a bare
    # rTorrent is documented to give for a genuinely unsupported command.
    return xmlrpc.client.Fault(-506, f"Could not find command: {method!r}")


def create_fake_rtorrent_app(state: FakeRtorrentState) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        if state.fail_all:
            return Response(status_code=500, content=b"simulated outage")
        if state.require_auth:
            header = request.headers.get("authorization", "")
            if state.force_unauthorized or header != _expected_auth_header(state):
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="ruTorrent Private Area"'},
                    content=b"",
                )
        return await call_next(request)

    @app.post("/RPC2")
    async def rpc2(request: Request) -> Response:
        body = await request.body()
        try:
            params, method = xmlrpc.client.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001 - malformed body from a broken test/client, not a real path
            return Response(
                content=_fault_response(-500, "malformed XML-RPC request"),
                media_type="text/xml",
            )
        state.action_calls.append({"method": method, "params": list(params)})
        result = _dispatch(state, method, params)
        if isinstance(result, xmlrpc.client.Fault):
            content = xmlrpc.client.dumps(result, methodresponse=True).encode("utf-8")
        else:
            content = _ok_response(result)
        return Response(content=content, media_type="text/xml")

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeRtorrentServer:
    state: FakeRtorrentState
    base_url: str


@asynccontextmanager
async def run_fake_rtorrent_server() -> AsyncIterator[FakeRtorrentServer]:
    """A real, listening `uvicorn` server for `create_fake_rtorrent_app`, on its own thread with
    its own event loop -- same reasoning as `fake_arr.py.run_fake_arr_server` /
    `fake_sabnzbd.py.run_fake_sabnzbd_server`: driving this through a blocking call on the same
    loop the fake server runs on would starve it of scheduling entirely.
    """
    state = FakeRtorrentState()
    app = create_fake_rtorrent_app(state)
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
            raise RuntimeError("fake rTorrent server did not start in time")
        yield FakeRtorrentServer(state=state, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
async def fake_rtorrent_server() -> AsyncIterator[FakeRtorrentServer]:
    async with run_fake_rtorrent_server() as server:
        yield server
