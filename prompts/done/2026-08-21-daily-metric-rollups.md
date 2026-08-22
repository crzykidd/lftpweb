---
name: 2026-08-21-daily-metric-rollups
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: >
  Added metric_daily (migration 026, one row per queue per UTC day, 13-month retention),
  rolled up hourly from the raw tables strictly before prune_metrics (ordering pinned by a
  dedicated test), with idempotent upsert and automatic startup backfill. Raised raw retention
  default 7 -> 30 days. New GET /api/metrics/total and 90d/1y throughput ranges backed by the
  daily table; Dashboard gained a "total downloaded" readout, two new chart-range buttons, and
  a partial-coverage marker. 1654 backend / 649 frontend tests, 0 skipped (+17 backend / +4
  frontend). Not committed -- prepared for the orchestrating session to review and commit.
---

# Task: daily per-queue rollups, so "how much have I downloaded" survives past the raw retention window

**⏸ DO NOT START THIS BEFORE `0.3.0` IS CUT.** Written 2026-08-21 and deliberately parked: the
release is already large and coherent (the Transfers redesign), and this adds a migration, a
scheduler and new UI. It is the first thing to pick up afterwards.

## Why

`core/metrics.py` keeps two raw tables (`metric_sample`, `metric_heartbeat`) with a **7-day default
retention** and a **30-day hard maximum** (`DEFAULT_RETENTION_DAYS`, `MAX_RETENTION_DAYS`). The
Dashboard offers a **`30d`** range. So out of the box that range is ~77% empty, and no amount of
configuration gets you past 30 days.

The user wants a long-horizon view — *"a year table that has daily totals from each queue ... so
that a user can have the option to just see their total downloaded amount later."*

Raw data cannot serve that: 365 days of ~30-second samples is on the order of a million heartbeat
rows plus millions of samples, and nobody needs 30-second granularity from nine months ago.

## Decided with the user — do not revisit

| | |
|---|---|
| **One daily table, not daily + weekly** | A daily row is one per queue per day — ~790 rows for two queues over 13 months. Weekly is derivable by summing daily on read, and keeping both risks the two disagreeing. Compute weekly/monthly views from the daily table. |
| **Keep daily rows for 13 months** | Long enough for a year-over-year glance. |
| **Raise raw retention default 7 → 30 days** | Fold in here rather than shipping separately: the offered `30d` chart range should work out of the box. Volume is trivial (~86k heartbeat rows over 30 days). `MAX_RETENTION_DAYS` stays 30 — the daily table is what serves anything longer. |

## The three things that make this non-trivial

### 1. Rollup MUST run before prune, or days are lost permanently

This is the only part of this feature that can **destroy data** rather than merely be wrong. The
pruner already deletes raw rows past retention (that is the `pruned N metric sample(s) and M
heartbeat(s)` log line). If it runs before a day has been rolled up, that day is gone with no
recovery.

- Order the two explicitly; do not rely on scheduling coincidence.
- The rollup window must be comfortably shorter than the retention window, so there is slack.
- Add a test that asserts the ordering — not just that each works alone.

### 2. Idempotent, and able to backfill

- Rolling up the same day twice must **not** double-count. Use an upsert keyed on
  `(queue_id, day)`, recomputed from raw rather than incremented.
- On startup, **backfill every complete day still present in raw that has no daily row yet** — the
  app may have been down for days, and it may have been upgraded into this feature with a week of
  raw data already sitting there.
- Never roll up **today** as if it were complete; it is still accumulating. Only closed days.

### 3. Preserve the idle-vs-down distinction

`core/metrics.py`'s module docstring explains why there are two raw tables at all: heartbeat
*presence* means "lftpweb was running", heartbeat *absence* means "down". A bucket with heartbeats
but no samples is a real, informative **zero (idle)**; a bucket with no heartbeats is a **gap
(down)**. The docstring is explicit that these must never render the same way.

**A daily row that records only bytes throws that away** — a day the container was off for 20 hours
would read identically to a quiet day. Carry a coverage figure on the daily row (seconds or count
of heartbeats observed that day) so a partial day is knowable, and make the UI able to say so.

## Timezone — decide and state it

Everything in this codebase is stored UTC with **no timezone handling anywhere**; README's Known
gaps already records that History's date filters are UTC calendar days, so away from UTC
"yesterday" includes a few hours of today.

**Default: inherit that, and document it** — daily rollups are UTC days. For a running *total
downloaded* the boundary barely matters; for "what did I pull yesterday" it does.

Introducing a real timezone setting would be a **separate, larger feature** touching History's
filters too — do **not** grow this task into it. If you think this feature makes UTC-only
untenable, say so in your report and let the user decide rather than building it.

## What to build

1. **Migration `026_`** — one daily table: `(queue_id, day, bytes, coverage, …)` with a unique key
   on `(queue_id, day)` for the upsert. Additive only.
2. **The rollup**, in `core/metrics.py`'s existing scheduler shape, ordered before the pruner, with
   startup backfill.
3. **Retention**: raw default 7 → 30. Daily rows pruned at 13 months.
4. **API** — a range/series endpoint over the daily table, and a **total** figure. Follow
   `api/metrics.py`'s existing `_RANGES` shape rather than inventing a second idiom; extend it with
   the longer ranges the daily table now makes possible (e.g. `90d`, `1y`).
5. **UI** — the Dashboard is the natural home. A **"total downloaded"** readout is the thing the
   user actually asked for; the longer chart ranges follow from it. Reuse the existing chart
   components and the retention-note pattern that already explains empty older buckets.

## Before you start

- `backend/lftpweb/core/metrics.py` — **read the whole module docstring.** It documents the
  two-table design, the idle-vs-down reasoning, and the non-monotonic `bytes_done`/`bytes_start`
  trap (a retried job's `bytes_done` already includes an earlier job's bytes — differencing by job
  id alone produces phantom spikes). Your rollup sums existing samples so it inherits the fix, but
  understand it before touching anything here.
- `backend/lftpweb/api/metrics.py` — `_RANGES`, the bucketing, and the existing series shape.
- `frontend/src/lib/bytesChart.ts` and the Dashboard's chart components.
- `DESIGN.md` §10.4 and `docs/decisions.md`'s metrics entries.

## Tests

- Rollup-before-prune ordering (the data-loss case).
- Idempotency: rolling the same day twice yields the same totals.
- Startup backfill across several missing days.
- Today is never rolled up as complete.
- Coverage distinguishes a full quiet day from a day the app was mostly down.
- Daily totals equal the sum of the raw samples they replace, for a day still present in both.
- 13-month pruning of daily rows.

## Docs

`DESIGN.md` §10.4; `CHANGELOG.md`; `docs/concepts.md` (users will ask why old detail disappears but
totals remain — that is exactly the kind of thing that page is for); `docs/decisions.md` for the
one-table choice, the rollup-before-prune ordering, and the UTC decision; `README.md` if the
retention default change affects anything it states.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`, and it caught
  three agents on 2026-08-20/21 regardless.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should check.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
