---
name: 2026-08-21-chart-grouping
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: >
  Decoupled range and grouping in api/metrics.py (_RANGE_HOURS/_DAILY_RANGES for how far back,
  _DEFAULT_GROUP/_AVAILABLE_GROUPS for how wide a bar); new defaults per the task table (7d
  6-hour->daily, 90d/1y daily->weekly); week/month summed from daily rows on read via
  _DayPoint/_aggregate_day_points, no new table; hourly rejected 422 at 90d/1y both server- and
  client-side (lib/bytesChart.ts.groupOptionsForRange); Dashboard gained a group-by dropdown
  (dashboard.bytesGroup, validated per range). 1685 backend / 680 frontend tests, 0 skipped.
  Not committed or pushed -- prepared for the orchestrating session to commit.
---

# Task: Dashboard chart — better default bucketing, and a group-by control

Finding **5** of `prompts/test-findings-2026-08-21.md`, from the user's browser test of `8ae1e53`.
**Not a defect** — the ranges work. This is about bucket width being fixed per range.

> *"24h = hourly. 7 day = daily. 30 day maybe daily, but 90 day and yearly is weekly I think… might
> be good to default to those but have a dropdown for group by hour/day/week/month."*

## What to change

**New per-range defaults**, then a **group-by dropdown (hour / day / week / month)** over the top —
the defaults being defaults, not the only option:

| Range | Bucket today | New default |
|---|---|---|
| `1h` | 60 × 1-minute | unchanged |
| `12h` | 48 × 15-minute | unchanged |
| `24h` | 24 × 1-hour | **hourly** (already correct) |
| `7d` | **28 × 6-hour** | **daily** ← the one default that actually changes |
| `30d` | 30 × 1-day | **daily** (already correct) |
| `90d` | 90 × 1-day | **weekly** |
| `1y` | 365 × 1-day | **weekly** |

## The constraint that must shape the control

**Not every grouping is available at every range, and the dropdown must say so rather than fake it.**

- Raw tables (`metric_sample`/`metric_heartbeat`) are capped at **30 days** retention
  (`MAX_RETENTION_DAYS`), and `metric_daily` is **one-day granularity by construction**.
- Therefore **hourly grouping is impossible at `90d` and `1y`** — there is no sub-day data that far
  back and no setting can produce it.
- **Disable unavailable groupings with a visible reason.** Do not offer them and silently return
  something coarser. This is the same discipline as
  `docs/download-client-api-survey.md` §4's capability rule: a missing capability disables a control,
  it never fakes one.

## The cheap half

**Week and month are derived by summing daily rows on read.** No new table, no migration. That was
anticipated by the rollup design and is written down in
`prompts/done/2026-08-21-daily-metric-rollups.md`: *"weekly is derivable by summing daily on read,
and keeping both risks the two disagreeing."* **Do not add a weekly or monthly rollup table.**

## Structural note

`api/metrics.py._RANGES` currently couples range and bucket width in one tuple
(`"7d": (168, 21600)`), and `_DAILY_RANGES` is a second dict for the daily-table ranges. A group-by
control means **decoupling those two concepts** — the range says *how far back*, the grouping says
*how wide a bar*. Do that deliberately rather than adding more entries to the dicts; the current
shape does not extend to a second axis.

Preserve what the existing endpoint already gets right:
- the **idle-vs-down distinction** (heartbeat presence = a real zero, absence = a gap) — this is
  the whole reason there are two raw tables, and a regrouping must not flatten it;
- the **coverage** figure and its partial-day marker added by `8ae1e53`. Re-grouping to a week means
  deciding what coverage means for a week — say what you chose;
- the **retention note** that explains empty older buckets.

## Before you start

- `backend/lftpweb/api/metrics.py` — `_RANGES`, `_DAILY_RANGES`, `_get_daily_throughput`, and the
  module docstring.
- `backend/lftpweb/core/metrics.py` — the module docstring on idle-vs-down and the non-monotonic
  `bytes_done` trap.
- `frontend/src/lib/bytesChart.ts` (+ its tests), `frontend/src/components/charts/BytesChart.tsx`,
  `frontend/src/pages/DashboardPage.tsx`.
- The existing range selector's persistence idiom (`dashboard.bytesRange` in localStorage,
  validated on read) — the grouping choice should follow the same pattern, including validating a
  stale or hand-edited stored value rather than trusting it.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

Each range's default grouping; week/month totals equal the sum of the daily rows they aggregate;
hourly is rejected/unavailable for `90d`/`1y` (server-side too, not only greyed out in the UI); the
idle-vs-down distinction survives regrouping; a stale stored grouping falls back rather than being
trusted; coverage aggregates sensibly to the wider bucket.

## Docs

`CHANGELOG.md`; `DESIGN.md` §10.4 if it states bucket widths; `docs/concepts.md` if it describes the
chart ranges. Mark finding 5 **done** in `prompts/test-findings-2026-08-21.md`. Append a one-line
entry to `prompts/startnewsession.md`'s "On `dev` since the release" — same commit.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will stall
  forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test -- --run`.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `feat:`. No
  `Co-authored-by:`.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
