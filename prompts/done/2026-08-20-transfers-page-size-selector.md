---
name: 2026-08-20-transfers-page-size-selector
status: completed          # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Added independent "Show 10/20/50" selectors to both Transfers boxes, both defaulting to
  20 and persisted per browser via lib/storage.ts with isPageSize validation; page size changes
  reset to page 1; useCompleteJobs's existing request-id guard covers the new race case
  unmodified. Backend unchanged, frontend-only.
---

# Task: a 10 / 20 / 50 rows-per-page selector on each Transfers box, remembered per browser

**Follow-up to phase 1 stage 4b of `docs/transfers-redesign-spec.md` §3.2**, from the user's first
real look at the finished page (2026-08-20). Phase 1 is complete through commit `130a52c`.

Today the Active/pending box is fixed at 20 rows/page and the Complete box at 50, both hardcoded
(`lib/pagination.ts.ACTIVE_PAGE_SIZE` / `COMPLETE_PAGE_SIZE`). The user wants **each box to carry
its own "show 10 / 20 / 50" dropdown**, both **defaulting to 20**, with the choice **remembered
across reloads in browser storage**.

Their words, and the reasoning matters: *"probably default to 20 now that I have seen it on
screen"* — 50 is too many rows at once in practice.

## One thing that is NOT a bug, so don't "fix" it

The user noted they only see page info on the Complete box. That is correct behaviour:
`TransfersPage.tsx`'s `Pager` returns `null` when `count <= 1`, and their active queue currently
holds fewer than 20 rows. **Keep that guard** — a pager for a single page is noise.

But the **selector itself should follow a different rule from the pager**. Decide which of these
is right and say why:

- always visible, so the control is discoverable and the user can see the current page size; or
- hidden when the box has fewer rows than the smallest option (10), since there is nothing to
  page.

Lean toward the first unless the layout argues otherwise — a control that vanishes is hard to find
again.

## Before you start

- `frontend/src/lib/pagination.ts` — `ACTIVE_PAGE_SIZE`, `COMPLETE_PAGE_SIZE`, `pageCount`,
  `pageWindow`, `paginateClientSide`. All pure and unit-tested; extend rather than replace.
- `frontend/src/pages/TransfersPage.tsx` — both `<Pager>` call sites (~1311 active, ~1375
  complete), `activePage`/`completePage` state, and `useCompleteJobs`.
- `frontend/src/hooks/useCompleteJobs.ts` — the Complete box's server-side fetch. **Its page size
  is a server request parameter, not just a slice** — changing it changes what is fetched.
- `frontend/src/lib/storage.ts` — this project's existing localStorage helpers. **Use them.**
  There are existing persisted preferences to copy the idiom from (`dashboard.bytesRange`, the
  Files tree's sort and expand/collapse choices). Match their key-naming convention.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1467 backend / 535 frontend tests passing, 0 skipped**.

## What to do

### 1. Page size becomes state, defaulting to 20, persisted per box

Two **independent** stored preferences — the boxes do not share a setting. Read once on mount,
write on change, following `lib/storage.ts`'s existing pattern.

**Validate what comes out of storage.** A hand-edited or stale value (`"999"`, `"abc"`, a size
that is no longer offered) must fall back to 20 rather than being trusted — the same defensive
shape the other persisted preferences use. Put the allowed set in one place and validate against
it.

`ACTIVE_PAGE_SIZE` / `COMPLETE_PAGE_SIZE` become the *defaults* rather than fixed sizes. Both
default to **20** — the Complete box changes from 50 to 20; update the constant, don't special-case
it at the call site.

### 2. The selector

A small `<select>` (or the project's existing equivalent — check for one before adding a new
control) reading "Show 10 / 20 / 50", placed near each box's pager or header. Labelled for
accessibility; it is not obvious from an unlabelled number what it controls.

### 3. Changing page size must not strand the user

**This is the part that is easy to get wrong.** If the user is on page 4 of 10 at size 10 and
switches to 50, page 4 may no longer exist.

Reset to page 1 on any size change. That is the simplest correct behaviour and matches what the
existing code already does when the filter changes. Do **not** attempt to preserve scroll position
or compute an equivalent page — say so explicitly in the commit/report as a deliberate choice.

Also confirm the existing clamp still holds: a page index that outlives its result set must clamp
rather than render an empty box.

### 4. The Complete box's server round-trip

Its size is a fetch parameter. Changing it must refetch, and must not leave a stale in-flight
response from the previous size overwriting the new one — `useCompleteJobs` already has a race
guard; **confirm it covers this case** rather than assuming it does.

### 5. What must not change

- The `Pager`'s `count <= 1` guard.
- The filters, "Dismiss list", chevrons, badges, row expansion, and everything else on the page.
- The Active box stays client-side; the Complete box stays server-side.

### 6. Tests

Pure logic in `lib/` with unit tests, per this codebase's convention: the allowed-sizes validation
(including the bad-stored-value fallback) and any page-clamping helper. The existing
`pagination.test.ts` is the natural home.

### 7. Docs

`CHANGELOG.md` under `[Unreleased]`; `docs/transfers-redesign-spec.md` §3.2's table (the sizes are
stated there as fixed 20/50 — correct it, and note the default change); `DESIGN.md` §9.2 if it
states the page sizes. `docs/decisions.md` only for the reset-to-page-1 choice if you think it
warrants it.

## Conventions to honor

- **Never background a verification gate.** Run each in the FOREGROUND with an explicit generous
  timeout (pytest takes ~3.5 min; set the tool timeout to 600000 ms) and read its exit code. A
  spawned agent receives no background completion notification and will stall forever — this is a
  written rule in `CLAUDE.md` and has already caught out several agents here.
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
