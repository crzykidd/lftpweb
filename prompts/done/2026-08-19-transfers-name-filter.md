---
name: 2026-08-19-transfers-name-filter
status: completed          # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: Added the Transfers page name filter + scoped "Dismiss list" bulk-dismiss (job_ids on dismiss-all); all gates green, frontend UI browser-unverified.
---

# Task: a name filter on the Transfers page, with a "Dismiss list" button scoped to what it matches

The Transfers page shows every queued/running/terminal job across every queue, grouped by queue.
On a busy install that is a long list, and there is no way to narrow it. Add a **text filter**:
start typing and only rows whose item name contains that string (case-insensitive) stay visible —
`Married` surfaces everything with "married" in it, `at.first.sight` matches literally. Alongside
the input, add a **"Dismiss list"** button that dismisses exactly the terminal rows currently
matching the filter — greyed out while the filter is empty, actionable the moment it isn't.

Design settled with the user 2026-08-19. Two decisions they made explicitly, do not revisit them:

- **The filter text does NOT persist.** No `localStorage`, no URL param. It clears on reload and
  on navigating away, matching the Files page's text filter and the Logs filter. A stale filter
  hiding active transfers after a reload is its own confusion.
- **"Dismiss list" is a new, separate control in the filter box** — not a re-scoping of the
  existing "Clear all" / "Dismiss all" / per-queue "Dismiss Queue" buttons, which keep their
  current whole-queue meaning and are **left completely unchanged by this task**.

## Before you start

Read, in this order:

1. `DESIGN.md` §9.2 (the Transfers page) — the architectural source of truth.
2. `CLAUDE.md` — per-session operating rules.
3. `frontend/src/lib/transferPanel.ts` — where this feature's pure logic belongs. Note the
   existing shape of `sortTransferRows`, `groupJobsByQueue`, `groupHasDismissable`, and
   `isDismissable`; you are adding a sibling to those, not a new module.
4. `frontend/src/components/FileTree.tsx` around line 1290 (`visiblePaths`) — the Files page's
   text filter, and specifically **why a filter ignores collapse state**. Same principle applies
   here (see step 4 below).
5. `backend/lftpweb/api/jobs.py`'s `dismiss_all_jobs` and `backend/lftpweb/models.py`'s
   `DismissAllRequest` — the contract you are extending, and its own "omitted means exactly the
   old behavior" convention.

**The one constraint that shapes the backend change:** `dismiss_all_jobs`'s docstring says
explicitly that this is "a single server-side `UPDATE` ... **not** a client-side loop over every
dismissable row's own `/dismiss` call." Honor that. "Dismiss list" must be **one** request, not N.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this plan
needs to modify. If any of those files have uncommitted changes, list them and ask the user before
touching them. Surface unrelated dirty files once as awareness; don't block. This file (the
handoff prompt itself) is exempt — it's expected to be modified by "When done" below.

At the time this prompt was written the tree was clean on `dev`, three unpushed commits ahead of
`origin/dev`.

## What to do

### 1. Pure filter logic in `frontend/src/lib/transferPanel.ts`

Add (and export) two pure functions, next to `groupJobsByQueue`:

```ts
export function filterTransferJobs(jobs: JobOut[], search: string): JobOut[]
export function dismissableJobIds(jobs: JobOut[]): number[]
```

- `filterTransferJobs` trims and lowercases `search`; an empty/whitespace-only search returns the
  input array **unchanged and by identity** (`jobs`, not a copy) so downstream `useMemo`s don't
  churn. Otherwise keep every job whose `rel_path` lowercased contains the needle. Preserve input
  order — the caller has already sorted.
- Match on **`rel_path` only**. `JobOut` has no separate `name` field; `rel_path` is the item's
  path within the queue and contains the name, so a substring over it covers both a bare name and
  a nested one. Do **not** also match `queue_name` — a queue named "movies" would otherwise make
  every row in it match the word "movies", which is not what the user asked for.
- `dismissableJobIds` returns the ids of jobs whose state passes the existing `isDismissable`
  (reuse it — do not re-derive the terminal-state set).

Unit-test both in `frontend/src/lib/transferPanel.test.ts` following the existing tests' shape.
Cover at minimum: case-insensitivity, a dotted literal like `at.first.sight`, empty search
returning the same array identity, no-match returning empty, and `dismissableJobIds` skipping
active rows.

### 2. Backend: let `dismiss-all` take an explicit id list

In `backend/lftpweb/models.py`, add to `DismissAllRequest`:

```python
job_ids: list[int] | None = None
```

Document it in the existing docstring's own voice, including that omitting it (or `null`) is
exactly the pre-existing behavior — the same "optional field, omitted means unchanged" shape
`queue_id` itself already set. **`job_ids` and `queue_id` are mutually exclusive**: if both are
given, reject with a 422 rather than guessing an intersection. Enforce that with a Pydantic
model validator, not in the endpoint body.

In `backend/lftpweb/core/queue.py`, extend `dismiss_all_terminal` to accept
`job_ids: Sequence[int] | None = None` and add `AND id IN (...)` to its existing `UPDATE` when
given. Keep it one statement. An **empty** list must dismiss **nothing** and return `0` — it must
never degrade into "no filter, so dismiss everything." Write that intent as a comment; it is the
dangerous edge of this change.

