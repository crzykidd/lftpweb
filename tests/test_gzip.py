"""Response compression (2026-11-27): `GZipMiddleware` is the outermost layer in `main.create_app`.

Pins the behaviours the placement relies on: gzip only on request, identical body once decoded,
WebSockets untouched, and nothing that must stream or is already compressed gets re-encoded.
"""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient

from lftpweb.main import app

ROWS = 600


def _seed(client: TestClient) -> None:
    """Seed one host/queue and `ROWS` items directly, then expose the queue to `/api/files`."""
    loop_db = client.app.state.db

    async def go() -> int:
        cur = await loop_db.execute(
            "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
            "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
        )
        cur = await loop_db.execute(
            "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
            "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
            (cur.lastrowid,),
        )
        queue_id = cur.lastrowid
        for i in range(ROWS):
            await loop_db.execute(
                "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
                "VALUES (?, ?, 0, 100, 100, 'REMOVED_BOTH')",
                (queue_id, f"Some.Show.S01E{i:04d}.1080p.WEB-DL.x264-GROUP"),
            )
        await loop_db.commit()
        return queue_id

    queue_id = client.portal.call(go)
    client.app.state.engine.queue_meta[queue_id] = type("Q", (), {"name": "q"})()


@pytest.fixture
def probe_routes():
    router = APIRouter()
    payload = {"rows": ["x" * 40] * 200}

    @router.get("/api/_probe/big")
    async def big() -> dict:
        return payload

    @router.get("/api/_probe/zip")
    async def zipped() -> Response:
        return Response(b"PK" + b"a" * 5000, media_type="application/zip")

    @router.get("/api/_probe/encoded")
    async def encoded() -> Response:
        body = gzip.compress(b"z" * 5000)
        return Response(body, media_type="text/plain", headers={"Content-Encoding": "gzip"})

    @router.get("/api/_probe/stream")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"data: one\n\n"
            yield b"data: two\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    start = len(app.router.routes)
    app.include_router(router)
    yield payload
    del app.router.routes[start:]


def test_files_is_gzipped_on_request_and_decodes_identically(isolated_config):
    with TestClient(app) as client:
        _seed(client)
        plain = client.get("/api/files", headers={"Accept-Encoding": "identity"})
        assert plain.status_code == 200
        assert "content-encoding" not in plain.headers
        raw = plain.content
        assert len(raw) > 1000

        zipped = client.get("/api/files", headers={"Accept-Encoding": "gzip"})
        assert zipped.headers["content-encoding"] == "gzip"
        assert "Accept-Encoding" in zipped.headers["vary"]
        # TestClient (httpx) decodes transparently; its body must equal the plain body.
        assert zipped.json() == json.loads(raw)
        assert len(zipped.json()["queues"][0]["nodes"]) == ROWS
        assert int(zipped.headers["content-length"]) < len(raw) / 3


def test_no_accept_encoding_means_plain_like_the_curl_healthcheck(isolated_config):
    with TestClient(app) as client:
        _seed(client)
        resp = client.get("/api/files", headers={"Accept-Encoding": ""})
        assert "content-encoding" not in resp.headers
        health = client.get("/api/health", headers={"Accept-Encoding": ""})
        assert health.status_code == 200
        assert "content-encoding" not in health.headers


def test_websocket_still_connects(isolated_config):
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws", headers={"Accept-Encoding": "gzip"}) as ws:
            assert ws is not None


def test_already_compressed_and_streaming_responses_are_left_alone(isolated_config, probe_routes):
    gz = {"Accept-Encoding": "gzip"}
    with TestClient(app) as client:
        big = client.get("/api/_probe/big", headers=gz)
        assert big.headers["content-encoding"] == "gzip"
        assert big.json() == probe_routes

        zipped = client.get("/api/_probe/zip", headers=gz)
        assert "content-encoding" not in zipped.headers
        assert zipped.content.startswith(b"PK")

        # Own Content-Encoding: not wrapped a second time (httpx decodes it exactly once).
        enc = client.get("/api/_probe/encoded", headers=gz)
        assert enc.headers["content-encoding"] == "gzip"
        assert enc.content == b"z" * 5000

        sse = client.get("/api/_probe/stream", headers=gz)
        assert "content-encoding" not in sse.headers
        assert sse.text == "data: one\n\ndata: two\n\n"
