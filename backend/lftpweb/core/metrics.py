"""Throughput sample store (DESIGN.md §10.4, written alongside this module and applied to the
document on 2026-08-12; `docs/decisions.md` carries the reasoning and the rejected
alternatives). Feeds the Dashboard page's two charts (`api/metrics.py`) from the *same* byte
accounting `core/progress.py`/`core/queue.py` already derive from the filesystem (§1.3/§4.4) --
nothing here parses lftp's stdout, and nothing here re-measures anything; it only persists a
delta of numbers `TransferQueue.tick()`'s existing progress sample already computed for the
live WebSocket feed (as of 2026-08-16, sampled every `PROGRESS_SAMPLE_TICKS`-th tick, ~5s, not
every tick -- see that constant's comment in `core/queue.py`).

**Two tables, one deliberately kept separate from the other** (docs/decisions.md has the full
idle-vs-down reasoning; migrations/005_throughput_metrics.sql has the schema):

- `metric_sample` -- one row per (queue, sample), written **only** when that queue's running
  jobs moved a nonzero number of bytes in that ~30s window. A queue with nothing running, or
  running but genuinely stalled, writes no row for that interval.
- `metric_heartbeat` -- one row per sample tick, unconditionally, regardless of whether any
  queue transferred anything. Its presence across a time range is what "lftpweb was running"
  means; its *absence* is what "down" means. A bucket with heartbeats but no `metric_sample`
  row for a given queue reads as a real, informative zero (idle); a bucket with no heartbeats
  at all reads as a gap (down) -- the two must never render the same way (decision recorded in
  docs/decisions.md).

**The non-monotonic trap (DESIGN.md §4.4, `job.bytes_start`).** `job.bytes_done` is the
*absolute* local footprint `core/progress.py` measures for the active job's `local_root`, not
a per-job delta -- so a retried transfer's new job row starts life already holding whatever the
failed attempt left on disk (a resumed mirror does not start over, `-c`). Differencing
`bytes_done` across ticks *by job id alone* misses this: the moment a job is retried, its
`bytes_done` on the very next tick already includes bytes an *earlier, different job* moved,
which would read as this job having transferred all of it in one ~30s window -- a phantom
spike.

The fix: track `bytes_done - bytes_start` per job, never `bytes_done` alone. `bytes_start` is
set once, at spawn (`core/queue.py`'s `_admit`, mirroring the same value written to
`job.bytes_start`), to whatever local size already existed on disk -- so this quantity is zero
at a job's first tick and can only grow with bytes *that job* actually moved. A retry gets a
brand new job id, so it starts its own tracking at zero and can never inherit a dead job's
history; see `ThroughputSampler.tick()` and `tests/test_metrics.py`'s restart-mid-flight test.

**Daily rollups** (2026-08-21, prompts/done/2026-08-21-daily-metric-rollups.md; docs/decisions.md
carries the one-table, rollup-before-prune, and UTC-day reasoning). The two raw tables above are
kept only a matter of days -- nowhere near long enough to answer "how much have I downloaded
this year." `metric_daily` (migration 026) is one row per `(queue_id, day)`, `day` a UTC
calendar date, recomputed from the raw tables and upserted -- never incremented, so re-rolling an
already-rolled day is a no-op. `heartbeat_count` on that row carries the idle-vs-down distinction
up to daily granularity: full coverage with zero bytes is a genuinely quiet day; partial coverage
means the day was mostly down; a day absent from the table entirely had zero heartbeats (never
rolled up, or since pruned). **Rollup MUST run before pruning the raw tables** --
`MetricsRetentionScheduler.run_once` calls `rollup_recent_days` then `prune_metrics`, in that
order, in the same function call, every cycle -- a day rolled up after its raw rows are gone has
nothing left to sum. See `rollup_day`/`rollup_recent_days`/`prune_daily_metrics` below.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Sample every 30th call to `TransferQueue.tick()` (~1 Hz, DESIGN.md §4.4), not a second
# `asyncio` timer. Two reasons this beats a wall-clock check like `BackupScheduler`'s: it
# piggybacks on a loop that already exists rather than opening a second one, and it can't
# drift out of step with the transfer engine's own notion of a "tick" the way two independent
# `asyncio.sleep` loops eventually do under load (one running long delays the other's next
# fire, but never their relative alignment when both are just "count my own ticks"). If
# `tick_s` is ever reconfigured, the sample cadence scales with it (still 30 ticks) instead of
# silently decoupling from the transfer engine's own notion of a tick.
SAMPLE_INTERVAL_TICKS = 30

SETTING_KEY = "metrics_settings"
# 2026-08-21 (prompts/done/2026-08-21-daily-metric-rollups.md, decided with the user): 7 -> 30
# so the Dashboard's own offered `30d` range (api/metrics.py._RANGES) works out of the box,
# rather than arriving ~77% empty. `MAX_RETENTION_DAYS` is unchanged -- 30 stays the raw-table
# ceiling; `metric_daily` below is what serves anything longer.
DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 30

# The daily table's own retention (2026-08-21, "keep daily rows for 13 months" -- long enough
# for a year-over-year glance) -- independent of the raw tables' `*_RETENTION_DAYS` above, and
# not user-configurable (the task asked for configurable *raw* retention only; see
# docs/decisions.md). 396 days ~= 13 * 30.4 -- a fixed day count, not a calendar-month
# calculation, matching every other day-based retention window in this codebase.
DAILY_RETENTION_DAYS = 396

# Coverage baseline for `heartbeat_count` (see `rollup_day`): at the ~1 Hz `tick_s` default
# (`main.py`'s `settings.transfer_tick_s`) a heartbeat lands every `SAMPLE_INTERVAL_TICKS`
# ticks, i.e. ~30s, so a fully-covered UTC day sees about this many. `tick_s` is a site tuning
# knob (not exposed here) -- a site that has changed it will see `heartbeat_count` read as a
# different fraction of "full" by that same factor. That's a documented approximation, not a
# per-day recorded fact (no history of `tick_s` changes is kept anywhere), and it's good enough
# for "was this day mostly up or mostly down," which is all coverage is for.
EXPECTED_HEARTBEATS_PER_DAY = 86400 // SAMPLE_INTERVAL_TICKS


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- Settings (JSON in `setting`, the same pattern core/backup.py.BackupSettings and
# core/queue.py.TransferSettings use) ------------------------------------------------------


@dataclass(frozen=True)
class MetricsSettings:
    """Settings -> ... retention only -- the sample interval (30s) is a code constant, not a
    user-facing knob (decision recorded in docs/decisions.md: the task that added this asked
    for a *configurable retention*, nothing else). Retention default 7 days, user-configurable
    up to 30 -- validated server-side in `api/metrics.py`, not just clamped silently, so a bad
    value is a 422, not a quietly-different number.
    """

    retention_days: int = DEFAULT_RETENTION_DAYS


def clamp_retention_days(days: int) -> int:
    return max(MIN_RETENTION_DAYS, min(days, MAX_RETENTION_DAYS))


async def load_metrics_settings(db: aiosqlite.Connection) -> MetricsSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return MetricsSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return MetricsSettings()
    try:
        days = int(data.get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return MetricsSettings(retention_days=clamp_retention_days(days))


async def save_metrics_settings(db: aiosqlite.Connection, settings: MetricsSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES "
        "(?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, json.dumps({"retention_days": settings.retention_days})),
    )
    await db.commit()


# --- Sampling --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunningJobBytes:
    """What `TransferQueue.tick()` hands the sampler each tick for one running job -- the
    exact fields `_sample_and_publish_progress` already computed/read this same tick (`job_id`,
    `queue_id`, `bytes_done`) plus `bytes_start` (already carried on `_RunningProcess`, the same
    value written to `job.bytes_start` at spawn). Never a second measurement.
    """

    job_id: int
    queue_id: int
    bytes_done: int
    bytes_start: int


class ThroughputSampler:
    """Counts ticks; every `SAMPLE_INTERVAL_TICKS`th one, persists a heartbeat row and one
    `metric_sample` row per queue that moved a nonzero number of bytes since the previous
    sample. One instance lives for the process lifetime, owned by `core/queue.py.TransferQueue`
    (`self.metrics`) -- the same shape as `core/progress.py.ProgressSampler` (`self.progress`).

    `tick()` must be called every real transfer-queue tick (~1 Hz), including ticks where
    nothing is running (`running=[]`) -- that is what keeps the heartbeat alive while lftpweb
    is idle, which is exactly the signal idle-vs-down (module docstring) depends on.
    """

    def __init__(
        self, db: aiosqlite.Connection, *, tick_interval: int = SAMPLE_INTERVAL_TICKS
    ) -> None:
        self.db = db
        self.tick_interval = tick_interval
        self._tick_count = 0
        # job_id -> last-seen (bytes_done - bytes_start), clamped >= 0. Cleared for any job id
        # no longer in the running set -- the same "drop on exit from the active set" shape as
        # `ProgressSampler._prev_bytes` -- so a future job id (a retry) is a fresh key that can
        # never inherit a dead job's history (the module docstring's whole point).
        self._prev_contribution: dict[int, int] = {}

    def drop(self, job_id: int) -> None:
        self._prev_contribution.pop(job_id, None)

    async def tick(self, running: list[RunningJobBytes], *, now_iso: str | None = None) -> None:
        live_ids = {j.job_id for j in running}
        for stale_id in set(self._prev_contribution) - live_ids:
            self.drop(stale_id)

        self._tick_count += 1
        if self._tick_count < self.tick_interval:
            return
        self._tick_count = 0

        ts = now_iso if now_iso is not None else _now_iso()
        deltas_by_queue: dict[int, int] = {}
        for job in running:
            # Never negative (module docstring's trap): `bytes_start` is fixed at this job's
            # own spawn time, so this quantity is zero on the job's first tick and can only
            # grow from there -- but clamp anyway, the same defensive posture
            # `ProgressSampler.sample` already takes against a sidecar read momentarily
            # going backwards mid-write.
            contribution = max(job.bytes_done - job.bytes_start, 0)
            prev = self._prev_contribution.get(job.job_id)
            # `prev is None` (this job's first sample) is deliberately NOT "no history, delta
            # 0" -- the job's own contribution-so-far already excludes every byte that was on
            # disk before *it* started (that's what `bytes_start` subtracts out), so on a
            # fresh job id that whole amount is real, newly-moved data belonging to this job,
            # not a phantom spike inherited from whatever job ran before it.
            delta = max(contribution - prev, 0) if prev is not None else contribution
            self._prev_contribution[job.job_id] = contribution
            if delta:
                deltas_by_queue[job.queue_id] = deltas_by_queue.get(job.queue_id, 0) + delta

        await self.db.execute("INSERT INTO metric_heartbeat (ts) VALUES (?)", (ts,))
        for queue_id, bytes_delta in deltas_by_queue.items():
            await self.db.execute(
                "INSERT INTO metric_sample (queue_id, ts, bytes_delta) VALUES (?, ?, ?)",
                (queue_id, ts, bytes_delta),
            )
        await self.db.commit()


# --- Querying (api/metrics.py) ---------------------------------------------------------------


async def queue_breakdown(
    db: aiosqlite.Connection,
    *,
    start_ts: str,
    end_ts: str,
    bucket_seconds: int,
    queue_id: int | None = None,
) -> list[tuple[int, int, int]]:
    """Bucketed `(bucket_epoch, queue_id, bytes)` rows for the half-open range
    `[start_ts, end_ts)`. Server-side bucketing via SQL `GROUP BY`, not raw rows for the
    browser to aggregate -- that is what the two covering indexes on `metric_sample`
    (migrations/005_throughput_metrics.sql) exist to serve.

    `queue_id=None` returns every queue's rows in the range -- the "site total over a time
    range, bucketed" query shape, driven by `idx_metric_sample_ts_queue`; the site total
    itself is a sum across `queue_id` at the caller's end (decision recorded in
    docs/decisions.md: one table, no separately stored total row, no double counting). A real
    `queue_id` filters to one queue's own series -- the "one queue's series over a time range"
    shape, driven by `idx_metric_sample_queue_ts`. See docs/decisions.md for the
    EXPLAIN QUERY PLAN / benchmark numbers behind both.
    """
    where = ["ts >= ?", "ts < ?"]
    params: list[Any] = [start_ts, end_ts]
    if queue_id is not None:
        where.append("queue_id = ?")
        params.append(queue_id)
    where_sql = " AND ".join(where)
    cursor = await db.execute(
        "SELECT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS bucket_epoch, "
        "queue_id, SUM(bytes_delta) AS bytes "
        f"FROM metric_sample WHERE {where_sql} "
        "GROUP BY bucket_epoch, queue_id ORDER BY bucket_epoch, queue_id",
        [bucket_seconds, bucket_seconds, *params],
    )
    rows = await cursor.fetchall()
    return [(r["bucket_epoch"], r["queue_id"], r["bytes"]) for r in rows]


async def heartbeat_buckets(
    db: aiosqlite.Connection, *, start_ts: str, end_ts: str, bucket_seconds: int
) -> set[int]:
    """Which bucket epochs had at least one heartbeat in `[start_ts, end_ts)` -- "lftpweb was
    running" (module docstring). A bucket epoch missing from this set is a gap (down), never a
    zero -- `api/metrics.py` uses this to decide `up` per bucket.
    """
    cursor = await db.execute(
        "SELECT DISTINCT (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS bucket_epoch "
        "FROM metric_heartbeat WHERE ts >= ? AND ts < ?",
        (bucket_seconds, bucket_seconds, start_ts, end_ts),
    )
    rows = await cursor.fetchall()
    return {r["bucket_epoch"] for r in rows}


# --- Daily rollups (module docstring's "Daily rollups" section; migration 026) -------------


def _utc_today() -> str:
    """Today's UTC calendar date, `'YYYY-MM-DD'` -- the one date `rollup_day` must never be
    asked to roll up (it's still accumulating), and the upper bound `rollup_recent_days` walks
    up to but never reaches.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _day_bounds(day: str) -> tuple[str, str]:
    """Half-open `[start_ts, end_ts)` covering one UTC calendar day (`'YYYY-MM-DD'`), in the
    same ISO timestamp shape every other `ts` column in this module uses -- so it can be handed
    straight to `queue_breakdown`-style `ts >= ? AND ts < ?` queries against the raw tables.
    """
    start = f"{day}T00:00:00.000000Z"
    end = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return start, end


async def rollup_day(db: aiosqlite.Connection, day: str) -> int:
    """Recompute and upsert `metric_daily` rows for one **closed** UTC calendar day
    (`'YYYY-MM-DD'`), one row per currently-existing queue. Always recomputed from
    `metric_sample`/`metric_heartbeat` -- never incremented -- so calling this twice for the
    same day is a no-op the second time (idempotency, task item 2): the upsert is keyed on
    `(queue_id, day)` and overwrites `bytes`/`heartbeat_count` with freshly summed values every
    time, rather than adding to whatever was there before.

    Returns the number of `(queue_id, day)` rows written. That's 0 exactly when this day had
    zero heartbeats site-wide -- entirely down, nothing to roll up -- consistent with
    `metric_sample`'s own gap semantics: no row means no data for that day, not a measured zero.
    It is also the caller's responsibility, not this function's, to never pass today's own date
    (`rollup_recent_days` enforces that); this function has no way to tell "today, still open"
    from "a past day with no heartbeats" and does not try.
    """
    start_ts, end_ts = _day_bounds(day)

    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM metric_heartbeat WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    )
    heartbeat_count = (await cur.fetchone())["c"]
    if heartbeat_count == 0:
        return 0

    cur = await db.execute("SELECT id FROM path_queue")
    queue_ids = [r["id"] for r in await cur.fetchall()]
    if not queue_ids:
        return 0

    cur = await db.execute(
        "SELECT queue_id, SUM(bytes_delta) AS bytes FROM metric_sample "
        "WHERE ts >= ? AND ts < ? GROUP BY queue_id",
        (start_ts, end_ts),
    )
    bytes_by_queue = {r["queue_id"]: r["bytes"] for r in await cur.fetchall()}

    updated_at = _now_iso()
    for queue_id in queue_ids:
        await db.execute(
            "INSERT INTO metric_daily (queue_id, day, bytes, heartbeat_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (queue_id, day) DO UPDATE SET "
            "bytes = excluded.bytes, heartbeat_count = excluded.heartbeat_count, "
            "updated_at = excluded.updated_at",
            (queue_id, day, bytes_by_queue.get(queue_id, 0), heartbeat_count, updated_at),
        )
    await db.commit()
    return len(queue_ids)


