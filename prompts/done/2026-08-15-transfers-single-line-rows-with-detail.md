---
name: 2026-08-15-transfers-single-line-rows-with-detail
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Transfers rows collapse to one line (name/queue/state/one live number) with a chevron-expand
  panel (Transfer/Processing/*arr groups). Backend: JobOut extended with
  verified_at/extracted_at/remote_deleted_at/arr_status/arr_status_at/arr_instance_name (bounded
  join in TransferQueue.list_jobs()); new GET /api/items/{id}/events (bounded, item-scoped);
  new POST /api/jobs/dismiss-all + core/queue.py.dismiss_all_terminal(). Frontend: new
  lib/transferPanel.ts (pure row-collapse + panel-group-assembly functions, tested), rewritten
  TransfersPage.tsx Row/RowDetailPanel, "Dismiss all" control at the top of the page. All gates
  green: ruff check/format, full pytest (1151 passed, 0 skipped), frontend lint/test (302
  passed)/build. Two deviations from the most literal prompt reading, both recorded in
  docs/decisions.md: queue position + action buttons stay on the collapsed line rather than
  moving into the panel; the Transfer group's "per-file mirror progress" is the existing file
  count rather than a duplicate of ItemDrawer's own virtualized per-file table. UI unviewed --
  no browser in this environment.
---

# Task: Transfers page — one line per download, expandable detail with processing + *arr info

User request (2026-08-15, from live use): each Transfers row must be a **single compact
line**, with an info/expand control revealing a detail panel that tells the download's
whole story — the transfer numbers, **more post-processing detail than today's single
state word**, and the ***arr integration status**.

## Before you start

- Read `frontend/src/pages/TransfersPage.tsx` and its hooks (`useJobs.ts`) to see what a
  row currently renders — the elapsed / average speed / queued wait / post-processing
  state / per-file mirror progress additions (`6e6b217`, `25bc33c`) are what's crowding
  the row; none of that information should be *lost*, it moves into the panel.
- Read `backend/lftpweb/api/jobs.py` (`JobOut` — note it inlines `output_tail` because
  this endpoint's row set is bounded by construction) and `api/history.py` for the
  expand-to-fetch pattern History already uses.
- Read `backend/lftpweb/core/audit.py` + the `event` table schema (migration 001 and
  later) to see how events reference items; `docs/arr-integration-spec.md`'s lifecycle
  section for what `arr_status` values mean.
- `docs/decisions.md` recent entries for conventions.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## What to do

### Backend (bounded additions only)

1. Extend `JobOut` with the item-level facts the panel needs and the join can supply
   cheaply: `verified_at`, `extracted_at`, `remote_deleted_at`, `arr_status`,
   `arr_status_at`, and the bound instance's display name (null when the queue has no
   instance). The jobs list is bounded by construction, so inlining these is fine — but
   do NOT inline anything unbounded (the phase-6 trap).
2. The panel's "processing story" needs the *why*, not just timestamps — and
   `core/postprocess.py` + `core/arrsync.py` already write every branch's reasoning as
   `event` rows. Add a bounded on-demand endpoint (e.g.
   `GET /api/items/{id}/events?limit=…`, server-capped, newest first) that returns the
   event rows for one item, following `api/history.py`'s existing pagination/cap
   conventions — check how events reference items (item_id column vs. message text) and
   if the schema only carries the name in the message, say so in your report and filter
   accordingly rather than adding a migration.

### Frontend

3. Collapse each Transfers row to one line: name, queue, state word, and the one most
   relevant live number (progress+current speed while downloading; final size + outcome
   when terminal). Everything else moves out of the row.
4. An expand control per row (chevron or ⓘ, matching existing idioms — History's
   expandable failed rows are the precedent). The panel, three groups:
   - **Transfer** — bytes done/total, elapsed, average + current/allocated speed, queued
     wait, per-file mirror progress, and for failed jobs the error class + output tail
     (already-fetched or on-demand as today).
   - **Processing** — verify / extract / rename / relocation / remote-delete outcomes:
     the timestamps from `JobOut`, enriched by the item-events fetch (on expand, on
     demand) rendering the pipeline's own event messages verbatim — same philosophy as
     History's §7.3 legibility ("the carefully-worded event messages ARE the UI").
   - ***arr** — instance name, `arr_status` with the same icon/vocabulary the Files page
     uses (reuse `lib/fileTree.ts`'s variant helpers, don't fork them), `arr_status_at`.
     Hidden entirely when the queue has no bound instance.
5. Keep dismiss, "Start now", and every existing action working. Keep the §9.2
   three-word visible vocabulary for the state word.
5b. **"Dismiss all" at the top of the page** (user addition, 2026-08-15): one control
   that dismisses every currently-dismissable (terminal, not-yet-dismissed) job. Prefer a
   small bulk endpoint (`POST /api/jobs/dismiss-all`, returning the count dismissed) over
   a client-side loop; either way report partial failure honestly (the phase-9
   `Promise.allSettled` precedent) and hide/disable the control when nothing is
   dismissable. Backend test for the bulk path: dismisses only terminal jobs, never an
   active one.
6. Tests: Vitest for the row-collapse logic (what renders in the line vs. the panel),
   the panel's group assembly from a `JobOut` fixture, and the arr-group hidden/shown
   logic. Backend tests for the new endpoint (cap enforced, item scoping correct) and
   the extended `JobOut` fields.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report and the
  startnewsession.md row; the layout ships unviewed.
- No new dependencies; Tailwind + existing component idioms.
- Docs same commit: `docs/decisions.md` entry; `CHANGELOG.md` under Unreleased;
  startnewsession.md arr build-run table row (this is session follow-on work).

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — note skip counts honestly.
4. `cd frontend && npm run lint`
5. `cd frontend && npm test`
6. `cd frontend && npm run build`

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
