---
name: 2026-08-13-delete-archives-after-extract
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: |
  Implemented as a site-level, default-off PostprocessSettings.delete_archives_after_extract.
  Avoided the re-download-loop trap by adding a `deleted_archive` table (migration 010) and a
  new `core/engine.py.build_scan_counts_predicate` that folds it into the same completeness
  seam `core/patterns.py.build_counts_predicate` already feeds -- a deleted archive reads
  EXCLUDED, exactly like a file_exclude match, never a second completeness rule and never
  auto_queue_suppressed. Deletion lives in core/local_delete.py.delete_extracted_archives, a
  third function in that module (not a fourth deletion path), reusing resolve_within_root and
  the mount-sentinel gate but not delete_local() itself, since it removes only the archive
  volumes (including continuation volumes), never the whole item. Directories only -- a loose
  top-level archive file is withheld. No additional gate for `move` mode or the relocate step
  (both reasoned through and tested). Sidecars survive without special-casing (find_archives
  never returns them). 15 new tests added to tests/test_postprocess.py using the real RAR
  fixtures from 855e7a3, including a regression test driving a real Engine.scan_queue pass and
  a cold-start (fresh Engine instance) test -- 611 total pass, both ruff gates and npm
  lint/build clean. DESIGN.md §6 wording drafted in docs/decisions.md, not applied. Nothing
  left unfixed; full reasoning and rejected alternatives in docs/decisions.md's 2026-08-13
  entry.
---

# Task: Optionally delete archive files after a successful extraction

User request: once a release has been extracted, the `.rar`/`.r00`/… volumes are dead weight
on local disk. Add an option to remove them.

**The naive implementation causes an infinite re-download loop.** Read the next section
before writing any code — it is the whole difficulty of this task.

## The trap

Deleting the archives drops the item's local byte total below its remote total. On the next
scan `core/reconcile.py` computes `local < remote` → **`PARTIAL`**. And `PARTIAL` beats
post-processing outcomes in the precedence rule (`core/postprocess.py.outcome_survives_rescan`
— rule 2, "the bytes are not all there, and an outcome is a stronger claim still"), so
`EXTRACTED` will **not** protect the item. Auto-queue then re-fetches the archives, extracts
them again, deletes them again — every scan interval, forever.

This is the same shape as the `REMOVED_LOCAL` bug reverted in `6d3bd95`. Read
`prompts/open-issues.md` § "4 — closed, shipped, and reversed the same night" first; it is a
short read and it is the same lesson.

**The seam that already exists:** `file_exclude` patterns face the identical problem — an
excluded file never arrives, so if the reconciler counted it as missing, every filtered
release would sit `PARTIAL` forever and be re-queued. `core/patterns.py.build_counts_predicate`
solves it by marking the file `EXCLUDED` — a real state, not an absence — and removing it from
its parent directory's completeness accounting. See `prompts/startnewsession.md`'s traps list,
"Excluded files break completeness".

Reuse that mechanism or one deliberately modelled on it. **Do not** invent a second
completeness rule, and **do not** solve it by suppressing auto-queue on the item — suppression
is for user decisions and error states (§4.6), and using it here would also stop the item
being re-fetched for legitimate reasons.

Watch the `relevant == 0` trap while you are in there: a directory whose children are *all*
excluded is vacuously `DOWNLOADED`, and that is load-bearing. An item whose archives were all
deleted after extraction must land in the same place, not in the "genuinely empty remote
directory → `REMOTE_ONLY`" branch that `core/reconcile.py`'s `remote_file_totals` rollup
distinguishes.

## Before you start

- `DESIGN.md` §6 (extraction), §3.2 (state rules, especially rules 1, 2, 8), §4.7.
- `core/extract.py` — `find_archives`, `_is_first_rar_volume`, `_RAR_PART_RE`,
  `check_extract_preconditions`, `_staging_dirs`, `sweep_failed_dirs`, `resolve_within_root`.
- `core/postprocess.py` — `process_item`, `_do_extract`, `outcome_survives_rescan`.
- `core/local_delete.py` — the shared deletion primitive added 2026-08-12, with its
  containment, active-job, in-flight, and mount-sentinel guards. **Reuse it.** Do not write a
  third deletion path.
- `core/patterns.py.build_counts_predicate` and `core/reconcile.py`'s `counts_predicate` seam.
- `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. If files you need are dirty, list them and ask.

## What to build

1. **A setting, default OFF.** Deletion never defaults on in this project. Site-level in the
   `setting` table (no migration) unless you find a strong reason otherwise; say which and why.
   Natural home is alongside the other post-processing settings.

2. **Delete only what was actually extracted, and only on full success.**
   - Every archive in the set, including continuation volumes (`.r00`/`.r01`/…, `.partNN.rar`)
     — not just the head `find_archives` returned.
   - Never on `EXTRACT_FAILED`, never on a precondition failure, never when a `_FAILED_`
     directory was produced.
   - Never delete a non-archive file. A release directory holds `.nfo`, `.sfv`, samples,
     subtitles — none of that is yours to remove.
   - Consider whether the `.sfv`/`.md5` sidecars should survive: they are the evidence
     `core/verify.py` uses, and a future re-verify would want them. Decide and record it.

3. **Make the absence not count as incomplete**, per the trap above. Reuse the `EXCLUDED`
   mechanism or a deliberate analogue. Persist enough to survive a restart — the reconciler
   must reach the same conclusion on a cold start with only the database and the filesystem,
   which means an in-memory set of "files we deleted" is not sufficient.

4. **Audit every deletion** with an `event` row, same discipline as
   `core/local_delete.py` and `_maybe_delete_remote`: every delete *and* every withhold
   writes a row before returning, no silent paths.

5. **Interaction with `move` mode — check this explicitly.** On a `move` queue the remote copy
   is already deleted by the time extraction runs (`process_item` order: verify → delete
   remote → extract → relocate). So archive cleanup there removes the *last* copy of those
   bytes anywhere. Decide whether that is acceptable, gate it if not, and state the reasoning.
   Note the related concern already raised to the user: the remote delete happening *before*
   extraction means a failed extraction leaves no remote copy — **do not change that ordering
   in this task**, but flag it in your report if it bears on your decision.

6. **Interaction with the relocate step.** `_do_move` relocates the item to `staging_path`
   after extraction. Make sure cleanup and relocation compose in either order without leaving
   orphans or double-deleting, and test it.

## Tests

- **The regression test is the point**: extract, delete archives, then run a real scan and
  assert the item does **not** read `PARTIAL` and is **not** re-queued. Without this the
  feature is a bug.
- Cold start: same conclusion after a restart with only the database and the filesystem.
- Continuation volumes are all removed, not just the head.
- Nothing deleted on `EXTRACT_FAILED` or a precondition failure.
- Non-archive files survive.
- Use the **real** RAR fixtures added in `855e7a3` (`tests/test_postprocess.py`), not fake
  bytes. Those fixtures exist because fake ones hid a nine-phase bug.

## Conventions to honor

- `docs/decisions.md`, newest at top, with rejected alternatives.
- `CHANGELOG.md` under `### Added`, stating it defaults off.
- `DESIGN.md` — draft wording for §6 and record it; the user has been approving these, but do
  not apply it without one of them saying so in this task's report loop.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build` if you touch the frontend (a settings toggle belongs
  wherever the other post-processing options live).
- `uv run pytest` with the fake seedbox up. 596 pass as of `c8d3e8b`.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, test count,
   lint results, how you stopped the re-download loop, your call on sidecars and on `move`
   queues, and anything not fixed. Never `git add -A`, never push.