async def rollup_recent_days(db: aiosqlite.Connection, lookback_days: int) -> int:
    """Roll up every closed UTC day from `lookback_days` ago through yesterday, inclusive --
    never today (task item 2: today is still accumulating). `rollup_day` is idempotent, so this
    doubles as both the steady-state rollup (called every cycle from
    `MetricsRetentionScheduler.run_once`, right before `prune_metrics`) and the startup backfill
    the task calls for: a day nobody has ever rolled up yet (the app was down for a stretch, or
    this feature was just installed with days of raw data already sitting there) is picked up
    the same way an already-rolled day is re-confirmed -- there is no separate "which days are
    missing a row" bookkeeping to get wrong.

    `lookback_days` should be at least the currently-configured raw retention
    (`run_once` passes exactly `settings.retention_days`) so that every day still present in the
    raw tables gets a chance to be rolled up here before `prune_metrics` -- called immediately
    after, in the same `run_once` invocation -- can ever delete it. Returns the total number of
    `(queue_id, day)` rows written across every day walked, for the caller to log.
    """
    today = _utc_today()
    now = datetime.now(UTC)
    total = 0
    for offset in range(lookback_days, 0, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        if day >= today:
            continue  # defensive: offset >= 1 already guarantees this, but never roll up today
        total += await rollup_day(db, day)
    return total


async def daily_totals(
    db: aiosqlite.Connection, *, start_day: str, end_day: str, queue_id: int | None = None
) -> list[tuple[str, int, int, int]]:
    """`(day, queue_id, bytes, heartbeat_count)` rows for the inclusive day range
    `[start_day, end_day]` (both `'YYYY-MM-DD'`) -- the long-horizon counterpart to
    `queue_breakdown` above, read by `api/metrics.py`'s 90d/1y ranges once a range outruns what
    the raw tables' own retention can ever serve. `queue_id=None` returns every queue's rows
    (the site-total-broken-down-by-queue shape); a real `queue_id` filters to one queue's own
    daily series -- the identical two shapes `queue_breakdown` serves for the raw table.
    """
    where = ["day >= ?", "day <= ?"]
    params: list[Any] = [start_day, end_day]
    if queue_id is not None:
        where.append("queue_id = ?")
        params.append(queue_id)
    where_sql = " AND ".join(where)
    cursor = await db.execute(
        f"SELECT day, queue_id, bytes, heartbeat_count FROM metric_daily WHERE {where_sql} "
        "ORDER BY day, queue_id",
        params,
    )
    rows = await cursor.fetchall()
    return [(r["day"], r["queue_id"], r["bytes"], r["heartbeat_count"]) for r in rows]


async def total_bytes(
    db: aiosqlite.Connection, *, queue_id: int | None = None
) -> tuple[int, str | None]:
    """The "total downloaded" figure the task's user request asked for by name -- all bytes
    moved, as far back as retained history goes. Two pieces, added together:

    - `metric_daily`, summed -- every rolled-up closed day still within `DAILY_RETENTION_DAYS`;
    - today's own not-yet-rolled-up raw samples (`metric_sample`, filtered to today's UTC
      calendar day) -- so the number is live and doesn't sit stale until the next rollup cycle
      crosses UTC midnight.

    A day that was rolled up and later pruned (past 13 months), or one that predates the very
    first heartbeat this instance ever wrote, is simply gone -- this is "total we still have a
    record of," not a promise of every byte ever moved since install. `since_day` (the earliest
    day present in `metric_daily`, `None` on an empty table) lets the caller say "since <date>"
    honestly instead of implying an unbounded history.

    This deliberately does *not* try to detect "yesterday was never rolled up yet" (e.g. the
    scheduler hasn't completed its first cycle since a restart) and fold that in too -- normal
    operation rolls up every closed day within one `run_once` cycle of startup, so that gap is
    self-healing within the hour and not worth a second, more expensive query here.
    """
    where_q = "WHERE queue_id = ?" if queue_id is not None else ""
    params: list[Any] = [queue_id] if queue_id is not None else []
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(bytes), 0) AS total, MIN(day) AS since_day "
        f"FROM metric_daily {where_q}",
        params,
    )
    row = await cursor.fetchone()
    daily_total: int = row["total"]
    since_day: str | None = row["since_day"]

    today = _utc_today()
    start_ts, end_ts = _day_bounds(today)
    where_today = ["ts >= ?", "ts < ?"]
    params_today: list[Any] = [start_ts, end_ts]
    if queue_id is not None:
        where_today.append("queue_id = ?")
        params_today.append(queue_id)
    cursor = await db.execute(
        "SELECT COALESCE(SUM(bytes_delta), 0) AS total FROM metric_sample "
        f"WHERE {' AND '.join(where_today)}",
        params_today,
    )
    today_total: int = (await cursor.fetchone())["total"]

    return daily_total + today_total, since_day


