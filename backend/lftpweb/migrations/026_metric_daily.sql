-- Daily per-queue throughput rollups (prompts/done/2026-08-21-daily-metric-rollups.md,
-- DESIGN.md §10.4, docs/decisions.md carries the one-table-not-two, rollup-before-prune, and
-- UTC-day reasoning). `metric_sample`/`metric_heartbeat` (migration 005) are raw, ~30s-interval
-- rows kept a matter of days (`core/metrics.py.DEFAULT_RETENTION_DAYS`/`MAX_RETENTION_DAYS`) --
-- nowhere near long enough for "how much have I downloaded this year". This table is the
-- long-horizon answer: one row per (queue, UTC calendar day), recomputed from raw by
-- `core/metrics.py.rollup_day` and kept for `DAILY_RETENTION_DAYS` (~13 months) -- long enough
-- for a year-over-year glance without ever storing 30s-resolution data for nine-month-old days
-- nobody needs.
--
-- **Additive only** -- a new table, no change to any existing one, nothing to backfill in this
-- migration itself (the rollup scheduler backfills from existing raw rows the first time it
-- runs after upgrade, same as any other day it's ever rolled up -- see that function's
-- docstring).
--
-- One row per queue per day, not one row per day total: `bytes` is that one queue's own bytes
-- for that day (`SUM(metric_sample.bytes_delta)`, recomputed from raw every time, never
-- incremented -- idempotent by construction). A queue with zero bytes on a day the app was up
-- still gets a row (bytes = 0) -- the daily-granularity equivalent of `metric_sample`'s own
-- idle-vs-down distinction, carried up from `metric_heartbeat` via `heartbeat_count` below.
--
-- `heartbeat_count` is the coverage figure item 3 of the task calls for -- how many
-- `metric_heartbeat` rows fell on this UTC day, site-wide (heartbeats aren't per-queue, so
-- every queue's row for the same day carries the same count). A day the container was up the
-- whole time reads near the full expected count for the sample cadence in force; a day it was
-- down for hours reads a fraction of that -- so a genuinely quiet day (full coverage, bytes 0)
-- is never confused with a mostly-down one (partial coverage, bytes 0) the way a bytes-only row
-- would. A day with **zero** heartbeats at all (entirely down) gets no row for any queue --
-- consistent with `metric_sample`/`metric_heartbeat`'s own gap semantics: no row here means "no
-- data for this day," not "zero was measured."
--
-- `day` is a UTC calendar date ('YYYY-MM-DD'), not a timestamp -- this codebase stores
-- everything UTC with no timezone handling anywhere (README's Known gaps already documents
-- History's date filters as UTC calendar days for the same reason); a daily rollup boundary at
-- UTC midnight is the existing convention, not a new one, and a real timezone setting is out of
-- scope for this task (docs/decisions.md).
--
-- `(queue_id, day)` as the PRIMARY KEY, no surrogate id, doubles as the UNIQUE constraint the
-- upsert needs (`INSERT ... ON CONFLICT (queue_id, day) DO UPDATE`) -- same shape
-- `metric_heartbeat`'s `TEXT PRIMARY KEY` already uses for the identical reason.
CREATE TABLE metric_daily (
    queue_id        INTEGER NOT NULL REFERENCES path_queue (id) ON DELETE CASCADE,
    day             TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    heartbeat_count INTEGER NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (queue_id, day)
);

-- The one query shape this table serves: "every queue's row(s) across a day range" -- the
-- 90d/1y bytes-chart ranges (`api/metrics.py`) and the all-time total (`core/metrics.py.
-- total_bytes`). `day` leads so a range scan (`WHERE day >= ? AND day <= ?`) with no queue_id
-- filter is index-only; a query naming one queue_id benefits too since it's still a covering
-- index for either column order (small table, ~790 rows/13 months for two queues -- no second
-- index is worth the write cost here the way `metric_sample`'s two indexes are at ~430k rows).
CREATE INDEX idx_metric_daily_day ON metric_daily (day);
