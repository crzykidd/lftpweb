"""core/metrics.py (this task's handoff prompt) -- the throughput sample store. Settings
round-trip, the sampler's tick-counting and non-monotonic-safe delta math (the "job
restarting mid-flight" trap the prompt calls out by name), idle-vs-down via the heartbeat
table, bucketed querying, and retention pruning.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from lftpweb.core.metrics import (
    DEFAULT_RETENTION_DAYS,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    MetricsRetentionScheduler,
    MetricsSettings,
    RunningJobBytes,
    ThroughputSampler,
    clamp_retention_days,
    heartbeat_buckets,
    load_metrics_settings,
    prune_metrics,
    queue_breakdown,
    save_metrics_settings,
)
from lftpweb.db import connect, migrate


async def _fresh_db(config_dir: str):
    conn = await connect(config_dir)
    await migrate(conn)
    return conn


async def _seed_host_and_queues(conn, n: int = 2) -> None:
    await conn.execute(
        "INSERT INTO host (id, name, address, username, auth_method) VALUES "
        "(1, 'seedbox', '1.2.3.4', 'user', 'key')"
    )
    for q in range(1, n + 1):
        await conn.execute(
            "INSERT INTO path_queue (id, host_id, name, remote_path, local_path) VALUES "
            "(?, 1, ?, ?, ?)",
            (q, f"queue{q}", f"/remote/{q}", f"/local/{q}"),
        )
    await conn.commit()


# --- Settings --------------------------------------------------------------------------------


async def test_default_retention_is_seven_days(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    settings = await load_metrics_settings(conn)
    assert settings.retention_days == DEFAULT_RETENTION_DAYS == 7
    await conn.close()


async def test_settings_round_trip(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await save_metrics_settings(conn, MetricsSettings(retention_days=21))
    loaded = await load_metrics_settings(conn)
    assert loaded.retention_days == 21
    await conn.close()


def test_clamp_retention_days_bounds():
    assert clamp_retention_days(0) == MIN_RETENTION_DAYS
    assert clamp_retention_days(1) == 1
    assert clamp_retention_days(30) == 30
    assert clamp_retention_days(365) == MAX_RETENTION_DAYS


async def test_load_clamps_a_previously_stored_out_of_range_value(tmp_path):
    """A value written before validation existed, or edited directly in the database, must
    still load to something sane rather than propagate an out-of-range number into a query.
    """
    conn = await _fresh_db(str(tmp_path))
    await conn.execute(
        "INSERT INTO setting (key, value) VALUES ('metrics_settings', ?)",
        (json.dumps({"retention_days": 9999}),),
    )
    await conn.commit()
    settings = await load_metrics_settings(conn)
    assert settings.retention_days == MAX_RETENTION_DAYS
    await conn.close()


# --- ThroughputSampler: tick counting, heartbeat, idle vs down -----------------------------


async def test_sampler_writes_nothing_before_the_interval_elapses(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=30)
    for _ in range(29):
        await sampler.tick([RunningJobBytes(job_id=1, queue_id=1, bytes_done=100, bytes_start=0)])

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_heartbeat")
    assert (await cur.fetchone())["c"] == 0
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_sample")
    assert (await cur.fetchone())["c"] == 0
    await conn.close()


async def test_idle_tick_still_writes_a_heartbeat_but_no_sample_row(tmp_path):
    """Decision 6: idle (heartbeat continues, no bytes moved) must be told apart from down
    (no heartbeat at all) without a zero-byte row per queue per interval.
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=5)
    for _ in range(5):
        await sampler.tick([])  # nothing running -- lftpweb is up, just idle

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_heartbeat")
    assert (await cur.fetchone())["c"] == 1
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_sample")
    assert (await cur.fetchone())["c"] == 0
    await conn.close()


