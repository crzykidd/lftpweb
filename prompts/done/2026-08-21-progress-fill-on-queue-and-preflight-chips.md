---
name: 2026-08-21-progress-fill-on-queue-and-preflight-chips
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Queue row's chip now passes percent (queueRowPercent, reusing stateProgressPercent);
  Preflight's *arr "Waiting" chip gets its own fillable WAITING bucket (reusing PARTIAL's
  confirmed amber shades) via preflightFillPercent/preflightChipState, with SETTLING kept
  fill-less; missing/zero/inconsistent *arr size data renders a plain chip. 16 new frontend
  tests (591 -> 607), backend untouched (1577 passed). All gates green.
---

# Task: the ticking progress fill returns to the Queue row's chip, and comes to Preflight's "Waiting" chip

From the user's browser review, 2026-08-21:

> *"The downloading chip in files uses a bar to show % as well. we lost that."*
> *"no % is good it is small but the chip updating makes it dynamic and cool. Same with waiting. We
> get that detail from arr so we should include it behind the chip..."*

`StateChip` draws a progress fill behind its own text — the 2026-08-13 lifecycle-icons work, whose
brief was *"a box with the word partial in it that shows a color background that keeps ticking up
... Something sexy looking"*, with SABnzbd named as the reference. Two places lost or never got it.

## 1. The Queue row's own chip has no percent

`frontend/src/pages/TransfersPage.tsx` renders the row's chip as:

```tsx
<StateChip state={chipStateFor(job)} />
```

— **no `percent`**. Yet the *per-file* rows inside that same row's expansion do pass it
(`<StateChip state={row.state} percent={percent} />`), and the Files tree passes it. So the one row
that most wants a ticking bar is the only one without it.

The number already exists on that row — it is what `transferLineValue` renders as
`45% · 40 MB/s · 25m left`. It simply never reaches the chip. **Pass it.**

`StateChipProps.percent`'s own docstring says a caller may "pass a percent for every row without
checking the state itself", because a state with no `FILL_STYLES` entry renders plain regardless.
So pass it unconditionally rather than branching on state.

**The user has explicitly approved the duplication** ("no % is good it is small") — the chip
showing `Downloading 45%` alongside the figure column's `45% · 40 MB/s · 25m left` is wanted, not a
problem to solve. Do not suppress either.

## 2. Preflight's "Waiting" chip should fill as the remote client downloads

This is the interesting half. An *arr row's release is being downloaded by a client that is not
lftpweb — and the *arr reports how far along it is. Showing that as a ticking fill gives live
visibility into work happening entirely outside this app.

**The data is already on the row**: `PreflightRow` carries `size_bytes` and `size_remaining_bytes`,
taken from the *arr's queue record. Percent is derivable from those two. **Do not add a request.**

**The blocker, and why this is not just "pass a percent":**

```ts
const FILL_STYLES: Record<string, string> = {
  PARTIAL: 'bg-amber-300 dark:bg-amber-700/80',
  DOWNLOADING: 'bg-blue-300 dark:bg-blue-700/80',
}
```

Preflight chips currently render through the `SETTLING` bucket, which has **no `FILL_STYLES`
entry** — so they render plain no matter what percent is passed.

**Give the *arr preflight chip its own fillable state key** (e.g. `WAITING`) with an amber base and
an amber fill, rather than reusing `PARTIAL`'s bucket — conflating "partially transferred by
lftpweb" with "partially downloaded by someone else" would be exactly the vocabulary confusion the
"Waiting" label was introduced to fix.

**`SETTLING` keeps no fill, deliberately.** A settling release is not downloading — lftpweb is
waiting for its remote fingerprint to stop changing, and there is no meaningful percentage. Its
detail belongs in its tooltip (added in the task before this one), not a bar. So in the finished
box: **Waiting fills and ticks; Settling does not.** Both stay amber.

Follow `FILL_STYLES`'s own stated rule: the fill is "a second, more saturated shade of the same
chip color (never a different hue), so text stays legible whether it's sitting on the filled or
unfilled portion."

**Handle missing data honestly.** `size_bytes`/`size_remaining_bytes` are frequently absent — a
paused or stalled client item, a record the *arr never populated. Absent, zero-size, or nonsensical
(remaining > total) must render a **plain chip with no bar**, never `0%`, never a full bar, never a
computed `NaN%`. Put the percent derivation in a pure, unit-tested helper in `lib/preflight.ts`
rather than inline in the component.

## Before you start

- `frontend/src/components/StateChip.tsx` — `STYLES`, `FILL_STYLES`, `StateChipProps`, and the
  comment recording that the two shades' legibility is **"unverified against a real browser"**.
  It has since been confirmed good by the user for `PARTIAL`/`DOWNLOADING`; a new fill shade is
  once again unverified, so match an existing pair's relationship rather than inventing one.
- `frontend/src/pages/TransfersPage.tsx` — `Row`, `chipStateFor`, `transferLineValue`, and the
  child-row `StateChip` call that already passes a percent.
- `frontend/src/components/FileTree.tsx` / `lib/fileTree.ts` — `stateProgressPercent`, the one
  place that already decides what a percent means for a chip. **Reuse it if it fits**; do not write
  a second definition of "how full is this chip" for the Queue row.
- `frontend/src/components/PreflightBox.tsx` and `lib/preflight.ts` — the Preflight chips.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt. **This task follows
`2026-08-21-preflight-label-and-page-size.md`, which touches the same Preflight files — make sure
it has landed and the tree is clean before you start.**

## Tests

Pure logic in `lib/`, per this codebase's convention: the Preflight percent helper across present /
absent / zero-total / remaining-exceeds-total, and whatever helper feeds the Queue row's percent.
Assert that an absent percent yields no bar rather than a zero-width one.

This is very likely frontend-only. If you find you need a backend change, stop and explain why.

## Docs

`CHANGELOG.md` under `[Unreleased]`. `DESIGN.md` §9.2 only if it describes the chip's progress fill.
`docs/decisions.md` for the separate `WAITING` fill bucket and why `SETTLING` deliberately has none.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page**, and this task is entirely about something animating on screen. Say
  plainly what a human should watch — a real transfer's chip filling, and a Preflight row's chip
  filling as the remote client works.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
