---
name: 2026-08-13-frontend-test-runner
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Added Vitest + happy-dom, wired into CI's "Frontend lint + typecheck" job via `npm test`.
  105 unit tests across 4 new files cover lib/format.ts, lib/storage.ts, lib/resetWarning.ts,
  and the pure tree/collapse/facet/column-width helpers in components/FileTree.tsx (exported
  trivially, plus one hoisted one-liner, resolveCollapsed, for the collapse-preference model).
  Backend untouched: 887 passed, both ruff gates clean. No component tests (would have been a
  mocking exercise); README's "Known gaps" entry amended rather than removed outright, since
  no component is actually rendered by this suite. CI job name deliberately left unchanged
  (it's a required branch-protection check on main) -- see docs/decisions.md.
---

# Task: Give the frontend a test runner, and cover the logic that already needs it

There is **no frontend test runner in this project** — no vitest, no jest, nothing, anywhere.
Every backend behaviour has tests; none of the frontend logic does. Three separate agents
declined to add one unasked, because it is an infrastructure decision. **The user has now asked
for it.**

## Why it matters now

The 2026-08-13 Files work added a lot of real logic to the frontend, written as pure, isolable
functions *specifically so it could be tested*, and all of it is currently unverified:

- `lib/format.ts` — `percentValue`/`formatPercent` (divide-by-zero and null handling),
  `formatBytes`, `formatRelativeTime`, `stateAgeLabel`, `settleWaitShortLabel`,
  `settleArrivingLabel`, `bothSidesRows`/`hasBothSides`
- `lib/storage.ts` — the safe `localStorage` wrapper, including the throwing/quota-exceeded path
- `components/FileTree.tsx` — sibling-preserving tree sorting, null-last ordering, the
  default-plus-exceptions collapse preference, column width clamping, the facet filter
- `lib/resetWarning.ts` — the reset warning text, which varies by sync mode, auto-queue state,
  and remote count, and which tells the user whether their files are about to re-download

That last one is worth singling out: **it is prose that tells the user the consequence of a
destructive action**, and it has no test at all.

## Choose the stack, but these are the sensible defaults

- **Vitest** — shares Vite's config and transform pipeline, so there is no second build to keep
  in sync. This is the obvious fit; deviate only with a reason.
- **A DOM environment** (`happy-dom` or `jsdom`) — pick one and say why. `happy-dom` is faster,
  `jsdom` is more faithful.
- **`@testing-library/react`** if you write component tests. Available, but see scope below.

**These are `devDependencies`, and that is a different bar from runtime dependencies.** This
project has deliberately added exactly one *runtime* frontend dependency since phase 1
(`@tanstack/react-virtual`) and flagged it. A test runner ships nothing to users and does not
enter the bundle — do not agonise over it, but do keep the set minimal and say what you added
and why.

## Scope: cover the pure logic first, do not try to test everything

The goal is a runner that works and real coverage of the logic most likely to be wrong — not a
comprehensive component suite in one pass.

**Do:**
- Every pure function listed above, with their edge cases: nulls, zeros, divide-by-zero,
  missing timestamps, storage that throws.
- The tree-sorting invariant explicitly: **sorting reorders siblings within each parent and
  never flattens the hierarchy.** Assert on the resulting tree structure, not just visible
  order. This is the one that would be worst to get wrong and is easy to break.
- The collapse preference's default-plus-exceptions model, including that a **newly-arrived**
  directory inherits the default — that is the case the naive "save the collapsed set"
  implementation gets wrong, and the reason the current design exists.
- `resetWarning`'s branches: remote count zero, `move` versus `copy`, auto-queue on versus off.

**Consider, but do not force:**
- Component tests. If a couple are cheap and valuable (a confirm panel rendering the right
  counts, an action label picking `redownload` over `queue`), add them. If they turn into a
  mocking exercise, stop and say so — a runner plus solid unit coverage is a complete
  deliverable.

**Do not:**
- Chase a coverage percentage or add a coverage threshold to CI. Thresholds on a suite this
  young produce busywork, not confidence.
- Refactor application code to make it testable beyond trivial exports. If something is
  genuinely untestable without restructuring, note it rather than restructuring it here.

## Wire it into CI

`.github/workflows/` has a **Frontend lint + typecheck** job. Add the test run to it — an
unrun test suite decays within weeks. Keep the job's name accurate if you change what it does.

Also add an `npm test` script (or equivalent) so it is runnable the obvious way, and make sure
it works both locally and in the container-less CI environment.

## Before you start

- `frontend/package.json`, `vite.config.ts`, `tsconfig*.json`.
- The files listed under "Why it matters now".
- `.github/workflows/` — the Frontend job.
- `README.md` and `DESIGN.md` §14 if it describes testing, so the docs match reality afterwards.

## Working tree check

`git status --porcelain`. Should be clean. If not, list what is dirty and ask.

## Conventions to honor

- `docs/decisions.md`, newest at top — which runner and DOM environment, and why; that
  devDependencies are a different bar; what you deliberately did not test.
- `CHANGELOG.md`; `README.md`'s "Known gaps" currently names the absence of a frontend test
  runner — **remove or amend that entry**, it is the whole point of this task.
- `DESIGN.md` §14 if it discusses testing — standing approval to edit directly.
- Backend must be untouched: `uv run pytest` should still be 887 passing, both ruff gates clean.
- `npm run lint` and `npm run build` clean.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, which runner and DOM
   environment and why, the dependencies added, how many tests and what they cover, what you
   deliberately left untested, the CI change, and anything not fixed. Never `git add -A`, never
   push.
