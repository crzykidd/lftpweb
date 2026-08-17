---
name: 2026-08-17-stranded-source-delete-retry
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: |
  All three parts shipped, plus two same-day scope additions folded in (namespace-mismatch
  detection, scan-command outcome verification -- both closing the other half of the same
  "notify was too trusting" incident). Retry sweep + backoff + cleanup gate in core/arrsync.py;
  manual-delete widening in lib/fileTree.ts (canDeleteLocal moved + widened, shouldOfferLocalScope
  new) with FileTree.test.ts coverage. Full suites green: ruff check/format clean, pytest 1268
  passed, npm test 419 passed, npm run lint clean (pre-existing warnings only), npm run build
  clean. DESIGN.md/CHANGELOG.md/docs/decisions.md/prompts/startnewsession.md (row W) all updated.
  Unviewed in a browser -- no browser in this environment.
---

# Task: A failed rung-4 source delete must retry, gate cleanup, and stay manually deletable

Found live on BOTH the user's test and production systems, 2026-08-17, diagnosed from the
test system's audit trail (item `Bull.2016.S06E22.1080p.WEB.H264-CAKES`, ar-tv, `move` queue):

```
10:11:24  arr_imported             (import confirmed on two passes)
10:16:52  ERROR remote_delete_failed: delete of .../Bull.2016.S06E22... failed: SSH connection closed
10:16:52  arr_cleanup              (local copy removed anyway)
```

