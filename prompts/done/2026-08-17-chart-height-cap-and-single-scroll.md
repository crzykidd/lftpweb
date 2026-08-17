---
name: 2026-08-17-chart-height-cap-and-single-scroll
status: completed
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Capped both Dashboard charts' SVG height at max-h-80 (320px) with a max-w-4xl chart-block
  width cap chosen so the height cap never actually pillarboxes in practice; pinned Layout's
  shell root to h-dvh + overflow-hidden (single scroll context, main is the only scroller);
  themed html/body background in index.css so overscroll can't flash white in dark mode.
  Verified all four react-virtual scroll containers already use their own ref'd div (never
  window) with independent height/overflow, so the shell change didn't touch their wiring.
  All gates green: ruff check, ruff format --check, pytest (1281 passed), npm run lint,
  npm test (468 passed), npm run build.
---

# Task: Cap the Dashboard charts' rendered height; one scroll context, no white reveal

User report (2026-08-17, live on the test system): (1) the Dashboard charts grow as the
window widens — dragging the window wider makes them taller, maintaining aspect ratio,
with no ceiling; (2) the page has a dual-scroll feel — the inner section scrolls *and*
the window scrolls, and scrolling past the shell shows white below the app background
(worst in dark mode).

Both causes are confirmed in code:

- `frontend/src/components/charts/BytesChart.tsx` and `SpeedLineChart.tsx` render
  `<svg viewBox=… className="w-full">` with no height constraint — the browser derives
  height from the viewBox aspect ratio, so height scales with width unbounded.
- `frontend/src/components/Layout.tsx`'s shell root is
  `flex h-full min-h-screen bg-white … dark:bg-zinc-950` with `<main
  className="min-w-0 flex-1 overflow-auto p-4">`. `min-h-screen` lets the root *grow*
  past the viewport when content is tall, so the window scrollbar engages alongside
  `main`'s own `overflow-auto` — two scroll contexts — and whatever shows beyond the
  shell's painted background is the unstyled body (white).

## Before you start

- Read `CLAUDE.md`. Read the two chart components fully (they're small), and
  `Layout.tsx` fully.
- Survey what depends on the current scroll structure before touching it: the
  virtualized lists (`FileTree.tsx`, `HistoryJobsSection.tsx`,
  `HistoryEventsSection.tsx`, `ItemDrawer.tsx` — `@tanstack/react-virtual`) — find
  what element each measures/scrolls against (`getScrollElement`/`window` vs a ref)
  and make sure the shell change cannot break their scrolling. State what you found
  in your report.
- `git log --oneline -3` for context: the charts were just extended with 7d/30d
  ranges (`4f1a912`); don't disturb that work beyond the sizing.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. This file (the handoff prompt itself) is
exempt.

## What to do

1. **One scroll context.** Make the shell a fixed viewport frame: root becomes
   `h-screen` (drop `min-h-screen`; prefer `h-dvh` if the project's Tailwind version
   supports it — check — for mobile-viewport correctness) with `overflow-hidden`, so
   `<main>`'s `overflow-auto` is the *only* scroll context and the window scrollbar
   never engages. The sidebar stays fixed while content scrolls — which is already
   the intent of the current markup. Also give the document a themed background
   (e.g. `bg-white dark:bg-zinc-950` on the `html`/`body` level via `index.css` or
   the root element) so any overscroll/rubber-band region can never flash white in
   dark mode.
2. **Verify the virtualizers still scroll** after the change — if any of them measure
   `window` rather than an ancestor scroll element, fix their scroll-element wiring
   accordingly (they should already target their own containers given `main` has
   been `overflow-auto` all along; confirm, don't assume).
3. **Cap chart heights.** Both charts get a maximum rendered height (pick one cap,
   shared constant or repeated class, ~`max-h-72`–`max-h-80` for the bytes chart and
   the speed chart alike — your call within that band, state it). With the default
   `preserveAspectRatio` ("meet"), a width-capped-by-height SVG letterboxes and
   centers its content horizontally, which reads as wasted side space on very wide
   windows — avoid that by *also* constraining the chart block's width
   (`max-w-*` + centered, or capping the section) so the chart neither towers on a
   wide window nor pillarboxes oddly. Keep the chart fully fluid *below* the cap —
   small windows behave exactly as today. Don't switch to
   `preserveAspectRatio="none"` (it distorts the SVG text labels).
4. **Tests:** these are layout/CSS concerns with little pure logic; add/extend tests
   only where something testable changed (e.g. if you extract a sizing constant or
   helper). Run the full existing suites regardless — the virtualizer-dependent
   pages' tests must stay green.
5. **Docs, same commit:** `CHANGELOG.md` — one `### Fixed` entry under Unreleased
   (append after existing entries): dashboard charts no longer grow unbounded with
   window width, and the app now has a single scroll context with no white flash
   below the page. `docs/decisions.md` entry only if you had to make a non-obvious
   call (e.g. rewiring a virtualizer, or choosing `h-dvh` vs `h-screen` for a
   reason worth recording).

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  pytest takes ~3 minutes — timeout 400000ms; never background a gate and wait for
  a notification), exit codes read: `uv run --project backend ruff check`,
  `uv run --project backend ruff format --check`, `uv run pytest` (repo root);
  frontend `npm run lint`, `npm test`, `npm run build`.
- Comment style: dated, rationale-naming — match the files you touch.
- No browser here — this is exactly the kind of change that ships unviewed; say so
  plainly and name what a human should check (wide-window chart height, dark-mode
  overscroll, every virtualized list still scrolling).
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter: set `status`, `completed`, `result`.
2. Move this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit covering this prompt file, the files modified, and the prompt
   move. Present the file list and a one-line message.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree and
     report the file list + proposed message back to the orchestrating session.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
