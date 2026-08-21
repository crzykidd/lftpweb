---
name: 2026-08-20-active-box-holds-inflight-pipeline
status: completed        # pending | completed | failed
created: 2026-08-20
model: opus              # investigate-then-build: the split predicate needs care before code
completed: 2026-08-20
result: >-
  Active/pending now holds a row until its whole pipeline finishes, split by ONE server-side
  SQL predicate (core/pipeline_flight.py) shared by list_jobs/list_complete_jobs/dismiss_all_terminal;
  every blocking condition has a bounded exit (paused source delete handled by age, not by its
  unindexed event); manual Mark complete/failed (migration 025) is a classification only and
  touched neither the delete ladder nor arrsync's terminal transitions. Browser-unverified.
---

# Task: Active/pending holds an item until its whole pipeline finishes, not until lftp exits

**Follow-up to phase 1 of `docs/transfers-redesign-spec.md`**, from the user's browser review on
2026-08-20. Their observation, and it is correct:

> *"Shouldn't a job live in that state until the sonarr/radarr hook lands if they are enabled?
> Currently they move to complete but they technically aren't."*

## The problem

The two boxes currently split on **job termination** — lftp exits 0 and the row moves to Complete.
But the item's pipeline continues well past that: verify → extract → move → notify the *arr →
wait for confirmed import → delete the seedbox source → local cleanup. So a row sits under
"Complete" while the release is demonstrably not complete.

That contradicts what the spec says the Queue tab is *for* (§2): *"What is moving, and in what
order?"* An item awaiting import is still moving.

## The rule (decided with the user, 2026-08-20)

**Split on pipeline completion, not job termination — and apply it consistently whether or not a
queue is *arr-bound.** The user chose the consistent rule explicitly over a narrower *arr-only
one: one definition of "done", because post-processing on a large release is not instant either.

A row belongs in **Active/pending** while any of these hold:

