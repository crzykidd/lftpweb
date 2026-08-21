---
name: 2026-08-20-transfers-row-file-progress
status: completed        # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: >
  Added GET /api/items/{id}/children (itemview-projected, capped 500/default 200) and a Files
  group in the Transfers row's existing expand panel; live updates overlay the already-open
  useLiveModel WebSocket rather than polling. Backend 1462->1467 tests, frontend 524->545, 0
  skipped, all gates green. Browser-unverified.
---

# Task: expand a Transfers row to show its per-file progress

**Phase 1, stage 5 of `docs/transfers-redesign-spec.md` — read §3.3 first.** Stages 1–4b are
landed and browser-confirmed (through commit `3bdef7a`).

This is the stage that removes the last real reason to leave the Queue tab. The user's own words:
watching per-file progress is what they currently open the Files page for. Move it to where the
ordering lives.

## The key fact: this is re-presentation, not new plumbing

`core/queue.py._publish_child_progress` **already** computes each child file's size and state from
the same filesystem walk the running job performs, persists it, and publishes it live. The Files
tree is one renderer of that data. You are adding a second renderer, not a second source.

Read `_publish_child_progress` and its surrounding comments before designing anything. Note also
that a `pget` job (single file) has **no children** — `JobProgress.children` is `None` there, and
a single-file row must not render an empty or misleading expansion.

## The constraint that matters most

**Children must be fetched lazily, when a row is expanded — never inlined into the jobs list.**

This is a documented trap in this codebase, not a preference. `api/history.py` deliberately
carries only `has_output_tail` and fetches the blob from a separate endpoint, precisely because
inlining a per-row payload onto a list endpoint reintroduces the cost the row cap exists to
prevent. A season pack has dozens of children; the Active box shows 20 rows and the Complete box
50. Inlining would multiply that on every poll.

The Complete box's own `RowDetailPanel` already establishes the on-demand-fetch-on-expand pattern
(`GET /api/history/jobs/{id}/output`). Follow it.

## Before you start

- `docs/transfers-redesign-spec.md` §3.3.
- `backend/lftpweb/core/queue.py` — `_publish_child_progress`, and how child rows are persisted.
  Children are `item` rows; understand their relationship to the parent before designing the
  endpoint.
- `backend/lftpweb/core/itemview.py` — the single projection shared by the socket, the snapshot,
  and `GET /api/files`. **"Nothing may publish a state it did not read back from the `item`
  table"** is an invariant of this codebase; if you are serving child state, serve it through the
  existing projection rather than inventing a parallel one.
- `frontend/src/components/FileTree.tsx` — how the Files page renders per-file progress today.
  Reuse its presentation logic where it is already pure; do not fork a second copy of the
  progress/percent/speed formatting.
- `frontend/src/pages/TransfersPage.tsx` — `Row`, `RowDetailPanel`, and the existing expand
  affordance.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1462 backend / 524 frontend tests passing, 0 skipped**.

## What to do

### 1. Backend: an on-demand children endpoint

Serve a job's (or item's) child files with the fields the UI needs: name, size, local bytes so
far, and state. Paginate or cap it if a pathological release could return thousands of children —
**decide, and say what you chose**; a silent unbounded list is the same mistake this task exists
to avoid.

Serve child state through `core/itemview.py`'s existing projection. Do not build a parallel
projection.

### 2. Live updates while expanded

A running job's children change constantly. Decide how an expanded row stays current — the
existing WebSocket `item_delta` stream already carries child progress, so preferring it over
polling is likely right, but **check whether the Transfers page is already subscribed to the
relevant stream** before assuming. If a poll is simpler and the page already polls, say so and
justify it.

Whatever you choose: **an expanded row must not multiply request volume.** Ten expanded rows must
not mean ten independent polls.

### 3. Frontend

Expansion on a row shows its files with per-file progress. Reuse the Files page's existing pure
formatting helpers rather than duplicating them.

- A **single-file (`pget`) job** has no children — do not render an empty expansion. Either omit
  the affordance or show the single file's own progress, whichever reads better; say which and
  why.
- Collapse state is per-row and need not persist across reloads.
- The Complete box's rows already expand for failed-job output. **Both expansions must coexist
  coherently on the same row** — do not create two competing expand affordances. If a completed
  row should show files *and* output, decide how, and say so.

### 4. What must not change

- The two boxes, their pagination, the filters, "Dismiss list", chevrons, badges, Start now, Stop.
- The jobs list payload must not grow — that is the whole point of §the constraint above.

### 5. Tests

Backend: the endpoint including the empty/`pget` case and whatever bound you chose. Frontend: pure
logic in `lib/` with unit tests, per this codebase's convention.

### 6. Docs

`DESIGN.md` §9.2, `CHANGELOG.md` under `[Unreleased]`, `docs/decisions.md` for the live-update
choice and the bound you picked, and tick stage 5 in `docs/transfers-redesign-spec.md` §7.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — running from there collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**. Do not
  background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say so plainly and name what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
