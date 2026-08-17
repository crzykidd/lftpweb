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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from lftpweb.core import metrics
from lftpweb.models import (
    MetricsBucketOut,
    MetricsSettingsIn,
    MetricsSettingsOut,
    MetricsThroughputResponse,
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


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@router.get("/throughput", response_model=MetricsThroughputResponse)
async def get_throughput(
    request: Request, range: str = "24h", queue_id: int | None = None
) -> MetricsThroughputResponse:
    """`range` picks the window and bucket width (`_RANGES`); `queue_id` is optional --
    omitted, this is the "site total, bucketed, broken down by queue" query shape (Chart 1's
    bar chart, and Chart 2's "All queues" line); supplied, it's the "one queue's series" shape
    (Chart 2 with a specific queue selected). Both shapes share this endpoint because they
    share the same underlying table and bucketing logic (`core/metrics.py.queue_breakdown`) --
    the only thing that changes is a `WHERE queue_id = ?` and which covering index SQLite
    picks.
    """
    if range not in _RANGES:
        raise HTTPException(status_code=422, detail=f"range must be one of {sorted(_RANGES)}")
    hours, bucket_seconds = _RANGES[range]
    db = request.app.state.db

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
