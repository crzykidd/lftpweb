---
name: 2026-08-13-header-24h-from-metrics
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Repointed /api/stats's transferred_24h_bytes at metric_sample via the shared
  core/metrics.py.queue_breakdown call the Dashboard already uses (idx_metric_sample_ts_queue),
  deleted the stale bytes_done comment, linked the header's 24h item to /dashboard, and added
  tests/test_stats_24h.py (5 tests) proving the two agree, survive a history clear, and that the
  other header stats are unaffected. 887 tests passing, both lint gates and the frontend build
  clean.
---

# Task: The header's 24h figure should be bytes actually transferred, and should link to the Dashboard

Found by the user on 2026-08-13: the header read `24h 0 B` while the Dashboard showed real
data.

## Why they disagree

They read different tables.

**Header** (`backend/lftpweb/api/stats.py:34-37`):

```sql
SELECT COALESCE(SUM(bytes_done), 0) AS bytes FROM job
WHERE state = 'succeeded' AND finished_at >= STRFTIME('%Y-%m-%dT%H:%M:%fZ','now','-1 day')
```

**Dashboard**: `metric_sample` (migration 005), via `core/metrics.py`.

So `48ad72c`'s Clear History — which deletes `job` rows and deliberately leaves `metric_sample`
alone — zeroes the header while the Dashboard is unaffected. Both behaved as designed; the
design was inconsistent. Clearing *history* should not zero a *usage* statistic.

They also measure different things, and the user has chosen:

- Job-based: bytes of **successfully completed** transfers. Partial bytes from failed or
  stopped attempts are excluded on purpose (the existing comment explains why: they would be
  counted again on whatever attempt eventually finishes).
- `metric_sample`: bytes **actually moved over the wire**, sampled every 30s, including
  attempts that later failed.

> the 24 hour counter should be actual bytes transferred so that is correct. We want to give
> people a quick glance on usage.

**Use `metric_sample`.** Usage is the intent, so bytes moved is the right measure, and it makes
the header and Dashboard structurally incapable of disagreeing.

## What to do

1. **Repoint `transferred_24h_bytes`** at `metric_sample`, summing `bytes_delta` over the last
   24 hours. **Reuse whatever `core/metrics.py` already exposes** rather than writing a fresh
   query in `api/stats.py` — if there is a suitable function, call it; if not, add one there so
   the Dashboard and the header share it. Two independently-written 24h sums is precisely how
   they drift apart again.
   - Migration 005 added two covering indexes chosen for the query shapes `api/metrics.py`
     serves, verified with `EXPLAIN QUERY PLAN` and benchmarked at ~430k rows. Check your query
     uses one of them, and say which.
   - `metric_sample` retention defaults to 7 days, so a 24h window is comfortably inside it —
     but confirm rather than assume, and consider what should happen if retention were ever set
     below 1 day.
2. **Delete the now-stale comment** in `api/stats.py` explaining the completed-transfers
   reasoning, and replace it with the new one. A leftover comment describing the old semantics
   is worse than none.
3. **Make the header's 24h item a link to the Dashboard** (`frontend/src/components/
   StatsHeader.tsx:49`). The user asked for it directly — the header is the glance, the
   Dashboard is the detail. Use the existing router `Link`, keep it keyboard-reachable, and do
   not make the whole header row clickable — just that item.

## Check the header's other stats while you are there

`queued_count`/`queued_bytes` come from `job WHERE state = 'queued'`, and
`current_speed_bps`/`allocated_bps`/`ceiling_bps` from the transfer queue's live state. Those
are live state rather than history, and Clear History refuses to touch non-terminal jobs, so
they should be unaffected — **verify that rather than assuming it**, and say so in your report.

## Before you start

- `backend/lftpweb/api/stats.py`, `core/metrics.py`, `api/metrics.py`.
- `backend/lftpweb/migrations/005_throughput_metrics.sql` — the table shape and both indexes.
- `frontend/src/components/StatsHeader.tsx` and the router setup for the Dashboard route.
- `48ad72c`'s Clear History, for why `metric_sample` is deliberately out of its scope.

## Working tree check

`git status --porcelain`. Another task may have just landed in `models.py` and the frontend. If
files you need are dirty, list them and ask.

## Tests

- The 24h figure reflects `metric_sample`, not `job` — including after every `job` row is
  deleted, which is the reported bug.
- It matches what the Dashboard reports for the same window. **Assert they agree**, since that
  equality is the point of the change.
- A window with no samples returns 0 rather than erroring or returning null.
- The other header stats are unaffected by clearing history.

## Conventions to honor

- `docs/decisions.md`, newest at top — bytes-moved versus bytes-completed, and why the shared
  source matters.
- `CHANGELOG.md` under `### Fixed`; `DESIGN.md` §9.1 describes this header stat — update it,
  standing approval applies.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `fix:` message, which index your
   query uses, whether you shared a function with the Dashboard or wrote a new one and why, what
   you found about the other header stats, test count, lint results, and anything not fixed.
   Never `git add -A`, never push.
