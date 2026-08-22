"""core/metrics.py (this task's handoff prompt) -- the throughput sample store. Settings
round-trip, the sampler's tick-counting and non-monotonic-safe delta math (the "job
restarting mid-flight" trap the prompt calls out by name), idle-vs-down via the heartbeat
table, bucketed querying, and retention pruning.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from lftpweb.core.metrics import (
    DAILY_RETENTION_DAYS,
    DEFAULT_RETENTION_DAYS,
    EXPECTED_HEARTBEATS_PER_DAY,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    MetricsRetentionScheduler,
    MetricsSettings,
    RunningJobBytes,
    ThroughputSampler,
    clamp_retention_days,
    daily_totals,
    heartbeat_buckets,
    load_metrics_settings,
    prune_daily_metrics,
    prune_metrics,
    queue_breakdown,
    rollup_day,
    rollup_recent_days,
    save_metrics_settings,
    total_bytes,
)
from lftpweb.db import connect, migrate


async def _fresh_db(config_dir: str):
    conn = await connect(config_dir)
    await migrate(conn)
    return conn


def _day_str(offset_days: int) -> str:
    """UTC calendar date `'YYYY-MM-DD'`, `offset_days` before today -- 0 is today, 1 is
    yesterday, etc. Every daily-rollup test below anchors its seeded days to `datetime.now(UTC)`
    this way, exactly like the existing raw-retention tests above anchor their timestamps.
    """
    return (datetime.now(UTC) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _iso_on_day(day: str, seconds_offset: int = 0) -> str:
    """A raw `ts` string that falls on the given UTC calendar day, `seconds_offset` seconds
    after its midnight -- the same ISO shape every raw table column already uses.
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return (base + timedelta(seconds=seconds_offset)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _seed_heartbeats(conn, day: str, count: int) -> None:
    rows = [(_iso_on_day(day, i),) for i in range(count)]
    if rows:
        await conn.executemany("INSERT INTO metric_heartbeat (ts) VALUES (?)", rows)
        await conn.commit()


async def _seed_sample(
    conn, day: str, queue_id: int, bytes_delta: int, *, seconds_offset: int = 1
) -> None:
    await conn.execute(
        "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
        (queue_id, _iso_on_day(day, seconds_offset), bytes_delta),
    )
    await conn.commit()


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


async def test_default_retention_is_thirty_days(tmp_path):
    """2026-08-21 (prompts/done/2026-08-21-daily-metric-rollups.md): raised 7 -> 30 so the
    Dashboard's offered `30d` range works out of the box; `MAX_RETENTION_DAYS` is unchanged.
    """
    conn = await _fresh_db(str(tmp_path))
    settings = await load_metrics_settings(conn)
    assert settings.retention_days == DEFAULT_RETENTION_DAYS == 30
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


# --- Daily rollups (prompts/done/2026-08-21-daily-metric-rollups.md) -----------------------


async def test_rollup_day_sums_raw_samples_and_records_coverage(tmp_path):
    """Task test 6: daily totals equal the sum of the raw samples they replace, for a day
    still present in both -- plus the coverage figure (item 3) that comes along for the ride.
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    day = _day_str(2)  # a comfortably closed day
    await _seed_heartbeats(conn, day, count=100)
    await _seed_sample(conn, day, queue_id=1, bytes_delta=3_000_000, seconds_offset=10)
    await _seed_sample(
        conn, day, queue_id=1, bytes_delta=2_000_000, seconds_offset=20
    )  # 2nd row, same queue/day
    await _seed_sample(conn, day, queue_id=2, bytes_delta=500_000, seconds_offset=15)

    written = await rollup_day(conn, day)
    assert written == 2  # one row per currently-existing queue

    cur = await conn.execute(
        "SELECT queue_id, bytes, heartbeat_count FROM metric_daily WHERE day = ? ORDER BY queue_id",
        (day,),
    )
    rows = await cur.fetchall()
    assert [(r["queue_id"], r["bytes"], r["heartbeat_count"]) for r in rows] == [
        (1, 5_000_000, 100),  # 3,000,000 + 2,000,000 -- summed, not just the last row
        (2, 500_000, 100),
    ]
    await conn.close()


async def test_rollup_day_returns_zero_and_writes_nothing_without_any_heartbeat(tmp_path):
    """A day with zero heartbeats site-wide is entirely down -- no row for any queue, consistent
    with `metric_sample`'s own gap semantics (no row means no data, not a measured zero)."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    day = _day_str(2)
    await _seed_sample(
        conn, day, queue_id=1, bytes_delta=1_000_000
    )  # samples with no heartbeat: shouldn't happen in practice, but the day still can't be rolled up honestly

    written = await rollup_day(conn, day)
    assert written == 0
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_daily")
    assert (await cur.fetchone())["c"] == 0
    await conn.close()


async def test_rollup_day_is_idempotent_and_recomputes_rather_than_increments(tmp_path):
    """Task test 2: rolling up the same day twice must not double-count -- the upsert overwrites
    with a freshly-summed value rather than adding to whatever was there before."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    day = _day_str(2)
    await _seed_heartbeats(conn, day, count=50)
    await _seed_sample(conn, day, queue_id=1, bytes_delta=1_000_000)

    assert await rollup_day(conn, day) == 1
    assert await rollup_day(conn, day) == 1  # re-rolling writes the same row again, not a 2nd one

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_daily")
    assert (await cur.fetchone())["c"] == 1
    cur = await conn.execute(
        "SELECT bytes FROM metric_daily WHERE day = ? AND queue_id = 1", (day,)
    )
    assert (await cur.fetchone())["bytes"] == 1_000_000

    # More raw data for the same day arrives (e.g. a late-arriving sample), then rolled up again
    # -- the stored total must be the fresh sum, never the old stored value plus the new sample.
    await _seed_sample(conn, day, queue_id=1, bytes_delta=250_000, seconds_offset=99)
    await rollup_day(conn, day)
    cur = await conn.execute(
        "SELECT bytes FROM metric_daily WHERE day = ? AND queue_id = 1", (day,)
    )
    assert (await cur.fetchone())["bytes"] == 1_250_000  # recomputed sum, not 1,000,000 + 1,250,000
    await conn.close()


async def test_rollup_recent_days_backfills_several_missing_days(tmp_path):
    """Task test 3: startup backfill -- several days' worth of raw data sitting unrolled (the
    app was down for a stretch, or this feature was just installed) all get picked up in one
    call, with no per-day bookkeeping the caller has to drive."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    days = [_day_str(offset) for offset in (5, 3, 2)]  # gaps at 4 and 1 -- no heartbeats there
    for day in days:
        await _seed_heartbeats(conn, day, count=10)
        await _seed_sample(conn, day, queue_id=1, bytes_delta=100_000)

    total_written = await rollup_recent_days(conn, lookback_days=6)
    assert total_written == len(days)  # one row per seeded day (1 queue each)

    cur = await conn.execute("SELECT day FROM metric_daily ORDER BY day")
    rolled_days = [r["day"] for r in await cur.fetchall()]
    assert rolled_days == sorted(days)
    await conn.close()


