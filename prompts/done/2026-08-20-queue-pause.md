---
name: 2026-08-20-queue-pause
status: completed          # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Pause (after-current/now) shipped -- caller-side gate in _admit(), pause-now reuses the
  shutdown/rescue model (not stop), setting row for persistence, start-now 409 while paused,
  reordering untouched. 1530 backend / 566 frontend tests, 0 skipped. Browser-unverified.
---

# Task: pause the transfer queue — "pause after current" and "pause now"

User request, 2026-08-20: a **Pause control at the top of the Transfers → Queue tab**, so all new
transfers can be held for a while. Two ways in:

- **Pause after current** — running transfers finish; nothing new is admitted.
- **Pause now** — also stop what is running, leaving each item **ready to resume** where it left
  off when unpaused.

One paused state; the two options differ only in how you enter it.

## The trap — do NOT reuse the Stop path for "pause now"

`core/queue.py`'s stop **deliberately sets `item.auto_queue_suppressed`** (DESIGN.md §4.6: a
stopped item still matches its pattern, so without suppression auto-queue restarts it 30 s later,
forever). If "pause now" reuses stop, every running item gets suppressed and **will not come back
on unpause** — the exact opposite of what the user asked for.

**The correct model already exists: the graceful-shutdown path.** §10.3 SIGTERMs in-flight lftp
children so their `-c` resume state is clean; the v0.2.6 startup rescue then re-queues them
carrying their original queue position. "Pause now" is that, without the restart — and it is clean
to express now that `job.queue_position` is a real column (migration 023).

So "pause now" must:

- SIGTERM the child (clean resume state, partial bytes untouched);
- return the job to `queued`, **keeping its `queue_position`** so the running set resumes at the
  front, not behind the backlog;
- **not** set `auto_queue_suppressed`, **not** set the item to `STOPPED`;
- **not** be classified as a failure. A SIGTERM'd lftp exits non-zero — check how the supervisor
  classifies that today (graceful shutdown already faces this) and make sure a pause does not
  produce `FAILED` rows or an `error_class`. **This is the most likely way to get this wrong.**

## Decisions already made — do not revisit

| | |
|---|---|
| **Auto-queue keeps running while paused** | The queue builds up and is worked through on unpause. Pause means "stop moving bytes", not "stop noticing things" — a release that ages off the seedbox during a pause would otherwise be missed. Manual Queue actions likewise still enqueue. |
| **"Start now" does NOT work while paused** | Paused means paused. Disable it client-side **with a reason in the tooltip** *and* reject it server-side with a **409** — the same belt-and-braces shape the Start-now fraction options already use when no site bandwidth limit is set. A disabled button alone is not the guard. |
| **Reordering MUST keep working while paused — this is why Start now isn't needed** | The user's own reasoning: *"you should be able to move things up in the queue while paused, so if you wanted something new to start next you can rearrange the queue before unpausing."* Pause is the moment you curate the order. So the chevrons (▲ ▼ ▲▲) and any reorder endpoint stay **fully functional while paused**, and unpause starts work in exactly the resulting `queue_position` order. Do not gate reordering behind the pause check — and make sure the Start-now 409 guard cannot accidentally catch the reorder endpoint too. |
| **Pause survives a restart** | Persist it. Someone who paused for NAS maintenance or bandwidth would not want a container restart to quietly resume everything. |
| **Post-processing continues while paused** | Pausing *transfers* is not pausing the pipeline. Verify/extract/notify/import/cleanup all keep running for items already downloaded, and those rows stay in Active per the in-flight rule (commit `bd614c0`). |

## Before you start

- `backend/lftpweb/core/scheduler.py` — `admit()`, deliberately pure, with worked examples in
  DESIGN.md §4.5 and `queue_id` appearing zero times. **Decide where the pause gate lives**: adding
  a flag to `SchedulerSettings` versus the caller simply skipping the admission step. The caller
  gate is likely simpler and leaves §4.5's worked examples untouched — justify whichever you pick.
- `backend/lftpweb/core/queue.py` — the admission loop, `stop_job`/`stop_item` (and their
  suppression, ~962/~2707), `start_now`, the reap/classify path, and the shutdown handler (~636).
- The startup rescue's re-queue (`_requeue_interrupted_item`, ~735) — the closest existing
  precedent for what "pause now" does to a job.
- Wherever site-level transfer settings persist (`SchedulerSettings` is described as persisted in
  `setting`). **A settings row is preferable to a migration if it fits** — say which you used.
- `backend/lftpweb/api/health.py` (or wherever `/api/health` lives) — the header already reads
  seedbox reachability and scheduler liveness; paused belongs there too.
- `frontend/src/components/StartNowMenu.tsx` / `DismissMenu.tsx` — the existing keyboard-navigable
  menu pattern. Reuse it for the pause menu; do not add a dependency.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1510 backend / 566 frontend tests passing, 0 skipped**.

## What to do

1. **Persisted pause state** + API to read/set it, including which entry mode was used if that
   matters to your implementation.
2. **The admission gate** — nothing new is admitted while paused. Reaping, progress publishing,
   post-processing, scanning and auto-queue all continue.
3. **"Pause now"** per the model above.
4. **Start now**: disabled + 409 while paused.
5. **UI**: a pause control at the top of the Queue tab with the two options, an unmistakable
   **paused banner** — a queue that silently does nothing is a support question waiting to happen —
   and paused surfaced in the header/health readout.
6. **Unpause** resumes admission immediately, in queue-position order.

## Tests

- Paused admits nothing, while reaping and post-processing continue.
- "Pause now" returns running jobs to `queued`, **keeps `queue_position`**, sets **no**
  `auto_queue_suppressed`, produces **no** `FAILED` row and no `error_class`, and leaves partial
  bytes intact.
- Unpause resumes in position order, and a paused-now item resumes from its partial rather than
  restarting.
- Auto-queue still enqueues while paused.
- Start now returns 409 while paused.
- Pause state survives a restart (however this project's tests express that).

## Docs

`DESIGN.md` §4.5/§4.6 (admission and the stop-vs-pause distinction — **§4.6 currently says stop
suppresses auto-queue; make clear pause does not**), `CHANGELOG.md` under `[Unreleased]`,
`docs/concepts.md` (this is a user-facing behaviour people will ask about — especially "why is
nothing downloading"), and `docs/decisions.md` for the gate's home and the pause-now mechanism.

## Conventions to honor

- **Never background a verification gate.** Foreground, explicit generous timeout (pytest ~3.5 min;
  set the tool timeout to 600000 ms), read each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`.
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
