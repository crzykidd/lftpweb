---
name: 2026-08-12-local-deletion-and-retention
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Built core/local_delete.py's delete_local() primitive (path containment via a new shared
  core/extract.resolve_within_root, active-job guard, in_flight guard, mount-sentinel guard,
  nlink guard as a caller parameter), the manual POST /api/items/{item_id}/delete endpoint,
  RetentionScheduler + dry-run preview + PUT/GET /api/settings/retention (default off),
  migration 008 (suppressed_reason CHECK widened to add 'deleted_local', via full item-table
  rebuild), core/engine.py._persist's downloaded_at COALESCE backfill, and FileTree.tsx's
  per-row + bulk Delete with a confirmation dialog. Also fixed issue 4 (REMOVED_LOCAL added to
  autoqueue.ELIGIBLE_STATES), safe because of the suppression marker this task added. "Delete
  remote" deliberately left out of scope. 26 new/changed tests, all green; ruff + npm lint/build
  clean. Not committed -- proposed commit reported back to the orchestrating session.
---

# Task: Delete local files — manually, and on a retention schedule

Two user requests plus one coupled bug, built as **one** task because they share a
primitive, a migration, and an audit path. Splitting them would produce two half-safe
implementations of the same irreversible operation.

**This is the second irreversible-delete feature in this codebase and the first that
touches the user's own data.** `move` mode set the bar: two-layer opt-in, forced
verification, an `event` row on every delete *and every withhold*, and a UI confirmation.
Meet it.

## Before you start

- Read `DESIGN.md` §7.1, §7.3, §7.4 (deletion and its gates), §9.2 (Files page), §4.6.
- Read `core/postprocess.py._maybe_delete_remote` — the existing delete gate. Every branch
  writes an event before returning; **there is no silent path.** Copy that discipline.
- Read `core/mount_sentinel.py` and `core/autoqueue.py.on_scan`'s blanket gate.
- Read `prompts/open-issues.md` § "7 + 8 — the deletion cluster". **That section is the
  specification.**
- Read `prompts/startnewsession.md`'s "⚠ Open items awaiting the user" — item 6 says
  nothing ever deletes `item` rows and that row lifetime is an unanswered design question.
  **This task must not answer it by accident.**

## Working tree check

Run `git status --porcelain`. This touches `core/`, `api/`, migrations, and the frontend.
If files you need are dirty, list them and ask before proceeding.

## Use migration number 008

`005` metrics, `006` `state_changed_at`, `007` settle gate. Use **`008`**.

## One primitive, two callers

Build a single `delete_local(item)`. Retention calls it on a schedule; the Files button
calls it on a click. Do not write it twice.

**Where the callers must differ — the `nlink > 1` guard.** If the user's `*arr` hardlinks
out of the download directory, a file with more than one link is provably safe to delete:
you are removing one link, not the data. A file with exactly one link is the only copy.

- **Retention: guard ON by default.** A robot deleting unattended should refuse when it
  cannot prove another copy exists.
- **Manual delete: guard OFF.** `LOCAL_ONLY` junk with exactly one link is precisely what
  the user is trying to remove.

A guard that is right for the robot is wrong for the human. Make it a parameter of the
caller, not of the primitive.

**Unresolved and not blocking:** whether the user's `*arr` hardlinks, copies, or moves out
of the local downloads directory. It shapes how often the retention guard fires, not
whether the code is correct. Note it in your report.

## Guards the primitive must enforce, every call

1. **Path containment.** Resolve the target and assert it is within the queue's
   `local_path`. A `LOCAL_ONLY` directory **can be a symlink**, and `rm -rf` through one
   pointing outside the download root is the worst possible outcome of this feature. Do
   not follow symlinks out. Test this with an actual symlink escaping the root.
