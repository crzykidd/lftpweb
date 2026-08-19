---
name: 2026-08-19-rescue-requeue-keeps-queue-position
status: completed
created: 2026-08-19
model: sonnet
completed: 2026-08-19
result: "enqueue_item gained an opt-in queued_at override; both startup-rescue paths now carry
  the interrupted job's original timestamp forward, so a mid-download item resumes at its
  original queue position instead of the back of the line. 3 new tests, all gates green."
---

# Task: The startup rescue's re-queue keeps the item's original queue position

Production find (2026-08-19, support bundle `lftpweb-support-0.2.5-*`, first restart on
v0.2.5 with a heavy queue): S10 (job 203) was mid-download at 39.7 of 66.6 GB when the
container restarted for the upgrade. The new startup rescue (2026-08-18, `5b28a32`)
correctly re-queued it (`interrupted_requeued` at 03:58:32) — but `enqueue_item` stamps
a fresh `queued_at`, while jobs that were merely *queued* (not running) at restart
survive untouched with their original older timestamps. Scheduler order is
`rank DESC, queued_at ASC` (§4.5, `core/scheduler.py`), so the item that was actively
downloading with 40 GB of partial bytes went to the **back** of a long line, behind
everything that hadn't even started.

The fix, settled: the rescue carries the **interrupted job's original `queued_at`**
forward onto the re-queued job. A running item was by definition among the oldest in
line, so it naturally resumes at (or near) the front — and because `rank` is left
alone, it never jumps ahead of anything the user explicitly moved to top. Backdating is
also *honest*: the item genuinely has been waiting since that timestamp, so the
Transfers page's queued-wait readout tells the truth.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §4.5 (ordering: `rank DESC, queued_at ASC`, "Move to
  top" is a higher rank — this task must not touch rank semantics).
- Read before editing:
  - `backend/lftpweb/core/queue.py` — `enqueue_item` (how a job row is created and
    what stamps `queued_at`), `_requeue_interrupted_item` (~line 445) and the
    stranded-`DOWNLOADED` rescue path from `5b28a32`, both of which must pass the
    original timestamp through.
  - `core/scheduler.py`'s `QueuedJob` ordering docstring (~lines 54–77) — the
    contract this restores rather than modifies.
  - `tests/test_queue_orphans.py` — yesterday's rescue coverage you'll extend.

## Working tree check

Run `git status --porcelain` before editing; cross-reference; ask before touching
dirty files. This prompt file is exempt.

## What to do

1. **`enqueue_item` gains an optional `queued_at` override** (default `None` = today's
   now-stamp, so every existing caller is byte-for-byte unaffected — same
   opt-in-parameter pattern as `perform_remote_delete`'s `caller`). Validate/passthrough
   only; no other behavior change in the normal path.
2. **Both rescue paths pass the original job's `queued_at`:**
   - The just-marked-INTERRUPTED re-queue uses that job's own `queued_at`.
   - The stranded-`DOWNLOADED` startup rescue uses the *most recent* interrupted
     job's `queued_at` (the row it already keys off).
   The `interrupted_requeued` event message gains a short clause noting the position
   is preserved ("re-queued at its original position, not the back of the line").
3. **Idempotence check:** confirm `enqueue_item`'s existing duplicate/active-job
   guards behave identically with the override (they key off item/job state, not
   `queued_at` — verify, don't assume).
4. **Tests** (extend `tests/test_queue_orphans.py`): an interrupted item re-queued
   among pre-existing queued jobs with older timestamps ends up ordered *ahead* of
   them (assert via the scheduler's own ordering — build the `QueuedJob`s or query
   order the way the admission path does, not by re-implementing the sort);
   `enqueue_item` without the override still stamps now (regression on the default);
   the stranded-`DOWNLOADED` path also carries the timestamp.
5. **Docs, same commit:** `CHANGELOG.md` under Unreleased `### Fixed` (user-voiced: a
   transfer interrupted by a restart now resumes at its original place in the queue
   instead of dropping to the back behind everything that hadn't started).
   `docs/decisions.md`: the preserve-`queued_at`-not-boost-`rank` choice and why
   (honest wait-time display; never outranks an explicit Move-to-top).

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from the repo root, ~3.5 min — timeout 400000ms; never background a
  gate or wait on Monitor notifications), exit codes read: `uv run --project backend
  ruff check`, `uv run --project backend ruff format --check`, `uv run pytest`;
  frontend untouched — re-verify anyway (`npm run lint`, `npm test`,
  `npm run build`).
- Comment style: dated, incident-citing.
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Frontmatter: `status: completed` (or `failed`), `completed` date, one-line
   `result`.
2. Move this file into `prompts/done/` (or `prompts/failed/`).
3. Hand off ONE commit (prompt file + changes + move). Present file list + one-line
   message. **You are a spawned agent: do not commit, never `git add -A`, never
   push.** Branch is `dev`.