A transient SSH failure on the move-delete ladder's rung-4 deferred source delete
(`core/arrsync.py._maybe_delete_remote_on_import`) currently strands the remote copy
**permanently**: the delete only ever fires from `_commit_terminal`'s one-shot `imported`
transition, so it is never re-attempted; `_maybe_cleanup` doesn't check the debt and removed
the local copy anyway (violating the ladder's own "delete source → delete local" ordering);
and the resulting row (`REMOVED_LOCAL`, remote copy alive, `arr_status='cleaned'`) has **no
delete affordance in the UI** — `FileTree.tsx.canDeleteLocal` hides Delete for
`REMOVED_LOCAL`, which makes the v0.2.1 Source-scope checkbox unreachable exactly when it is
the escape hatch. The SSH pool had recovered within minutes (the next item's delete at
13:03:52 succeeded), so a single retry would have fixed this unattended.

One piece of good news to build on, verified: **on failure, `remote_delete_pending` is left
set** (`_maybe_delete_remote_on_import` only clears it on `ok`), so the debt is already
persisted. No migration needed.

## The fix (settled design) — three parts

### 1. Retry sweep, keyed off the debt, not the transition (`core/arrsync.py`)

In `_process_queue`, after the import-check loop, sweep this queue's items with
`remote_delete_pending IS NOT NULL` and a terminal-import `arr_status` (`imported` OR
`cleaned`) and `remote_deleted_at IS NULL`, and re-attempt the existing
`_maybe_delete_remote_on_import` for each (it is already guarded: no-ops when pending is
NULL or the delete already happened).

- **Backoff, not spam:** a bare every-pass retry writes a `remote_delete_failed` error event
  every ~60s while a seedbox is down. Reuse the module's existing bounded-retry/backoff
  conventions (`_maybe_retry_notify`'s pattern) — attempts spaced by a growing multiple of
  poll intervals is fine; the *final* state after exhausting attempts must still leave
  `remote_delete_pending` set (the manual path and the next lftpweb restart's fresh sweep
  can still act) and must write ONE clear event saying retries are paused, not silently stop.
- **Retroactive self-heal, required:** rows already stranded in an existing database
  (production has some now) match the sweep's own query — verify by test that a seeded
  `cleaned` + pending + remote-alive row gets its delete attempted on the first pass after
  upgrade, with no state massaging.

### 1b. A `gone` commit on a pending source delete becomes visible (`core/arrsync.py._commit_terminal`)

Production evidence (2026-08-17, `private_data/debug_logs/productionlftpweb.log`): 15 items
went `notified` → `gone`, each with `remote_delete_pending` still set — rung-4 never fires on
`gone` **by design** (ambiguity must not trigger an irreversible delete; do NOT change that),
so each source sits stranded silently. When `_commit_terminal` commits `gone` for an item
whose `remote_delete_pending` is non-NULL, extend the existing `arr_gone` event message (or
write one adjacent event — whichever reads better in History) to say the deferred source
delete remains withheld and manual deletion from the Files page is the intended path. Purely
audit-trail visibility; no behavior change.

### 2. Cleanup withholds while the source delete is owed (`core/arrsync.py._maybe_cleanup`)

Add the check: if `remote_delete_pending` is non-NULL (a `move`-queue item whose source
delete hasn't succeeded yet), withhold cleanup this pass via the existing
`_record_cleanup_withheld` (reason naming the pending source delete), and let the next pass
retry both in ladder order. This restores "import green → delete source → delete local" as
an enforced ordering rather than a hoped-for one. A `copy` queue (pending never set) is
untouched.

### 3. The manual escape hatch becomes reachable (`frontend/src/components/FileTree.tsx` + `lib/fileTree.ts`)

The Files-page Delete action must be offered when the row has local content **or** a
surviving remote copy (`hasRemoteCopy`), not only local content. For a no-local-content row
(`REMOVED_LOCAL` etc. with remote alive): the dialog opens with the Local checkbox absent or
disabled-with-hint (nothing local to delete) and the Source checkbox available (defaulted per
the existing `defaultSourceChecked` rule). The backend source-only path
(`POST /api/items/{id}/delete {"local": false, "source": true}`) already exists, already
clears a stale `remote_delete_pending`, and already suppresses re-auto-queue — this part is
UI-gating only. Keep the change in the pure helpers (`lib/fileTree.ts`) with tests; follow
the existing `canDeleteLocal` docstring style and update its reasoning rather than stacking
a second predicate nobody can reconcile with the first.

**Pin with a frontend test:** a `REMOVED_LOCAL` node with `remote_size` set offers the
delete action; a `REMOVED_BOTH` node (nothing anywhere) still offers nothing.

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan touches.
One unrelated untracked prompt file (`prompts/2026-08-17-logs-search-and-lookback.md`)
belongs to a separate queued task — leave it alone, exclude it from the proposed commit.
This file is exempt.

## Before you start

- `DESIGN.md` §7/§7.3/§7.4 (the ladder), §9.2. `docs/decisions.md`'s 2026-08-16 move-delete
  ladder and manual-delete entries — this task is the ladder's error-path completion.
- `tests/test_arrsync.py` (the rung-4 tests around `_FakeRemotePool`) and
  `tests/test_arr_cleanup.py` — extend, don't fork, their fixtures. `_FakeRemotePool` likely
  needs a fail-then-succeed mode for the retry test.
- `frontend/src/components/FileTree.test.ts` for the pure-helper test conventions.

## Tests (beyond those named above)

- Rung-4 delete fails (SSH error) → `remote_delete_pending` still set, cleanup withheld with
  an event, item state untouched; next pass (pool healthy) → delete succeeds, pending
  cleared, cleanup proceeds — full ladder order preserved across the failure.
- Backoff: repeated failures space out attempts and eventually write the one
  paused-retries event; pending remains set.
- Existing rung-4 and cleanup tests pass unmodified (the success path is byte-identical).
- Full suites green.

## Docs, same commit

- `CHANGELOG.md` `[Unreleased]` → Fixed: user-facing entry (transient seedbox failure no
  longer permanently strands a source copy; stranded rows self-heal; Delete reachable for
  rows with only a remote copy).
- `docs/decisions.md`: the sweep-not-transition choice, the backoff bound, the cleanup gate,
  the UI-gate widening. `prompts/startnewsession.md`: row **W** (after row V; the logs task
  queued behind this one will take X).
- `DESIGN.md` §7.3/§7.4: if the ladder text asserts the one-shot delete or unordered
  cleanup, correct it minimally to describe the retry + gate.

## Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full) · `npm test -- --run` · `npm run lint` · `npm run build`.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. Move this file into `prompts/done/` (or `failed/`).
3. Hand off ONE commit (prompt file + changes + prompt move). **You are a spawned agent: do
   not commit.** Prepare the tree, then report the file list + proposed `fix:` message back
   to the orchestrating session, which surfaces the `y/n`.