2. **No active job** for the item.
3. **Not in `PostprocessPipeline.in_flight_item_ids()`** — the live-worker check, never the
   state string. (`prompts/startnewsession.md`: "a state that is merely protected is a
   state that can never be un-stuck".)
4. **Mount-sentinel gated**, like auto-queue. Never delete against a local root that is not
   really mounted.
5. **An `event` row for every deletion and every withhold**, naming the item, the caller,
   and the gating condition. History renders these messages verbatim — write them for a
   human reading an incident.

## The coupling with `REMOVED_LOCAL` — read this twice

Deleting local files makes local absent while remote persists → §7.3 grace period →
`REMOVED_LOCAL`. That state is **not** in `core/autoqueue.py`'s `ELIGIBLE_STATES`, so
auto-queue leaves it alone.

**Retention works today only because of that exclusion** — which is *also* the bug (issue
4) that stops legitimately-moved-away items from ever being re-queued. Fix one naively and
the other breaks: make `REMOVED_LOCAL` eligible and retention re-downloads everything it
just deleted, on a 30-second loop, forever.

Make the two absences distinguishable: both deletion paths set `auto_queue_suppressed = 1`
with a **new `suppressed_reason`**, reusing §4.6's existing mechanism rather than inventing
one. The column's `CHECK` currently allows only
`('user_stopped', 'retries_exhausted', 'permanent_error')` — extend it in migration 008.

Then decide whether to also fix issue 4 (letting a genuinely moved-away item become
eligible again) in this task. If you do, the suppression marker is what makes it safe. If
you judge it too large, leave issue 4 open and **say so explicitly** — do not half-do it.

## Row lifetime — do not answer it

**Delete files, keep `item` rows.** After the file is gone the path is in neither tree, so
`_project`'s `rel_paths` filter drops it and `diff_nodes` publishes it as `removed`; it
leaves the UI without anyone deleting a row. Set `REMOVED_BOTH` on the way out to keep the
state honest for History. Row lifetime stays an open question for the user.

## Retention specifics

- Key on **`downloaded_at`**, not `state_changed_at`. "When did it complete" and "when did
  it last move" are different questions; an item that dips `DOWNLOADED → PARTIAL →
  DOWNLOADED` would otherwise earn a fresh lease it has not earned.
- **`downloaded_at` is NULL for items that reached `DOWNLOADED` via reconcile** rather
  than a job reap — it is written in exactly one place, `core/queue.py:485`. So: in
  `core/engine.py._persist`, stamp it on the transition *into* `DOWNLOADED` when still
  NULL, `COALESCE`-style so a rescan can never overwrite a real job's timestamp with
  "now". Without this the feature silently ignores a large share of the user's library.
- **Default OFF.** Non-negotiable — this project ships new capabilities off, and deletion
  is not where to make the exception. (Scheduled backups were the one reasoned exception;
  they add files rather than remove them.)
- **A dry-run / preview endpoint** — "here is exactly what would be deleted, and the total
  bytes" — mirroring the existing pattern-preview endpoint rather than inventing an idiom.
- Same background-loop shape as `BackupScheduler`/`Engine`/`TransferQueue`
  (`_task`/`start()`/`stop()`, cancellable, one bad cycle must not kill the loop).

## Manual delete specifics

- Per-item and bulk. `FileTree.tsx` already has multi-select with shift-range and phase 9's
  `Promise.allSettled` honest partial-failure reporting for Queue/Stop — slot into that,
  do not build a parallel mechanism.
- **A confirmation dialog** showing the count and total bytes. Queue and Stop are
  reversible; this is not.
- This is the **first delete endpoint in the API**. There is no prior art here — no manual
  per-item or bulk delete exists anywhere. It is already behind `AuthMiddleware`'s
  default-deny; confirm that rather than assuming it.

## Explicitly out of scope

**"Delete remote."** Phase 9's gap list names both, and the temptation to add it while the
plumbing is open will be strong. Do not. The only remote deletion today is `move` mode's
verification-gated pipeline; a manual remote-delete button is a much larger safety
conversation. Say in your report that it was deliberately left out, so the next session
does not read the gap list and assume it was forgotten.

## Tests

- Path containment against a real symlink escaping the local root. **Non-negotiable.**
- Every guard, each in isolation, asserting the withhold event is written.
- The `nlink` guard both ways.
- Retention selecting on `downloaded_at`, including the reconcile-path backfill.
- Dry-run returns exactly what a real run would delete.
- An item deleted by retention is not re-queued on the next scan.

## Conventions to honor

- `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` under `## [Unreleased]` → `### Added`, stating plainly that retention
  defaults off.
- `README.md`'s "Known gaps" — update the entry saying Files has no delete.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up; tear down afterward, confirm `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, test count, lint results, whether you also fixed issue 4, and anything
   found but not fixed. Never `git add -A`, never push.
