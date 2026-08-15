---
name: 2026-08-13-dismiss-terminal-jobs
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Added migration 016 (job.dismissed_at, plain ADD COLUMN), core/queue.py.dismiss_job (rejects
  queued/running via JobNotDismissableError, never touches item state/suppression),
  POST /api/jobs/{id}/dismiss, HistoryJobOut.dismissed_at, and Transfers-page Dismiss +
  "Clear all failed" bulk action (Promise.allSettled). 806 tests pass (798 + 8 new), both lint
  gates clean, frontend lint/build clean. Not committed -- spawned agent, tree left for the
  orchestrator to review and commit.
---

# Task: Let a finished-and-failed transfer be dismissed from the Transfers page

User report, 2026-08-13, from live testing:

> during testing I deleted a set of files on the host mid transfer. So now it is in the transfer
> queue as failed "REMOTE GONE" but the only option I have is to retry. I should have a clear or
> delete button.

## Why it sticks

`core/queue.py.list_jobs()` deliberately includes **an item's most recent `failed`/`cancelled`
job** alongside the active ones. That is not an oversight — phase 3b added it because
`DESIGN.md` §9.2 requires the Transfers page to show a failure's error class and output tail,
and the phase 3a query structurally could not: a job vanished from it the moment it stopped
being active. See `prompts/startnewsession.md`'s traps list.

The consequence is that a terminal failure stays visible until a *newer* job for that item
supersedes it — and only Retry creates one. So Retry is the only button, and for `REMOTE_GONE`
it is precisely the wrong action: the remote files are actually gone, the class is in
`PERMANENT_ERROR_CLASSES`, and the item is already suppressed with `permanent_error`.

## Dismiss, not delete

**Do not delete the `job` row.** `api/history.py` reads the same table — `GET
/api/history/jobs` is where a completed or failed job's record lives — so deleting would erase
the evidence of what happened, which is the opposite of what the History page exists for.

Add a **dismissal marker** instead: the job stays in History, and only stops appearing on
Transfers.

- Migration **016** (verify nothing has claimed it): a nullable timestamp column on `job`,
  e.g. `dismissed_at`.
- `list_jobs()` excludes a terminal job whose marker is set. Active jobs (`queued`/`running`)
  must be unaffected — dismissing one should be impossible, not merely unusual, so reject it
  server-side rather than relying on the UI not offering it.
- A new endpoint alongside the existing `POST /api/jobs/{id}/stop` and `/retry`.
- History keeps showing it. Consider whether History should *indicate* dismissal; it is
  arguably useful and arguably noise — decide and say why.

## Do not change the item's state or its suppression

The item is suppressed with `suppressed_reason = 'permanent_error'` (§4.6), which is correct
and deliberate — `REMOTE_GONE` means auto-queue must not pick it up again. **Dismissing the job
is a display action about the job row, not a decision about the item.** Retry already clears
suppression (`enqueue_item`: "always wins, clears suppression, resets `attempt`"), which is the
path for "actually, try again".

Be explicit about this in the code comment, because the obvious next change someone will make
is to have dismiss also un-suppress, and that would silently re-enable auto-queue for an item
whose remote is gone.

## UI

`frontend/src/pages/TransfersPage.tsx` — the row already has Retry, Stop, Move to top, Start
now, and the drawer, gated by state.

- A **Dismiss** action on terminal rows only (`failed`/`cancelled`).
- Consider a **"Clear all failed"** bulk action. The user's phrasing ("a clear or delete
  button") suggests they may accumulate several; one-at-a-time dismissal of ten dead rows is
  its own annoyance. If you add it, it must be honest about partial failure the way phase 9's
  bulk actions are (`Promise.allSettled`, not `Promise.all`).
- Do not add a confirmation dialog. This is reversible in the sense that nothing is destroyed —
  the record stays in History — and a confirm on a non-destructive action trains people to
  click through confirms that matter.

## Before you start

- `core/queue.py.list_jobs()` and its docstring on why terminal jobs are included.
- `api/jobs.py` — the existing `/stop`, `/retry`, `/move-to-top`, `/start-now` shapes.
- `api/history.py` — what it reads and its `MAX_LIMIT` reasoning.
- `frontend/src/pages/TransfersPage.tsx`.
- `core/queue.py`'s `PERMANENT_ERROR_CLASSES` and §4.6 suppression.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- A dismissed terminal job disappears from `list_jobs()` and **still appears** in
  `GET /api/history/jobs`.
- Dismissing a `queued` or `running` job is rejected.
- The item's `state` and `auto_queue_suppressed`/`suppressed_reason` are untouched by a
  dismissal.
- After dismissal, a manual Retry still works and produces a fresh job that appears normally.
- Bulk clear, if built, reports partial failure honestly.

## Conventions to honor

- `docs/decisions.md`, newest at top — why dismissal rather than deletion, and why it does not
  touch suppression.
- `CHANGELOG.md` under `### Added`; `DESIGN.md` §9.2 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up (798 pass today).
- **A note on migrations:** `db.py.migrate()` now disables `PRAGMA foreign_keys` for the whole
  batch, because a table rebuild with them on cascade-deletes children (found in `3500b3f`).
  A plain `ADD COLUMN` needs no rebuild — prefer that and avoid the whole issue.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, whether you
   added the bulk clear, your call on showing dismissal in History, test count, lint results,
   and anything not fixed. Never `git add -A`, never push.
