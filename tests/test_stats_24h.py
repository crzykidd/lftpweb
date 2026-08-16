"""GET /api/stats's "24h" figure (`api/stats.py`, DESIGN.md §9.1) -- reads `metric_sample`
(`core/metrics.py`), not `job`. Found by the user (2026-08-13,
prompts/2026-08-13-header-24h-from-metrics.md): the header read `24h 0 B` after Clear History
while the Dashboard showed real data, because they read different tables and Clear History
deliberately never touches `metric_sample` (`api/history.py`'s module docstring).

The 24h-figure tests mirror `tests/test_metrics_api.py`'s shape: seed rows directly against the
same on-disk database the app just created (`isolated_config` points `config_dir` at
`tmp_path`), then drive the real HTTP endpoints via `TestClient`. The last test (the rest of the
header) instead calls the route functions directly against an in-memory database, the same way
`tests/test_history_api.py` does -- see its own comment for why.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from lftpweb.api import history, stats
from lftpweb.db import connect, migrate
from lftpweb.main import app


def _make_queue(client: TestClient) -> int:
    resp = client.put(
        "/api/settings/host",
        json={
            "name": "seedbox",
            "address": "1.2.3.4",
            "port": 22,
            "username": "user",
            "auth_method": "key",
            "key_path": "/config/id_rsa",
            "known_hosts_policy": "strict",
        },
    )
    assert resp.status_code == 200, resp.text
    # `local_path` must be a real, readable directory (mid-run scope addition to
    # `prompts/done/2026-08-16-path-browse-dialog.md`); `remote_path` stays a fake literal --
    # that check is best-effort and this test's host is unreachable.
    resp = client.post(
        "/api/settings/queues",
        json={"name": "TV", "remote_path": "/remote", "local_path": tempfile.mkdtemp()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_metric_sample(tmp_path, queue_id: int, bytes_delta: int, *, minutes_ago: int = 5) -> None:
    """Insert one `metric_sample` row (plus its heartbeat, the way `ThroughputSampler.tick()`
    always writes both together) -- bypassing the sampler itself, which
    `tests/test_metrics.py` already exercises.
    """

    async def seed():
        conn = await connect(str(tmp_path))
        ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await conn.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (ts,))
        await conn.execute(
            "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
            (queue_id, ts, bytes_delta),
        )
        await conn.commit()
        await conn.close()

    asyncio.run(seed())


def _seed_succeeded_job(tmp_path, queue_id: int, bytes_done: int) -> None:
    """A `job` row that finished successfully, inside the 24h window, with real bytes --
    the old (pre-fix) query's data source. Must have zero effect on `transferred_24h_bytes`
    now that it reads `metric_sample` instead.
    """

    async def seed():
        conn = await connect(str(tmp_path))
        cursor = await conn.execute(
            "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
            "VALUES (?, 'movie.mkv', 0, ?, ?, 'DOWNLOADED')",
            (queue_id, bytes_done, bytes_done),
        )
        item_id = cursor.lastrowid
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await conn.execute(
            "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queued_at, "
            "finished_at, bytes_done) VALUES (?, 'pget', 'succeeded', 'main', 0, 1, ?, ?, ?)",
            (item_id, now, now, bytes_done),
        )
        await conn.commit()
        await conn.close()

    asyncio.run(seed())


def test_24h_reads_metric_sample_not_job_bytes_done(isolated_config, tmp_path):
    with TestClient(app) as client:
        queue_id = _make_queue(client)

    # A succeeded job with real bytes, finished within the window -- the pre-fix data source.
    _seed_succeeded_job(tmp_path, queue_id, bytes_done=999_999_999)
    # The actual metric_sample total, much smaller and a different number entirely, so the two
    # can't accidentally agree.
    _seed_metric_sample(tmp_path, queue_id, bytes_delta=42)

    with TestClient(app) as client:
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["transferred_24h_bytes"] == 42


def test_24h_survives_every_job_row_being_deleted(isolated_config, tmp_path):
    """The exact bug report: Clear History deletes every `job` row and deliberately leaves
    `metric_sample` alone (`api/history.py` module docstring) -- the header must not zero out.
    """
    with TestClient(app) as client:
        queue_id = _make_queue(client)

    _seed_succeeded_job(tmp_path, queue_id, bytes_done=1_000_000)
    _seed_metric_sample(tmp_path, queue_id, bytes_delta=777)

    with TestClient(app) as client:
        before = client.get("/api/stats").json()["transferred_24h_bytes"]
        assert before == 777

        resp = client.delete("/api/history/jobs")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

        after = client.get("/api/stats").json()["transferred_24h_bytes"]
        assert after == before == 777


def test_24h_with_no_samples_returns_zero_not_null_or_error(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json()["transferred_24h_bytes"] == 0


def test_24h_matches_dashboard_throughput_total_for_the_same_window(isolated_config, tmp_path):
    """The point of the fix: the header and `GET /api/metrics/throughput?range=24h` (the
    Dashboard's own bytes-per-hour total) must agree, because both now read the same table
    through the same `core/metrics.py.queue_breakdown` call.
    """
    with TestClient(app) as client:
        queue_id = _make_queue(client)

    _seed_metric_sample(tmp_path, queue_id, bytes_delta=123_456, minutes_ago=5)
    _seed_metric_sample(tmp_path, queue_id, bytes_delta=654_321, minutes_ago=120)

    with TestClient(app) as client:
        header_total = client.get("/api/stats").json()["transferred_24h_bytes"]

        dash = client.get("/api/metrics/throughput", params={"range": "24h"}).json()
        dashboard_total = sum(b["total_bytes"] or 0 for b in dash["buckets"] if b["up"])

        assert header_total == dashboard_total == 123_456 + 654_321


# --- The rest of the header, verified without the live scheduler in the loop ------------------
#
# The tests above go through `TestClient`'s real app, whose `TransferQueue` admission loop is
# alive for the duration of the `with` block -- fine for jobs already terminal, but a directly
# inserted `queued` job row is exactly the kind of thing that loop picks up and tries to spawn
# on its own schedule, racing this test (and failing here, since `/local`/`/remote` don't
# exist). So this one calls `api/stats.stats()` and `api/history.clear_history_jobs()` directly
# against an in-memory database, the same way `tests/test_history_api.py` does -- proving the
# two things actually in scope: the DB-level guard that keeps a clear from ever reaching a
# `queued` job, and that `current_speed_bps`/`allocated_bps`/`ceiling_bps` are a pure passthrough
# of whatever `TransferQueue.stats()` returns, never the database.


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeQueue:
    """Stands in for `core/queue.py.TransferQueue` -- fixed, recognizable sentinel numbers so
    a passthrough is unambiguous, and no admission loop to race against.
    """

    def stats(self, settings):
        return {
            "current_speed_bps": 4_200_000,
            "allocated_bps": 8_000_000,
            "ceiling_bps": 20_000_000,
        }


class _FakeState:
    def __init__(self, db):
        self.db = db
        self.queue = _FakeQueue()


class _FakeApp:
    def __init__(self, db):
        self.state = _FakeState(db)


class _FakeRequest:
    def __init__(self, db):
        self.app = _FakeApp(db)


async def _make_queue_row(db, *, name: str = "q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, ?, '/remote', '/local', 1, 'copy')",
        (host_id, name),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item_row(db, queue_id: int, rel_path: str, *, remote_size: int, state: str) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (queue_id, rel_path, remote_size, remote_size, state),
    )
    await db.commit()
    return cursor.lastrowid


async def test_other_header_stats_unaffected_by_clearing_history(db):
    """`queued_count`/`queued_bytes` (`job WHERE state = 'queued'`) and
    `current_speed_bps`/`allocated_bps`/`ceiling_bps` (live `TransferQueue` state, never the
    database) must be untouched by a history clear -- Clear History's own WHERE clause can
    never reach a non-terminal job (`api/history.py`'s `_jobs_where_clause`), and the speed/
    allocation figures never read the database at all (`core/queue.py.TransferQueue.stats`).
    """
    queue_id = await _make_queue_row(db)

    queued_item = await _make_item_row(
        db, queue_id, "queued-item.mkv", remote_size=5_000_000, state="QUEUED"
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queued_at, bytes_done) "
        "VALUES (?, 'pget', 'queued', 'main', 0, 1, '2026-08-13T00:00:00.000000Z', 0)",
        (queued_item,),
    )
    succeeded_item = await _make_item_row(
        db, queue_id, "done.mkv", remote_size=1_000, state="DOWNLOADED"
    )
    await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, queued_at, finished_at, "
        "bytes_done) VALUES (?, 'pget', 'succeeded', 'main', 0, 1, "
        "'2026-08-13T00:00:00.000000Z', '2026-08-13T00:01:00.000000Z', 1000)",
        (succeeded_item,),
    )
    await db.commit()

    request = _FakeRequest(db)
    before = await stats.stats(request)
    assert before.queued_count == 1
    assert before.queued_bytes == 5_000_000

    cleared = await history.clear_history_jobs(request)
    assert cleared.deleted == 1  # only the succeeded job, never the queued one

    after = await stats.stats(request)
    assert after.queued_count == before.queued_count == 1
    assert after.queued_bytes == before.queued_bytes == 5_000_000
    assert after.current_speed_bps == before.current_speed_bps == 4_200_000
    assert after.allocated_bps == before.allocated_bps == 8_000_000
    assert after.ceiling_bps == before.ceiling_bps == 20_000_000
