---
name: 2026-08-17-transfers-dismiss-per-queue
status: completed
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Backend: POST /api/jobs/dismiss-all gains an optional {queue_id?} body (DismissAllRequest,
  models.py); TransferQueue.dismiss_all_terminal gains queue_id: int | None = None, scoping its
  UPDATE via a subquery over item (job has no queue_id column of its own). Omitted body/None
  queue_id is byte-for-byte the original every-queue behavior; an unknown queue_id matches zero
  rows, no 404. Frontend: GroupHeader gains a "Dismiss Queue" button, shown only when
  groupHasDismissable(group.jobs) is true (new lib/transferPanel.ts helper; isDismissable moved
  there from TransfersPage.tsx, which re-exports it so the existing test import keeps working).
  Per-queue busy/error/outcome state is keyed by queueId (Set/Record) so two groups' controls
  never lock each other. The header's outer element changed from <button> to <div role="button">
  (with hand-rolled onKeyDown) so the new Dismiss Queue <button> can nest inside it without
  invalid button-in-button HTML; its onClick calls stopPropagation so it never toggles the
  group's collapse. All six gates pass: ruff check, ruff format --check, uv run pytest (1284
  passed), npm run lint (0 exit, pre-existing-style fast-refresh warnings only), npm test (473
  passed), npm run build. No browser verification -- the control ships unviewed.
---

# Task: Transfers queue-group headers gain a per-queue Dismiss

User request (2026-08-17): the Transfers page groups jobs by queue (2026-08-16, row I —
collapsible `GroupHeader` per queue), and the page has a global "Dismiss all"
(2026-08-15, row F — `POST /api/jobs/dismiss-all`), but there's no way to dismiss just
one queue's terminal rows. Add that, scoped to the group header.

## Before you start

- Read `CLAUDE.md`. Read before editing:
  - `backend/lftpweb/api/jobs.py` — `dismiss_all_jobs` (~line 195) and its
    `DismissAllResponse`; `backend/lftpweb/core/queue.py.dismiss_all_terminal` (find
    it; note what it deliberately touches and doesn't — `dismissed_at` only, never
    `item.state`).
  - `frontend/src/pages/TransfersPage.tsx` — `GroupHeader` (~line 422), the existing
    Dismiss-all state/handler cluster (~lines 469–570) and how outcomes/errors are
    reported; `frontend/src/lib/transferPanel.ts` — `groupJobsByQueue`,
    `queueGroupSummary`/`formatQueueGroupCounts` (the header already knows its
    per-outcome counts).
  - `prompts/done/2026-08-15-transfers-single-line-rows-with-detail.md` (Dismiss all)
    and `prompts/done/2026-08-16-transfers-group-by-queue.md` (grouping) for the
    conventions both features settled.

## Working tree check

Run `git status --porcelain` before editing; cross-reference against the files this
plan touches, ask before touching any that are dirty, surface unrelated dirty files
once. This prompt file itself is exempt.

## What to do

1. **Backend — scope the existing endpoint, don't add a second one.**
   `POST /api/jobs/dismiss-all` gains an optional JSON body `{queue_id?: number}`
   (omitted/`null` = today's behavior, byte-for-byte, so every existing caller is
   unaffected — same pattern `DeleteItemRequest` set on 2026-08-16).
   `TransferQueue.dismiss_all_terminal` gains `queue_id: int | None = None`, adding
   the `item.queue_id` restriction to its one UPDATE (however it currently scopes —
   read it; jobs join items for the queue). A `queue_id` naming no existing queue
   simply dismisses nothing (`dismissed: 0`) — same as dismissing an empty queue;
   don't 404 it. Response shape unchanged.
2. **Frontend — a small "Dismiss" control on each `GroupHeader`**, rendered only when
   that group actually has dismissable rows (terminal, not yet dismissed — reuse the
   page's existing `isDismissable` helper against the group's own jobs; don't invent
   a parallel predicate). Clicking calls `dismissAllJobs(queueId)` (extend
   `api/client.ts`'s function with the optional arg). Busy/error/outcome handling
   mirrors the existing Dismiss-all cluster's shape — per-group busy state so two
   groups' controls don't lock each other, errors reported the same honest way the
   global one reports (`dismissAllError` pattern), and the local jobs state updated
   the same way the global handler updates it after success. Make sure the click
   doesn't toggle the group's collapse (stopPropagation — the header line is a
   collapse toggle).
3. **Naming in the UI — settled by the user:** the control lives in the group-by
   header bar and is labeled **"Dismiss Queue"**, with a title/tooltip along the lines
   of "Dismiss this queue's finished rows". Not your call; don't rename it.
4. **Tests:** backend — extend the existing dismiss-all API/queue tests: scoped
   dismiss only touches the named queue's terminal jobs (another queue's terminal job
   stays undismissed), omitted body still dismisses across queues, unknown queue_id
   → 0. Frontend — if you factor the "does this group have dismissable rows" decision
   into `lib/transferPanel.ts` (do — it's the same pure-helper convention the file is
   built on), cover it in Vitest.
5. **Docs, same commit:** `CHANGELOG.md` `### Added` entry under Unreleased (append
   after existing entries). `docs/decisions.md` only if a genuinely non-obvious call
   comes up (the scope-the-existing-endpoint choice is settled by this prompt).

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from the repo root takes ~3 minutes — timeout 400000ms; never
  background a gate and wait on a notification), exit codes read:
  `uv run --project backend ruff check`, `uv run --project backend ruff format
  --check`, `uv run pytest`; frontend `npm run lint`, `npm test`, `npm run build`.
- Comment style: dated, rationale-naming — match the neighboring docstrings.
- No browser here — the control ships unviewed; say so.
- Conventional-Commit prefix `feat:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result` — status value is
   `completed`, not `done`).
2. Move this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit covering this prompt file, the files modified, and the prompt
   move. Present the file list and a one-line message.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree and
     report the file list + proposed message back to the orchestrating session.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
