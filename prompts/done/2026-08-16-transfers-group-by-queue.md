---
name: 2026-08-16-transfers-group-by-queue
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: >
  Transfers rows now group under one collapsible header per queue (ordered by queue name),
  header carries queue name + outcome counts (active/queued/succeeded/failed, plus a `stopped`
  bucket for cancelled added beyond the prompt's literal four -- docs/decisions.md) + total
  bytes_done + combined current rate while active. Collapse state persists per queue in
  localStorage via the existing lib/storage.ts wrapper, default expanded, never pruned. Pure
  logic + tests in lib/transferPanel.ts/.test.ts; TransfersPage.tsx updated to render grouped,
  per-row queue tag removed. All 5 gates green: lint, 329 frontend tests, build, ruff
  check+format, 1154 backend tests (0 skipped). Unviewed -- no browser in this environment.
---

# Task: Transfers page — group rows by queue, collapsible with remembered state

User request (2026-08-16, from live use): per-row queue labels make the page busy. Group
the Transfers rows **by queue**; each group collapses/expands with a single click and the
choice is **remembered**; the group header line carries the queue-level summary so the
individual rows can stop repeating the queue name.

## Before you start

- Read `frontend/src/pages/TransfersPage.tsx` + `frontend/src/lib/transferPanel.ts` as
  shaped by `4139e22` (single-line rows + expand panel) and `527b7ec` (completed time +
  sort). This task layers on top; don't undo either.
- Read `frontend/src/components/HistoryJobsSection.tsx` — the existing group-by-queue
  precedent (queue headers + rows flattened for one virtualizer). Match its idioms where
  they fit; Transfers' row set is bounded so virtualization is not required.

## What to do

1. **Group rows by queue** (queue name from the payload, one group per queue with ≥1
   visible job). Within a group, keep the existing sort exactly (active first in
   scheduler order, then terminal newest-completed-first). Groups order by queue name.
2. **Group header line**, one click anywhere on it toggles collapse. Contents: queue
   name, job counts by outcome (active / queued / succeeded / failed — omit zero counts
   to keep it quiet), and total size (sum of `bytes_done`; while anything is downloading
   also show the group's combined current rate). Keep it one line.
3. **Remove the per-row queue label** from the collapsed row line (the header now carries
   it). The expand panel may keep queue context if it already has it.
4. **Persist collapse state** per queue in `localStorage` (keyed by queue id, surviving
   reload). Check first whether the codebase already has a small persisted-UI-state
   helper (e.g. for Files column widths) and reuse it; only hand-roll if nothing exists.
   Default: expanded. A queue that disappears from the payload keeps its stored
   preference for when it returns.
5. **"Dismiss all" stays global** at the top of the page, unchanged.
6. Pure logic (grouping, header aggregates, collapse-state read/write) goes in
   `lib/transferPanel.ts` or a sibling, exported and unit-tested: grouping correctness,
   aggregate math (counts, size sum, combined rate only when active), zero-count
   omission, collapse persistence round-trip, unknown-queue default-expanded.
7. Docs same commit: `CHANGELOG.md` Unreleased entry; `prompts/startnewsession.md` arr
   build-run table row; `docs/decisions.md` only if something non-obvious comes up.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report; ships unviewed.
- No new dependencies; Tailwind + existing idioms. `feat:` prefix.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` — these are CI's exact pinned
   commands and scope (a `backend/`-scoped run let unformatted `tests/` files ship on
   2026-08-15; don't repeat it).
5. `uv run pytest` — note skip counts honestly (must run even if backend untouched).

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
