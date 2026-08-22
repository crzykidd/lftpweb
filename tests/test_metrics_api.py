"""Integration tests for GET /api/metrics/throughput and the retention settings endpoint,
over the real HTTP app via TestClient -- mirrors tests/test_backup_api.py's shape.

2026-08-21 (chart grouping, prompts/done/2026-08-21-chart-grouping.md): range (how far back)
and group (how wide a bar) are now two separate query params. Most of the section below groups
tests by that split: per-range defaults, explicit-group overrides, the hourly-at-90d/1y
rejection, and week/month aggregation (raw-table-sourced and metric_daily-sourced).
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lftpweb.core.metrics import EXPECTED_HEARTBEATS_PER_DAY
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
    back `up: false`, not a fabricated zero (decision recorded in docs/decisions.md). `1h` is
    one of the speed chart's own untouched fixed-width ranges -- `group` is always `null` here,
    never one of the four grouping names.
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1h"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "1h"
        assert body["group"] is None
        assert body["bucket_seconds"] == 60
        assert len(body["buckets"]) >= 1
        assert all(b["up"] is False for b in body["buckets"])
        assert all(b["total_bytes"] is None for b in body["buckets"])
        assert all(b["by_queue"] == {} for b in body["buckets"])


def test_throughput_rejects_unknown_range(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "9d"})
        assert resp.status_code == 422


def test_throughput_rejects_unknown_group(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "24h", "group": "fortnight"})
        assert resp.status_code == 422


# --- Per-range default grouping (task table) ----------------------------------------------


def test_throughput_24h_default_group_is_hour(isolated_config):
    """24h was already right before this task -- hourly stays the default."""
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "24h"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "24h"
        assert body["group"] == "hour"
        assert body["bucket_seconds"] == 3600
        assert len(body["buckets"]) == 25  # 24 buckets, inclusive walk -> 25 points
        assert all(b["up"] is False for b in body["buckets"])


def test_throughput_7d_default_group_is_day(isolated_config):
    """The one default that actually changes (task table): 7d moves from 28 x 6-hour buckets
    to daily.
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "7d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "7d"
        assert body["group"] == "day"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 8  # 168h / 86400s = 7 buckets, inclusive walk -> 8 points
        assert all(b["up"] is False for b in body["buckets"])


def test_throughput_30d_default_group_is_day(isolated_config):
    """30d was already daily before this task -- unchanged."""
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "30d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "30d"
        assert body["group"] == "day"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 31
        assert all(b["up"] is False for b in body["buckets"])


