---
name: 2026-11-27-f3-vanished-remote-only-ghosts
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: "vanished REMOTE_ONLY rows resolve to REMOVED_BOTH on fresh full scans only (remote_fresh); tests and docs added"
---

# Task: F3, resolve items that vanish while `REMOTE_ONLY`, and clean up the existing ghosts

This comes from a live test round. Read **F3** and the **F2 follow-up** table in
`prompts/test-findings-2026-11-27.md` first.

The test system's seedbox `complete/ar-tv` holds **1** entry, but Files lists **199** top-level
`REMOTE_ONLY` items, plus 307 nested rows. Of those 199:
- 197 are SAB `_UNPACK_<name>` staging folders. SAB renames each one away when extraction
  finishes.
- The other 4 are real releases that are now gone from the seedbox.

**The bug:** in `core/engine.py`'s vanished-path loop in `_persist` (around line 1446), a path
missing from both trees whose `prev_state` isn't one `resolve_absence` handles is "left exactly
as it was". `REMOTE_ONLY` is one of those states, so it freezes forever.

**The user's hard rule is that nothing breaks for an existing install.** The only visible
change allowed is that ghost rows stop showing as "on the seedbox".

## Before you start

- Read `CLAUDE.md`, then `DESIGN.md`:
  - §7.3 and §7.4, the absence and grace rules
  - the scan and reconcile sections
- Read `core/mount_sentinel.py` (`resolve_absence`, `_STICKY_PREV_STATES`), and the whole
  vanished loop in `core/engine.py._persist`.
- Read `git log -p -1 a4a626d -- backend/lftpweb/core/engine.py`. That change stopped
  publishing terminal vanished rows.
- Work in **this checkout on `dev`**. Do not use a worktree. Ignore `.claude/worktrees/`.

## Working tree check

Run `git status --porcelain`. Only `.claude/worktrees/`, this prompt, and possibly
`prompts/test-findings-2026-11-27.md` should be dirty. If anything else is, stop and report.

## What to do

1. **Fix the vanished loop.** A path whose `prev_state` is `REMOTE_ONLY` and which is now in
   **neither** tree resolves to the same terminal "gone from both sides" outcome as any other
   vanished item. That means `REMOVED_BOTH`, unless you find a better existing state, in which
   case justify it. Apply this to its nested rows too, so the whole subtree resolves together.
   - **Only on a trustworthy full scan.** The local-only pass (`_scan_queue_local_only`) calls
     `_persist` with a **cached** remote tree and `partial_scan=False`. A cached tree is not
     fresh evidence that something left the remote. Resolve these rows **only** on a full scan
     whose remote listing was fresh and not partial. If `_persist` can't tell the two callers
     apart today, add an explicit parameter; don't infer it.
   - Make sure a failed or errored remote scan, an unreachable host, or the mount gate refusing
     never resolves anything, the same as the existing guards.
   - `PARTIAL`, `QUEUED` and other in-flight states stay as they are, as today. Only
     `REMOTE_ONLY` changes.
   - Check that every downstream reader of `REMOVED_BOTH` handles a row that was never
     downloaded: `core/itemview.py`'s facets, auto-queue suppression, the arr and clients
     matching, and the frontend chips. One example is the "removed by us" reasoning that
     `core/itemview.py` attaches. If a distinct reason is needed, such as "vanished from
     remote before download", add it in the itemview layer, not as a new `state` value.
2. **Clean up existing ghosts.** These rows are already frozen, so the fix in step 1 must reach
   them too. Confirm that it does: they sit in `previous`, are absent from both trees, and
   resolve on the next full scan. If so, no migration is needed. If not, add the smallest
   one-time path and explain it.
3. **Tests**
   - `REMOTE_ONLY` vanishes from both sides on a fresh full scan → resolved, nested rows too.
   - The same during a local-only pass with a cached remote tree → unchanged.
   - The same with a partial or failed remote scan → unchanged.
   - An `_UNPACK_<name>` dir renamed to `<name>` → the `_UNPACK_` row resolves, and `<name>`
     follows its normal path.
   - Existing tests for vanished `DOWNLOADED` / `EXTRACTED` / `REMOVED_LOCAL` and similar pass
     unchanged.
4. **Docs, in the same commit**
   - `CHANGELOG.md` `[Unreleased]` Fixed, in user words. For example: "Files no longer lists
     things that disappeared from the seedbox before they were downloaded, such as SABnzbd's
     temporary `_UNPACK_` folders. Existing leftovers clear on the next full scan."
   - `DESIGN.md` §7.3 / §7.4, wherever the vanished rules live.
   - A `docs/decisions.md` entry.
   - `prompts/test-findings-2026-11-27.md`: mark F3 **done**, and mark the minor
     "`_UNPACK_` folders appear" item **done** too, since it has the same cause.

## Conventions to honor

- Every verification gate runs in the **foreground** with an explicit timeout; for pytest use
  600000 ms. You will never receive a background-task notification, so read each exit code
  yourself.
- From the **repo root**, run each of these and read its exit code separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`. The fake seedbox containers are up, so the target is **0 skipped**. The
    baseline is 2446 passed.
- If you touch the frontend, also run from `frontend/`: `npm run lint`, `npx tsc -b`, and
  `npm test` (baseline 867).
- Report every gate's exit code and counts.
- Another finding, F4, is a periodic event-loop stall. Leave it alone; it is being diagnosed
  separately. But if you notice synchronous filesystem I/O on the event loop in the code you
  read, mention it in your report.

## When done

1. Set this file's frontmatter (`status`, `completed`, `result`) and move it into
   `prompts/done/`. It is untracked, so a plain `mv` is fine.
2. **Do not commit.** Report back:
   - what changed
   - anything that could behave differently for an existing install
   - the gate results
   - the full explicit path list for a single commit, including this prompt in
     `prompts/done/` and the findings file
   - a proposed one-line `fix:` message, with no `Co-authored-by` trailer