async def test_rollup_recent_days_never_rolls_up_today(tmp_path):
    """Task test 4: today is still accumulating and must never be treated as a closed day, even
    though it plainly has heartbeats and samples like any other day."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    today = _day_str(0)
    await _seed_heartbeats(conn, today, count=20)
    await _seed_sample(conn, today, queue_id=1, bytes_delta=9_999)

    await rollup_recent_days(conn, lookback_days=5)

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_daily WHERE day = ?", (today,))
    assert (await cur.fetchone())["c"] == 0
    await conn.close()


async def test_coverage_distinguishes_a_full_quiet_day_from_a_mostly_down_day(tmp_path):
    """Task test 5: a day with full heartbeat coverage and zero bytes is a genuinely quiet day;
    a day with only partial coverage was mostly down -- both have `bytes == 0`, and only
    `heartbeat_count` tells them apart."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    quiet_day = _day_str(3)
    mostly_down_day = _day_str(2)
    await _seed_heartbeats(conn, quiet_day, count=EXPECTED_HEARTBEATS_PER_DAY)
    await _seed_heartbeats(conn, mostly_down_day, count=100)  # a small fraction of a full day

    await rollup_day(conn, quiet_day)
    await rollup_day(conn, mostly_down_day)

    cur = await conn.execute("SELECT day, bytes, heartbeat_count FROM metric_daily ORDER BY day")
    rows = {r["day"]: (r["bytes"], r["heartbeat_count"]) for r in await cur.fetchall()}
    assert rows[quiet_day] == (0, EXPECTED_HEARTBEATS_PER_DAY)
    assert rows[mostly_down_day] == (0, 100)
    # Both read as a real, present zero (bytes == 0, not absent) -- only the coverage figure
    # says which one was actually a full day.
    assert rows[quiet_day][1] > rows[mostly_down_day][1]
    await conn.close()


async def test_daily_totals_reflects_rolled_up_rows_over_a_day_range(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=2)
    day1, day2 = _day_str(3), _day_str(2)
    for day in (day1, day2):
        await _seed_heartbeats(conn, day, count=10)
    await _seed_sample(conn, day1, queue_id=1, bytes_delta=1_000_000)
    await _seed_sample(conn, day2, queue_id=2, bytes_delta=2_000_000)
    await rollup_day(conn, day1)
    await rollup_day(conn, day2)

    rows = await daily_totals(conn, start_day=day1, end_day=day2)
    by_day_queue = {(day, qid): bytes_ for day, qid, bytes_, _hb in rows}
    assert by_day_queue[(day1, 1)] == 1_000_000
    assert by_day_queue[(day1, 2)] == 0
    assert by_day_queue[(day2, 2)] == 2_000_000

    filtered = await daily_totals(conn, start_day=day1, end_day=day2, queue_id=2)
    assert {(d, q) for d, q, _b, _hb in filtered} == {(day1, 2), (day2, 2)}
    await conn.close()