- its job is `queued` or `running` (today's rule), **or**
- post-processing is in progress (`VERIFYING`/`EXTRACTING` — note these are protected by the live
  worker's existence via `PostprocessPipeline.in_flight_item_ids()`, **not** by the state string,
  so a crashed worker cannot wedge an item), **or**
- the queue is bound to an **enabled** *arr instance and `arr_status` is non-terminal
  (`detected`/`notified`/`dropped`), **or**
- a deferred source delete is still owed (`item.remote_delete_pending` non-null) **and has not
  been paused**.

Otherwise it belongs in **Complete**.

**Rather than one vague label, the row says what it is waiting on** — *Verifying*, *Extracting*,
*Awaiting import*, *Deleting source*. The point of this tab is to say what is moving and why. Derive
the label from the same predicate that does the splitting, in one place, so the label and the box
can never disagree.

## The thing that makes this dangerous — design it in from the start

**Every blocking condition must have a guaranteed terminal exit, or rows accumulate in Active
forever and the box silently stops being trustworthy.** Three concrete ways this bites, all real
in this codebase:

1. **A disabled *arr instance.** If the user disables Sonarr, every item sitting at `notified`
   blocks permanently. The test must be "bound to a **currently enabled** instance", not merely
   "`arr_status` is set".
2. **`gone` and `dropped`.** `dropped` holds amber for `DROPPED_GONE_GRACE_S` (6h) then commits
   `gone`; `gone` is terminal and must land in **Complete**, not block. Verify `dropped`'s own
   exit actually fires — `core/arrsync.py._check_dropped_items` runs every pass.
3. **A paused source delete.** `_sweep_stranded_source_deletes` gives up after
   `MAX_SOURCE_DELETE_RETRY_ATTEMPTS` and writes one `remote_delete_retries_paused` event, but
   **`remote_delete_pending` stays set** — deliberately, so a manual delete or a restart can still
   act. A naive "`remote_delete_pending` non-null ⇒ still in flight" test therefore blocks
   forever. Find how a paused retry is distinguishable and handle it; if it is **not**
   distinguishable from persisted state alone, say so and choose a bounded fallback rather than
   blocking indefinitely.

**Write a test for the property itself**, not just the cases: no row can remain in Active once
every pipeline actor has stopped working on it.

## The manual escape hatch (decided with the user, 2026-08-20)

Automatic exits are necessary but not sufficient — a genuinely wedged item needs a human override.
Add a small per-row control on **in-flight rows**: a menu offering **"Mark complete"** and
**"Mark failed"**, which resolves the row out of Active into Complete with that outcome.

**THE SAFETY CONSTRAINT, and it is not negotiable: a manual resolution is a CLASSIFICATION ONLY.
It must never be read as evidence of anything by any other subsystem.** Specifically it must
**not**:

- advance the `move`-mode delete ladder, or cause a seedbox source to be deleted;
- be treated as a confirmed *arr import, or write `arr_status`;
- trigger notify, cleanup, retention eligibility, or post-processing;
- suppress or alter auto-queue's own eligibility rules.

DESIGN.md §7.3 exists because a source delete is irreversible and waits on a *confirmed* import
held across two consecutive checks. A user clicking "Mark complete" on a hunch is not that. If
implementing this makes you touch `core/postprocess.py`'s delete gate or `core/arrsync.py`'s
terminal transitions, **you have gone wrong — stop and report.**

Implementation shape:

- A new nullable item column (migration **`025_`**) recording the manual outcome and when it was
  set — e.g. `manual_outcome TEXT` + `manual_outcome_at TEXT`. Read **only** by the
  classification predicate.
- The predicate treats a manually-resolved item as complete regardless of every other blocking
  condition. That is the whole point: it is the override of last resort.
- **Write an audit event** naming who resolved it and to what. This is a human overriding the
  system's own judgement; it belongs in the forensic trail alongside remote deletes and
  withheld deletes.
- **Reversible.** If the pipeline later genuinely completes (the *arr does import it after all),
  decide what happens — the honest options are "the manual outcome stands, it is already filed" or
  "a real terminal outcome supersedes the guess". Pick one, justify it, and record it.
  Consider also whether the user can simply un-resolve a row they resolved by mistake.
- The row must **show** it was manually resolved rather than silently looking like a normal
  completion — otherwise the audit trail says one thing and the UI another.

## The correctness risk unique to this task

**The two boxes must use ONE predicate.** The Active box is client-side over `list_jobs()`; the
Complete box is a **server-side paginated query** (`GET /api/jobs/complete`, added in stage 4b).
If the frontend's "still in flight" test and the server's "is complete" test are written
separately, they *will* drift — and a row will appear in **both boxes or neither**, with the
Complete box's `total` disagreeing with what is on screen.

Decide how to guarantee one definition. The most likely correct answer is that the predicate lives
**server-side**, with the Complete query excluding in-flight items and the classification exposed
to the client rather than re-derived there. If you choose differently, justify it and say how
drift is prevented.

This also means **the Complete box's server query must change** — it currently selects terminal
jobs without regard to pipeline state.

## Before you start

- `docs/transfers-redesign-spec.md` §2 and §3.2.
- `backend/lftpweb/core/queue.py` — `list_jobs()`, `list_complete_jobs()`, `dismiss_all_terminal`.
- `backend/lftpweb/core/postprocess.py` — `OWNED_STATES`, `in_flight_item_ids()`, and the module
  docstring on why transient states are protected by the worker's existence, not the state string.
- `backend/lftpweb/core/arrsync.py` — the module docstring (long, and worth it): `dropped`,
  `DROPPED_GONE_GRACE_S`, `_commit_terminal`, `_sweep_stranded_source_deletes`,
  `MAX_SOURCE_DELETE_RETRY_ATTEMPTS`, and `remote_delete_pending`'s lifecycle.
- `backend/lftpweb/models.py` — `JobOut` already carries `arr_status`, `arr_status_at`,
  `arr_instance_name`, `arr_instance_kind`. It does **not** carry the item's own `state`; that is
  likely a needed field addition.
- `frontend/src/pages/TransfersPage.tsx` — the two boxes, and `ArrRowChip` which already renders
  *arr outcome per row.

**Two other tasks may be in flight on `TransfersPage.tsx`** (a page-size selector, and a
dismiss-menu/count-readout change). Run `git status --porcelain` first; **if it is dirty, stop and
ask** rather than working around them.

## What to do

1. **Establish the predicate first, in one place, server-side unless you can justify otherwise.**
   Write it and its tests before touching any UI.
2. Exclude in-flight items from `list_complete_jobs()` and its `total`.
3. Expose the classification and the waiting-reason to the client.
4. Frontend: rows land in the right box; the reason shows on the row.
5. **Dismiss interaction:** an in-flight row is not dismissable — dismissing something still being
   worked on makes no sense. Confirm `isDismissable`'s behaviour here and make it consistent.

## Tests

The three exit traps above, each explicitly. The one-predicate property (a row is in exactly one
box). The "nothing blocks forever" property. Plus: a plain non-*arr queue's item still reaches
Complete once post-processing finishes.

## Docs

`DESIGN.md` §9.2; `CHANGELOG.md` under `[Unreleased]`; `docs/decisions.md` for the predicate's
home and the paused-delete choice; and **add this rule to `docs/transfers-redesign-spec.md`** — it
is a genuine extension of §3.2 that the original spec did not anticipate, so record it there
rather than leaving the spec describing a split that no longer exists.

## Conventions to honor

- **Never background a verification gate.** Foreground, explicit generous timeout (pytest ~3.5
  min; set the tool timeout to 600000 ms), read each exit code. A spawned agent receives no
  background completion notification and will stall forever — written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`,
  `uv run ruff format --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There
  is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **Surface, don't silently resolve.** If the pipeline turns out to have a state this rule cannot
  classify, say so rather than guessing.
- **You cannot render a page.** Say plainly what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