async def test_a_running_job_produces_a_positive_delta_on_its_first_sample(tmp_path):
    """First sighting of a job is not "no history, delta 0" -- `bytes_done - bytes_start`
    already excludes anything on disk before this job started, so the whole amount is real,
    newly-moved data (module docstring).
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=3)
    for _ in range(3):
        await sampler.tick(
            [RunningJobBytes(job_id=1, queue_id=1, bytes_done=5_000_000, bytes_start=0)]
        )

    cur = await conn.execute("SELECT queue_id, bytes_delta FROM metric_sample")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["queue_id"] == 1
    assert rows[0]["bytes_delta"] == 5_000_000
    await conn.close()


async def test_delta_across_two_sample_windows_is_the_incremental_amount_only(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=2)
    job = RunningJobBytes(job_id=1, queue_id=1, bytes_done=1_000_000, bytes_start=0)
    await sampler.tick([job])
    await sampler.tick([job])  # first sample: delta = 1,000,000

    job2 = RunningJobBytes(job_id=1, queue_id=1, bytes_done=1_600_000, bytes_start=0)
    await sampler.tick([job2])
    await sampler.tick([job2])  # second sample: delta = 600,000, not 1,600,000

    cur = await conn.execute("SELECT bytes_delta FROM metric_sample ORDER BY id")
    deltas = [r["bytes_delta"] for r in await cur.fetchall()]
    assert deltas == [1_000_000, 600_000]
    await conn.close()


async def test_job_restarting_mid_flight_produces_no_negative_or_inflated_sample(tmp_path):
    """The exact trap the handoff prompt names: `job.bytes_done` is the *absolute* local
    footprint, not a per-job delta, so a naive `current - previous` differenced by job id goes
    negative (job id disappears, a new one appears already holding the old job's bytes) or
    inflated (the new job's first `bytes_done` reads as a phantom spike of everything the old
    job had already moved). This sampler must show neither: only the bytes the *new* job
    itself moved, after it started.
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=3)

    # job 1 (attempt 1): starts from nothing, accumulates to 8 MB on disk, then dies (a
    # transient error) -- 8 MB is genuinely new data and must be counted once.
    job1 = RunningJobBytes(job_id=101, queue_id=1, bytes_done=8_000_000, bytes_start=0)
    await sampler.tick([job1])
    await sampler.tick([job1])
    await sampler.tick([job1])  # sample #1: delta = 8,000,000 (job1's first sighting)

    # job1 is gone (reaped -- core/queue.py._reap_one calls sampler.drop(101), simulated here
    # by simply never passing job_id=101 again). job 2 (attempt 2, a new job id) resumes with
    # `-c`: bytes_start = 8,000,000 (the 8 MB job1 already left on disk), and moves another
    # 2 MB of genuinely new data before the next sample.
    job2 = RunningJobBytes(job_id=102, queue_id=1, bytes_done=10_000_000, bytes_start=8_000_000)
    await sampler.tick([job2])
    await sampler.tick([job2])
    await sampler.tick(
        [job2]
    )  # sample #2: delta must be 2,000,000, not 10,000,000 and not negative

    cur = await conn.execute("SELECT bytes_delta FROM metric_sample ORDER BY id")
    deltas = [r["bytes_delta"] for r in await cur.fetchall()]
    assert deltas == [8_000_000, 2_000_000]
    assert all(d >= 0 for d in deltas)
    assert sum(deltas) == 10_000_000  # exactly what was ever on disk -- no double count either
    await conn.close()


async def test_drop_prevents_a_reused_job_id_from_inheriting_stale_history(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=1)
    await sampler.tick([RunningJobBytes(job_id=1, queue_id=1, bytes_done=5_000_000, bytes_start=0)])
    assert 1 in sampler._prev_contribution
    sampler.drop(1)
    assert 1 not in sampler._prev_contribution
    await conn.close()


async def test_stale_job_ids_are_pruned_automatically_when_absent_from_running(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=1)  # samples every tick
    await sampler.tick([RunningJobBytes(job_id=1, queue_id=1, bytes_done=1000, bytes_start=0)])
    assert 1 in sampler._prev_contribution
    await sampler.tick([])  # job 1 no longer running
    assert 1 not in sampler._prev_contribution
    await conn.close()


