---
name: 2026-08-20-transfers-queue-files-tabs
status: completed          # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Transfers is now the main nav section with Queue (default) and Files tabs at /transfers/queue and /transfers/files, reusing nav.ts's tabsForPath pattern; /files and /transfers redirect for back-compat; docs/quick-start.md and docs/concepts.md links updated to the new paths. Navigation only, browser-unverified.
---

# Task: Transfers becomes the main section, with Queue and Files as tabs

**Phase 1, stage 6 of `docs/transfers-redesign-spec.md` — read §2 first.** Stages 1–5 are landed
(through commit `8c3c448`), and 1–4b are browser-confirmed by the user.

Now that a Queue row expands to per-file progress (stage 5), the Files page is no longer where you
watch a transfer. Make the navigation say so: **Transfers is the main section**, containing two
tabs — **Queue** (today's Transfers page) and **Files** (today's Files page, unchanged in
behavior).

## The reasoning, so you don't over-reach

The current split is drawn on an *implementation* seam — `item` (a thing in the trees) versus
`job` (an attempt to transfer it). To the user they are one object: "this release." The tabs put
both views of that object in one place.

**Files is demoted, not removed, and it is not merged into Queue.** It stays because it is the
only view that shows **things with no job** — `REMOTE_ONLY` items on the seedbox that were never
queued because no pattern matched or auto-queue was off. If the Queue tab only shows what entered
the pipeline, nothing shows what didn't. Files is also the only home for Delete and the only
tree-shaped view of the remote.

**This task is navigation only.** Do not change what either page renders, what it fetches, or how
it behaves. If you find yourself editing `FileTree.tsx`'s rendering or the Queue's data flow, you
have left the task.

## Before you start

- `docs/transfers-redesign-spec.md` §2.
- `frontend/src/nav.ts` (or wherever the left-nav entries are defined) and `frontend/src/App.tsx`
  — the route table.
- `frontend/src/pages/TransfersPage.tsx` and `frontend/src/pages/FilesPage.tsx`.
- Any existing tabbed surface in this app — **Settings already has tabs**. Reuse that pattern and
  its components rather than inventing a second tab idiom. Find it before you build anything.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1467 backend / 545 frontend tests passing, 0 skipped**.

## What to do

### 1. Routes and nav

- Transfers becomes one left-nav entry with two tabs beneath/inside it: **Queue** and **Files**.
- **Each tab must have its own URL** and be linkable and reloadable — a tab that only exists in
  component state loses your place on refresh and can't be bookmarked. Follow whatever the
  Settings tabs already do here.
- **Existing URLs must not 404.** Anything currently linking to the Files page — the in-app docs
  (`docs/quick-start.md` and `docs/concepts.md` link to app routes like `/settings/queues`), the
  what's-new popup, any `docLinks` mapping — must still land somewhere sensible. **Grep for the
  old route strings before you change them** and either keep the old path working (redirect) or
  update every reference. List what you found in your report.

### 2. Default tab

Queue. It is the working surface now.

### 3. What must not change

- Either page's rendering, data fetching, filters, pagination, expansion, or actions.
- The Files page keeps Delete, its tree, its own filters, and its bulk actions exactly as they are.
- History/Events is untouched by this task (that is stage 7).

### 4. Tests

Whatever is testable in the existing style — route/tab resolution is the obvious pure part. If
tab-from-URL resolution ends up as a pure function, put it in `lib/` and unit-test it, per this
codebase's convention.

### 5. Docs

- `DESIGN.md` §9 (the page/navigation description).
- **The in-app user docs matter here more than usual.** `docs/quick-start.md` and
  `docs/concepts.md` are rendered *inside the running app* and link to real app routes — if a
  route moved, those links must move with it, or the quick start walks a new user into a dead
  link. Check both.
- `CHANGELOG.md` under `[Unreleased]`; tick stage 6 in `docs/transfers-redesign-spec.md` §7.
- `docs/decisions.md` only if you hit a decision not already settled here.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — running from there collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**. Do not
  background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Navigation changes are unusually easy to get wrong unseen — say
  plainly what a human should click first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
