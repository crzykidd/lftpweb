---
name: 2026-08-21-pause-for-duration
status: pending          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed:               # filled when the work is done
result:                  # one-line summary of the outcome
---

# Task: pause the queue for a fixed duration, then resume automatically

**⏸ DO NOT START BEFORE `0.3.0` IS CUT.** User request, 2026-08-21, parked deliberately.

> *"Pause for x minutes so drop down with 1/10/30/60 minutes. all other actions are the same."*

Extends the queue pause shipped in `07e2471`. **Everything about pause itself stays exactly as it
is** — the two entry modes (*after current* / *now*), auto-queue continuing to enqueue, Start now
disabled + 409'd, reordering staying live, persistence across restart. This adds only a deadline
after which the queue unpauses itself.

## The one design decision that matters

**Store a deadline (an absolute timestamp), not a countdown or a timer.**

A running timer dies with the process. A stored `paused_until` makes restart-correctness fall out
for free:

- Restart *before* the deadline → still paused, and it expires on time.
- App **down past** the deadline → it comes back **unpaused**, which is the honest answer. A
  ten-minute pause should not become an eight-hour one because the container was restarted.
- No catch-up logic, no timer reconstruction, no drift.

Pause state already persists in a `setting` row (no migration was needed for pause; this probably
needs none either — confirm and say which you did).

## Behaviour to get right

- **Indefinite pause stays the default.** The dropdown adds durations; it does not replace
  "pause until I say otherwise". Both entry modes (*after current* / *now*) must be combinable
  with a duration — pausing *now* for 10 minutes is the obviously useful combination.
- **Manual unpause clears the deadline.** Obvious, but assert it: a stale deadline that later
  re-pauses the queue would be baffling.
- **Re-pausing replaces the deadline** rather than extending or stacking it.
- **Pausing indefinitely after a timed pause clears the deadline**, it does not keep it.
- **Expiry must actually fire without a page open.** This is server-side state; the queue must
  resume on the backend's own clock, not because a browser polled. Decide where the check lives
  (the engine tick is the obvious candidate — it already runs continuously) and say why.
- **The UI must show the deadline, not just "paused"** — a countdown or "resumes at HH:MM". A queue
  that will silently restart itself in 40 minutes must say so; otherwise "why did it start again?"
  is a support question.
- Expiry is a **state change worth an audit event**, like the pause itself.

## Before you start

- `prompts/done/2026-08-20-queue-pause.md` and the pause implementation it produced —
  `TransferQueue.pause`, the `_admit` gate, the persisted `setting` row, `/api/health`'s paused
  field, and `components/PauseMenu.tsx`.
- `docs/concepts.md`'s "queue is paused" section — it will need the duration case.
- `core/engine.py`'s tick loop, if that is where expiry ends up.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

Deadline expiry resumes admission; restart before the deadline stays paused; restart **after** the
deadline comes back unpaused; manual unpause clears the deadline; re-pausing replaces rather than
stacks; a duration combined with each of the two entry modes. Prefer testing against an injected
clock over sleeping.

## Docs

`CHANGELOG.md`; `docs/concepts.md` (the pause section); `DESIGN.md` §4.5's pause subsection;
`docs/decisions.md` for the deadline-not-timer choice and where expiry is evaluated.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will
  stall forever — written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`,
  `uv run ruff format --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There
  is **no `typecheck` npm script**.
- Report test counts before and after; confirm 0 skipped. Prefix `feat:`. No `Co-authored-by:`.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **Do NOT commit.** Prepare the tree, report the file list and a proposed commit message.
