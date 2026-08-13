---
name: 2026-08-13-clear-history
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Added DELETE /api/history/jobs[/{id}] and DELETE /api/history/events[/{id}] (server-side
  bulk delete built from the same filters the matching GET endpoints already accept, sharing
  _jobs_where_clause/_events_where_clause so GET and DELETE can't drift) plus per-row and
  bulk "Clear" controls with a confirm panel on both HistoryJobsSection and
  HistoryEventsSection, and a scope-setting banner on HistoryPage. No protected categories
  (delete-audit events clear too, per the user's own overrule -- recorded in
  docs/decisions.md). Verified item/auto_queue_suppressed/suppressed_reason and the Dashboard
  (metric_sample/metric_heartbeat) are structurally unreachable from either DELETE path, with
  dedicated tests. An active (queued/running) job is rejected server-side (404/409), not just
  hidden from the UI. 828 tests pass (49 in the History module, 22 new), both ruff gates
  clean, npm run lint/build clean. No migration needed.
---

# Task: Let the History page be cleared — all, by outcome, or one row

User request, 2026-08-13, modelled on SABnzbd's history:

> it keeps a history and you can clear all or individual.. or based on outcome … sometimes
> people with a seedbox don't want history … they won't want to have a db that shows the last
> 2 years of all the transfers they did from their seedbox.

## The decision already taken

**Clear-all means all — jobs *and* events, no protected categories.**

This was discussed. The counter-argument was that delete-audit events (`remote_delete`,
`remote_delete_withheld`, `local_delete`, `archive_cleanup`) are the record of what happened to
the user's files, and wiping them removes the evidence you would want in exactly the situation
you would go looking for it. **The user overruled it, correctly**: for a seedbox user an
indefinite record of every transfer is a liability, not a safety feature, and protecting
categories they explicitly asked to delete is paternalistic. Record this reasoning in
`docs/decisions.md` so it is not re-litigated.

Logs and backups are deliberately **out of scope** — the operator chose to keep those. The
database is the thing that accumulates without anyone opting in.

## What this must not do

**Clearing history is bookkeeping, not behaviour.** It must not touch `item` rows,
`auto_queue_suppressed`, `suppressed_reason`, or anything that changes what happens on the next
scan. "I cleared my history and it re-downloaded everything" is the failure mode to design out.

Resetting an item's suppression is a **separate, still-unbuilt** feature that belongs on the
Files page where items live. Do not build it here and do not let a clear action imply it.

## Scope

`api/history.py` serves two lists: `GET /api/history/jobs` and `GET /api/history/events`.
Both need clearing:

- **One row** — delete a single job or event record.
- **By outcome** — jobs by state (`completed` / `failed` / `cancelled`); events by `level` or
  `kind`, whichever composes better with the filters already on that page.
- **All** — everything in both lists.

Reuse the filters `api/history.py` already supports (queue, state, error class, kind, level,
date range) rather than inventing a second filtering vocabulary — "clear what I am currently
looking at" is the natural shape and it falls out of the existing query builders.

## The foreign keys, which are already correct

- `event.job_id → job(id) ON DELETE SET NULL` (`001_initial_schema.sql:140`)
- `event.item_id → item(id) ON DELETE SET NULL` (line 139)
- `job.item_id → item(id) ON DELETE CASCADE` (line 109)

So deleting `job` rows nulls the link from any surviving `event`, which is right. **Deleting
`item` rows would cascade-delete jobs** — another reason this task must not touch `item`.

**Note the recent bug** (`3500b3f`): `db.py.migrate()` now disables `PRAGMA foreign_keys` for
the migration batch because a table *rebuild* with them on cascade-deletes children. That is a
migration-time concern; you need **no migration at all** for this task, and normal runtime
deletes must run with foreign keys **on**, as they do today. Do not disable them.

## Dashboard is unaffected — verify, then say so in the UI

`metric_sample` holds only `queue_id`, `ts`, `bytes_delta` (migration 005) — no paths, no item
references — and `core/metrics.py`/`api/metrics.py` never query `job` or `event`. Throughput is
aggregated at sample time into its own table, so clearing history cannot retroactively change
the Dashboard. **Confirm this rather than trusting the prompt**, then state it plainly in the
UI next to the clear controls, along with a note that logs and backups are not covered.

That line is not a warning, it is scope-setting: a control that implies more than it does is
worse than no control.

## UI

`frontend/src/pages/HistoryPage.tsx` and its `HistoryJobsSection` / `HistoryEventsSection`.

- Per-row clear, a clear-what-is-filtered action, and a clear-all.
- **Confirm before clearing.** Unlike the Dismiss action added in `b1eb8a4` — which hides a row
  but destroys nothing — this is irreversible. Say how many records will go.
- Both sections are virtualized (`@tanstack/react-virtual`); do not regress that, and do not
  fetch the full row set client-side just to count it — the endpoints already return `total`.
- Bulk operations report partial failure honestly (`Promise.allSettled`, matching phase 9's
  pattern), or do the whole delete server-side in one request, which is simpler and preferable
  here. Choose and say which.

## Before you start

- `api/history.py` — the two list endpoints, their filters, and `MAX_LIMIT`.
- `frontend/src/pages/HistoryPage.tsx`, `components/HistoryJobsSection.tsx`,
  `components/HistoryEventsSection.tsx`.
- `core/audit.py` — what writes events and why ("an audit record that isn't durable the instant
  it's produced isn't an audit record"). That docstring's reasoning is about *writing*, not
  about retention; do not read it as a prohibition on user-initiated clearing.
- `b1eb8a4` — the Dismiss feature, so the two do not overlap confusingly. Dismiss removes a job
  from **Transfers** and keeps it in History; Clear removes it from History.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- Clear one, clear by outcome, clear all — for both jobs and events.
- **`item` rows, `auto_queue_suppressed`, and `suppressed_reason` are untouched** by every
  clear path. This is the important one.
- A surviving `event` whose `job_id` pointed at a cleared job has `job_id IS NULL` and still
  renders.
- Clearing does not change `metric_sample`, and the Dashboard endpoints return the same data
  before and after.
- Filters compose: clearing "failed jobs in queue X" leaves everything else alone.
- An item mid-transfer with a live job is unaffected — an active job is not history and must not
  be clearable. Reject it server-side, not just in the UI.

## Conventions to honor

- `docs/decisions.md`, newest at top — including why no category is protected.
- `CHANGELOG.md` under `### Added`; `DESIGN.md` §9.2/§10 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up (806 pass today).
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, whether you did
   bulk deletes server-side or client-side and why, the exact UI text about what clearing does
   and does not cover, test count, lint results, and anything not fixed. Never `git add -A`,
   never push.
