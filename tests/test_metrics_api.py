"""Integration tests for GET /api/metrics/throughput and the retention settings endpoint,
over the real HTTP app via TestClient -- mirrors tests/test_backup_api.py's shape.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lftpweb.db import connect
from lftpweb.main import app


def test_metrics_settings_default_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/metrics")
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 30}  # 2026-08-21: default raised 7 -> 30


def test_metrics_settings_round_trip(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/metrics", json={"retention_days": 30})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 30}

        resp = client.get("/api/settings/metrics")
        assert resp.json() == {"retention_days": 30}


def test_metrics_settings_rejects_out_of_range_values(isolated_config):
    with TestClient(app) as client:
        resp = client.put("/api/settings/metrics", json={"retention_days": 0})
        assert resp.status_code == 422
        resp = client.put("/api/settings/metrics", json={"retention_days": 31})
        assert resp.status_code == 422


def test_throughput_empty_database_returns_buckets_all_down(isolated_config):
    """No queue has ever existed and the sampler has never ticked long enough to write a
    heartbeat within the (very short, test-duration) window -- every bucket in range must come
    back `up: false`, not a fabricated zero (decision recorded in docs/decisions.md).
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1h"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "1h"
        assert body["bucket_seconds"] == 60
        assert len(body["buckets"]) >= 1
        assert all(b["up"] is False for b in body["buckets"])
        assert all(b["total_bytes"] is None for b in body["buckets"])
        assert all(b["by_queue"] == {} for b in body["buckets"])


def test_throughput_rejects_unknown_range(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "9d"})
        assert resp.status_code == 422


def test_throughput_7d_range_uses_6_hour_buckets(isolated_config):
    """2026-08-17: the bytes chart's new week view -- 21600s (6h) buckets covering the trailing
    168 hours. The bucket-boundary walk (`get_throughput`) is inclusive of both ends, so a
    range of N whole buckets always yields N+1 points (168h / 21600s = 28 buckets, 29 points,
    exactly the same off-by-one the existing 24h/3600s case already has -- see 24h's own math:
    24 buckets, 25 points); same empty-database all-down shape the 1h test above pins.
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "7d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "7d"
        assert body["bucket_seconds"] == 21600
        assert len(body["buckets"]) == 29
        assert all(b["up"] is False for b in body["buckets"])


def test_throughput_30d_range_uses_1_day_buckets(isolated_config):
    """2026-08-17: the bytes chart's new month view -- 86400s (1d) buckets covering the
    trailing 720 hours (30 buckets, 31 points -- see the 7d test above for the off-by-one
    reasoning).
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "30d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "30d"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 31
        assert all(b["up"] is False for b in body["buckets"])


def test_throughput_reflects_seeded_heartbeat_and_samples(isolated_config, tmp_path):
    """Drive the real endpoint against real rows, inserted directly (bypassing the sampler,
    which is exercised separately in tests/test_metrics.py) -- proves the endpoint's own SQL
    bucketing and idle-vs-down assembly, not the sampler's math again.
    """
    with TestClient(app) as client:
        # A host + queue so /api/settings/host and /api/settings/queues have something real,
        # though the throughput endpoint itself only needs a queue_id to exist for the FK.
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
        # `prompts/done/2026-08-16-path-browse-dialog.md`); `remote_path` stays a fake literal
        # -- that check is best-effort and this test's host is unreachable.
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/remote", "local_path": tempfile.mkdtemp()},
        )
        assert resp.status_code == 201, resp.text
        queue_id = resp.json()["id"]

    # Insert rows directly against the same on-disk database the app just created (isolated_config
    # points config_dir at tmp_path for the whole test).
    async def seed():
        conn = await connect(str(tmp_path))
        now = datetime.now(UTC)
        recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await conn.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (recent,))
        await conn.execute(
            "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
            (queue_id, recent, 12_345_678),
        )
        await conn.commit()
        await conn.close()

    asyncio.run(seed())

    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1h", "queue_id": queue_id})
        assert resp.status_code == 200
        body = resp.json()
        up_buckets = [b for b in body["buckets"] if b["up"]]
        assert len(up_buckets) >= 1
        assert any(b["total_bytes"] == 12_345_678 for b in up_buckets)
        assert any(b["by_queue"].get(str(queue_id)) == 12_345_678 for b in up_buckets)

        # Same window, no queue filter -- the site-total shape, must show the same total.
        resp = client.get("/api/metrics/throughput", params={"range": "1h"})
        body = resp.json()
        up_buckets = [b for b in body["buckets"] if b["up"]]
        assert any(b["total_bytes"] == 12_345_678 for b in up_buckets)


