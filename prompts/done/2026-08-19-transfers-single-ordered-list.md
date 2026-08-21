---
name: 2026-08-19-transfers-single-ordered-list
status: completed        # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: Transfers page now renders one flat, globally-ordered list (no per-queue grouping) with
  a per-row queue badge and fast-lane marker; confirmed the stage-2 chevron oddity is resolved.
  Backend JobOut gained queue_short_name (field addition only, no new endpoint). 1439 backend /
  503 frontend tests passing, 0 skipped (frontend count down from 513 net of dead-test removal
  vs. new fast-lane coverage). All gates green.
---

# Task: drop per-queue grouping on Transfers — one globally-ordered list

**Phase 1, stage 4a of `docs/transfers-redesign-spec.md` — read §3.1, §3.5 and §3.6 first.**

Stage 4 in the spec bundled four things; it has been split. **This task is 4a: frontend only.**
No migration, no new endpoint, no pagination. 4b adds the two paginated boxes afterwards.

Replace the Transfers page's per-queue grouping with **one list in true global order**, with each
row carrying its queue's short name and a fast-lane marker.

## Why grouping goes — this is a correctness fix, not a preference

`core/scheduler.py` contains **zero** references to `queue_id`. Admission is entirely
queue-agnostic: there is exactly **one** global line. Grouping by queue visually implies each
queue has its own line and its own ordering, which is false — and it is why positions inside one
group read `#3`, `#7`, `#11`. The numbering was always honest; the grouping was the lie.

It also fixes a real oddity shipped in stage 2: the chevrons move a job in the global order, so
today they can swap a job with something in a *different* group, appearing not to move it. Once
there is one list, ▲ trades with the row directly above. **Confirm this actually resolves and say
so in your report** — it is the main user-visible payoff of this change.

**This reverses a documented decision.** `prompts/done/2026-08-16-transfers-group-by-queue.md`
introduced grouping because *"per-row queue labels make the page busy."* That was sound with no
filter available. The name filter (commit `e0befc5`) changes it: a row needs far less queue signal
when a queue can be isolated on demand. **`docs/decisions.md` must record this as a reversal with
its cause** — do not silently contradict the earlier entry.

## Before you start

- `docs/transfers-redesign-spec.md` §3.1 (grouping), §3.5 (fast lane), §3.6 (short name).
- `frontend/src/pages/TransfersPage.tsx` and `frontend/src/lib/transferPanel.ts` — `GroupHeader`,
  `groupJobsByQueue`, `groupHasDismissable`, the collapse helpers, and `queuePositions`.
- `frontend/src/lib/queueDisplayName.ts` — the `short_name or name` helper from stage 3. **Use
  it**; do not re-derive the fallback.
- `backend/lftpweb/core/scheduler.py` — `LANE_MAIN` / `LANE_SMALL` and
  `small_item_threshold_bytes`, so the badge you write says something true.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1439 backend / 513 frontend tests passing, 0 skipped**.

## What to do

### 1. One list

Render `sortedJobs` (after the existing name filter) as a single flat list in its existing order.
Remove the grouping layer: `groupJobsByQueue`, `GroupHeader`, the per-queue collapse state and its
`transfers.collapsedQueues` persistence, and the per-queue "Dismiss Queue" control.

**The per-queue Dismiss Queue button is superseded, not merely deleted** — the name filter plus
scoped "Dismiss list" (commit `e0befc5`) does the same job: filter to a queue, dismiss the list.
Say this in the CHANGELOG so it reads as a consolidation rather than a regression.

**Clean up what becomes dead**, don't leave it lying around: exported helpers in
`transferPanel.ts` that now have no caller, their tests, and the orphaned `localStorage` key.
`HISTORY_COLLAPSED_QUEUES_KEY` and the history-side collapse helpers are **still in use by the
History page — leave those alone.** Check each symbol's callers before removing it.

### 2. Queue identity per row

A compact, muted badge showing `queueDisplayName(short_name, name)`. It must not dominate the row
— it is a locator, not a headline. The full queue name belongs in its `title`/tooltip when the
short name is what's displayed.

**`JobOut` does not currently carry `short_name`.** Getting it there is a backend change; that is
acceptable in this task *only* as a field addition to the existing jobs response — no new
endpoint, no shape change. If you find the queue's short name is not reachable without something
larger, stop and report rather than inventing an endpoint.

### 3. Fast-lane marker

Spec §3.5 decided: **one `1..N` numbering, with fast-lane rows marked** — rejected were per-lane
numbering and a separate box.

Small items (under `small_item_threshold_bytes`, 10 MB default) admit from a separate lane with
its own concurrency cap and reserved bandwidth, so a job at `#9` can genuinely start before `#2`.
In a single ordered list that reads as a bug unless it is explained. Mark those rows and make the
tooltip say *why* — something to the effect of "small file, transfers on its own lane and may
start before higher-numbered items."

If `JobOut` already exposes `lane`, use it. If it does not, the same rule as §2 applies: a field
addition is fine, a new endpoint is not.

### 4. What must not change

- The name filter and "Dismiss list" behave exactly as they do now, just over a flat list.
- "Clear all" / "Dismiss all" are untouched.
- Row content, the item drawer, chevrons, Start now, Stop — all unchanged.
- `queuePositions` still numbers queued jobs globally. Do not make it per-queue.

### 5. Tests

Pure logic in `lib/`, unit-tested, per this codebase's convention. Cover at minimum: the badge's
display-name resolution with and without a short name, and the fast-lane predicate. Remove tests
belonging to deleted helpers rather than leaving them asserting dead code.

### 6. Docs

`DESIGN.md` §9.2 (the Transfers page description), `CHANGELOG.md` under `[Unreleased]`,
`docs/decisions.md` (the reversal, per above), and tick stage 4a in
`docs/transfers-redesign-spec.md` §7 — note in that table that 4b (pagination) is still pending.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — running from there collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**. Do not
  background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** This is the most visually significant change in the redesign so
  far and it ships unseen — say so plainly, and be specific about what a human should look at
  first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
