---
name: 2026-08-14-rename-after-postprocessing-not-before
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Moved the download-prefix rename from core/queue.py._reap_one (ran before postprocess.trigger)
  to core/postprocess.py._process_item as the pipeline's own last step, gated on release_ok
  (verify != CORRUPT and extract != EXTRACT_FAILED). _reap_one now leaves
  item.pending_download_prefix set and DOWNLOADED alone when it hands off to postprocess.trigger
  -- core/postprocess.py is the only writer that ever clears the column now.
  core/postprocess.py._process_item resolves local_root via
  core/local_delete.py._physical_local_root (reused, not re-derived) once at the top; every step
  (verify/delete/extract/rename-or-move) operates on that. Four branches: prefixed+bad ->
  withheld (download_prefix_rename_withheld event, staging move withheld too if configured);
  prefixed+ok+move_effective -> _do_move relocates straight from the prefixed source to the
  already-unprefixed staging destination (no separate rename), clearing the prefix only on
  success (_do_move now returns bool); prefixed+ok+no-move -> new
  PostprocessPipeline._finalize_download_prefix does the standalone rename via move_tree
  (asyncio.to_thread, unlike the old queue.py copy) and clears the prefix; unprefixed -> untouched
  pre-existing behavior. A rename-conflict failure now just logs
  download_prefix_rename_failed and leaves the item where postprocessing put it (no PARTIAL
  downgrade path exists post-DOWNLOADED; said so explicitly rather than implying a retry that
  won't happen).
  Audited every local_path+rel_path path builder in the widened window per the prompt's
  instruction: core/extract.py needed no change (its _UNPACK_/_FAILED_ staging siblings are
  already computed relative to whatever root they're handed, physical or not -- verified with a
  new spy-based test). core/local_delete.py.delete_extracted_archives had a real bug -- it
  recorded a deleted archive's path relative to the physical (possibly prefixed) root instead of
  the item's logical rel_path, which core/engine.py's completeness accounting compares against
  verbatim -- fixed by resolving the physical root the same way and reattaching onto the logical
  rel_path. core/postprocess.py._find_item_id_for_failed_dir had the same bug class (stripping
  only FAILED_PREFIX would miss a still-prefixed _FAILED_<prefix><name> dir) -- fixed with a
  second SQL match candidate. Also found and fixed, not asked for but load-bearing: descendant
  item rows inside a mirrored release could flicker PARTIAL/REMOTE_ONLY during the new,
  longer-lived VERIFYING/EXTRACTING window (queue_is_active already keeps the 5s fast-scan
  cadence running through it) because core/engine.py._protected_rel_paths only protected
  descendants of an active-*job* parent, not an in-flight-*postprocess* one -- extended that
  clause to cover PostprocessPipeline/DeleteInFlight in-flight parents too.
  CORRUPT/EXTRACT_FAILED items are never renamed (the prompt's recommended choice, taken) --
  bytes stay under the prefixed name until a human retries. Frontend: ItemDrawer.tsx's
  physical-location note was flatly wrong for the new lifecycle ("will be renamed once the
  transfer completes" is false the moment verify/extract can fail) -- replaced with a
  state-aware downloadPrefixNote() covering downloading / still-postprocessing /
  permanently-hidden-after-failure. Settings -> Transfer and Settings -> Queues FieldHelp copy,
  and docs/quick-start.md, updated to match. DESIGN.md not edited, per this project's own
  convention -- the addendum is drafted in docs/decisions.md for a human to fold in.
  Verification: uv run pytest, 1004 passed (including the task's required single-most-important
  e2e assertion against the real fake seedbox -- verify.verify_item monkeypatched to sleep so the
  VERIFYING window is observable: real name absent while it runs, present after -- plus a second
  new e2e test for a CORRUPT item via a deliberately-wrong .sfv sidecar, and seven new unit tests
  directly against PostprocessPipeline). One pre-existing, unrelated e2e test
  (test_autoqueue_e2e.py) broke as a side effect of "folder prefix during transfer" defaulting on
  plus this task removing _reap_one's unconditional rename -- fixed by disabling the prefix
  feature in that file's shared db fixture, the same "isolate what this file actually tests"
  idiom it already uses for the settle gate. ruff check / ruff format --check clean, npm run
  lint / npm test (221 passed) / npm run build clean, docker compose config --quiet clean on all
  three compose files.
---

# Task: Rename a prefixed download onto its real name *after* post-processing, not before it

"Folder prefix during transfer" currently renames `<local_path>/<prefix><name>/` onto its real
name at the completeness check (`core/queue.py._reap_one`, ~line 762) and only then triggers
post-processing (~line 851). So a release appears under its real name **before it has been
verified**, and stays there for however long verification takes.

Measured, from the live instance: a 1.7 GB item took **7.7 seconds** to verify (the hash-on-disk
fallback reads every byte). A 21 GB release is therefore exposed for something like a minute and
a half. If verify returns `CORRUPT`, an importer watching that directory has had the whole window
to take it — which is the exact scenario this feature exists to prevent.

Move the rename to the end: nothing appears under its real name until it is downloaded, verified,
and extracted.

## This reverses a decision made hours earlier — read it first

`prompts/done/2026-08-14-in-flight-folder-prefix.md` chose the current ordering deliberately, and
`docs/decisions.md` records the reasoning: the setting is named "during transfer", by the time
`_completeness_on_disk` passes the transfer is genuinely over, and delaying the rename would make
`core/postprocess.py` — the module that deletes and moves data — prefix-aware mid-pipeline.

That reasoning optimised for the setting's *name* over its *purpose*. The purpose is that no
importer ever sees a release that is not ready, and an unverified release is not ready. **Say so
explicitly in `docs/decisions.md`** — name the earlier entry, state what changed (a user watched
a real 21 GB transfer and asked why verification runs after the rename), and do not quietly
overwrite the old reasoning.

## What still argues for the old order, and must be preserved

The completeness check already runs before the current rename and is not being moved: no leftover
lftp temp files anywhere under the item, and local bytes ≥ the relevant remote total. Whatever you
do, **an item must still not reach `DOWNLOADED` without that check passing** — this task changes
*when the directory is renamed*, not what gates completion.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §6 and §7.
- Read `core/queue.py._reap_one` and `_finalize_download_prefix` in full.
- Read `core/postprocess.py` end to end — especially `process_item`'s `local_root`, the
  `move_tree` staging→final step, and `_maybe_delete_remote`'s verification gate.
- Read `core/extract.py`'s `_UNPACK_`/`_FAILED_` sibling-staging placement.
- Read `core/local_delete.py._physical_local_root` (2026-08-14) — it already resolves an item's
  physical root from `item.pending_download_prefix`, including for a nested child, and is the
  established way to answer "where are this item's bytes actually". **Reuse it rather than
  writing a second resolver.**

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## What to do

### 1. Move the rename to the end of the pipeline

Post-processing should run against the **prefixed** root, and the rename onto the real name
should be the last thing that happens on a successful item.

Order to land on (confirm against the real code rather than trusting this sketch):
completeness check → verify → extract → **rename** → move-to-final-destination, with the
`move`-mode remote delete staying exactly where its verification gate puts it today.

Work out where the rename sits relative to the `staging_path` move specifically. If a queue has a
final destination configured, the item is relocated out of `local_path` anyway — decide whether
the rename is then redundant or still required, and say which in `docs/decisions.md`.

### 2. Decide what a failed item looks like on disk, and say so

If verify returns `CORRUPT` or extraction fails, the item never gets renamed and its bytes stay
in a hidden `<prefix><name>/` directory. That is the safe outcome and it is the point — but it is
also a behaviour change someone will hit while confused, so it needs to be deliberate:

- The Files row is unaffected either way (`item.rel_path` never carries the prefix), and the item
  drawer already shows the physical path — **verify both of those claims** rather than assuming.
- If the drawer does not make the physical location obvious for a failed item, make it.
- Consider whether a `CORRUPT`/`EXTRACT_FAILED` item should be renamed anyway so a human finds it
  where they expect. **Recommendation: no** — an importer would find it there too, and that is the
  whole problem. But state the choice.

### 3. Keep every existing guard intact

- `_completeness_on_disk` still gates `DOWNLOADED`; a failed rename must still mark the item
  incomplete rather than letting it pass (the current code sets `complete = False` on rename
  failure — preserve that shape wherever the rename ends up).
- `move`-mode remote deletion stays gated on verification. Do not let this reordering create any
  path where the remote copy is deleted before verify has passed.
- `_UNPACK_` staging must remain **outside** the tree the reconciler walks and outside anything a
  later move relocates. Work out where the sibling lands when the item itself is inside a prefixed
  directory, and confirm it is still filtered from `scan_local`.
- Extraction's archive-cleanup step, and `deleted_archive` bookkeeping, are keyed on `rel_path` —
  check they are unaffected by the physical root changing.

### 4. Watch for the assumption this feature keeps breaking

Three defects have already come from one assumption — that an item's logical path and its
physical path are the same thing (the PARTIAL/REMOTE_ONLY child flip, delete failing on a stopped
transfer, and the settle-gate stuck-item interaction in `prompts/open-issues.md`). This task
widens the window in which those differ, from "during transfer" to "until post-processing
finishes", so **every caller that builds a path from `local_path + rel_path` during that window
is now suspect**. Audit them; `core/postprocess.py`, `core/extract.py`, and the retention sweeper
are the ones to check.

## Testing

- An end-to-end test against the fake seedbox proving the real name does **not** exist on disk
  while verification is running, and does exist afterwards. This is the whole point of the task —
  without this assertion the change is unverified.
- A test that a `CORRUPT` item's bytes remain under the prefixed name and the real name never
  appears.
- A test that `move`-mode still verifies before deleting the remote.
- A test that extraction still stages into `_UNPACK_` correctly for a prefixed item.
- Run `uv run pytest` with the fake seedbox up (if already running, leave it), `ruff check`
  **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- `docs/decisions.md`, newest at top, naming the earlier entry this reverses.
- `CHANGELOG.md` entry — this changes where files are on disk during post-processing.
- Update `docs/how-it-works.md` and `docs/concepts.md` if either describes the ordering; they are
  the single source the in-app Docs render from.
- **You cannot see the UI** — no browser exists here.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
