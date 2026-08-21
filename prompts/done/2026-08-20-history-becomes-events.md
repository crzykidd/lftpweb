---
name: 2026-08-20-history-becomes-events
status: completed        # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: History became Events (jobs list dropped); per-item deep link added in the item
  drawer's header, not on the row itself (layout-risk decision, docs/decisions.md); kept
  GET/DELETE /api/history/jobs* since ItemDrawer.tsx still depends on GET (prompt's own premise
  was wrong on this point); backend untouched (stage is frontend-only per the spec's own build
  table). 1467/1467 backend tests unchanged, 548->535 frontend tests (net -13: -22 dead
  History-job tests, +8 eventsLink tests, +1 nav test), 0 skipped, all 6 gates green.
  Browser-unverified -- phase 1 of docs/transfers-redesign-spec.md is now code-complete but has
  never been looked at in a browser.
---

# Task: History becomes Events — drop its jobs list, add a per-item deep link

**Phase 1, stage 7 (the last) of `docs/transfers-redesign-spec.md` — read §2 first.** Stages 1–6
are landed through commit `8146fe1`.

The Queue tab's Complete box (stage 4b) now covers "what finished, in what order," paginated and
filterable. That makes History's own jobs list a second, overlapping answer to the same question —
the exact duplication this redesign exists to remove.

**History becomes Events**: the audit-event log only. It keeps what nothing else has — remote
deletes, deletes withheld, verify outcomes, notify failures, the whole forensic trail — and sheds
the part that now has a better home.

Plus: **a per-item deep link.** A row on the Queue or Files tab gets an affordance that opens
Events pre-filtered to that item. This is what lets the item drawer stop duplicating the audit
trail: one canonical place, reachable in one click from anywhere.

## The good news, already checked

`api/history.py`'s events endpoints **already accept an `item_id` filter parameter.** The deep
link is frontend route/query-param wiring — no backend filtering work needed. Verify this yourself
rather than taking it on faith, but that is what the code showed.

## Before you start

- `docs/transfers-redesign-spec.md` §2.
- `backend/lftpweb/api/history.py` — the events endpoints, their existing filters (`kind`,
  `level`, `item_id`, `queue_id`, date range), and the jobs endpoints you are about to orphan.
- `frontend/src/pages/HistoryPage.tsx` and `frontend/src/components/HistoryJobsSection.tsx` /
  `HistoryEventsSection.tsx`.
- `frontend/src/nav.ts` and `App.tsx` — stage 6 just restructured routing into nested tabs; follow
  the same idiom.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1467 backend / 548 frontend tests passing, 0 skipped**.

## What to do

### 1. Rename the page to Events, drop the jobs section

Nav label and route become Events. **Keep the old `/history` route working via redirect** — stage
6 established that pattern (`/files` → `/transfers/files`), and the in-app docs may link to it.
**Grep for old route strings** in `docs/quick-start.md`, `docs/concepts.md`, `docLinks`, and
anywhere else, exactly as stage 6 did, and report what you found.

Remove `HistoryJobsSection.tsx` and its supporting client-side machinery.

**Deletion discipline — this is the main risk in this task.** That component is the last consumer
of several things: `groupHistoryJobsByQueue`, `HistoryVirtualRow`, `historyQueueGroupCounts`,
`decrementHistoryQueueSummary`, `failedJobPanelContent`, `HISTORY_COLLAPSED_QUEUES_KEY`,
`readHistoryCollapsedQueues`/`writeHistoryCollapsedQueues`, `formatQueueGroupCounts`, and possibly
more. **Check every symbol's callers before deleting it** — some may still be used by the Queue
tab's Complete box, which was built later and may share helpers. Delete dead tests along with dead
code; do not leave tests asserting nothing.

### 2. Backend: what happens to the jobs endpoints?

`GET /api/history/jobs` and its queue-summary sibling lose their only frontend consumer.
`GET /api/history/jobs/{id}/output` is **still used** — the Complete box's failed-output panel
fetches it (stage 4b). Confirm that before touching anything.

**Decide and justify**: remove the now-unused endpoints, or leave them. Leaving them is defensible
(they are harmless, tested, and someone may script against them); removing them is cleaner. Either
is acceptable — an unjustified choice is not. If you leave them, say so in `docs/decisions.md` so
a later reader knows it was deliberate rather than forgotten.

### 3. The per-item deep link

An affordance on a Queue row and a Files row that opens Events filtered to that item. The filter
must be **in the URL**, so the resulting view is linkable, reloadable, and back-button friendly —
same principle as stage 6's tabs.

The Events page must show clearly that it is filtered to one item, and offer a one-click way back
to the unfiltered log. A filtered view that looks like the whole log is the same class of mistake
as stage 4b's page-scoped filter.

### 4. The item drawer

The spec's stated purpose for this deep link is to let the drawer stop duplicating the audit
trail. **Do not gut the drawer in this task** — but if it has an events section that is now plainly
redundant, say so in your report with a recommendation, and leave it for a follow-up. Scope
discipline: this task is the rename, the deletion, and the link.

### 5. Tests

Backend: only if you change endpoints. Frontend: route/redirect resolution and the deep-link URL
construction, as pure functions in `lib/` per this codebase's convention.

### 6. Docs

`DESIGN.md` §9 (nav) and §9.2/§9.3 wherever History is described; `docs/quick-start.md` and
`docs/concepts.md` (**rendered inside the running app — a stale route is a dead link for a new
user**); `CHANGELOG.md` under `[Unreleased]`; `docs/decisions.md` for the endpoint decision; and
tick stage 7 in `docs/transfers-redesign-spec.md` §7 — **that completes phase 1**, so say so in
that table.

## Conventions to honor

- **Never background a verification gate.** Run each in the FOREGROUND with an explicit generous
  timeout (the pytest run takes ~3.5 min; set the tool timeout to 600000 ms) and read its exit
  code. A spawned agent receives no background completion notification and will stall forever —
  this has happened repeatedly in this repo and is now a rule in `CLAUDE.md`.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should click first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