async def test_total_bytes_sums_daily_rows_plus_todays_unrolled_raw_samples(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    past_day = _day_str(2)
    await _seed_heartbeats(conn, past_day, count=10)
    await _seed_sample(conn, past_day, queue_id=1, bytes_delta=4_000_000)
    await rollup_day(conn, past_day)

    today = _day_str(0)
    await _seed_sample(conn, today, queue_id=1, bytes_delta=1_500_000, seconds_offset=5)

    total, since_day = await total_bytes(conn, queue_id=1)
    assert total == 5_500_000  # 4,000,000 rolled up + 1,500,000 still-raw today
    assert since_day == past_day
    await conn.close()


async def test_total_bytes_since_day_is_none_when_nothing_rolled_up_yet(tmp_path):
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    total, since_day = await total_bytes(conn)
    assert total == 0
    assert since_day is None
    await conn.close()


async def test_prune_daily_metrics_removes_only_rows_older_than_thirteen_months(tmp_path):
    """Task test 7: 13-month pruning of daily rows -- `metric_daily`'s own retention, entirely
    independent of the raw tables' `prune_metrics`."""
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    old_day = _day_str(DAILY_RETENTION_DAYS + 10)
    recent_day = _day_str(10)
    await conn.execute(
        "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
        "VALUES (1, ?, 100, 10, '2020-01-01T00:00:00.000000Z')",
        (old_day,),
    )
    await conn.execute(
        "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
        "VALUES (1, ?, 200, 10, '2020-01-01T00:00:00.000000Z')",
        (recent_day,),
    )
    await conn.commit()

    removed = await prune_daily_metrics(conn)
    assert removed == 1

    cur = await conn.execute("SELECT day FROM metric_daily")
    remaining = [r["day"] for r in await cur.fetchall()]
    assert remaining == [recent_day]
    await conn.close()


async def test_rollup_runs_before_prune_in_run_once(tmp_path):
    """Task test 1, and the whole reason this feature exists in a scheduler at all: if
    `prune_metrics` ever ran before `rollup_recent_days` for the same cycle, a day whose raw
    rows are old enough to be pruned in *this very call* would be rolled up as a silent,
    permanent zero -- there would be nothing left in `metric_sample` to sum by the time rollup
    got to it. This seeds exactly that day (old enough to be deleted by this call's own
    `prune_metrics`, but within this call's own rollup lookback window) and asserts both halves
    at once: the daily row holds the *real* byte total, and the raw rows are gone -- which is
    only possible if rollup ran first.
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    await save_metrics_settings(conn, MetricsSettings(retention_days=2))

    # Two days ago at UTC midnight -- comfortably older than a 2-day retention cutoff (now - 2
    # days, which is "two days ago at right now's time-of-day", strictly after midnight that
    # same day), and squarely inside a lookback window of 2 days (offsets 2 and 1).
    doomed_day = _day_str(2)
    await _seed_heartbeats(conn, doomed_day, count=5)
    await _seed_sample(conn, doomed_day, queue_id=1, bytes_delta=12_345, seconds_offset=0)

    scheduler = MetricsRetentionScheduler(db=conn)
    await scheduler.run_once()

    # The daily row exists with the *real* total -- proves rollup read the raw rows while they
    # still existed.
    cur = await conn.execute(
        "SELECT bytes, heartbeat_count FROM metric_daily WHERE day = ? AND queue_id = 1",
        (doomed_day,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["bytes"] == 12_345
    assert row["heartbeat_count"] == 5

    # And the raw rows for that same day are now gone -- proves prune ran, afterward, in this
    # same call.
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_sample")
    assert (await cur.fetchone())["c"] == 0
    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_heartbeat")
    assert (await cur.fetchone())["c"] == 0
    await conn.close()


async def test_rollup_survives_a_database_that_already_had_days_of_raw_data(tmp_path):
    """Simulates upgrading into this feature: raw data for several past days already sits in
    the database (written before migration 026 ever existed), and the very first
    `MetricsRetentionScheduler.run_once` after upgrade must backfill all of it, not just the
    single most recent day.
    """
    conn = await _fresh_db(str(tmp_path))
    await _seed_host_and_queues(conn, n=1)
    for offset in (7, 6, 5, 4, 3, 2):
        day = _day_str(offset)
        await _seed_heartbeats(conn, day, count=20)
        await _seed_sample(conn, day, queue_id=1, bytes_delta=10_000 * offset)

    scheduler = MetricsRetentionScheduler(db=conn)  # default retention (30 days) -> full lookback
    await scheduler.run_once()

    cur = await conn.execute("SELECT COUNT(*) AS c FROM metric_daily")
    assert (await cur.fetchone())["c"] == 6
    await conn.close()
