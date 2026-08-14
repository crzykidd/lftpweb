---
name: 2026-08-13-reset-item-tracking
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Shipped "Reset item tracking" (core/local_delete.py: reset_item/reset_queue/reset_by_pattern/
  reset_pattern_matches, all built on the shared _reset_targets primitive), a violet-accented
  Files-page action distinct from Delete and from History's Clear. Three scopes: selected
  items (FileTree.tsx bulk action), whole queue (typed confirmation, api/jobs.py's
  reset_queue_all), and purge by filename pattern (single-queue only, live preview via
  reset_preview/reset_by_pattern, mid-task addition). All three tables (item, item_settle,
  deleted_archive) cleared together, with deleted_archive's subtree computed independently of
  item's own (a dedicated test caught this trap during implementation). New
  Engine.forget_rel_paths() evicts reset rows from the in-memory model and republishes over
  the existing queue_delta shape. Refuse-not-race guards (active job / postprocess in-flight /
  DeleteInFlight), per-target withholding for multi-target scopes. 25 new tests
  (tests/test_item_reset.py, tests/test_reset_api.py); 882 total passing (857 baseline + 25).
  Both ruff gates and npm run lint/build clean. DESIGN.md, CHANGELOG.md, docs/decisions.md
  updated.
---

# Task: A real reset — forget an item entirely so its path can be reused clean

User request, 2026-08-13, after hitting this three separate times (a reused directory name, a
cross-queue test, and clearing history and finding the item still suppressed):

> I need a way to requeue file names clean and reuse them. we should have something that says
> "clean history" and it should have big warning that anything in a copy config will try to
> redownload. we can have a warning on this but man it is important I think

## What is missing today

Suppression can only be cleared by **starting a download** (`enqueue_item` clears it as a side
effect). There is no way to say "forget you ever saw this path." So a path that was once
stopped, deleted, or permanently failed carries that decision forever — and since `item` rows
are keyed `(queue_id, rel_path)`, a *new* release arriving at the same name inherits it.

`48ad72c`'s Clear History deliberately does **not** touch item rows — that was the right call
for that feature (clearing records must not change behaviour) and it is why the user's item was
still suppressed afterwards. This task is the other half, and it must be unmistakably distinct.

## Naming — do not call it "clean history"

The user's phrase, but "Clear history" already exists a few pixels away and does something
completely different. Two near-identical names with wildly different blast radii is a footgun
on the more dangerous of the two.

Name the destructive one for what it destroys — **"Reset item tracking"**, "Forget these
items", or similar. Say plainly in the UI how it differs from Clear History. The exact wording
is yours; the requirement is that nobody can confuse them.

## Scope: selected items and whole queue

- **Selected items** (Files page multi-select already exists, with shift-range and bulk
  actions). This is the surgical, everyday case — "let me reuse this one name" — and probably
  what gets used most.
- **A whole queue**, for the clean-slate case.

## What a reset must actually clear — all of it

Deleting the `item` row alone leaves a half-reset that behaves strangely:

- **`item`** — the row itself, which is what resets identity.
- **`item_settle`** (migration 007, `PRIMARY KEY (queue_id, rel_path)`) — cascades from
  `path_queue`, **not** from `item`, so it survives an item deletion. A stale settle record
  means the fresh item inherits someone else's fingerprint and scan count.
- **`deleted_archive`** (migration 010, same key shape, same cascade) — **this one is the
  trap.** A stale row here makes a freshly re-downloaded archive read `EXCLUDED` immediately,
  because `core/engine.py.build_scan_counts_predicate` folds it into the completeness seam. A
  reset that misses this produces an item that downloads and then looks wrong.

Check for any other `(queue_id, rel_path)`-keyed state before you finish; those three are the
ones known at time of writing.

## The consequence to warn about, computed from their actual config

**On a `copy`-mode queue with auto-queue enabled, every reset item whose remote copy still
exists will begin downloading again within one scan interval.** That is the feature working as
intended, and it is exactly what the user wants to be warned about.

A generic warning is much less useful than a specific one. The app knows: which queues are
`copy` versus `move`, whether `auto_queue_enabled` is on per queue, and how many of the
selected items still have a remote copy. **Say the real number** — "12 of these 14 items still
exist on the seedbox, and auto-queue is on for this queue, so they will start downloading again
within 30 seconds" beats "this may cause re-downloads" by a wide margin.

Also state plainly:

- **Local files are not deleted.** This resets tracking, not data. People will assume otherwise
  given where the button lives.
- **Transfer history for these items goes too.** `job.item_id` is `ON DELETE CASCADE`
  (`001_initial_schema.sql:109`), so deleting an item deletes its jobs; `event.item_id` is
  `ON DELETE SET NULL` (line 139), so events survive but lose their link. This is unavoidable
  without denormalising first (see
  [issue #1](https://github.com/crzykidd/lftpweb/issues/1)) — so say it rather than surprising
  someone.
- On a **`move`** queue, completed items already had their remote copy deleted, so most will
  not come back. The warning should reflect the queue's actual mode rather than assuming the
  worst case.

**Require a deliberate confirmation** — this is the most destructive action in the app. A
typed confirmation is defensible for the whole-queue variant; for a handful of selected items
a clear confirm panel with real numbers is probably enough. Decide and say why.

## Do not

- **Do not delete local files.** If someone wants that, Delete already exists and is separate.
- **Do not fold this into Clear History**, or put it on the History page. It belongs where the
  items are.
- **Do not turn auto-queue off as a "safety measure."** Silently changing an unrelated setting
  to protect someone from an action they confirmed is its own bug.

## Before you start

- `core/local_delete.py` — the existing delete primitive, its guards, and `DeleteInFlight`.
  Reuse the guards that apply (active job, in-flight post-processing) — resetting an item
  mid-transfer should be refused, not raced.
- `core/engine.py.build_scan_counts_predicate` and how `deleted_archive` reaches the reconciler.
- `core/settle.py` and `item_settle`'s lifecycle.
- `48ad72c`'s Clear History, so the two read as clearly different.
- `frontend/src/components/FileTree.tsx` — multi-select, bulk actions, the confirm panel
  pattern.
- `prompts/open-issues.md` and [issue #1](https://github.com/crzykidd/lftpweb/issues/1).

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## Tests

- A reset item is genuinely gone from `item`, `item_settle`, and `deleted_archive` — assert on
  all three, since missing one is the most likely defect.
- The path is then treated as brand new: next scan creates a fresh row, unsuppressed, and
  auto-queue picks it up if enabled.
- **Local files still exist afterwards.**
- An item with an active job or in-flight post-processing is refused, not raced.
- A previously `EXCLUDED`-because-`deleted_archive` path reads normally after reset — this is
  the trap case above.
- Whole-queue reset clears every item of that queue and nothing from any other queue.

## Conventions to honor

- `docs/decisions.md`, newest at top — the naming decision, and why this is separate from Clear
  History.
- `CHANGELOG.md` under `### Added`; `DESIGN.md` §9.2 (standing approval to edit directly).
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up (857 pass today).
- **You cannot see the UI.** You cannot judge whether the warning reads as alarming enough. Say
  so plainly.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, the name you
   chose and the exact warning text, what confirmation you required and why, everything the
   reset clears, test count, lint results, and anything not fixed. Never `git add -A`, never
   push.
