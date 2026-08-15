---
name: 2026-08-13-delete-during-transfer
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  api/jobs.py.delete_item now always calls core/queue.py.TransferQueue.stop_item() (the same
  SIGTERM -> grace -> SIGKILL path Stop uses) before delete_local, bounded by
  STOP_BEFORE_DELETE_TIMEOUT_S=25s via a background asyncio.Task that is awaited but never
  cancelled on timeout (cancelling would abandon core/queue.py's own bookkeeping half-updated);
  a timeout withholds with a 409 and an event, naming why. delete_local's own "no active job"
  guard is unchanged -- the endpoint satisfies it itself first. Fixed a second, related gap:
  core/local_delete.py's loose-file delete branch and existence guard now also handle lftp's
  own in-flight `<name>.lftp` temp file and `.lftp-pget-status` sidecar, so a delete reaching a
  stopped-mid-transfer loose file doesn't leave those bytes behind under a different name.
  FileTree.tsx's existing delete confirmation gained a distinct "N of M is/are transferring
  now -- deleting will cancel it/them first" line, alongside (not replacing) the remote-copy
  line -- no second dialog. Verified (not assumed) that the resulting row reads
  suppressed_reason='deleted_local' (never the stop path's own 'user_stopped') and is never
  re-queued by auto-queue either way. 9 new tests (2 unit, 4 fast API-level, 3 e2e against the
  fake seedbox reproducing the mid-transfer delete directly); 760 passing overall. Both ruff
  gates, npm lint, and npm build clean. DESIGN.md §9.2 and docs/decisions.md updated;
  CHANGELOG.md Added entry added.
---

# Task: Let a delete cancel an in-progress transfer, with a confirmation that says so

User request, 2026-08-13:

> need to be able to delete a folder or file when in progress. currently says you can't.. but
> it should say active copy going. are you sure confirm. and then let the delete happen. I
> think and then a cancelled job doesn't get auto added again?

Today `core/local_delete.py.delete_local` withholds when the item has an active job, writes an
event, and the UI reports the refusal. The Files page *offers* the Delete button (`canDeleteLocal`
only excludes states with no local content, and a `DOWNLOADING` item has partial bytes), so the
user gets a button that then refuses — the worst of both.

## The guard is right; the ordering is what is missing

**Do not simply drop the active-job check.** Deleting a directory out from under a running
lftp process means it keeps writing into a tree being removed: files reappear after the
delete, the unlink races the writer, and on a mirror job lftp may recreate directories it is
mid-way through. The guard exists for that reason.

**The fix is to stop the transfer first and wait for the process to actually be gone**, then
delete. `core/queue.py` already implements SIGTERM → grace → SIGKILL stop semantics
(`stop_job`/`stop_item`) — reuse them, do not write a second stop path.

Order, and each step must complete before the next:

1. Stop the item's active job and **await the process's real termination**, not just the
   signal being sent. Reaping must have happened — a returned-but-still-dying process is
   exactly the race this ordering exists to avoid.
2. Persist the job's terminal outcome so no row is left `running` (a restart would otherwise
   see a phantom — `_reconcile_orphaned_jobs` exists because that has happened before).
3. Then run the existing delete path, guards and all.

If the stop cannot be confirmed within a bounded time, **withhold the delete and say why**.
Failing loudly is correct here; deleting anyway is not.

## Partial-download leftovers

lftp writes to a temp name during transfer (`xfer:use-temp-file`, `.lftp` suffix). For a
**directory** item, `rmtree` removes them along with everything else. For a **loose top-level
file** item, `delete_local` targets the item's own path — and a partially-downloaded file is
on disk as `<name>.lftp`, which will not match and will be left orphaned.

Handle it, and test it. A delete that leaves the very bytes it was asked to remove is the
feature failing at its one job. Check `core/local_scan.py` for how the suffix is already
recognised rather than hardcoding it again.

## Auto-queue must not pick it up again

The user's own question: *"a cancelled job doesn't get auto added again?"*

`delete_local` already sets `auto_queue_suppressed = 1` with `suppressed_reason =
'deleted_local'`, and §4.6's stop path sets suppression with `user_stopped`. So this should
already hold — **verify it rather than assuming**, and add an explicit test: an item deleted
mid-transfer must not be re-queued on the next scan, with `re_download_externally_removed`
both off and on.

Also confirm the two suppression reasons do not fight: whichever the stop writes must not be
clobbered into something that changes behaviour, and the final row must read as a deliberate
deletion, not a user stop.

## The confirmation must say what is about to happen

`FileTree.tsx`'s delete confirmation panel already reports count, total bytes, and whether a
remote copy survives. Add the transfer case:

- When any selected item has an **active job**, say so plainly — "1 of 3 is transferring now;
  deleting will cancel it" — and keep it distinct from the existing remote-copy wording rather
  than replacing it. Both facts matter at once.
- Bulk selections can mix transferring and idle items. Handle the mixed case explicitly, the
  same way the existing dialog already handles a mixed remote/local-only selection.
- The user asked for a confirmation, and one already exists — do **not** add a second
  confirmation step on top of it. Strengthen the wording, do not stack dialogs.

## Before you start

- `core/local_delete.py` — `delete_local`, its guards, `DeleteInFlight`, the `removing`
  substate added in `7dc045f`.
- `core/queue.py` — `stop_job`, `stop_item`, `_reap_one`, `_reconcile_orphaned_jobs`.
- `core/engine.py._protected_rel_paths` — items with a `queued`/`running` job are protected
  from state recomputation; make sure the transition out of that protection is clean.
- `frontend/src/components/FileTree.tsx` — `canDeleteLocal`, the confirmation panel.
- `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- **Delete a directory item mid-transfer** against the fake seedbox: the lftp process is really
  gone before any unlink happens, the tree is removed, no job row is left `running`, and
  nothing reappears on the next scan. This is the reproduction; do not ship without it.
- A loose top-level file mid-transfer: the `.lftp` temp file is removed too.
- The item is **not** re-queued by auto-queue afterwards, setting both ways.
- A stop that cannot be confirmed withholds the delete and writes an event naming the reason.
- The existing idle-item delete path is unchanged — this is the regression risk.
- Bulk delete over a mix of transferring and idle items reports honestly (phase 9's
  `Promise.allSettled` reporting is already there; do not regress it).

## Conventions to honor

- `docs/decisions.md`, newest at top — record why the guard became an ordering requirement
  rather than being removed.
- `CHANGELOG.md` under `### Added` or `### Changed` as fits.
- `DESIGN.md` §4.6/§9.2 — standing approval to edit directly.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, how you confirmed the
   process was really dead before unlinking, what you did about `.lftp` leftovers, which
   suppression reason wins, test count, lint results, and anything not fixed. Never
   `git add -A`, never push.