# --- Retention/pruning ------------------------------------------------------------------------


async def prune_metrics(db: aiosqlite.Connection, retention_days: int) -> tuple[int, int]:
    """Delete `metric_sample`/`metric_heartbeat` rows older than `retention_days`. A single
    bounded `DELETE ... WHERE ts < cutoff` is inherently oldest-first -- unlike
    `core/backup.py.prune_backups` (files on disk can't be bulk-deleted by a WHERE clause and
    need an explicit oldest-first walk), rows can. Returns
    `(samples_removed, heartbeats_removed)` for the caller to log.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cursor = await db.execute("DELETE FROM metric_sample WHERE ts < ?", (cutoff,))
    samples_removed = cursor.rowcount
    cursor = await db.execute("DELETE FROM metric_heartbeat WHERE ts < ?", (cutoff,))
    heartbeats_removed = cursor.rowcount
    await db.commit()
    return samples_removed, heartbeats_removed


async def prune_daily_metrics(
    db: aiosqlite.Connection, retention_days: int = DAILY_RETENTION_DAYS
) -> int:
    """Delete `metric_daily` rows older than `retention_days` (default the 13-month decision) --
    this table's own retention, entirely independent of `prune_metrics`'s raw-table one above
    (different table, different cutoff, no shared bookkeeping). `day` is a plain `'YYYY-MM-DD'`
    string, which sorts and compares correctly against another `'YYYY-MM-DD'` cutoff with a
    simple `<` -- no `strftime` needed the way the full-timestamp raw tables require. Returns
    the number of rows removed, for the caller to log.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    cursor = await db.execute("DELETE FROM metric_daily WHERE day < ?", (cutoff,))
    removed = cursor.rowcount
    await db.commit()
    return removed


