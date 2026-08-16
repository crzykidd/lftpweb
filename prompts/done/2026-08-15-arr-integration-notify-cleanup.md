---
name: 2026-08-15-arr-integration-notify-cleanup
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  core/arrnotify.py (new): one notify_arr() shared by both callers -- path translation
  (NULL passthrough, prefix replacement, checks staging_path before local_path so a
  post-move item translates correctly), the *arr POST, arr_notified/arr_notify_failed
  events, gated on item.arr_status == 'detected'. PostprocessPipeline gains config_dir +
  _maybe_notify_arr, called from _process_item's tail only on a branch that reached a
  stable, fully-succeeded resting place (never on a withheld rename or a failed move).
  ArrSyncScheduler gains _maybe_retry_notify (bounded, MAX_NOTIFY_RETRY_ATTEMPTS=5,
  in-memory counter, gated on item.state being a finished outcome + no active job) and
  _maybe_cleanup (withheld on CORRUPT verify or an active job, re-evaluated every pass;
  otherwise auto_queue_suppressed=1 set before any disk touch, then bytes removed via
  local_delete._physical_local_root + _do_remove_from_disk -- deliberately never writes
  item.state, so the existing scan + mount_sentinel absence-grace machinery discovers the
  disappearance and carries it to REMOVED_LOCAL on its own clock, matching the spec's
  "downloaded -> processed -> (countdown) -> gone" UX; see docs/decisions.md for the full
  reasoning on why this reads "the existing local-deletion machinery" narrowly). main.py
  wires config_dir into PostprocessPipeline and in_flight_provider/delete_in_flight into
  ArrSyncScheduler. tests/fake_arr.py gained FakeArrState.fail_command (fail only
  POST /api/v3/command, distinct from fail_all which also fails /queue and would never
  let a notify/cleanup pass run at all). 25 new tests across tests/test_arr_notify.py and
  tests/test_arr_cleanup.py. All 4 verification gates green: ruff check, ruff format
  --check, pytest (1125 passed, 0 skipped), frontend lint/test/build (untouched,
  re-verified).
---

# Task: Sonarr/Radarr integration — notify + cleanup (phase B of 3)

Add the two active behaviors on top of phase A's foundation: the post-processing
"import now" push to the *arr, and the delete-after-import cleanup with its safety
gates. **This is the phase that deletes user data — the safety requirements in the spec
are the point of the phase, not overhead.**

## Before you start

- Read **`docs/arr-integration-spec.md`** end to end, especially "Path namespaces",
  "Notify", "Cleanup", and the lifecycle section's fully-done requirements. The spec
  wins over this prompt on any disagreement.
- Phase A (`prompts/done/2026-08-15-arr-integration-backend.md`) is committed — read its
  result note and the modules it created (`core/arrclient.py`, `core/arrsync.py`,
  `api/settings_arr.py`, migration 018).
- Study before writing:
  - `backend/lftpweb/core/postprocess.py` — `process_item`'s success tail (after verify /
    extract / rename / move) is where notify hooks in. Note how the delete gate writes
    withheld events; cleanup copies that discipline.
  - `backend/lftpweb/core/local_delete.py` — **`_physical_local_root` is the one resolver
    for where an item's bytes actually are. Use it; never write a second one.** (This
    assumption has caused five separate defects; see `prompts/startnewsession.md`.)
  - How the manual/retention local-delete path handles suppression and state, so cleanup
    reuses rather than reimplements.

## Working tree check

Run `git status --porcelain` before editing. This run is authorized unattended: if a file
you must touch is dirty, STOP and report back. This prompt file is exempt.

## What to do

1. **Notify** — at the tail of a fully successful post-processing pass (after the
   `.downloading-` rename AND any Move relocation), for an item whose queue is bound to
   an enabled instance with `notify_on_complete`:
   - Compute the item's final **physical** path, translate it into the *arr's namespace
     by replacing the queue's `local_path` prefix with `arr_visible_path` (no-op when
     NULL), and POST the scan command (`DownloadedEpisodesScan` / `DownloadedMoviesScan`,
     `importMode: "Copy"`) via phase A's client.
   - Success → `arr_status = notified` + `arr_notified` event. Failure → `arr_notify_failed`
     event, non-fatal, bounded retries on subsequent poller ticks (CDH may import anyway).
2. **Cleanup** — in `core/arrsync.py`, for items whose association reached `imported`
   (phase A's three-layer fully-done detection) on a queue with `arr_delete_completed`:
   - Withhold (with an `arr_cleanup_withheld` event naming the exact reason) when: the
     item's verification had FAILED, or the item has an active job. Withheld is
     re-evaluated on later passes, not terminal.
   - Otherwise: set `auto_queue_suppressed = 1` FIRST, then delete the local tree via the
     existing local-deletion machinery (resolving through `_physical_local_root`), then
     `arr_status = cleaned` + `arr_cleanup` event.
   - The cleaned item must ride the existing ~10-minute removal-grace machinery and stay
     visible through it (the spec's "no new timer" rule) — verify the normal
     absence-grace flow picks it up; do not special-case a bypass.
3. **Tests** (extend phase A's fake-*arr fixture):
   - **The headline test:** a slow multi-file import — queue record in
     `trackedDownloadState: importing`, per-file history events accreting across several
     poller passes — must never trigger cleanup; cleanup fires only after record-gone +
     history + two quiescent passes. This is the 40 GB-season-pack-over-slow-network
     scenario the user explicitly raised.
   - Path translation: NULL passthrough, prefix replacement, and the post-move case
     (item relocated by the Move step → translated path is the *post-move* location).
   - Withheld cases (failed verify, active job) each write their event and do not delete.
   - Suppression is set before deletion (assert ordering or at least final state).
   - `gone` items are never cleaned, ever.
   - Notify failure is non-fatal and retried; notify fires only after full postprocess
     success, never on a failed pipeline.

## Conventions to honor

- Defaults OFF everywhere; nothing changes for an install that hasn't opted in.
- Never delete on ambiguity — when in doubt, withhold with an event explaining why.
- Update the "*arr integration build run" table in `prompts/startnewsession.md` with this
  phase's row. Record non-obvious decisions in `docs/decisions.md`.

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — note skip counts honestly.
4. `cd frontend && npm run lint && npm test && npm run build` (untouched; prove it).

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (success) or `prompts/failed/` (failure).
3. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. The orchestrating
   session commits. Never `git add -A`, never push.
