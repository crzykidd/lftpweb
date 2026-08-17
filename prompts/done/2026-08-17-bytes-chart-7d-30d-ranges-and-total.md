---
name: 2026-08-17-bytes-chart-7d-30d-ranges-and-total
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: |
  Backend: added "7d" (168h, 21600s buckets) and "30d" (720h, 86400s buckets) to
  api/metrics.py's _RANGES, extending the existing finer/coarser comment; queue_breakdown/
  heartbeat_buckets already took arbitrary bucket_seconds, verified rather than assumed.
  Frontend: split MetricsRange into SpeedRange ('1h'|'12h'|'24h', Chart 2, untouched) and
  BytesRange ('24h'|'7d'|'30d', Chart 1, new) in api/types.ts. DashboardPage gained an
  independent Chart 1 range selector with its own localStorage key (dashboard.bytesRange,
  synchronous initial read, same no-flash pattern as Chart 2's). Renamed
  BytesPerHourChart.tsx -> BytesChart.tsx (thin rename, all imports updated) since it no
  longer only shows per-hour buckets; title and bar/tooltip labels now scale with
  bucket_seconds via new pure helpers in lib/bytesChart.ts (bucketLabel, bytesChartTitle,
  sumTotalBytes, sumBytesByQueue, retentionNoteForRange), Vitest-tested in
  lib/bytesChart.test.ts. Header total changed to "Total: X"; legend entries now append each
  queue's own range total. Retention honesty: Dashboard now calls the existing
  GET /api/settings/metrics once (no new endpoint) and shows a muted note when the selected
  bytesRange's day-span exceeds configured retention. Backend tests extended in
  tests/test_metrics_api.py for both new ranges (bucket_seconds, and the N+1 bucket count from
  the existing inclusive-boundary walk -- 29 for 7d, 31 for 30d -- plus the pre-existing
  unknown-range-still-422 test covers rejection). All gates green: backend ruff check, ruff
  format --check, and the full `uv run pytest` (1281 passed) each run separately in the
  foreground; frontend npm run lint, npm test (468 passed), npm run build. No browser was
  used -- the chart ships unviewed, per the prompt's own instruction.
---

# Task: Dashboard bytes chart gains 24h / 7d / 30d ranges and a range total

User request (2026-08-17): the Dashboard's bytes-transferred chart only ever shows the
last 24 hours by hour. Add a range selector — **24h, 7 days, 30 days** — plus a
**total transferred over the selected range** readout, "giving you a good view and
total of all." The speed chart's existing 1h/12h/24h selector is untouched — speed
over a month is not what was asked and its fine buckets don't scale there.

## Before you start

- Read `CLAUDE.md`; skim `DESIGN.md`'s Dashboard/metrics material if any (§ index) and
  `docs/decisions.md`'s 2026-08-12 metrics/Dashboard entries.
- Read before editing:
  - `backend/lftpweb/api/metrics.py` — `_RANGES` (range → (hours back, bucket
    seconds)), `get_throughput`, and the idle-zero vs. gap bucket-walk logic its
    comments explain. Note the deliberate "24h bucket width == the bar chart's hourly
    width" comment — your new ranges extend that reasoning, they don't break it.
  - `backend/lftpweb/core/metrics.py` — `queue_breakdown`/`heartbeat_buckets`
    (shared bucketing; confirm they take arbitrary `bucket_seconds` — they should) and
    **where retention lives** (default 7 days, configurable to 30): find the actual
    setting name and how/whether the frontend can read it.
  - `frontend/src/pages/DashboardPage.tsx` — the whole file is short; Chart 1 is
    hard-wired `getThroughput('24h')`, Chart 2 owns the existing range
    selector/localStorage pattern (`dashboard.range`, synchronous initial read — copy
    that pattern exactly, including the no-flash reasoning in its comment).
  - `frontend/src/components/charts/BytesPerHourChart.tsx` and `api/types.ts`'s
    `MetricsRange`/`MetricsThroughputResponse`.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. Surface unrelated dirty files once as
awareness; don't block. This file (the handoff prompt itself) is exempt.

## What to do

1. **Backend — two new ranges in `_RANGES`:** `"7d": (168, 21600)` (28 × 6-hour
   buckets) and `"30d": (720, 86400)` (30 × 1-day buckets). Same
   finer-when-short/coarser-when-long reasoning as the existing comment — extend that
   comment rather than writing a parallel one. Everything downstream
   (`queue_breakdown`, `heartbeat_buckets`, the bucket walk, the response model)
   should already be parameterized by `bucket_seconds`; verify rather than assume, and
   keep the endpoint's validation of unknown `range` values whatever it is today.
2. **Frontend — Chart 1 gets its own range selector: 24h / 7d / 30d.** Same button
   group styling as Chart 2's, its own localStorage key (`dashboard.bytesRange`,
   default `'24h'`, synchronous initial read — same no-flash pattern and the same
   `lib/storage.ts` helpers). Chart 1's fetcher uses the selected range. The two
   charts' selectors are independent; extend `MetricsRange` (or split the type) so
   each selector's option list is typed to what it actually offers.