class MetricsRetentionScheduler:
    """Background loop, same `_task`/`start()`/`stop()` shape as `core/backup.py.BackupScheduler`
    and `core/engine.py.Engine`. Unlike backups (whose scheduler tracks "was one already taken
    recently" because *taking* one is a real, costly action), pruning is idempotent and cheap --
    an indexed `DELETE ... WHERE ts < cutoff` that does nothing when there's nothing to prune --
    so this just runs on a fixed cadence with no "was it already done" bookkeeping to get wrong.

    2026-08-21 (prompts/done/2026-08-21-daily-metric-rollups.md): this is also where the daily
    rollup lives, deliberately folded into the *same* cycle as pruning rather than a second
    independent loop -- `run_once` calls `rollup_recent_days` before `prune_metrics`, in that
    order, every time, so "rollup happens before its raw data can be pruned" is a property of
    this one function's body, not something that depends on two schedulers' cadences never
    drifting apart. The first `run_once` (fired immediately on `start()`, via `_loop` below)
    doubles as the startup backfill the task calls for -- no separate backfill entry point.
    """

    CHECK_INTERVAL_S = 3600.0  # hourly, same cadence BackupScheduler checks on

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-metrics-retention-loop")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the loop
                logger.exception("metrics retention cycle failed")
            await asyncio.sleep(self.CHECK_INTERVAL_S)

    async def run_once(self) -> tuple[int, int]:
        settings = await load_metrics_settings(self.db)
        # Rollup MUST run before prune -- the only part of this feature that can destroy data
        # (task item 1), not merely be wrong. `prune_metrics` below deletes raw rows past
        # `retention_days`; a day rolled up *after* that delete has already run has nothing left
        # to sum and would roll up as a silent, permanent zero. Calling `rollup_recent_days`
        # here, synchronously, immediately before `prune_metrics` -- in the same function, not on
        # a separate schedule -- is what makes that ordering a guarantee rather than a scheduling
        # coincidence; `tests/test_metrics.py`'s
        # `test_rollup_runs_before_prune_in_run_once` pins exactly this.
        # `lookback_days=settings.retention_days` means every day still present in the raw
        # tables gets a chance to be rolled up in this same pass, before the prune call two lines
        # down can ever touch it (`rollup_recent_days`'s own docstring has the full reasoning).
        await rollup_recent_days(self.db, lookback_days=settings.retention_days)
        removed = await prune_metrics(self.db, settings.retention_days)
        daily_removed = await prune_daily_metrics(self.db)
        if removed != (0, 0):
            logger.info(
                "pruned %d metric sample(s) and %d heartbeat(s) beyond %d day retention",
                removed[0],
                removed[1],
                settings.retention_days,
            )
        if daily_removed:
            logger.info(
                "pruned %d daily metric row(s) beyond %d day retention",
                daily_removed,
                DAILY_RETENTION_DAYS,
            )
        return removed