def _expected_week_bucket_count(days: int) -> int:
    """Independent calendar arithmetic (not a re-import of the SUT) pinning how many
    epoch-anchored 7-day buckets a `days`-long trailing window actually spans -- avoids a
    hardcoded count that would be wrong (or flaky) depending on which day the test happens to
    run, since `api/metrics.py._week_key` anchors to the Unix epoch, not to "today."
    """
    now = datetime.now(UTC)
    end_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start_day = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    keys = set()
    d = start_date
    while d <= end_date:
        keys.add(int(d.timestamp()) // (7 * 86400))
        d += timedelta(days=1)
    return len(keys)


def test_throughput_90d_default_group_is_week(isolated_config):
    """90d moves from daily to weekly by default (task table) -- all down on an empty database."""
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "90d"
        assert body["group"] == "week"
        assert body["bucket_seconds"] == 604800
        assert len(body["buckets"]) == _expected_week_bucket_count(90)
        assert all(b["up"] is False for b in body["buckets"])
        assert all(b["total_bytes"] is None for b in body["buckets"])
        assert all(b["coverage"] is None for b in body["buckets"])


def test_throughput_1y_default_group_is_week(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1y"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "1y"
        assert body["group"] == "week"
        assert body["bucket_seconds"] == 604800
        assert len(body["buckets"]) == _expected_week_bucket_count(365)


# --- Explicit group overrides -- range and group are independent axes ----------------------


def test_throughput_7d_explicit_group_hour(isolated_config):
    """Decoupling range from grouping means a caller can still ask for hourly detail over a
    week, even though daily is now the default -- 168 hourly buckets, inclusive walk -> 169
    points.
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "7d", "group": "hour"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "hour"
        assert body["bucket_seconds"] == 3600
        assert len(body["buckets"]) == 169


def test_throughput_90d_explicit_group_day_matches_pre_task_shape(isolated_config):
    """The 90d/1y ranges' old (pre-task) default was daily -- still available, just not the
    default anymore. Same shape as before: one bucket per day, straight from `metric_daily`.
    """
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d", "group": "day"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "day"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 90
        assert all(b["up"] is False for b in body["buckets"])


def test_throughput_1y_explicit_group_day(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1y", "group": "day"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "day"
        assert body["bucket_seconds"] == 86400
        assert len(body["buckets"]) == 365


# --- Hourly is architecturally impossible at 90d/1y -- rejected server-side, not downgraded -


def test_throughput_hourly_rejected_for_90d(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d", "group": "hour"})
        assert resp.status_code == 422
        assert "90d" in resp.json()["detail"]


def test_throughput_hourly_rejected_for_1y(isolated_config):
    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "1y", "group": "hour"})
        assert resp.status_code == 422
        assert "1y" in resp.json()["detail"]


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


def test_throughput_90d_reflects_rolled_up_daily_rows_and_their_coverage(isolated_config, tmp_path):
    """Unlike the raw-table ranges, 90d/1y at `group=day` read `metric_daily` directly -- seed a
    rolled-up row (bypassing `core/metrics.py.rollup_day`, which is exercised in
    tests/test_metrics.py) and prove the endpoint surfaces its bytes, up/down, and coverage
    correctly. Explicit `group=day` since the default changed to `week` in this task.
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
        resp = client.get(
            "/api/metrics/throughput",
            params={"range": "90d", "group": "day", "queue_id": queue_id},
        )
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


# --- Week/month grouping (2026-08-21, chart grouping): summed from daily rows on read -------


def _same_week_offsets(count: int, *, min_start: int = 5, max_start: int = 39) -> list[int]:
    """`count` consecutive "days ago" offsets, starting no closer than `min_start` days back (by
    default 5, so they're never today or yesterday, which the daily table never rolls up anyway),
    that all land in the same epoch-anchored week bucket (`api/metrics.py._week_key`). Computed
    from the real "now" at test time rather than a hardcoded date, so the test is never flaky
    depending on which day it happens to run relative to a week boundary. `max_start` caps how far
    back the search goes -- callers seeding a range shorter than 39 days must lower it so the
    offsets found still fall inside that range.
    """
    now = datetime.now(UTC)
    for start in range(min_start, max_start + 1):
        days = [(now - timedelta(days=start + i)).strftime("%Y-%m-%d") for i in range(count)]
        keys = {
            int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()) // (7 * 86400)
            for d in days
        }
        if len(keys) == 1:
            return [start + i for i in range(count)]
    raise AssertionError(  # pragma: no cover
        f"could not find {count} consecutive same-week day offsets in [{min_start}, {max_start}]"
    )


def _week_member_count(days_back: int, target_day: str) -> int:
    """How many days of the `days_back`-long trailing window share `target_day`'s epoch-anchored
    week bucket -- independent calendar arithmetic (not a re-import of `_week_key`) used to
    compute an exact expected denominator for a week bucket's coverage fraction, rather than
    assuming every week bucket has exactly 7 members (the two at the range's own edges can have
    fewer).
    """
    now = datetime.now(UTC)
    end_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start_day = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    target_key = int(datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()) // (
        7 * 86400
    )
    count = 0
    d = start_date
    while d <= end_date:
        if int(d.timestamp()) // (7 * 86400) == target_key:
            count += 1
        d += timedelta(days=1)
    return count


