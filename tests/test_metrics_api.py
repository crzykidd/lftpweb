"""Integration tests for GET /api/metrics/throughput and the retention settings endpoint,
over the real HTTP app via TestClient -- mirrors tests/test_backup_api.py's shape.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from lftpweb.db import connect
from lftpweb.main import app


def test_metrics_settings_default_shape(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/settings/metrics")
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 7}


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
        resp = client.post(
            "/api/settings/queues",
            json={"name": "TV", "remote_path": "/remote", "local_path": "/local"},
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
