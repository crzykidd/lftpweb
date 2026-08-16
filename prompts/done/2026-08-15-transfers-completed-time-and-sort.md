---
name: 2026-08-15-transfers-completed-time-and-sort
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Frontend-only. `lib/transferPanel.ts` gained `completedTimeLabel(job)` (relative value +
  exact-timestamp title, null for active jobs or a terminal job with no finished_at yet) and
  `sortTransferRows(jobs)` (active rows keep the scheduler's own order, running then queued;
  terminal rows sort newest-completed-first, missing finished_at last, stable ties). Wired into
  `pages/TransfersPage.tsx`: the collapsed line shows the completed-time span next to the state
  chip for terminal rows only; the row list renders `sortTransferRows(jobs)` instead of raw
  `jobs` (queue-position numbering still reads the original `jobs` array, unaffected). Also added
  a "Completed" field to `transferGroupFields`'s panel output for terminal jobs, matching the
  existing Verified/Extracted/Remote-deleted relative+hover-title pattern. Replaced ordering:
  before this, terminal rows sorted by the same `rank DESC, queued_at ASC` scheduler order active
  rows use (backend `list_jobs`), which said nothing about actual completion time. 15 new/changed
  Vitest cases in `lib/transferPanel.test.ts` (28 total in that file, all passing); full frontend
  suite 313/313 green. Backend untouched, re-verified anyway (ruff check/format clean, pytest
  1154 passed, 0 skipped). No browser in this environment — unviewed.
---

# Task: Transfers page — show completed time, sort by it

User request (2026-08-16, from live use of the new single-line Transfers rows): each
terminal row should show **when it completed**, and the list should **sort by that**.

## Before you start

- Read `frontend/src/pages/TransfersPage.tsx` and `frontend/src/lib/transferPanel.ts` as
  just reshaped by `4139e22` (one line per row + expand panel). Match that structure.
- Check what `JobOut` already carries (`backend/lftpweb/api/jobs.py`, `models.py`) — a
  finished/completed timestamp likely already exists (`api/history.py` exposes one); only
  touch the backend if the field genuinely isn't on the Transfers payload yet.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt. (Other `prompts/*.md` siblings may exist — leave them alone.)

## What to do

1. **Completed time on the collapsed line** for terminal jobs (succeeded / failed /
   cancelled): compact relative form consistent with existing time rendering on the page
   (exact timestamp on hover/in the panel). Active jobs show what they show today.
2. **Sort order**: active jobs (running, then queued in scheduler order) stay at the top
   exactly as today; terminal jobs below them, **newest completed first**. If the page
   currently has a different explicit ordering for terminal rows, this replaces it —
   note the old order in your report.
3. Keep the sort logic in a pure exported function (in `lib/transferPanel.ts` or a
   sibling), unit-tested: active-before-terminal, newest-first among terminal, stable
   for ties/missing timestamps (missing sorts last).
4. Vitest coverage for the line rendering (terminal row shows the completed time; active
   row doesn't) and the sort function.
5. Docs same commit: `CHANGELOG.md` Unreleased entry; `docs/decisions.md` only if a
   non-obvious call comes up; startnewsession.md arr build-run table row.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report; ships unviewed.
- No new dependencies. `feat:` prefix.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. `uv run ruff check backend` and `uv run ruff format --check backend`
5. `uv run pytest` — note skip counts honestly (must run even if backend untouched).

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