def test_throughput_90d_week_grouping_sums_daily_rows_and_survives_idle_vs_down(
    isolated_config, tmp_path
):
    """Week buckets are summed from `metric_daily` rows on read (task: no new table) -- and cover
    *every* day in that epoch-anchored week within the queried range, not just the days a caller
    happened to seed. Seeds two same-week days with real rolled-up data (one fully covered, one
    partially); every other day sharing that week is left entirely absent (a true down day, since
    `metric_daily` never gets a row for a day nothing rolled up). Checks: the week bucket sums
    only the seeded (`up`) days' bytes -- the absent days contribute nothing (idle-vs-down
    survives regrouping) -- and coverage is the day-count fraction (seeded days over the week's
    real day count), not a heartbeat-density average.
    """
    with TestClient(app) as client:
        queue_id = _seed_host_and_queue(client)

    offset_a, offset_b = _same_week_offsets(2)

    async def seed():
        conn = await connect(str(tmp_path))
        now = datetime.now(UTC)
        day_a = (now - timedelta(days=offset_a)).strftime("%Y-%m-%d")
        day_b = (now - timedelta(days=offset_b)).strftime("%Y-%m-%d")
        # Every other day sharing this week is deliberately never inserted -- a full gap each.
        await conn.execute(
            "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (queue_id, day_a, 4_000_000, EXPECTED_HEARTBEATS_PER_DAY, "2026-08-21T00:00:00Z"),
        )
        await conn.execute(
            "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (queue_id, day_b, 1_000_000, 720, "2026-08-21T00:00:00Z"),  # partial day, half up
        )
        await conn.commit()
        await conn.close()
        return day_a, day_b

    day_a, day_b = asyncio.run(seed())

    with TestClient(app) as client:
        resp = client.get(
            "/api/metrics/throughput",
            params={"range": "90d", "group": "week", "queue_id": queue_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "week"

        # Both seeded days must land in the very same bucket (that's the whole point of the
        # offset search above) -- find it by content rather than by exact `ts`, to stay robust
        # to which of the two seeded days sorts first as the bucket's own `ts`.
        matching = [
            b
            for b in body["buckets"]
            if b["up"]
            and b["total_bytes"] == 5_000_000
            and b["by_queue"].get(str(queue_id)) == 5_000_000
        ]
        assert len(matching) == 1, body["buckets"]
        week_bucket = matching[0]
        expected_coverage = 2 / _week_member_count(90, day_a)
        assert week_bucket["coverage"] == pytest.approx(expected_coverage, abs=0.01)


def test_throughput_30d_week_grouping_sums_raw_table_days(isolated_config, tmp_path):
    """The raw-table-sourced counterpart to the `metric_daily` test above -- 30d reads
    `metric_sample`/`metric_heartbeat` directly, so a week bucket there is built from
    `_raw_day_points`, not `metric_daily`. Two seeded days, both landing in the same
    epoch-anchored week bucket (`_same_week_offsets`, not just "close together" -- adjacent
    calendar days can still straddle a week boundary), must sum into one bucket.
    """
    with TestClient(app) as client:
        queue_id = _seed_host_and_queue(client)

    # 30d's raw window is 720 hours back -- keep both offsets comfortably inside it.
    offset_a, offset_b = _same_week_offsets(2, min_start=2, max_start=25)

    async def seed():
        conn = await connect(str(tmp_path))
        now = datetime.now(UTC)
        # Noon UTC of each day -- comfortably inside that calendar day's own bucket regardless
        # of what time "now" itself is.
        ts_a = (now - timedelta(days=offset_a)).strftime("%Y-%m-%dT12:00:00.000000Z")
        ts_b = (now - timedelta(days=offset_b)).strftime("%Y-%m-%dT12:00:00.000000Z")
        for ts, amount in ((ts_a, 2_000_000), (ts_b, 3_000_000)):
            await conn.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (ts,))
            await conn.execute(
                "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
                (queue_id, ts, amount),
            )
        await conn.commit()
        await conn.close()

    asyncio.run(seed())

    with TestClient(app) as client:
        resp = client.get(
            "/api/metrics/throughput",
            params={"range": "30d", "group": "week", "queue_id": queue_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "week"
        assert body["bucket_seconds"] == 604800

        up_buckets = [b for b in body["buckets"] if b["up"]]
        assert len(up_buckets) >= 1
        assert any(b["total_bytes"] == 5_000_000 for b in up_buckets)
        assert any(b["by_queue"].get(str(queue_id)) == 5_000_000 for b in up_buckets)


def test_throughput_90d_month_grouping_spans_the_expected_number_of_calendar_months(
    isolated_config,
):
    """`month` buckets group by calendar month string, not a fixed number of seconds -- on an
    empty database, the bucket *count* must still match how many distinct calendar months a
    90-day trailing window actually touches (computed independently below, the same calendar
    arithmetic `api/metrics.py._month_key` performs, not a re-import of it).
    """
    now = datetime.now(UTC)
    end_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start_day = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    months = set()
    d = start_date
    while d <= end_date:
        months.add(d.strftime("%Y-%m"))
        d += timedelta(days=1)

    with TestClient(app) as client:
        resp = client.get("/api/metrics/throughput", params={"range": "90d", "group": "month"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["group"] == "month"
        assert body["bucket_seconds"] == 2_592_000
        assert len(body["buckets"]) == len(months)
        assert all(b["up"] is False for b in body["buckets"])