# --- Daily rollups (prompts/done/2026-08-21-daily-metric-rollups.md): 90d/1y ranges and the
# "total downloaded" endpoint --------------------------------------------------------------


def _seed_host_and_queue(client: TestClient) -> int:
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
    resp = client.post(
        "/api/settings/queues",
        json={"name": "TV", "remote_path": "/remote", "local_path": tempfile.mkdtemp()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_throughput_90d_range_uses_one_day_buckets_and_is_all_down_on_an_empty_database(
    isolated_config,
):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "90d"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 90
        assert all(b["up"] is False for b in body["buckets"])
        assert all(b["total_bytes"] is None for b in body["buckets"])
        assert all(b["coverage"] is None for b in body["buckets"])


def test_throughput_1y_range_uses_one_day_buckets(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1y"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "1y"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 365


def test_throughput_90d_reflects_rolled_up_daily_rows_and_their_coverage(isolated_config, tmp_path):
    """Unlike the raw-table ranges, 90d/1y read `metric_daily` directly -- seed a rolled-up row
    (bypassing `core/metrics.py.rollup_day`, which is exercised in tests/test_metrics.py) and
    prove the endpoint surfaces its bytes, up/down, and coverage correctly.
    """
    with TestClient(app) as client:
        queue_id = _seed_host_and_queue(client)

    async def seed():
        conn = await connect(str(tmp_path))
        day = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
        await conn.execute(
            "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (queue_id, day, 7_000_000, 1440, "2026-08-21T00:00:00.000000Z"),  # ~half coverage
        )
        await conn.commit()
        await conn.close()
        return day

    day = asyncio.run(seed())

    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d", "queue_id": queue_id})
        assert resp.status_code == 200
        body = resp.json()
        seeded = next(b for b in body["buckets"] if b["ts"].startswith(day))
        assert seeded["up"] is True
        assert seeded["total_bytes"] == 7_000_000
        assert seeded["by_queue"].get(str(queue_id)) == 7_000_000
        assert seeded["coverage"] == pytest.approx(0.5, abs=0.01)

        # Every other bucket in range has no daily row at all -- a full gap, same as "no
        # heartbeat" on the raw-table ranges.
        other_buckets = [b for b in body["buckets"] if not b["ts"].startswith(day)]
        assert all(b["up"] is False and b["total_bytes"] is None for b in other_buckets)


def test_metrics_total_is_zero_with_no_history_on_an_empty_database(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/total")
        assert resp.status_code == 200
        assert resp.json() == {"total_bytes": 0, "since_day": None}


def test_metrics_total_sums_daily_rows_plus_todays_raw_samples(isolated_config, tmp_path):
    with TestClient(app) as client:
        queue_id = _seed_host_and_queue(client)

    async def seed():
        conn = await connect(str(tmp_path))
        past_day = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%d")
        await conn.execute(
            "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (queue_id, past_day, 3_000_000, 2880, "2026-08-21T00:00:00.000000Z"),
        )
        today = datetime.now(UTC)
        await conn.execute(
            "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
            (queue_id, today.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 500_000),
        )
        await conn.commit()
        await conn.close()
        return past_day

    past_day = asyncio.run(seed())

    with TestClient(app) as client:
        resp = client.get("/api/metrics/total", params={"queue_id": queue_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_bytes"] == 3_500_000
        assert body["since_day"] == past_day
