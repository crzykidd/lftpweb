---
name: 2026-08-11-phase6-history-page
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-12
result: |
  Built GET /api/history/jobs, GET /api/history/jobs/{id}/output, and GET /api/history/events
  (backend/lftpweb/api/history.py), plus the History page itself (frontend HistoryPage.tsx,
  HistoryJobsSection.tsx, HistoryEventsSection.tsx) -- grouped by queue, filterable, paginated
  and row-capped, with output_tail fetched on demand and the delete audit rendered legibly via
  core/postprocess.py's existing event messages. No schema change. Verified end to end against
  the real fake seedbox: a real transfer landed in history with its exact byte count, and a
  forced bad-password failure carried error_class AUTH_FAILED and a real, non-empty
  output_tail. uv run pytest: 268 passed with the seedbox up (258 passed/10 skipped without
  it). Both ruff gates and npm build/lint clean. All three compose files clean. Fake-seedbox
  containers torn down and confirmed removed. NOT verified: browser rendering -- no browser
  available in this environment. Ten decisions recorded in docs/decisions.md. Not committed
  per instructions -- see the phase report for the proposed commit message and file list.
---

# Task: Phase 6 — History page

The `job` and `event` tables have been filling up since phase 3a. Nothing renders them. Build
the page that answers "what happened last night" without a shell.

**Done when:** you can see every completed, failed, and cancelled transfer grouped by queue,
filter it, and read the remote-delete audit trail legibly.

## Before you start

- **Read `DESIGN.md` §9.2** (History is its own page, grouped by queue), §3.1 (`job` and `event`
  schemas — what's actually available), §7.3/§7.4 (what the delete audit contains), §13 phase 6.
- Read `prompts/startnewsession.md` and `docs/decisions.md` — phase 5 added the `event` audit
  writer (`core/audit.py`) and the delete/withheld-delete records this page must surface.
- Phases 1–5 are committed. `move` mode now writes `remote_delete` events.

## Working tree check

`git status --porcelain` first. Anything dirty: list it and ask. This file is exempt.

## What to do

### 1. API

- `GET /api/history/jobs` — completed/failed/cancelled jobs, **grouped by queue**, with
  pagination or a sane limit (a busy install accumulates thousands of rows; do not return them
  all). Filterable by queue, state, error class, and date range.
- `GET /api/history/events` — the `event` table, filterable by kind, level, item, and date range.
- Whatever the UI needs to render a job's captured `output_tail` on demand rather than in the
  list payload.

Reuse the existing DB access patterns; don't introduce an ORM.

### 2. The History page

Its own route (it already exists in the nav as a placeholder). Per §9.2:

- **Grouped by queue**, filterable by state, error class, date range, and event kind.
- A failed row shows its **error class and the captured lftp output tail** — phase 3a stores up
  to ~4 KB per failed job precisely so this page can show *why* rather than a red dot.
- **The delete audit must be legible**: what was deleted, from which queue, under which mode,
  and what gated it — including deletes that were **withheld**, with the failing precondition.
  A remote delete is irreversible; this page is where a user reconstructs what happened.
- Virtualize the list; a real install will have thousands of rows.

### 3. Don't regress the Transfers page

Phase 3b decided `list_jobs()` deliberately excludes `succeeded` jobs — a completed transfer's
record belongs here, not on the live queue view. Keep that split; this page is where succeeded
jobs become visible.

## Verify before reporting — actually run these

Fake seedbox: `docker/test-seedbox/gen_key.sh` then
`docker compose -f docker-compose.test.yml up -d` (`seeduser`/`testpass123`, ports 2222/2223).
**Tear it down afterwards** and confirm with `docker ps -a`.

1. `uv run pytest` passes, with tests for the new endpoints including filtering and the row cap.
2. **End-to-end**: run a real transfer against the fake seedbox, then confirm it appears in
   `/api/history/jobs` with its byte count; force a failure (bad password) and confirm the
   failed row carries its error class and a non-empty `output_tail`. Report what you observed.
3. `npm run build` and `npm run lint` clean.
4. **Both lint gates repo-wide, exactly as CI runs them** — `check` alone is not enough and has
   broken the build before:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
5. `docker compose config --quiet` clean on all three compose files.

State plainly anything you could not verify — a browser click-through is not available in this
environment, so say so rather than implying the UI was exercised.

## Surfacing decisions

The user is asleep and asked that **every decision made without them be documented**. Record each
in `docs/decisions.md` (newest at top) with rejected alternatives, and repeat them in your report.
If `DESIGN.md` is wrong or silent, make the smallest reasonable call, record it, and **do not edit
`DESIGN.md`**.

## When done

1. `docs/decisions.md` entries.
2. Update `prompts/startnewsession.md` (phase table, "Where we are").
3. Frontmatter: `status`, `completed`, `result`.
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list and a proposed one-line commit message (`feat:`
   prefix, no `Co-authored-by:`; branch `dev`).