Then wire it through `api/jobs.py`'s `dismiss_all_jobs`. The response model is unchanged.

Backend tests in `backend/tests/` (find the file that already covers `dismiss-all` and extend it):
an explicit id list dismisses only those rows; an empty list dismisses zero; an id list containing
a non-terminal job's id dismisses nothing for that row (the endpoint's own terminal-state
predicate still applies — the client's list is a *narrowing*, never an override); `job_ids` plus
`queue_id` together is a 422; and every existing no-body call still behaves identically.

### 3. Frontend API client

Extend the existing dismiss-all call in `frontend/src/api/client.ts` to take an optional
`job_ids`, matching how `queue_id` is already threaded. Don't add a second function.

### 4. The Transfers page UI (`frontend/src/pages/TransfersPage.tsx`)

- A filter input in the page toolbar, above the groups. Placeholder something like
  `Filter by name…`. Plain `useState`, no persistence.
- Apply `filterTransferJobs` to `sortedJobs` **before** `groupJobsByQueue`, so a queue with no
  matching rows produces no group at all rather than an empty one.
- **While the filter is active, render every group expanded and ignore `collapsedQueues`
  entirely** — a match inside a collapsed queue must still surface. Do not write to the collapse
  preference while filtering; it applies again unchanged the moment the filter clears. This is
  exactly `FileTree.tsx`'s rule and the reasoning is identical; mirror it, don't reinvent it.
- A **"showing N of M"** readout while the filter is non-empty, matching the Logs filter's
  wording (`frontend/src/lib/logFilter.ts.logFilterSummary` — reuse it if its shape fits, and say
  so in the commit if you deliberately don't).
- An empty state when the filter matches nothing: a short line plus a control that clears the
  filter. Do not show the page's normal "no transfers" empty state — they mean different things.
- **The "Dismiss list" button**, at the end of the filter box:
  - Disabled while the filter is empty. Also disabled when the filter is non-empty but matches no
    *dismissable* rows (all matches are still running/queued) — a live tooltip should say which of
    those two it is.
  - Enabled otherwise, labelled so the count is visible (e.g. `Dismiss list (3)`).
  - On click, POST the ids from `dismissableJobIds(filteredJobs)` in **one** request.
  - Reuse the page's existing busy/error/outcome state conventions for the other dismiss controls
    — do not invent a fourth notification pattern. Refresh the job list afterwards the same way
    the existing dismiss handlers do.
  - **No confirmation dialog.** Dismiss is reversible in the sense that matters (it only sets
    `dismissed_at` on a terminal job row — it never touches `item.state`, never deletes bytes, and
    never touches the remote). Do not add one; if you find yourself wanting to, that means you
    have wired it to something more destructive than intended, which is a bug.
- Leave "Clear all", "Dismiss all", and the per-queue "Dismiss Queue" **untouched**, including
  while a filter is active. The user chose a dedicated filtered-scope button precisely so those
  keep their existing, unambiguous whole-queue meaning.

Extend `frontend/src/pages/TransfersPage.test.ts` for whatever page-level behavior is testable
there in the existing style.

### 5. Docs

- `DESIGN.md` §9.2: add the filter and the "Dismiss list" control to the Transfers page's
  description. This is a genuine extension of what §9.2 specifies, so the doc gets corrected
  rather than silently diverged from.
- `README.md`: one clause in the "What works today" bullet that covers the Transfers/History
  pages, if one fits naturally. Don't force it — this is a small feature and the README is
  already long.
- `CHANGELOG.md`: an entry under the `[Unreleased]` section, matching the surrounding style.
- `docs/decisions.md` (newest at top) if — and only if — you hit a decision the user hasn't
  already made above. The four they already settled (no persistence, separate button, existing
  bulk controls untouched, one request not N) are recorded here; don't re-log them as if they
  were yours.

## Conventions to honor

- **Pure logic lives in `lib/`, unit-tested; components stay thin.** This repo does that
  consistently (`startNow.ts`, `logFilter.ts`, `bytesChart.ts`, `fileTree.ts`) — follow it.
- **Run the gates in the FOREGROUND with a generous timeout, and read the exit code.** Two prior
  spawned agents on this project backgrounded the ~3-minute pytest run and then waited forever on
  a notification a subagent never receives. Do not background it. Run each gate separately:
  `uv run pytest` (from `backend/`), `uv run ruff check`, `uv run ruff format --check`, and in
  `frontend/`: `npm run lint`, `npm run typecheck`, `npm test`. `ruff check` passing is **not**
  `ruff format --check` passing; run both.
- Report the backend/frontend test counts before and after, and confirm 0 skipped.
- Conventional-Commit prefix (`feat:` here). **No `Co-authored-by:` trailer.**
- Doc updates ship in the same commit as the code they describe.
- **You cannot render a page.** Every frontend change in this project ships browser-unverified
  unless the user checks it. Say so plainly in your report rather than implying you saw it work.

## When done

1. Update this file's frontmatter: set `status` (completed/failed), `completed` (the date), and
   `result` (one line).
2. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on failure).
3. Record any non-obvious decisions in `docs/decisions.md`.
4. **You are a spawned agent: do NOT commit.** Prepare the working tree, then report back to the
   orchestrating session with the file list and a proposed one-line commit message. The
   orchestrating session surfaces the `y/n` to the user. Never `git add -A`, never push.
