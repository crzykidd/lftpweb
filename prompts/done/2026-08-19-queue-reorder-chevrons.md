---
name: 2026-08-19-queue-reorder-chevrons
status: completed        # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: One POST /api/jobs/{id}/move endpoint (direction up|down|top) added chevron reordering; renormalize-on-exhaustion and the not-queued-anymore race both handled and tested; UI ships browser-unverified.
---

# Task: per-row queue reordering — move up one, down one, to top

**Phase 1, stage 2 of `docs/transfers-redesign-spec.md` — read §3.4 and §3.5 first.** Stage 1
(commit `32dff87`) replaced the boost-based ordering with a dense `queue_position REAL` model and
left behind the primitive this task consumes: `core/queue.py.position_between(lower, upper)`,
already unit-tested with no caller.

Add **▲ up one**, **▼ down one**, and **▲▲ to top** controls to each queued row on the Transfers
page.

## Before you start

- `docs/transfers-redesign-spec.md` §3.4 (why the model changed) and §3.5 (the fast lane).
- `backend/lftpweb/core/queue.py` — `position_between`, `move_to_top`, `_rescue_position`, and the
  admission query. **Read `_rescue_position`'s docstring carefully**: it documents why a
  neighbour search must exclude boosted (`rank != 0`) jobs, with a proven counterexample. Your
  neighbour search is a *different* problem (adjacent by position, not by timestamp) — but read it
  so you understand what `rank` still means before you touch anything near it.
- `backend/lftpweb/api/jobs.py` — where the existing move-to-top endpoint lives; add alongside it.
- `frontend/src/pages/TransfersPage.tsx` — the row action controls and the existing per-row
  busy/error conventions.
- `tests/test_queue_position.py` — stage 1's tests; extend rather than starting a new file for the
  ordering primitives.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1409 backend tests / 504 frontend tests passing, 0 skipped**.

## What to do

### 1. Backend: one endpoint, not three

Add a single move endpoint taking a direction (`up` | `down` | `top`) rather than three
near-identical routes. Reuse the existing move-to-top implementation for `top` — do not write a
second one.

`up`/`down` resolve the target's adjacent neighbour **in the same lane-agnostic global position
order the scheduler uses** (`queue_position ASC, id ASC` over `state = 'queued'`), then set the
moved job's position to `position_between` the neighbour and the neighbour's own next-outward
neighbour. Standard midpoint reorder.

**Edge cases that must be handled explicitly, each with a test:**

- **Already at the top / already at the bottom** — a no-op that succeeds, not an error and not a
  crash. The UI should be disabling the control there anyway, but the endpoint cannot rely on it.
- **The job is no longer `queued`** (it started running, or was dismissed, between the page render
  and the click) — reject cleanly. Reordering a running job is meaningless; its allocation is
  fixed at spawn and never reshaped (DESIGN.md §4.5's invariant).
- **Only one queued job exists** — every direction is a no-op.
- **Concurrent moves** — two moves racing must not produce two jobs with the identical position.
  `id ASC` is the final tiebreak in the ordering, so identical positions are survivable rather
  than corrupting; confirm that holds and say so, or add a guard.

**Do not touch `rank`.** Stage 1 left it with one narrow remaining job (`_rescue_position` reads
it as a boosted/natural discriminator). `move_to_top` still writes it. Leave both alone.

### 2. Position exhaustion

Repeated midpoint insertion between the same two neighbours halves the gap each time and will
eventually exhaust `REAL` precision. This is a real, if distant, failure mode.

Handle it: detect when `position_between` returns a value indistinguishable from one of its
bounds, and renormalize the queued set (rewrite positions as 1.0, 2.0, 3.0… in current order)
before retrying. **Test it directly** by driving positions to adjacency rather than by waiting for
it to happen naturally.

### 3. Frontend

Chevron controls on each **queued** row — not on running rows (see above), not on terminal rows.

- `▲▲` to top, `▲` up one, `▼` down one.
- Disable `▲`/`▲▲` on the first queued row and `▼` on the last; the position number already tells
  the user where they are.
- Reuse the page's existing per-row busy/error state conventions — do not invent a new
  notification pattern.
- Refresh the list afterwards the same way the existing move-to-top control does.
- Accessible labels; these are icon-only buttons.

**The scope of a move is global, not within the visible group** (spec §3.4). The page still groups
by queue at this stage — grouping is not dropped until stage 4 — so the row visually above a job
is not necessarily the one it trades places with. **Do not try to fix that here.** It resolves
itself in stage 4 when grouping goes away. Note it in the commit report so it is a known
intermediate state rather than a surprise.

### 4. Fast lane

Positions span both lanes (spec §3.5) — a small-lane job and a main-lane job can be adjacent in
position order and a move can swap them. That is correct and intended. The fast-lane *badge* is
stage 4's job, not yours.

### 5. Tests

Backend: each direction, each edge case above, and the renormalization path. Frontend: whatever is
testable in the existing style — if the logic is pure enough to live in `lib/transferPanel.ts`
(e.g. "can this row move up"), put it there and unit-test it rather than burying it in the
component.

### 6. Docs

`DESIGN.md` §4.5 if the reorder API changes what it specifies, `CHANGELOG.md` under
`[Unreleased]`, `docs/decisions.md` only if you hit a decision not already settled here, and tick
stage 2 off in `docs/transfers-redesign-spec.md` §7's phase-1 table.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — running from there collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. Note there is **no `typecheck` script** in this repo's
  `package.json` — use `npx tsc -b`. Do not background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** State plainly that the UI ships browser-unverified.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
