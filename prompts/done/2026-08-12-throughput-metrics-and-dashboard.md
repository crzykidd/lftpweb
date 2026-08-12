---
name: 2026-08-12-throughput-metrics-and-dashboard
status: completed          # pending | completed | failed
created: 2026-08-12
model: sonnet            # coding; every product decision below is already made
completed: 2026-08-12
result: >
  Built core/metrics.py (30s-tick sampler decoupled from the ~1Hz transfer tick, retention
  settings + pruning scheduler), migration 005_throughput_metrics.sql (metric_sample +
  metric_heartbeat, two covering indexes), api/metrics.py (GET /api/metrics/throughput,
  GET/PUT /api/settings/metrics), and a new Dashboard page with two hand-rolled SVG charts
  (bytes/hour bar chart, speed line chart with a 1h/12h/24h + queue selector). Non-monotonic
  job.bytes_done trap closed via per-job bytes_done-bytes_start tracking, pinned by a
  restart-mid-flight test. Idle-vs-down via a separate heartbeat table. Both query shapes
  proven index-only (EXPLAIN QUERY PLAN) and benchmarked at ~430k rows (single-digit ms for
  the real 24h case). 486 pytest passed (461 + 25 new), ruff/tsc/oxlint clean, verified live
  over HTTP against the running dev stack. Full detail in docs/decisions.md, newest entry.
---

# Task: Persist throughput samples, and add a Dashboard page with two charts

lftpweb stores **no** metrics today. `core/progress.py` performs zero database writes — speed and
ETA are an in-memory EMA computed at ~1 Hz, pushed over the WebSocket, and lost on every restart.
The only durable throughput record is the `job` table's `bytes_done` / `started_at` /
`finished_at`, which says what a transfer moved in total but nothing about *when*.

Build a small sample store and a Dashboard page with two hand-rolled SVG charts:
- **bytes transferred per hour, last 24 hours** (bar chart)
- **transfer speed over time**, with a 1 h / 12 h / 24 h range selector (line chart)

## Decisions already made by the user — implement, do not re-litigate

1. **Per-queue samples**, not site-total only. The site total is a sum across queues at query time.
2. **Retention: 7 days by default, user-configurable up to 30 days.** Store it like every other
   `*Settings` dataclass (in `setting`, the way `TransferSettings`/`PostprocessSettings`/backup
   settings already do). Prune on the same pattern as `core/backup.py`'s retention loop.
3. **Hand-rolled inline SVG. No charting dependency.** This project has added exactly one
   frontend dependency since phase 3b and deliberately never adopted TanStack Query
   (`docs/decisions.md`). A bar chart and a line chart do not justify ~100 KB on a 360 KB bundle.
4. **A new Dashboard page**, not an expansion of the stats header. How the header might later
   surface this is a separate conversation the user will have after seeing it.
5. **Store deltas, not instantaneous speeds.** Speed is derived at query time (delta ÷ interval).
   One table then serves both charts by re-bucketing — no second mechanism, no double counting.
6. **Downtime renders as a gap, not a zero.** A flat zero line implies "nothing was transferring"
   when the truth is "lftpweb was not running". This distinction must survive into the chart.

## Before you start

- **Read `DESIGN.md` §1.3, §4.4, §5, and §9** before writing code. §1.3 is the load-bearing one:
  *lftp is a transfer engine, not a status API* — progress is derived from the filesystem. **This
  table must be fed from the same byte accounting everything else uses
  (`core/local_scan.py`/`core/progress.py`), never from parsing lftp's output.** A metrics table
  fed by scraping lftp would reintroduce exactly what §1.2 rejects.
- Read `core/progress.py` (the existing ~1 Hz sampler over the active set),
  `core/queue.py`'s tick loop, `core/backup.py` (settings + retention-loop shape to mirror), and
  `api/history.py` (pagination/filter conventions, and its documented reasoning about bounded vs
  unbounded endpoints).
- Read `docs/decisions.md`'s newest entries — several changes landed today.

## Working tree check

Run `git status --porcelain` first. The tree is dirty on purpose: dev-environment fixes
(`docker/Dockerfile`, `docker-compose*.yml`, `frontend/vite.config.ts`), the `_UNPACK_` extraction
change, a Settings → Transfer tab, a post-processing state-persistence change, and an
empty-directory reconcile fix. **None are yours — do not revert, refactor, or tidy them.**
`CHANGELOG.md`, `standards.md`, `prompts/startnewsession.md`, `.claude/commands/release-prep.md`
were dirty before the session; leave them alone. Append to `docs/decisions.md` at the top.
If a file you must modify is dirty, list it and ask first.

## Part A — the sample store

1. **Migration `005_*.sql`** adding the sample table. Follow the existing migrations' style.
   Columns at minimum: `queue_id`, a UTC timestamp, and bytes transferred in that interval.
   The pre-migration backup in `db.py.migrate()` runs automatically — do not disable it.

