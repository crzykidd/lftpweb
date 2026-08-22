---
name: 2026-08-21-bandwidth-from-the-queue-page
status: completed        # pending | completed | failed
created: 2026-08-21
model: opus              # design-sensitive: touches §4.5's central invariant
completed: 2026-08-21
result: >
  Queue-tab bandwidth slider onto the site-wide max_bandwidth_bps, with future-items-only and
  also-apply-to-in-progress; the latter re-admits via pause-now's own stop path rather than
  retuning, leaving §4.5's invariant and core/scheduler.py untouched, and leaves an already-paused
  queue (including a timed pause's deadline) completely alone. 1637 backend / 645 frontend, 0
  skipped.
---

# Task: change the site bandwidth limit from the Queue page, optionally applying it to running transfers

**⏸ DO NOT START BEFORE `0.3.0` IS CUT.** User request, 2026-08-21, parked deliberately.

> *"we set global bandwidth in settings, but allow the user to change from queue. with a slider
> maybe. and when changed have an option to change for future items or in progress. in progress
> will do a pause and unpause to reset the queue speed."*

## Read this first: why "apply to in-progress" is not a simple setting write

**DESIGN.md §4.5's central invariant: allocations are never re-shaped.** Bandwidth is assigned at
admission and fixed for a job's lifetime. `core/scheduler.py`'s module docstring is explicit —
`admit()` is *given* the running jobs' allocations as input and never adjusts them; it only decides
what to hand newly-admitted jobs. That invariant is **why the missing lftp control channel is a
non-issue**: lftp receives `--rate-limit` at spawn and there is no channel to change it afterwards.

So a running transfer's rate genuinely **cannot** be changed in place. The only way to give a
running job a new allocation is to **stop and re-admit it** — which is exactly what the user
proposed, and it is the right mechanism.

**It is only feasible because "pause now" already exists** (`07e2471`): that path SIGTERMs the
child, returns the job to `queued` **keeping its `queue_position`, `attempt` and partial bytes**,
sets no suppression, and is never classified `FAILED`. Unpause then re-admits it against the
current settings. Reuse that machinery — **do not write a second stop-and-respawn path.**

**Do not attempt to mutate a running job's `rate_limit_bps`, and do not add a control channel.**
If you find yourself editing `core/scheduler.py`'s treatment of `running` allocations, stop and
report — that is the invariant this whole task has to respect, not work around.

## What to build

**One control, one setting.** The slider edits the **existing site-wide** `max_bandwidth_bps` —
the same value Settings → Transfer owns. This is a second surface for one setting, **not** a new
per-queue limit. Both surfaces must reflect each other; changing it in one place must be visible in
the other without a reload (or as close as the existing polling allows).

**On change, offer two choices:**

- **Future items only** (the default, and the safe one) — write the setting. Running jobs keep the
  allocation they were admitted with, exactly as today. Nothing is interrupted.
- **Also apply to in-progress** — write the setting, then pause-now and immediately unpause so
  every running job is re-admitted against the new limit.

**The second option interrupts every running transfer**, so it must say so plainly *before* acting:
how many transfers will be interrupted, and that they resume from their partial bytes rather than
restarting. Presented as a confirmation, not a silent side effect.

## Details that will bite

- **Debounce the slider.** Committing on every pixel would write the setting hundreds of times and,
  on the in-progress path, restart every transfer repeatedly. Commit on release/blur, not on drag.
- **The re-admit is a real interruption**, however brief — each job spawns a fresh lftp. Confirm
  from the pause work that a paused-then-unpaused job resumes rather than re-fetching (the e2e test
  in `tests/test_queue_pause_e2e.py` already proves the resume; make sure this path inherits it).
- **Interaction with an already-paused queue.** If the user is paused and changes bandwidth with
  "apply to in-progress", there is nothing running and nothing to interrupt — it must not
  accidentally *unpause* them. Handle it and test it.
- **Interaction with "Start now" fractions.** Those are computed as a fraction of the site limit
  **at admission** (migration 022). Changing the limit changes what a future fraction means, which
  is fine and expected — but a job already running at a forced fraction and then re-admitted will
  recompute against the new limit. Decide whether that is right (it probably is) and state it.
- **Validation and floors.** `SchedulerSettings` has `min_share_floor_bps` and the small-lane
  reserve; a slider must not let the user set a site limit below what the scheduler needs to
  function. Reuse the existing validation rather than inventing bounds.
- **Zero / unlimited.** Establish what the existing setting means for 0 or unset before letting a
  slider produce it.

## Before you start

- **`DESIGN.md` §4.5 in full**, including its worked examples — they are the contract this task must
  not break.
- `backend/lftpweb/core/scheduler.py` — `admit()`, `SchedulerSettings`, `RunningJob`, and the
  module docstring on the invariant.
- `backend/lftpweb/core/queue.py` — `pause`/unpause, `_admit`, and the `pause_requested` reap path.
- `prompts/done/2026-08-20-queue-pause.md` — how pause-now avoids suppression and `FAILED`.
- Settings → Transfer's existing bandwidth field and its API.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

Setting-only change leaves running allocations untouched (the invariant); apply-to-in-progress
re-admits at the new rate and **resumes from partial bytes**, with no suppression and no `FAILED`
row; the already-paused case does not unpause; the two surfaces stay in sync; validation floors
hold.

## Docs

`DESIGN.md` §4.5 — **this is the section the feature is in tension with; it must describe the
re-admit path explicitly** rather than leaving the invariant looking violated. `CHANGELOG.md`;
`docs/concepts.md` (users will ask why changing bandwidth restarted their transfers);
`docs/decisions.md` for the pause-unpause mechanism and why in-place adjustment was not possible.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will
  stall forever — written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`,
  `uv run ruff format --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There
  is **no `typecheck` npm script**.
- Report test counts before and after; confirm 0 skipped. Prefix `feat:`. No `Co-authored-by:`.
- **Surface, don't silently resolve.** If this cannot be built without weakening §4.5, say so
  rather than weakening it.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **Do NOT commit.** Prepare the tree, report the file list and a proposed commit message.
