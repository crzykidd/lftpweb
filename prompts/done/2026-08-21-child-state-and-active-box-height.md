---
name: 2026-08-21-child-state-and-active-box-height
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Shared `childDisplayState` helper (`lib/fileTree.ts`) maps PARTIAL+running-job to DOWNLOADING for both the Queue row's file-list expansion and the item drawer's file view; Active/Complete boxes' zero-row empty states shrank from a fixed `h-40` dashed panel to one line, matching Preflight. All gates green (1585 backend / 612 frontend, 0 skipped).
---

# Task: an actively-transferring file reads "Downloading", and the Active box stops padding itself when empty

Two small fixes from the user's browser review, 2026-08-21, both wanted **before `0.3.0`**.

## 1. A file being transferred right now says "Partial", not "Downloading"

> *"The sidebar for active file. File shows progress and partial... I think it should show
> downloading and the chip should show progress. Not Partial. the expand below shows partial with
> the %"*

The **parent** row maps job state to chip state (`chipStateFor` in `TransfersPage.tsx`:
`running` → `DOWNLOADING`). The **child** rows do not — `FileListRow` passes `row.state` raw, which
is the item's *structural* state, and a partly-transferred file's structural state is `PARTIAL`.

Structurally correct, but wrong to read: while lftp is actively writing that file, "Downloading"
is what the user needs to see. The same applies in the item drawer's file view, which the user saw
first ("the sidebar").

**The rule — be careful here, this is the part that is easy to get wrong.** Do **not** map every
child of a running job to `DOWNLOADING`. `mirror` works through a release's files progressively:
some children are already complete, some have not started, typically one is in flight. Blanket-
mapping would label finished files as downloading.

Map to `DOWNLOADING` only a child that is **both**:
- currently reading `PARTIAL` (partly there — not complete, not untouched), **and**
- owned by a job that is currently `running`.

A complete child stays `DOWNLOADED`; an untouched one stays whatever it is. A `PARTIAL` child whose
job is **not** running (stopped, failed, paused) correctly stays `PARTIAL` — that is precisely the
"stopped part-way" case `PARTIAL` exists to express.

**Both surfaces must agree** — the file list in a Queue row's expansion *and* the item drawer's
file view. Put the mapping in one pure, unit-tested helper in `lib/` and use it from both; two
copies would drift, and the user is looking at both.

`StateChip` already has a `FILL_STYLES` entry for `DOWNLOADING` (blue) as well as `PARTIAL`
(amber), so the progress fill keeps working — the fill just changes colour with the state, which is
correct and matches the parent row.

**Do not change `item.state` itself, or anything in the backend state machine.** This is a display
mapping only. The stored state stays `PARTIAL`; §3.2's rules are unaffected.

## 2. The Active box pads itself out when empty

> *"Active box shrinks to one row when 1 active item. then expands to 5 rows when nothing is going
> on. We should keep this at one row always and only expand when we have more rows up to the max
> show size."*

With one active item the box is one row — correct. With **nothing** running it grows to roughly
five rows of empty space, which is backwards: the emptiest state takes the most room, pushing the
Complete box down.

**Rule: height follows content, always.** One row when there is one row. **Zero rows renders a
single line** ("Nothing queued or transferring", or similar), never a padded block. Growing only
happens with real rows, up to the selected page size.

This is the same rule already applied to the Preflight box; make the two consistent rather than
inventing a second approach — and check whether whatever causes this (a min-height, a placeholder
block, reserved row slots) also affects the **Complete** box's empty state.

**Related open question the user has already been asked and not yet answered — leave it alone:**
at zero rows the *Preflight* box still renders its footer (readout + page-size selector), so it is
"Nothing in preflight." plus a small selector row rather than a strict single line. Do **not**
change that here; it is a separate decision the user is sitting on. **But if your fix for the
Active box makes the two boxes visibly inconsistent with each other, say so in your report.**

## Before you start

- `frontend/src/pages/TransfersPage.tsx` — `chipStateFor`, `FileListRow`, `FileListGroup`, and the
  Active box's own rendering and empty state.
- `frontend/src/components/ItemDrawer.tsx` — the drawer's file view ("the sidebar").
- `frontend/src/lib/fileTree.ts` — `stateProgressPercent`, `nodeDisplaySize`, already shared
  between the Files tree and the Queue expansion.
- `frontend/src/components/PreflightBox.tsx` — the scale-to-content behaviour to match.
- `frontend/src/components/StateChip.tsx` — `STYLES`/`FILL_STYLES` for `PARTIAL` and `DOWNLOADING`.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1585 backend / 607 frontend tests passing, 0 skipped**.

## Tests

Pure logic in `lib/`, per this codebase's convention:
- the child-state mapping across the matrix — `PARTIAL` + running job → `DOWNLOADING`; `PARTIAL` +
  non-running job → `PARTIAL`; complete child + running job → unchanged; untouched child →
  unchanged;
- that both call sites use the one helper (a shared-helper test, or simply no second
  implementation to test).

This is very likely frontend-only. If you find you need a backend change, **stop and explain why**
— a display mapping should not require one, and needing one probably means the mapping drifted into
the state machine.

## Docs

`CHANGELOG.md` under `[Unreleased]`. `DESIGN.md` §9.2 only if it describes the child rows' state
display. `docs/decisions.md` only if you hit something not settled here.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`, and it caught
  three agents in the last two days regardless.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`fix:`). No `Co-authored-by:` trailer.
- **You cannot render a page**, and both of these are visual. Say exactly what a human should look
  at.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