2. **Indexes are an explicit requirement of this task, not an afterthought.** Both query shapes
   must be index-driven:
   - site total over a time range (sum across all queues, bucketed)
   - one queue's series over a time range

   Design the index(es) for those two shapes, and **prove it**: include `EXPLAIN QUERY PLAN`
   output for both queries in your report, showing an index being used and no full table scan.
   A covering index that avoids touching the table at all is worth considering — say why you did
   or didn't.

3. **Benchmark at realistic scale, not at three rows.** 30-day retention × 5 queues × a
   30-second interval is roughly **430,000 rows**. Seed a throwaway database at that size, run
   both queries, and report actual timings. If a query is slow, fix the schema — do not report a
   slow query as acceptable. The 24 h chart reading ~14,000 rows should be single-digit
   milliseconds.

4. **Sampling interval: 30 seconds.** The transfer tick is already ~1 Hz; sample every 30th tick
   rather than adding a second timer. Justify in a comment why the sample interval is decoupled
   from the tick interval.

5. **The non-monotonic trap — this will bite you if you ignore it.** `job.bytes_done` is **not**
   monotonic: a retry or a resume resets it, which is exactly why `bytes_start` exists. A naive
   `current − previous` delta goes negative and renders as a phantom spike. Compute deltas
   per job, clamp at zero, and **write a test that a job restarting mid-flight produces no
   negative or inflated sample.**

6. **Distinguish idle from down** (decision 6). An idle instance and a stopped instance must not
   look alike. Choose a mechanism — a heartbeat row whose absence marks downtime, or an explicit
   sampler-alive marker — and explain it. Do not write a zero-byte row per queue per interval
   just to fill the timeline; that inflates the table for an instance that transfers nothing.
   Absence of data for a queue that was running means zero; absence of the *heartbeat* means down.

7. **Retention/pruning** with the settings dataclass (default 7 days, max 30, validated
   server-side). Mirror `core/backup.py`'s loop shape (`_task`/`start()`/`stop()`), and prune
   oldest-first.

8. **API.** Endpoints for the two charts. Return **pre-bucketed** series (the server does the
   bucketing in SQL — that is what the indexes are for), not raw rows for the browser to
   aggregate. Include the queue breakdown and the site total. Follow `api/history.py`'s
   conventions for range parameters and server-enforced caps.

## Part B — the Dashboard page

9. New page + nav entry (`nav.ts`), matching the existing pages' structure and idiom. Use the
   project's hand-rolled fetch/poll hooks — **do not introduce TanStack Query.**

10. **Chart 1:** bytes per hour, last 24 h, bar chart, stacked or grouped per queue with a
    site total. **Chart 2:** speed over time, line chart, with a 1 h / 12 h / 24 h selector; pick
    the bucket width per range (finer for 1 h, coarser for 24 h) and say what you chose.

11. **Both charts hand-rolled SVG**, and both must:
    - render gaps as gaps (decision 6) — a broken line or an empty bucket, never a zero
    - be readable in **both** light and dark themes (the app has both — check how existing
      components handle it)
    - degrade honestly with no data at all: an empty state that says so, not an empty axis
    - carry accessible text alternatives for the numbers (the existing pages' conventions apply)

12. Format bytes/rates with the existing helpers in `frontend/src/lib/format.ts` — extend them if
    needed rather than adding a parallel formatter.

## Conventions to honor

- Comments explain **why**, matching the surrounding density and voice. Cite `DESIGN.md` sections.
- Backend gates: `uv run ruff format --check` **and** `uv run ruff check` (run the format check
  explicitly — it has caught files `check` alone missed four times here), plus the full
  `uv run pytest` (461 tests currently pass; no regressions).
- Frontend gates: `npm run build` and `npm run lint` clean.
- **No browser exists in this environment.** Exercise every new endpoint over real HTTP against
  the running dev stack (`http://localhost:8087`) and report exactly what you verified. **Never
  claim the charts render correctly** — you cannot see them.
- The dev stack and fake seedbox are **running and in use by the user** (`lftpweb-backend-1`,
  `lftpweb-frontend-1`, `lftpweb-test-seedbox-gnu`, `lftpweb-test-seedbox-busybox`). Leave them
  running; do not disturb `/data/pickup`. `docker compose -f docker-compose.dev.yml restart backend`
  picks up backend changes; the frontend hot-reloads.
- If `DESIGN.md` should gain a section for this (it has no Dashboard page and no metrics store),
  **propose the wording in your report** — do not edit `DESIGN.md` yourself.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`, newest at top — the schema and index choice with the
   measured numbers, the idle-vs-down mechanism, and the bucket widths.
4. **Do not commit. Do not push.** Prepare the tree, then report back with the file list and a
   proposed one-line commit message (`feat:` prefix, no `Co-authored-by:` trailer).