async def test_two_queues_are_kept_separate_in_the_same_sample(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    sampler = ThroughputSampler(conn, tick_interval=1)
    await sampler.tick(
        [
            RunningJobBytes(job_id=1, queue_id=1, bytes_done=3_000_000, bytes_start=0),
            RunningJobBytes(job_id=2, queue_id=2, bytes_done=1_000_000, bytes_start=0),
        ]
    )
    cur = await conn.execute("SELECT queue_id, bytes_delta FROM metric_sample ORDER BY queue_id")
    rows = {r["queue_id"]: r["bytes_delta"] for r in await cur.fetchall()}
    assert rows == {1: 3_000_000, 2: 1_000_000}
    await conn.close()


async def test_a_stalled_job_with_zero_new_bytes_writes_no_sample_row(tmp_path):
    """Decision 6, restated for a job rather than an idle queue: a job that's running but has
    made no progress since the last sample must not pad the table with a zero row."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    sampler = ThroughputSampler(conn, tick_interval=1)
    job = RunningJobBytes(job_id=1, queue_id=1, bytes_done=1_000_000, bytes_start=0)
    await sampler.tick([job])  # first sample: delta = 1,000,000 (job's own first sighting)
    await sampler.tick([job])  # second sample: no change -> delta 0 -> no row

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_sample")
    assert (await cur.fetchone())["c"] == 1
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_heartbeat")
    assert (await cur.fetchone())["c"] == 2  # heartbeat still fires every sample, stalled or not
    await conn.close()


# --- Querying (api/metrics.py's building blocks) --------------------------------------------


async def test_queue_breakdown_buckets_and_sums_within_range(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    await conn.executemany(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
        [
            (1, "2026-08-12T00:00:00.000000Z", 1_000_000),
            (1, "2026-08-12T00:00:30.000000Z", 2_000_000),  # same hour bucket as above
            (1, "2026-08-12T01:00:00.000000Z", 4_000_000),  # next hour bucket
            (2, "2026-08-12T00:00:00.000000Z", 500_000),
        ],
    )
    await conn.commit()

    rows = await queue_breakdown(
        conn,
        start_ts="2026-08-12T00:00:00.000000Z",
        end_ts="2026-08-12T02:00:00.000000Z",
        bucket_seconds=3600,
    )
    by_bucket_queue = {(epoch, qid): bytes_ for epoch, qid, bytes_ in rows}
    # Assert relative bucket spacing and totals rather than an absolute epoch -- timezone-safe
    # and doesn't rot if the bucketing expression's exact epoch math ever changes.
    epochs = sorted({e for e, _ in by_bucket_queue})
    assert len(epochs) == 2
    assert epochs[1] - epochs[0] == 3600
    assert by_bucket_queue[(epochs[0], 1)] == 3_000_000  # 1,000,000 + 2,000,000
    assert by_bucket_queue[(epochs[0], 2)] == 500_000
    assert by_bucket_queue[(epochs[1], 1)] == 4_000_000
    assert (epochs[1], 2) not in by_bucket_queue  # queue 2 moved nothing in that bucket
    await conn.close()


async def test_queue_breakdown_filters_to_one_queue_when_given_an_id(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    await conn.executemany(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
        [
            (1, "2026-08-12T00:00:00.000000Z", 1_000_000),
            (2, "2026-08-12T00:00:00.000000Z", 500_000),
        ],
    )
    await conn.commit()

    rows = await queue_breakdown(
        conn,
        start_ts="2026-08-12T00:00:00.000000Z",
        end_ts="2026-08-12T01:00:00.000000Z",
        bucket_seconds=3600,
        queue_id=2,
    )
    assert len(rows) == 1
    assert rows[0][1] == 2
    assert rows[0][2] == 500_000
    await conn.close()


async def test_heartbeat_buckets_reports_which_buckets_had_a_heartbeat(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await conn.executemany(
        "INSERT INTO metric_heartbeat (ts) VALUES (?)",
        [("2026-08-12T00:00:00.000000Z",), ("2026-08-12T02:00:00.000000Z",)],
    )
    await conn.commit()

    buckets = await heartbeat_buckets(
        conn,
        start_ts="2026-08-12T00:00:00.000000Z",
        end_ts="2026-08-12T03:00:00.000000Z",
        bucket_seconds=3600,
    )
    assert len(buckets) == 2  # hour 0 and hour 2 -- hour 1 has no heartbeat (a gap)
    epochs = sorted(buckets)
    assert epochs[1] - epochs[0] == 7200
    await conn.close()


# --- Retention/pruning -----------------------------------------------------------------------


async def test_prune_metrics_removes_only_rows_older_than_retention(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent_ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    await conn.execute(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)", (1, old_ts, 100)
    )
    await conn.execute(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
        (1, recent_ts, 200),
    )
    await conn.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (old_ts,))
    await conn.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (recent_ts,))
    await conn.commit()

    removed = await prune_metrics(conn, retention_days=7)
    assert removed == (1, 1)

    cur = await conn.execute("SELECT bytes_delta FROM metric_sample")
    rows = await cur.fetchall()
    assert [r["bytes_delta"] for r in rows] == [200]
    cur = await conn.execute("SELECT ts FROM metric_heartbeat")
    rows = await cur.fetchall()
    assert [r["ts"] for r in rows] == [recent_ts]
    await conn.close()


async def test_retention_scheduler_start_stop_and_is_alive(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    scheduler = MetricsRetentionScheduler(db=conn)
    assert scheduler.is_alive is False
    await scheduler.start()
    assert scheduler.is_alive is True
    await scheduler.stop()
    assert scheduler.is_alive is False
    await conn.close()


async def test_retention_scheduler_run_once_prunes(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    await save_metrics_settings(conn, MetricsSettings(retention_days=1))
    old_ts = (datetime.now(UTC) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    await conn.execute(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)", (1, old_ts, 100)
    )
    await conn.commit()

    scheduler = MetricsRetentionScheduler(db=conn)
    removed = await scheduler.run_once()
    assert removed == (1, 0)
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_sample")
    assert (await cur.fetchone())["c"] == 0
    await conn.close()
