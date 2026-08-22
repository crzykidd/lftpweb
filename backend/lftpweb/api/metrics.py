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
read `core/metrics.py.metric_daily` (`_DAILY_RANGES`/`_get_daily_throughput`) instead of the raw
tables, since raw retention (30 days max) can never serve them -- everything else about this
endpoint's shape and idle-vs-down handling still applies, just at one-day granularity. `GET
/api/metrics/total` is new alongside it: the all-time (as far as retained history goes) "total
downloaded" figure, `core/metrics.py.total_bytes`.
"""

from __future__ import annotations

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

# range -> (hours back, bucket width in seconds). Finer buckets for a short range, coarser as
# the range widens -- a 1h view wants to show minute-scale speed changes; a 24h view at the
# same 1-minute resolution would be 1,440 points on a chart nobody can read, for no more
# information than the hourly resolution the bytes-per-hour bar chart already uses. 24h's own
# bucket width (3600s) is deliberately the same as that bar chart's, so the two charts agree
# on what "one bar/point" means when both are looking at the same day. 2026-08-17
# (prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md) extends this same reasoning
# further out: 7d at 24 hourly bars would be a 168-bar chart nobody can read either, so it
# steps to 6-hour buckets (28 bars/week, still fine enough to see a day's shape); 30d steps
# again to 1-day buckets (30 bars/month, one bar per day -- any finer and the chart is wider
# than it is informative). Both new ranges feed the bytes chart only (Chart 1's own range
# selector, `dashboard.bytesRange`) -- the speed chart's 1h/12h/24h selector is untouched, on
# purpose: speed over a week/month at these bucket widths would average away exactly the
# spikes a speed chart exists to show.
_RANGES: dict[str, tuple[int, int]] = {
    "1h": (1, 60),  # 60 x 1-minute buckets
    "12h": (12, 900),  # 48 x 15-minute buckets
    "24h": (24, 3600),  # 24 x 1-hour buckets
    "7d": (168, 21600),  # 28 x 6-hour buckets
    "30d": (720, 86400),  # 30 x 1-day buckets
}

# 2026-08-21 (daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md): the longer
# ranges the daily table (`core/metrics.py.metric_daily`, migration 026) now makes possible --
# range -> whole days back. Deliberately a *second* dict rather than folding into `_RANGES`
# above: these read from `metric_daily` (`core/metrics.py.daily_totals`), not
# `queue_breakdown`/`heartbeat_buckets` against the raw tables, because raw retention (30 days
# max) can never serve either one. Both are always 1-day buckets -- finer granularity than a
# day isn't retained this far back, and isn't the point of a long-horizon view anyway.
_DAILY_RANGES: dict[str, int] = {
    "90d": 90,
    "1y": 365,
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _get_daily_throughput(
    db: Any, range_: str, days: int, queue_id: int | None
) -> MetricsThroughputResponse:
    """The `_DAILY_RANGES` counterpart to the raw-table walk below -- same
    `MetricsThroughputResponse` shape (`bucket_seconds` always 86400 here), sourced from
    `core/metrics.py.daily_totals` instead. Yesterday is the last bucket, never today (today has
    no `metric_daily` row yet -- `core/metrics.py.rollup_day` never rolls up an open day -- so it
    would render as an actually-misleading gap rather than "not rolled up yet"; the 24h/7d/30d
    ranges above already show today's own live activity from the raw tables).
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

    return MetricsThroughputResponse(range=range_, bucket_seconds=86400, buckets=buckets)


@router.get("/throughput", response_model=MetricsThroughputResponse)
async def get_throughput(
    request: Request, range: str = "24h", queue_id: int | None = None
) -> MetricsThroughputResponse:
    """`range` picks the window and bucket width (`_RANGES`, or `_DAILY_RANGES` for the longer
    90d/1y views the daily table makes possible); `queue_id` is optional -- omitted, this is the
    "site total, bucketed, broken down by queue" query shape (Chart 1's bar chart, and Chart 2's
    "All queues" line); supplied, it's the "one queue's series" shape (Chart 2 with a specific
    queue selected). Both shapes share this endpoint because they share the same underlying
    table and bucketing logic -- the only thing that changes is a `WHERE queue_id = ?` and which
    covering index SQLite picks.
    """
    db = request.app.state.db
    if range in _DAILY_RANGES:
        return await _get_daily_throughput(db, range, _DAILY_RANGES[range], queue_id)
    if range not in _RANGES:
        all_ranges = sorted({*_RANGES, *_DAILY_RANGES})
        raise HTTPException(status_code=422, detail=f"range must be one of {all_ranges}")
    hours, bucket_seconds = _RANGES[range]

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

    # Walk every bucket boundary the range covers, not just the ones with rows -- an idle
    # bucket (heartbeat present, nothing in metric_sample) must render as a real, present
    # zero, and only a bucket with no heartbeat at all is a gap (docs/decisions.md).
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

    return MetricsThroughputResponse(range=range, bucket_seconds=bucket_seconds, buckets=buckets)


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
