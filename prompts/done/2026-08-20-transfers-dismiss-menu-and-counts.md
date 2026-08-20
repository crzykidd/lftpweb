---
name: 2026-08-20-transfers-dismiss-menu-and-counts
status: completed          # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Dismiss moved into the Complete box header as an All/Downloaded/Failed/Stopped menu (folding in "Clear all failed"), outcome composes with name_filter server-side (DismissAllRequest restructured, queue_id/job_ids stay exclusive), both boxes now share one pageReadout and always render their shell. 1478 backend / 547 frontend tests passing, 0 skipped. Browser-unverified.
---

# Task: move Dismiss into the Complete box as an outcome menu, and show a count readout on both boxes

**Follow-up to phase 1 stage 4b of `docs/transfers-redesign-spec.md` §3.2**, from the user's
browser review on 2026-08-20. Three related fixes to the same region of the Transfers Queue tab.

## What the user reported

1. *"the dismissall button should move down the top of the completed section"* — it currently
   sits at the page top, far from the rows it acts on.
2. *"maybe it is dismiss with a drop down list all, downloaded, failed (or whatever the completed
   status are)"* — dismiss by outcome, not only wholesale.
3. *"I have Page 1 of 1 (30 total) at the bottom of the completed section. I don't see that at the
   active/pending section"* — a real inconsistency, not a design choice (see below).

## Item 3 is a genuine bug, and it is the cheapest of the three

The Complete box renders its readout whenever `completeTotal > 0`, independent of the pager:

```
{completeTotal > 0 && `Page ${completePage} of ${completePageCount} (${completeTotal} total)`}
```

The Active box has **no readout at all** — only `<Pager>`, which returns `null` at `count <= 1`.
So under 20 active rows the user sees nothing. Give the Active box the same readout, on the same
terms. **Keep the `Pager`'s `count <= 1` guard** — a pager for one page is noise; a count is not.

Word both identically and put the wording in one shared place so they cannot drift.

**Related pre-existing quirk, found 2026-08-20 — fix it here since it is the same complaint:** the
Active/pending box's *entire section* (header included) renders only when it has at least one row,
so an empty queue shows no box at all — and therefore no readout and no page-size selector. The
Complete box renders its footer unconditionally. Make the two consistent: an empty Active box
should still render with an honest empty state ("Nothing queued or transferring"), not vanish.

## Before you start

- `frontend/src/pages/TransfersPage.tsx` — `handleDismissAll` (~943), `handleDismissList` (~959),
  "Clear all failed" (~997), and both boxes' footer regions (~1305 active, ~1370 complete). **Read
  the docstrings on those handlers** — they carefully record which control has which scope, and
  that reasoning must survive this change.
- `backend/lftpweb/models.py` — `DismissAllRequest` and its mutual-exclusion validator
  (`queue_id` / `job_ids` / `name_filter`).
- `backend/lftpweb/core/queue.py` — `dismiss_all_terminal`, including the **`job_ids == []`
  early-return guard** and its comment explaining why an empty explicit set must dismiss nothing.
- `frontend/src/components/StartNowMenu.tsx` — **an existing keyboard-navigable dropdown menu
  built on this project's own popover mechanics, with no new dependency.** Reuse this pattern for
  the Dismiss menu rather than inventing a second one.

**Another task may be in flight on `TransfersPage.tsx` and `lib/pagination.ts`** (a page-size
selector). Run `git status --porcelain` first; if either is dirty, stop and ask.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt.

## What to do

### 1. Move Dismiss into the Complete box header

"Dismiss all" moves from the page top to the top of the **Complete** box, where the rows it
affects live. Decide what happens to **"Clear all failed"** — it may now be redundant with a
Dismiss menu that offers "Failed", in which case folding it in is the right call. **Say which you
chose and why**; do not silently drop a control that a previous task deliberately added.

### 2. Dismiss becomes an outcome menu

Options: **All**, plus one per terminal outcome. Read the actual states from the code rather than
guessing — the user wrote *"downloaded, failed (or whatever the completed status are)"*, so use
whatever `isDismissable` / the job state vocabulary actually defines, and label them the way the
rest of the UI already labels those states, not with raw enum values.

Show counts in the menu if cheap (e.g. "Failed (3)"). If the Complete box is server-paginated and
per-outcome counts are not already available, **do not add a query to get them** — say so and
leave the labels plain.

### 3. Backend: dismiss by outcome

`DismissAllRequest` gains a state/outcome filter.

**Decided by the user (2026-08-20): the outcome filter and `name_filter` COMPOSE.** Both are
*narrowings* of the same set, not alternative scopes — "dismiss the failed ones matching
`Married`" is a coherent request and must work. Do not make them mutually exclusive.

- **Outcome + `name_filter` → compose** (AND them in the same `WHERE`).
- **`job_ids` stays mutually exclusive with both** — it names exactly which rows to dismiss, so a
  narrowing alongside it is meaningless.
- **`queue_id`**: decide. It is also arguably a narrowing, so composing it would be consistent —
  but it has no caller since per-queue grouping was dropped in stage 4a, so leaving it exclusive
  costs nothing. Say which you chose.

The existing validator is written as strict mutual exclusion across all three; it must be
restructured, not merely extended. Make sure it still rejects genuinely incoherent combinations
(anything alongside `job_ids`). Record the shape in `docs/decisions.md`.

**Whatever composes must compose in the count too.** The UI's "Dismiss (N)" reading and the number
of rows actually dismissed must be built from the identical predicate — there is already a test
asserting this property for `name_filter`
(`test_dismiss_all_terminal_name_filter_count_matches_list_complete_jobs_total`); extend it to the
composed case rather than writing a parallel one.

**The empty-set guard is load-bearing and must extend to the new path**: an outcome filter that
matches zero rows must dismiss **nothing** — it must never degrade into "no filter, so dismiss
everything." That trap already exists for `job_ids == []`. Test the new path directly, seeding
real dismissable rows so a dropped filter clause would be caught dismissing them (see
`test_dismiss_all_terminal_name_filter_no_match_dismisses_nothing_not_everything` for the shape).

### 4. What must not change

- The `Pager`'s `count <= 1` guard.
- "Dismiss list" (the filter-scoped control) keeps its current meaning.
- Filters, chevrons, badges, row expansion, pagination behaviour.

### 5. Tests

Backend: the outcome filter, its composition (or exclusion) with `name_filter`, and the
zero-match guard. Frontend: pure logic in `lib/` per this codebase's convention — the shared
readout wording and any menu-option derivation.

### 6. Docs

`CHANGELOG.md` under `[Unreleased]`; `DESIGN.md` §9.2 if it describes these controls;
`docs/decisions.md` for the composition decision and the fate of "Clear all failed";
`docs/transfers-redesign-spec.md` §3.2 if the described layout changes.

## Conventions to honor

- **Never background a verification gate.** Run each in the FOREGROUND with an explicit generous
  timeout (pytest takes ~3.5 min; set the tool timeout to 600000 ms) and read its exit code. A
  spawned agent receives no background completion notification and will stall forever — a written
  rule in `CLAUDE.md`, already hit by several agents here.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
