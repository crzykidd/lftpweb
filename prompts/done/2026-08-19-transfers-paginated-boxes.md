---
name: 2026-08-19-transfers-paginated-boxes
status: completed        # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: >
  Split the Queue tab into Active/pending (20/page, client-side) and Complete (50/page,
  server-side via new GET /api/jobs/complete) boxes. Server-side name_filter added to the
  Complete listing and to dismiss_all_terminal (DismissAllRequest.name_filter, mutually
  exclusive with queue_id/job_ids) so "Dismiss list" acts on every matching row across every
  page, not just one; the name_filter dismiss branch re-adds the MAX(id)-per-item restriction
  so its count always matches list_complete_jobs's total for the same text. list_jobs() kept
  unchanged (all 3 callers checked; see docs/decisions.md). Backend 1439->1462 tests, frontend
  503->524 tests, 0 skipped; ruff/tsc/lint all clean. Browser-unverified -- see report.
---

# Task: split Transfers into two paginated boxes — active/pending and complete

**Phase 1, stage 4b of `docs/transfers-redesign-spec.md` — read §3.2 first.** Stage 4a (commit
`4465344`) already dropped grouping and produced one globally-ordered flat list. This task splits
that list into two boxes and paginates them.

| Box | Page size | Ordering | Pagination |
|---|---|---|---|
| **Active / pending** | 20 | true admission order (unchanged) | client-side — the set is bounded and already fully loaded |
| **Complete** | 50 | most recently finished first | **server-side** |

Numbered pages (`1 2 3 4 >`), SAB-style. **Rows shifting between pages as work completes is
accepted and explicitly not a problem to solve** — the user's decision, and it is how SAB behaves.
Do not build machinery to freeze pagination.

## The problem this task must actually solve

**The name filter is currently client-side, over rows already loaded.** The moment the Complete
box is paginated server-side, a client-side filter silently becomes a lie: typing `Married` would
filter only the 50 rows on the current page, while the user reasonably believes it is filtering
everything. That is worse than not having the filter, because it looks like it worked.

The same applies to **"Dismiss list"** (commit `e0befc5`), which sends the ids of the filtered
dismissable rows. If the filter only sees one page, "Dismiss list" silently dismisses one page's
worth while claiming to act on the list.

**You must solve both explicitly.** The intended shape:

- The filter string is passed to the server for the Complete box, and filtering happens in SQL
  there. The active box can stay client-side — it is bounded and fully loaded.
- "Dismiss list" over a server-filtered Complete box must act on **every matching row, not the
  current page.** Sending a page's worth of ids no longer expresses the user's intent. Extend the
  dismiss contract to carry the same filter the list is using, so the server dismisses exactly
  what the user is looking at. Keep the existing `job_ids` and `queue_id` scoping intact and
  mutually exclusive with the new one — `models.py`'s `DismissAllRequest` already establishes that
  validator pattern; follow it.
- **The empty `job_ids` lesson applies here too:** an explicitly empty filter result must dismiss
  **nothing**, and must never degrade into "no filter, so dismiss everything." That guard already
  exists in `dismiss_all_terminal` for `job_ids == []`; make sure the new path cannot bypass it.
  Test it directly.

If after reading the code you believe a different shape is better, say so in your report with
reasons — but do not ship a filter that silently only sees one page.

## Before you start

- `docs/transfers-redesign-spec.md` §3.2.
- `backend/lftpweb/api/history.py` — **the existing paginator. Reuse its shape; do not invent a
  second pagination idiom.** Note especially why it carries only `has_output_tail` and fetches the
  blob separately: inlining per-row payloads onto an unbounded list endpoint reintroduces the
  cost the row cap exists to prevent. Your Complete endpoint has the same property.
- `backend/lftpweb/core/queue.py` — `list_jobs()` and `dismiss_all_terminal()`.
- `backend/lftpweb/models.py` — `DismissAllRequest` and its mutual-exclusion validator.
- `frontend/src/pages/TransfersPage.tsx`, `frontend/src/lib/transferPanel.ts` — the flat list from
  stage 4a, the filter, and "Dismiss list".

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1439 backend / 503 frontend tests passing, 0 skipped**.

## What to do

### 1. Backend: a paginated endpoint for the Complete box

Terminal (`succeeded`/`failed`/`cancelled`), **not dismissed**, newest-finished first, paginated,
with an optional name-filter parameter matching on `rel_path` (same semantics as the client-side
`filterTransferJobs`: case-insensitive substring). Follow `api/history.py`'s pagination response
shape. **Do not inline `output_tail`.**

**Decide and justify** whether `list_jobs()` should now stop returning terminal rows, or keep
returning them with the frontend simply not rendering them in the active box. Keeping it is safer
(other consumers exist, and DESIGN.md §9.2 requires failed rows to remain visible); narrowing it
is cleaner. Say which you chose and why. **If you narrow it, check every caller first** — this is
exactly the kind of change that breaks something two pages away.

### 2. Backend: dismiss by filter

Per "The problem this task must actually solve" above.

### 3. Frontend

Two boxes, 20 and 50 per page, numbered pagination. Active box paginates client-side over the
already-loaded bounded set; Complete box fetches its page from the new endpoint and passes the
filter along.

Keep pagination logic **pure and in `lib/`** (page count, clamping, the visible page-number
window) with unit tests — page-number windowing in particular is easy to get subtly wrong at the
boundaries and is exactly the kind of thing this codebase keeps out of components.

Reset to page 1 when the filter changes. A page index that outlives its result set (filter
narrows while on page 4) must clamp rather than render an empty box.

### 4. What must not change

- Row content, chevrons, badges, the item drawer, Start now, Stop, "Clear all", "Dismiss all".
- The active box's ordering is still the true admission order.
- `queuePositions` still numbers queued jobs globally.

### 5. Tests

Backend: pagination boundaries, the filter, filter+dismiss acting on all matches rather than a
page, and the empty-filter-result-dismisses-nothing guard. Frontend: the pure pagination helpers,
including the clamp-on-filter-change case.

### 6. Docs

`DESIGN.md` §9.2, `CHANGELOG.md` under `[Unreleased]`, `docs/decisions.md` for the
`list_jobs()` decision and the dismiss-by-filter contract, and tick 4b in
`docs/transfers-redesign-spec.md` §7.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`, `uv run ruff format
  --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck`
  npm script**. Do not background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say so plainly and name what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
