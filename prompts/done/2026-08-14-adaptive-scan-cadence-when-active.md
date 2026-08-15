---
name: 2026-08-14-adaptive-scan-cadence-when-active
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Implemented. Two per-queue clocks (_next_due for full scans, unchanged by activity;
  _next_local_due for a new ~5s local-only pass while queue_is_active) driven by the existing
  single _loop/asyncio.wait_for scheduler. Local-only passes reconcile a fresh local walk
  against a cached remote tree (Engine._cached_remote_tree, populated only on a successful
  full scan) and call _persist with fingerprints=None. Found and fixed a settle-gate
  enforcement gap the prompt's literal "skip the bookkeeping entirely" wording would have
  reopened: _persist now always loads prev_settle and, on a local-only pass, reads (but never
  advances/writes) the last-persisted settle verdict so a DOWNLOADED reading is still gated
  correctly -- see docs/decisions.md for the full story and the deliberately-broken-then-fixed
  test proof. 24 new tests in tests/test_engine_adaptive_cadence.py, all pre-existing tests
  (test_engine_scan_cadence.py included) untouched and passing. Full suite: 927 passed. ruff
  check + format clean; npm lint/test/build clean; all three docker-compose files validate.
---

# Task: Scan a queue every ~5s while something is actually happening in it

A queue's scan interval is one fixed cadence (default 30s, per-queue since migration 009). When
a transfer is running, an item is settling, or post-processing is working, the UI can lag reality
by most of that interval. The user's rule, verbatim: **"local refresh 5 seconds if there is an
active job, arriving, downloading etc."**

Make the cadence adaptive: the configured interval when a queue is idle, ~5s while it is active.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §5 (scan cadence) and §1.3.
- Read `core/engine.py`'s `scan_queue`, `_persist`, `_is_due`/`_schedule_next`, and
  `effective_scan_interval`; `core/settle.py`; `core/progress.py`.
- **Read `docs/decisions.md`'s phase 2 entry on collapsing the cadences.** `DESIGN.md` §5
  originally specified *separate* 30s remote / 10s local cadences and phase 2 merged them into
  one. That was recorded as a simplification, not as a finding that the split was wrong — so this
  task is closer to restoring an original intent than to inventing something new. Say so if you
  end up re-splitting them.

## The constraint that matters more than the feature

**The settle fingerprint is computed from the remote tree, and `advance_settle` must only ever
run on a pass that actually re-read the remote.**

If you implement the fast cadence by reusing a cached remote tree for local-only passes, and
those passes still call `advance_settle`, then `matched_scans` inflates off a *stale* remote
reading and the settle gate silently weakens — an item reads "settled" without its remote
subtree ever having been re-observed. That is the same class of defect as
`prompts/open-issues.md`'s partial-scan rule (a scan carrying a warning must never advance the
counter) and it would reintroduce, in a new disguise, exactly the directory-corruption bug the
settle gate exists to prevent.

So: **a pass that does not re-read the remote must not touch `item_settle` at all.** Not advance
it, not reset it, not hold it — skip the bookkeeping entirely, exactly as `_persist` already does
when `fingerprints is None`.

Note that speeding up the *remote* cadence is safe for the gate by construction:
`SETTLE_MIN_AGE_S`'s 60s wall-clock floor exists precisely so a faster-polling queue cannot get a
weaker guarantee (`core/settle.py:64-81`). Do not weaken that floor to make this feature feel
snappier.

## Working tree check

Run `git status --porcelain` first. Other queued work touches `core/queue.py`, the frontend
Files/Transfers pages, and the docs pages. If any file this plan needs is dirty, list it and ask
before editing. This prompt file is exempt.

## What to do

### 1. The shape is decided: fast passes are LOCAL ONLY

**Decided by the user, 2026-08-14: the 5s pass re-scans only the local tree.** The remote keeps
its configured cadence. Running a full `scan_queue` every 5s would mean an SSH round trip and a
whole-tree `find` on the seedbox every 5 seconds per active queue, which is not a cost worth
paying for a display refresh. Do not "simplify" this back into a fast full pass.

