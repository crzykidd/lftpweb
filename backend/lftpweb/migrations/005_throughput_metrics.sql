-- Throughput metrics (DESIGN.md — new section proposed alongside this migration; see
-- docs/decisions.md for the exact wording proposed, not applied here per this task's
-- working-tree constraint). Backs the Dashboard page's two charts (api/metrics.py):
-- bytes-per-hour over the last 24h, and speed over a selectable 1h/12h/24h window.
--
-- Two tables, deliberately not one:
--
-- `metric_sample` — one row per (queue, ~30s interval), written ONLY when that queue's
-- running jobs moved a nonzero number of bytes in the interval (core/metrics.py's
-- ThroughputSampler). At 30-day retention x 5 queues x a 30s sample interval that's already
-- ~430k rows for real activity alone (this migration's own benchmark, see docs/decisions.md)
-- -- padding every idle queue with an explicit zero every 30s on top of that would only
-- inflate the table for information the second table already recovers for free.
--
-- `metric_heartbeat` — one row per sample tick, unconditionally, whether or not anything
-- was transferring. Its presence over a stretch of time is what "lftpweb was running" means;
-- its absence is what "down" means. This is how an idle instance (heartbeats continue, no
-- metric_sample rows -- a real, informative zero) is told apart from a stopped one
-- (heartbeats stop -- a gap in the chart, never rendered as a flat zero line) without
-- writing a row per queue per interval just to mark liveness.
CREATE TABLE metric_sample (
    id          INTEGER PRIMARY KEY,
    queue_id    INTEGER NOT NULL REFERENCES path_queue (id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,      -- UTC ISO-8601, the sample's end time (interval is [prev ts, ts))
    bytes_delta INTEGER NOT NULL    -- bytes moved by this queue's running jobs since the previous
                                     -- sample; always >= 0 (core/metrics.py clamps the non-monotonic
                                     -- job.bytes_done/bytes_start trap -- see that module's docstring)
);

-- Two covering indexes for the two query shapes api/metrics.py serves -- both are index-only
-- scans (no row lookup against the table itself), proven with EXPLAIN QUERY PLAN and
-- benchmarked at ~430k rows; numbers in docs/decisions.md.
--
-- (queue_id, ts, bytes_delta): "one queue's series over a time range" -- WHERE queue_id = ?
-- AND ts BETWEEN ? AND ?, the per-queue line in the Dashboard's speed chart.
CREATE INDEX idx_metric_sample_queue_ts ON metric_sample (queue_id, ts, bytes_delta);
-- (ts, queue_id, bytes_delta): "site total over a time range, bucketed" -- WHERE ts BETWEEN
-- ? AND ? with no queue_id filter, GROUP BY bucket, queue_id -- the bytes-per-hour bar chart
-- and the "all queues" speed line (site total is summed across queue_id at query time,
-- decision recorded in docs/decisions.md -- never a separately stored row).
CREATE INDEX idx_metric_sample_ts_queue ON metric_sample (ts, queue_id, bytes_delta);

-- The liveness marker described above. TEXT PRIMARY KEY (not AUTOINCREMENT) is intentional:
-- ts already is the sample's identity (core/metrics.py writes exactly one heartbeat per
-- sample tick), so a second surrogate id would be redundant and this doubles as a UNIQUE
-- constraint against writing two heartbeats for the same instant.
CREATE TABLE metric_heartbeat (
    ts TEXT PRIMARY KEY
);