3. **Bar labeling scales with the bucket width.** The x-axis/hover labels currently
   assume hourly buckets ("bytes per hour"); at 6h buckets label the bucket's start
   time/day, at 1d buckets the date. The chart title should say what it shows for the
   range (e.g. "Bytes transferred — per hour / per 6 hours / per day"). Rename the
   component only if it stops being per-hour-specific in name vs. behavior
   (`BytesPerHourChart` → e.g. `BytesChart`) — if you rename, update every import; a
   thin rename is fine, a parallel second chart component is not.
4. **The total.** In Chart 1's header row, show the summed bytes across all buckets
   and queues for the selected range (existing `formatBytes` helper), e.g.
   "Total: 84.2 GB". If the chart has a per-queue legend, append each queue's own
   range total to its legend entry — same numbers, one place. Totals are computed
   client-side from the buckets already fetched (pure helper + Vitest test; no new
   endpoint).
5. **Retention honesty.** Sample retention defaults to 7 days (30 max), so a 30d — or
   even 7d — selection can cover days with no data at all; those render as gaps
   (no-heartbeat buckets), which is correct but unexplained. If the frontend can
   cheaply read the retention setting (an existing settings GET the Dashboard can call
   once — check while reading `core/metrics.py`; do not build a new endpoint for
   this), show a one-line muted note when the selected range exceeds retention:
   "Only the last N days are retained — older buckets are empty. Retention is
   configurable in Settings." If no cheap read exists, put the same fact in the
   CHANGELOG entry and `docs/decisions.md` as a named, deliberate gap instead —
   don't silently ship a chart that looks broken at 30d on a default install.
6. **Tests:** backend — extend the existing metrics API tests for the two new ranges
   (bucket count, bucket width, an unknown range still rejected); frontend — Vitest
   for the total computation and any new pure labeling/range helpers.
7. **Docs, same commit:** `CHANGELOG.md` under Unreleased (append after existing
   entries). `docs/decisions.md` entry for the bucket-width choices and the retention
   note decision (whichever branch of 5 you took).

## Conventions to honor

- Gates, each run separately, exit codes read: backend `uv run --project backend ruff
  check`, `uv run --project backend ruff format --check`, `uv run pytest` (repo
  root); frontend `npm run lint`, `npm test`, `npm run build`.
- Comment style: dated, rationale-naming; match the metrics module's existing
  explanatory density.
- No browser here — the chart ships unviewed; say so.
- Conventional-Commit prefix `feat:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter: set `status`, `completed`, `result`.
2. Move this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit covering this prompt file, the files modified, and the prompt
   move. Present the file list and a one-line message.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree and
     report the file list + proposed message back to the orchestrating session.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