This restores `DESIGN.md` §5's original separate-cadence design (30s remote / 10s local) that
phase 2 collapsed — note that in `docs/decisions.md` rather than presenting it as new invention.

It also means the risky parts are all on this path, so treat the following as hard requirements,
not preferences:

- **A local-only pass reconciles against the *cached* remote tree from the last real remote
  scan.** `core/engine.py.scan_queue` does not currently retain it — you will need to, per queue.
- **Never run a local-only pass before that queue has completed at least one successful full
  scan.** With no cached remote tree, `reconcile` would see an empty remote and `_persist`'s
  vanished-row handling could mark a queue's entire tree as removed. Guard this explicitly and
  test it.
- **A local-only pass must not touch `item_settle`** — see the constraint section above. Pass
  `fingerprints=None` so `_persist`'s existing bookkeeping is skipped wholesale, rather than
  adding a new conditional inside it.
- **Invalidate or refresh the cached remote tree when the real remote scan runs**, including when
  that scan comes back with a partial-scan warning. A cached tree that outlives its own scan's
  error handling is a stale-data bug waiting to happen.

### 2. Define "active" from persisted state, not guesswork

A queue is active when any of these is true for it:

- a `job` row in `queued` or `running`
- an item in a transient lifecycle state — `DOWNLOADING`, `VERIFYING`, `EXTRACTING`
- an item held by the settle gate (`state = 'REMOTE_ONLY'` with `substate = 'settling'`) — the
  "arriving" case the user named explicitly
- post-processing in flight (`PostprocessPipeline.in_flight_item_ids()`)

Compute it with one cheap query per scheduling decision, not by threading new state through the
engine. Put the predicate in a named function so it is testable and so the definition lives in
one place.

### 3. Wire it into the existing scheduling

`Engine` already has per-queue due-time scheduling (`_is_due`/`_schedule_next`, and
`effective_scan_interval` resolving per-queue vs site default). Extend that resolution rather
than adding a second timer loop: the interval for the next pass becomes `min(configured,
ACTIVE_SCAN_INTERVAL_S)` when the queue is active.

Keep `_schedule_next`'s existing "schedule from this queue's own completion time" behaviour — its
docstring explains that it is what prevents an overrunning scan stacking a second one of itself.
A 5s cadence makes that property matter *more*, not less: if a scan takes 8s, you must not queue
scans faster than they complete.

`ACTIVE_SCAN_INTERVAL_S = 5.0` as a named module constant, not a bare literal. Do not add a
settings row for it in this task; if it proves worth configuring, that is a follow-up.

### 4. Do not duplicate what already updates at 1 Hz

`core/progress.py`'s sampler already publishes progress for the *actively transferring* item at
~1 Hz, independent of the scan loop. This feature is not about that. Its value is everything the
sampler does not cover: sibling items, the transition to `DOWNLOADED` after a job reaps, and the
verify/extract state changes.

**Do not make this a second progress path** for the active item. If you find yourself publishing
progress-shaped data from the scan loop, stop — that is the sampler's job and duplicating it is
how the two disagree.

## Testing

- Assert the "active" predicate over every state it is meant to catch, plus an idle queue.
- Assert cadence resolution: idle uses the configured interval; active uses 5s; a queue
  configured *faster* than 5s keeps its own faster interval (`min`, not "replace").
- Assert a queue configured with no timer (`scan_interval_s = 0`, on-demand only) is **not**
  dragged into a 5s timer by becoming active — check what `effective_scan_interval` does with 0
  today and preserve that meaning.
- If you chose design B, add an explicit test that a local-only pass leaves `item_settle`
  byte-for-byte unchanged.
- Run `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`, `gen_key.sh` first),
  `ruff check` **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` entry — this changes how often an install talks to its seedbox, which an
  operator may notice.
- **You cannot see the UI** — no browser exists here. Claims mean "builds, type-checks, lints,
  endpoints verified over HTTP", never "renders correctly."

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
