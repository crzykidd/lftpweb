"""GET /api/metrics/throughput -- the Dashboard page's two charts (DESIGN.md — new section
proposed alongside this task, see docs/decisions.md for the exact wording and for this
endpoint's benchmark/EXPLAIN QUERY PLAN numbers). Bucketed server-side (`core/metrics.py`),
never raw rows for the browser to aggregate -- that is what `metric_sample`'s two covering
indexes (migrations/005_throughput_metrics.sql) exist to serve.

**Idle vs down**, surfaced directly rather than left for the frontend to infer: a bucket with
heartbeats but no matching `metric_sample` row for a queue is a real, present zero; a bucket
with *no* heartbeat at all comes back with `up: false` and `total_bytes: null`, and the
frontend renders that as a gap (docs/decisions.md), never a flat zero line.

Same `router`/`settings_router` split as `api/auth.py` -- chart data lives under
`/api/metrics/*` (like `/api/history/*`, `/api/stats`), retention config under
`/api/settings/metrics` (like every other `*Settings` dataclass).

**2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md):** `range=90d`/`1y`
read `core/metrics.py.metric_daily` (`_DAILY_RANGES`) instead of the raw tables, since raw
retention (30 days max) can never serve them. `GET /api/metrics/total` is new alongside it: the
all-time (as far as retained history goes) "total downloaded" figure, `core/metrics.py.total_bytes`.

**2026-08-21 (chart grouping, prompts/done/2026-08-21-chart-grouping.md): range and bucket
width are now two separate axes, not one.** `range` still says how far back (`_GROUPABLE_RANGES`
for the bytes chart: `24h`/`7d`/`30d`/`90d`/`1y`); the new `group` query param
(`hour`/`day`/`week`/`month`, `GROUPINGS`) says how wide a bar, with a per-range default
(`_DEFAULT_GROUP`) so existing callers that never pass `group` keep getting a sensible bucket
width. The old `_RANGES` tuple that coupled the two (`"7d": (168, 21600)`) is gone; only the
speed chart's own untouched `1h`/`12h` fixed-width ranges (Chart 2, never gets a group-by
control) still use a range->bucket-width tuple, because they have exactly one width each and
no grouping choice to decouple.

**Not every grouping is available at every range -- disabled server-side, not just downgraded.**
Raw tables (`metric_sample`/`metric_heartbeat`) are capped at `core/metrics.py.MAX_RETENTION_DAYS`
(30 days) regardless of the currently configured retention setting, and `metric_daily` is
one-day granularity by construction (migration 026) -- so **hourly grouping is architecturally
impossible at `90d`/`1y`**: there is no sub-day data that far back and no setting can produce
one. `_AVAILABLE_GROUPS` encodes this and `get_throughput` rejects the combination with a 422
naming the reason, rather than silently serving daily (or coarser) buckets instead. Every other
range/group combination is available -- decoupling the two axes doesn't mean every combination
is *useful* (a `month` bucket over a `24h` range is one degenerate bar), only that none of the
others are actually *impossible* the way hourly-at-90d/1y is, so none of the others are blocked.

**Week and month are summed from daily rows on read -- no new table** (the daily-rollup task's
own anticipated reasoning: "weekly is derivable by summing daily; keeping both risks the two
disagreeing"). `_DayPoint` is the shared one-day-granularity shape both sources (the raw tables
for `24h`/`7d`/`30d` via `_raw_day_points`, `metric_daily` for `90d`/`1y` via
`_daily_table_day_points`) produce, so `_aggregate_day_points` can group either source's days
into week/month buckets the same way. **Coverage, once a bucket spans more than a day, is
redefined as the fraction of days in that bucket that were `up`** (had at least one heartbeat),
not a heartbeat-density average -- see `_aggregate_day_points`'s docstring for why: raw-table
days only ever carry a boolean up/down at that granularity, so a day-count fraction is the one
definition that means the same thing regardless of which table the days came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import metrics
from lftpweb.models import (
    MetricsBucketOut,
    MetricsSettingsIn,
    MetricsSettingsOut,
    MetricsThroughputResponse,
    MetricsTotalOut,
)

router = APIRouter(prefix="/api/metrics")
settings_router = APIRouter(prefix="/api/settings/metrics")

# Chart 2 (speed line)'s own fixed-width ranges -- never gets a group-by control (fine-grained
# speed over a week/month would average away exactly the spikes it exists to show, per the
# original 2026-08-17 reasoning), so each still couples range directly to one bucket width.
# range -> (hours back, bucket width in seconds).
_RANGES: dict[str, tuple[int, int]] = {
    "1h": (1, 60),  # 60 x 1-minute buckets
    "12h": (12, 900),  # 48 x 15-minute buckets
}

# Chart 1 (bytes chart)'s ranges -- these get the group-by control (2026-08-21, chart grouping).
# range -> hours back, for the ranges the raw tables can still serve (metric_sample/heartbeat).
_RANGE_HOURS: dict[str, int] = {
    "24h": 24,
    "7d": 168,
    "30d": 720,
}

# 2026-08-21 (daily rollups): range -> whole days back, for the ranges only `metric_daily` can
# serve (raw retention tops out at 30 days and can never reach these).
_DAILY_RANGES: dict[str, int] = {
    "90d": 90,
    "1y": 365,
}

_GROUPABLE_RANGES: tuple[str, ...] = ("24h", "7d", "30d", "90d", "1y")

# 2026-08-21 (chart grouping): the four bucket widths a human can ask for, widest-agnostic of
# which table actually backs them.
GROUPINGS: tuple[str, ...] = ("hour", "day", "week", "month")

# The per-range default grouping (task table, from the user's own stated preference): 24h stays
# hourly (already right), 7d moves from 6-hour to daily (the one default that actually changes),
# 30d stays daily, 90d/1y move from daily to weekly.
_DEFAULT_GROUP: dict[str, str] = {
    "24h": "hour",
    "7d": "day",
    "30d": "day",
    "90d": "week",
    "1y": "week",
}

# Which groupings are actually available per range -- hourly is impossible at 90d/1y (module
# docstring); every other combination is available, even a degenerate one (e.g. `month` over
# `24h`), because none of the others are architecturally impossible, only less useful as a
# default.
_AVAILABLE_GROUPS: dict[str, tuple[str, ...]] = {
    "24h": GROUPINGS,
    "7d": GROUPINGS,
    "30d": GROUPINGS,
    "90d": ("day", "week", "month"),
    "1y": ("day", "week", "month"),
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class _DayPoint:
    """One UTC calendar day's worth of throughput, the shared shape `_raw_day_points` (raw
    tables) and `_daily_table_day_points` (`metric_daily`) both produce so `_aggregate_day_points`
    can group either source's days into week/month buckets identically.
    """

    day: str  # 'YYYY-MM-DD'
    up: bool  # at least one heartbeat fell on this day
    total_bytes: int | None  # None when `up` is False -- a gap, not a measured zero
    by_queue: dict[int, int]


# --- Raw-table-sourced (24h/7d/30d): hour or day grouping, or the day-points week/month reads --


async def _get_raw_throughput(
    db: Any,
    range_: str,
    group_: str | None,
    *,
    hours: int,
    bucket_seconds: int,
    queue_id: int | None,
) -> MetricsThroughputResponse:
    """Bucketed straight from the raw tables (`metric_sample`/`metric_heartbeat`) -- serves the
    speed chart's untouched `1h`/`12h` (`group_=None`, bucket width fixed by the caller) and the
    bytes chart's `hour`/`day` groupings at `24h`/`7d`/`30d` (`group_` set, bucket width 3600 or
    86400). Walks every bucket boundary the range covers, not just the ones with rows -- an idle
    bucket (heartbeat present, nothing in `metric_sample`) must render as a real, present zero,
    and only a bucket with no heartbeat at all is a gap (docs/decisions.md).
    """
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    start_ts, end_ts = _iso(start), _iso(end)

    rows = await metrics.queue_breakdown(
        db, start_ts=start_ts, end_ts=end_ts, bucket_seconds=bucket_seconds, queue_id=queue_id
    )
    up_epochs = await metrics.heartbeat_buckets(
        db, start_ts=start_ts, end_ts=end_ts, bucket_seconds=bucket_seconds
    )

    by_bucket: dict[int, dict[int, int]] = {}
    for bucket_epoch, q_id, bytes_ in rows:
        by_bucket.setdefault(bucket_epoch, {})[q_id] = bytes_

    first_epoch = (int(start.timestamp()) // bucket_seconds) * bucket_seconds
    last_epoch = (int(end.timestamp()) // bucket_seconds) * bucket_seconds

    buckets: list[MetricsBucketOut] = []
    epoch = first_epoch
    while epoch <= last_epoch:
        up = epoch in up_epochs
        per_queue = by_bucket.get(epoch, {})
        buckets.append(
            MetricsBucketOut(
                ts=datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                up=up,
                total_bytes=sum(per_queue.values()) if up else None,
                by_queue=per_queue if up else {},
            )
        )
        epoch += bucket_seconds

    return MetricsThroughputResponse(
        range=range_, group=group_, bucket_seconds=bucket_seconds, buckets=buckets
    )


async def _raw_day_points(db: Any, hours: int, queue_id: int | None) -> list[_DayPoint]:
    """Day-granularity points sourced from the raw tables -- the week/month counterpart to
    `_get_raw_throughput`'s own day-bucketed call, returned as `_DayPoint`s for
    `_aggregate_day_points` rather than as a finished response. Unlike the `metric_daily`-sourced
    path below, this includes today's own not-yet-rolled-up partial day (same "24h/7d/30d ranges
    already show today's own live activity" rule the raw-table paths have always followed).
    """
    bucket_seconds = 86400
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    start_ts, end_ts = _iso(start), _iso(end)

    rows = await metrics.queue_breakdown(
        db, start_ts=start_ts, end_ts=end_ts, bucket_seconds=bucket_seconds, queue_id=queue_id
    )
    up_epochs = await metrics.heartbeat_buckets(
        db, start_ts=start_ts, end_ts=end_ts, bucket_seconds=bucket_seconds
    )
    by_bucket: dict[int, dict[int, int]] = {}
    for bucket_epoch, q_id, bytes_ in rows:
        by_bucket.setdefault(bucket_epoch, {})[q_id] = bytes_

    first_epoch = (int(start.timestamp()) // bucket_seconds) * bucket_seconds
    last_epoch = (int(end.timestamp()) // bucket_seconds) * bucket_seconds

    points: list[_DayPoint] = []
    epoch = first_epoch
    while epoch <= last_epoch:
        day = datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d")
        up = epoch in up_epochs
        per_queue = by_bucket.get(epoch, {})
        points.append(
            _DayPoint(
                day=day,
                up=up,
                total_bytes=sum(per_queue.values()) if up else None,
                by_queue=per_queue if up else {},
            )
        )
        epoch += bucket_seconds
    return points


# --- metric_daily-sourced (90d/1y): day grouping (existing shape), or the day-points week/month
# reads -----------------------------------------------------------------------------------------


async def _daily_rows_by_day(
    db: Any, days: int, queue_id: int | None
) -> tuple[dict[str, dict[int, int]], dict[str, int], str, str]:
    """Shared `metric_daily` read for both the `day`-grouped response and the week/month
    aggregation over the same range -- one query, two callers. Never includes today (still
    accumulating, `core/metrics.py.rollup_day` never rolls up an open day).
    """
    now = datetime.now(UTC)
    end_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start_day = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = await metrics.daily_totals(db, start_day=start_day, end_day=end_day, queue_id=queue_id)
    by_day: dict[str, dict[int, int]] = {}
    heartbeats_by_day: dict[str, int] = {}
    for day, q_id, bytes_, heartbeat_count in rows:
        by_day.setdefault(day, {})[q_id] = bytes_
        heartbeats_by_day[day] = heartbeat_count  # same value for every queue row on this day
    return by_day, heartbeats_by_day, start_day, end_day


async def _get_daily_throughput(
    db: Any, range_: str, group_: str, days: int, queue_id: int | None
) -> MetricsThroughputResponse:
    """The `_DAILY_RANGES` counterpart to `_get_raw_throughput` -- same `MetricsThroughputResponse`
    shape (`bucket_seconds` always 86400 here), sourced from `core/metrics.py.daily_totals`
    instead. Yesterday is the last bucket, never today (today has no `metric_daily` row yet).
    """
    by_day, heartbeats_by_day, start_day, end_day = await _daily_rows_by_day(db, days, queue_id)

    buckets: list[MetricsBucketOut] = []
    start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    day_date = start_date
    while day_date <= end_date:
        day = day_date.strftime("%Y-%m-%d")
        heartbeat_count = heartbeats_by_day.get(day)
        # A day absent from `metric_daily` entirely had zero heartbeats -- never rolled up (or
        # since pruned past 13 months) -- a gap, same as a raw-table bucket with no heartbeat at
        # all (docs/decisions.md's idle-vs-down rule, carried up to daily granularity).
        up = heartbeat_count is not None
        per_queue = by_day.get(day, {})
        buckets.append(
            MetricsBucketOut(
                ts=f"{day}T00:00:00Z",
                up=up,
                total_bytes=sum(per_queue.values()) if up else None,
                by_queue=per_queue if up else {},
                coverage=(
                    min(1.0, heartbeat_count / metrics.EXPECTED_HEARTBEATS_PER_DAY) if up else None
                ),
            )
        )
        day_date += timedelta(days=1)

    return MetricsThroughputResponse(
        range=range_, group=group_, bucket_seconds=86400, buckets=buckets
    )


async def _daily_table_day_points(db: Any, days: int, queue_id: int | None) -> list[_DayPoint]:
    by_day, heartbeats_by_day, start_day, end_day = await _daily_rows_by_day(db, days, queue_id)
    start_date = datetime.strptime(start_day, "%Y-%m-%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=UTC)
    points: list[_DayPoint] = []
    day_date = start_date
    while day_date <= end_date:
        day = day_date.strftime("%Y-%m-%d")
        heartbeat_count = heartbeats_by_day.get(day)
        up = heartbeat_count is not None
        per_queue = by_day.get(day, {})
        points.append(
            _DayPoint(
                day=day,
                up=up,
                total_bytes=sum(per_queue.values()) if up else None,
                by_queue=per_queue if up else {},
            )
        )
        day_date += timedelta(days=1)
    return points


# --- week/month: summed from daily rows on read (no new table) ---------------------------------


def _week_key(day: str) -> int:
    """Epoch-anchored 7-day bucket index -- consistent with every other bucket width in this
    module (`(epoch // bucket_seconds) * bucket_seconds`), rather than an ISO calendar week
    (Monday-start) or a "week ending today" scheme. Deliberately simple: a second, differently
    anchored alignment concept alongside the epoch-aligned scheme everything else here uses isn't
    worth it for what is, after all, just "seven days grouped together."
    """
    epoch = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    return epoch // (7 * 86400)


def _month_key(day: str) -> str:
    """Calendar month ('YYYY-MM') -- unlike hour/day/week, a month has no fixed number of
    seconds, so it can't be epoch-bucketed the way the others are; grouping by calendar month
    string is the only sensible reading of "group by month."
    """
    return day[:7]


def _aggregate_day_points(points: list[_DayPoint], group_: str) -> list[MetricsBucketOut]:
    """Week/month buckets, built by summing the daily rows they cover (task: "week and month are
    derived by summing daily rows on read... no new table"). Works identically whether `points`
    came from the raw tables (24h/7d/30d, `_raw_day_points`) or `metric_daily` (90d/1y,
    `_daily_table_day_points`) -- both produce the same `_DayPoint` shape, so this one aggregator
    serves every range.

    **Coverage, redefined for a multi-day bucket**: the fraction of *days* in the bucket that
    were `up` (had at least one heartbeat), not a heartbeat-density average -- e.g. 5 of 7 days
    up is coverage 0.71, regardless of how partial or complete each of those 5 days' own coverage
    was. Deliberate simplification: raw-table-sourced days only ever carry a boolean up/down at
    that granularity (no per-day heartbeat count is tracked there), so a day-count fraction is
    the one definition of "coverage" that means the same thing regardless of which table the
    days came from. A bucket with zero up days has `coverage=None` (nothing to compute a
    fraction of, and `total_bytes`/`by_queue` are a gap too) -- matching the existing per-day
    convention (`_get_daily_throughput`).
    """
    keyfn = _week_key if group_ == "week" else _month_key
    order: list[Any] = []
    groups: dict[Any, list[_DayPoint]] = {}
    for p in points:
        k = keyfn(p.day)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(p)

    buckets: list[MetricsBucketOut] = []
    for k in order:
        members = groups[k]
        up_members = [m for m in members if m.up]
        up = len(up_members) > 0
        by_queue: dict[int, int] = {}
        for m in up_members:
            for qid, b in m.by_queue.items():
                by_queue[qid] = by_queue.get(qid, 0) + b
        first_day = min(m.day for m in members)
        buckets.append(
            MetricsBucketOut(
                ts=f"{first_day}T00:00:00Z",
                up=up,
                total_bytes=sum(by_queue.values()) if up else None,
                by_queue=by_queue if up else {},
                coverage=(len(up_members) / len(members)) if up else None,
            )
        )
    return buckets


@router.get("/throughput", response_model=MetricsThroughputResponse)
async def get_throughput(
    request: Request, range: str = "24h", group: str | None = None, queue_id: int | None = None
) -> MetricsThroughputResponse:
    """`range` picks the window (`_RANGES` for the speed chart's untouched `1h`/`12h`,
    `_GROUPABLE_RANGES` -- `24h`/`7d`/`30d`/`90d`/`1y` -- for the bytes chart); `group` picks the
    bucket width for a groupable range (`hour`/`day`/`week`/`month`, defaulting per
    `_DEFAULT_GROUP` when omitted) and is rejected with a 422 naming the reason when the
    combination is architecturally impossible (`_AVAILABLE_GROUPS` -- hourly at `90d`/`1y`, module
    docstring) rather than silently served as something coarser. `queue_id` is optional --
    omitted, this is the "site total, bucketed, broken down by queue" shape; supplied, it's the
    "one queue's series" shape. Both share this endpoint because they share the same underlying
    tables and bucketing logic.
    """
    db = request.app.state.db

    if range in _RANGES:
        # Chart 2 (speed line) -- fixed width, no group-by control, `group` is not part of this
        # feature for these two ranges and is ignored if a caller passes one anyway.
        hours, bucket_seconds = _RANGES[range]
        return await _get_raw_throughput(
            db, range, None, hours=hours, bucket_seconds=bucket_seconds, queue_id=queue_id
        )

    if range not in _GROUPABLE_RANGES:
        all_ranges = sorted({*_RANGES, *_GROUPABLE_RANGES})
        raise HTTPException(status_code=422, detail=f"range must be one of {all_ranges}")

    group_ = group if group is not None else _DEFAULT_GROUP[range]
    if group_ not in GROUPINGS:
        raise HTTPException(status_code=422, detail=f"group must be one of {sorted(GROUPINGS)}")
    if group_ not in _AVAILABLE_GROUPS[range]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"group={group_!r} is not available for range={range!r} -- raw history tops out "
                f"at {metrics.MAX_RETENTION_DAYS} days and the daily rollup table is one-day "
                "granularity by construction, so there is no sub-day data this far back at any "
                "retention setting"
            ),
        )

    if group_ == "hour":
        return await _get_raw_throughput(
            db, range, group_, hours=_RANGE_HOURS[range], bucket_seconds=3600, queue_id=queue_id
        )
    if group_ == "day":
        if range in _DAILY_RANGES:
            return await _get_daily_throughput(db, range, group_, _DAILY_RANGES[range], queue_id)
        return await _get_raw_throughput(
            db, range, group_, hours=_RANGE_HOURS[range], bucket_seconds=86400, queue_id=queue_id
        )

    # week / month -- summed from daily rows on read (no new table, module docstring).
    if range in _DAILY_RANGES:
        points = await _daily_table_day_points(db, _DAILY_RANGES[range], queue_id)
    else:
        points = await _raw_day_points(db, _RANGE_HOURS[range], queue_id)
    buckets = _aggregate_day_points(points, group_)
    # Nominal bucket width for a variable-length bucket -- 7 days exactly for `week`; `month`
    # has no fixed length (28-31 days), so 2,592,000s (30 days) is an approximation for whatever
    # legacy/other consumer might still read `bucket_seconds` numerically. The frontend keys its
    # own labeling off the explicit `group` field instead (`lib/bytesChart.ts`), not this number.
    bucket_seconds = 604800 if group_ == "week" else 2_592_000
    return MetricsThroughputResponse(
        range=range, group=group_, bucket_seconds=bucket_seconds, buckets=buckets
    )


@router.get("/total", response_model=MetricsTotalOut)
async def get_total(request: Request, queue_id: int | None = None) -> MetricsTotalOut:
    """The Dashboard's "total downloaded" readout (task: "a user can have the option to just
    see their total downloaded amount later") -- `core/metrics.py.total_bytes`, summed from the
    daily table plus today's own not-yet-rolled-up raw samples. `queue_id` omitted is the
    site-wide total; supplied, one queue's own.
    """
    total, since_day = await metrics.total_bytes(request.app.state.db, queue_id=queue_id)
    return MetricsTotalOut(total_bytes=total, since_day=since_day)


@settings_router.get("", response_model=MetricsSettingsOut)
async def get_metrics_settings(request: Request) -> MetricsSettingsOut:
    settings = await metrics.load_metrics_settings(request.app.state.db)
    return MetricsSettingsOut(retention_days=settings.retention_days)


@settings_router.put("", response_model=MetricsSettingsOut)
async def put_metrics_settings(body: MetricsSettingsIn, request: Request) -> MetricsSettingsOut:
    # Explicit 422 on an out-of-range value, not a silent clamp -- decision 2 says "7 default,
    # configurable up to 30, validated server-side," and a client that asked for 90 days
    # should be told no, not quietly given 30 with no indication anything was rejected.
    if not (metrics.MIN_RETENTION_DAYS <= body.retention_days <= metrics.MAX_RETENTION_DAYS):
        raise HTTPException(
            status_code=422,
            detail=(
                f"retention_days must be between {metrics.MIN_RETENTION_DAYS} and "
                f"{metrics.MAX_RETENTION_DAYS}"
            ),
        )
    settings = metrics.MetricsSettings(retention_days=body.retention_days)
    await metrics.save_metrics_settings(request.app.state.db, settings)
    return MetricsSettingsOut(retention_days=settings.retention_days)
