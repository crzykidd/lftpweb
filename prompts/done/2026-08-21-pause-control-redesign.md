---
name: 2026-08-21-pause-control-redesign
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # frontend control redesign, backend untouched
completed: 2026-08-21
result: Pause collapsed to one PauseMenu dropdown (Till I unpause / 1 / 10 / 30 / 60 min,
  selection pauses immediately) plus a persistent "Pause after active" checkbox (unchecked by
  default, disabled with a hover reason when nothing is running); new lib/pause.ts owns the pure
  decisions and is unit-tested. Backend untouched. 1675 backend / 671 frontend tests, 0 skipped.
---

# Task: collapse the pause controls into one dropdown plus a checkbox

Findings **2 and 3** of `prompts/test-findings-2026-08-21.md`, from the user's browser test of
`1791af8`. They are the same control, so they ship together.

## The problem

> *"The pause for x minutes dialog is confusing. Currently I select it and then I have to hit the
> pause button, but really it should just do a pause on selection of an item."*

Today the top of the Queue tab carries **two** controls and takes **two** steps: a duration
`<select>`, then a separate `PauseMenu` button that itself asks *pause after current* vs *pause
now*. Choosing a duration does nothing until you click Pause — that is the confusing part.

## The target shape

- **One Pause control.** Clicking it opens a list:
  **"Till I unpause"** (first, the default), then **1 min / 10 min / 30 min / 60 min**.
- **Selecting an entry pauses immediately.** The selection *is* the action — no second click, no
  confirm step.
- **A checkbox beside it: "Pause after active." Unchecked by default.** That checkbox — not a second
  menu — chooses *pause after current* vs *pause now*. Unchecked (default) is pause now.
- **Hide or disable the checkbox when nothing is running** (finding 2). With zero running transfers
  *after current* and *now* are the same action, so offering both is noise at best and misleading at
  worst. If you disable rather than hide, give a reason on hover.

Net effect: one click to open, one click to act, with the mode a visible persistent toggle instead
of a fork buried in a menu.

## Scope

**The backend does not change.** Both entry modes and the `paused_until` deadline already exist with
the right semantics (`07e2471`, `1791af8`) — `pauseQueue(stopRunning)` already takes the mode, and
the duration already rides along. This is a **frontend control redesign**: it replaces the duration
`<select>` and the two-entry `PauseMenu` with a single menu plus a checkbox.

If you find yourself editing `core/queue.py`, stop and re-read — that is a signal the redesign has
drifted into changing behaviour, which is not what was asked for.

**Everything about pause itself stays as it is:** auto-queue keeps enqueueing while paused, Start
now stays disabled + 409'd, reordering stays live, the deadline still expires server-side, and
manual unpause still clears it.

## Details worth getting right

- **The paused banner is unchanged** — it already reads "…(resumes at HH:MM)" and that works.
- `PauseMenu.tsx` is keyboard-navigable today (built on the existing popover mechanics, no new
  dependency). **Keep that** — the new list must be operable from the keyboard, and the checkbox must
  be reachable in tab order.
- The control row is crowded: pause controls, then the bandwidth slider, then Rescan. Removing one
  control from it is part of the win — do not replace two controls with three.
- **A separate agent may be reshaping the bandwidth control in the same file.** Check
  `git log --oneline -5` before starting; if `BandwidthControl` changed very recently, read that diff
  first so the two control rows end up coherent rather than each redesigned in isolation.

## Before you start

- `prompts/test-findings-2026-08-21.md` findings 2 and 3 — the user's own words.
- `frontend/src/components/PauseMenu.tsx` and the pause block at the top of
  `frontend/src/pages/TransfersPage.tsx`.
- `prompts/done/2026-08-21-pause-for-duration.md` — what the duration select does today.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

This repo has **no component-rendering harness** (no `@testing-library/react`), so follow the
existing convention: put the decision logic in a pure function in `lib/` and unit-test it there —
which menu entries exist, which duration each maps to, and whether the "after active" checkbox is
available for a given running count. Existing pause tests must still pass unchanged.

## Docs

`CHANGELOG.md`; `docs/concepts.md`'s pause section if it describes the control rather than the
behaviour. Mark findings 2 and 3 **done** in `prompts/test-findings-2026-08-21.md`. Append a
one-line entry to `prompts/startnewsession.md`'s "On `dev` since the release" — same commit.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will stall
  forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test -- --run`.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `feat:`. No
  `Co-authored-by:`.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
