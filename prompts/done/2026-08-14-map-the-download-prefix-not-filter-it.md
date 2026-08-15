---
name: 2026-08-14-map-the-download-prefix-not-filter-it
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Reversed the original "in-flight folder prefix" filtering mechanism to a mapping one, per the
  user's own instruction that the .downloading- directory is a first-class citizen.
  core/local_scan.py.scan_local's extra_dir_prefixes parameter now maps a matched directory onto
  its logical (stripped) name and walks it in place, instead of dropping it from the walk --
  the same "physical detail mapped back to the logical one" scan_local already does for an
  in-flight *.lftp file, one level up. New helpers _matching_prefix/_resolve_prefixed_dir_names
  resolve the one real subtlety: a real, already-renamed directory and a stale prefixed sibling
  coexisting -- the real one always wins the shared name, the stale one stays visible under its
  own literal name (an ordinary local-only leftover, not silently merged or dropped). Every
  consumer of scan_local's output was checked (reconcile.py needs no change; engine.py._persist,
  autoqueue.py, progress.py, queue.py._completeness_on_disk/._spawn_decision's bytes_start,
  local_delete.py._physical_local_root, postprocess.py/extract.py -- see docs/decisions.md for
  what was found at each). Closes prompts/open-issues.md's "the folder prefix and the settle
  gate's stuck-item recovery don't compose" -- reproduced live against the real fake seedbox and
  proven to fire, not just inferred. Confirms this subsumes the separate bytes_start-reads-0 fix.
  One residual gap found and recorded, not silently fixed: a prefixed leftover with no recorded
  item.pending_download_prefix at all is now visible (LOCAL_ONLY) but not yet deletable through
  the normal path, since core/local_delete.py._physical_local_root has nothing to resolve
  against -- out of this task's own scope (core/local_scan.py, not local_delete.py's resolution
  heuristic). DESIGN.md needed no correction (it never described scan_local's filtering itself).
  Four existing unit tests in tests/test_download_prefix.py rewritten in place (filtering ->
  mapping assertions, same fixtures) plus one new one for the collision case; one comment block
  in tests/test_state_persistence.py reworded (not its assertions -- the test itself remains a
  valid, more general regression guard once its specific historical trigger no longer exists);
  three new e2e tests added to tests/test_download_prefix_e2e.py against the real fake seedbox.
  Verification: uv run pytest, full suite, 1036 passed (up from 1033); ruff check and ruff format
  --check clean; npm run lint / npm test (258 passed) / npm run build clean; docker compose
  config --quiet clean on all three compose files.
---

# Task: `scan_local` should map a `.downloading-` directory to its logical name, not filter it out

**The user's decision, 2026-08-14:** *"the .download is a first class citizen and so therefore we
need to map to that as that is where all directory level downloads happen if set."*

When "folder prefix during transfer" is on, `<local_path>/.downloading-Release/` **is** where the
release lives. Today `core/local_scan.py.scan_local` filters that directory out of the walk
entirely (`extra_dir_prefixes`, fed from `core/engine.py.Engine._active_download_prefixes`), so
lftpweb's own reconciler is blind to its own working directory. That single decision is behind
**five separate defects**:

| Defect | Cause |
|---|---|
| Child rows flipping `PARTIAL` ↔ `REMOTE_ONLY` every scan | children read as locally absent |
| Delete refusing a stopped transfer ("does not exist") | it looked for the logical path |
| The settle gate's stuck-item recovery can never fire | the item can never compute `DOWNLOADED` |
| Progress showing a false 100% | `bytes_start` blind, the sampler not |
| Leftovers invisible in the UI with no row to delete | the content has no local presence |

Three were patched individually. This removes the cause.

## The precedent — this is not a new idea, it is the same one a level up

`scan_local` already does exactly this for in-flight *files*: an `foo.mkv.lftp` is **reported
under its final, stripped name** so it can be matched against its remote counterpart, while
`find_temp_files` exists separately for callers that need the real on-disk path. The temp suffix
is a physical detail mapped back to the logical one.

A `.downloading-Release/` directory is the same thing at directory granularity. Report its
contents as `Release/...`.

## What must remain true

- **Importers still cannot see it.** That was always the entire point, and it is satisfied by the
  *name on disk* alone — dot-prefixed directories are skipped by Sonarr/Radarr/Plex/Jellyfin
  regardless of what lftpweb's reconciler does. Filtering was never what protected them.
- **No phantom `LOCAL_ONLY` node named `.downloading-Release`.** Mapping solves this *better*
  than filtering did: the content is attributed to the real item instead of vanishing.
- **`_UNPACK_`/`_FAILED_` stay filtered.** They are extraction staging, not the item. Note the
  real shape seen in production: `_FAILED_.downloading-Show.1…` — a `_FAILED_` directory whose
  name embeds the prefix. It must still be filtered, so check prefix-stripping order carefully.
- **`.lftpweb-mount-ok` stays filtered.**

## The hard cases — work these out before writing code

1. **Both names present.** `.downloading-Release/` *and* `Release/` can both exist — a stale
   prefixed directory beside a completed release is exactly what the user hit tonight. Decide what
   the mapped view reports and record it. Merging two sources into one rel_path silently is the
   worst option; preferring one and making the other visible somewhere is better. State the rule.
2. **A stale prefix.** The prefix is configurable and `_active_download_prefixes` unions every
   value currently in use (`item.pending_download_prefix`) with the resolved setting. Mapping must
   use that same set, or a directory written under an older prefix becomes invisible again.
3. **Nested prefixed directories.** `scan_local` filters at any depth today. Decide whether
   mapping applies at any depth or only at the item level, and why.
4. **Collision with a real release genuinely named `.downloading-something`.** Vanishingly
   unlikely, but say what happens rather than leaving it undefined.

## Every consumer of `scan_local`'s output — check each one

This is reconciler output; phase-2 code that everything else is built on. Name what you verified:

- `core/reconcile.py` — structural state per node; the counts predicate.
- `core/engine.py._persist` — state arbitration, the settle gate's completion half, the
  vanished-row sweep, `_protected_rel_paths`, and the `deleted_archive` exemption added today.
- `core/settle.py` — fingerprints come from the **remote** tree, so should be unaffected. Confirm.
- `core/autoqueue.py` — eligibility keys off `item.state`, which changes meaning here.
- `core/progress.py` — the sampler walks `job.local_root` (physical) directly. Confirm whether it
  should now use the mapped view instead, or stay physical.
- `core/queue.py._spawn_decision` — **`bytes_start` reads `item.local_size`.** Once that value is
  truthful this should be correct *by construction*; verify that rather than assuming, and if so
  say explicitly that this task subsumes the separate `bytes_start` fix.
- `core/queue.py._completeness_on_disk` — walks the physical root; confirm unaffected.
- `core/local_delete.py._physical_local_root` — still needed (a delete must remove the *physical*
  directory). Confirm it and the mapped view cannot disagree.
- `core/postprocess.py`, `core/extract.py` — operate on the physical root passed to them.

## What this should close

Verify each and say which actually closed:

- The false 100% progress (via `bytes_start`).
- Leftovers invisible with no row to delete — a stalled/failed item should now show real local
  content and be deletable through the normal Files-page path.
- `prompts/open-issues.md`'s *"The folder prefix and the settle gate's stuck-item recovery don't
  compose"* — with local content visible, a held item can compute `DOWNLOADED` again and the
  `unstuck` path can fire.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §3.2, §4.4, §4.4b, §4.7, §5.
- Read `core/local_scan.py` **end to end**, especially `scan_local`, `TEMP_FILE_RE`,
  `strip_temp_suffix`, `find_temp_files`, `find_orphan_sidecars`, and `effective_file_size`.
- Read `core/engine.py._active_download_prefixes` and both `scan_local` call sites.
- Read `prompts/done/2026-08-14-in-flight-folder-prefix.md` and its `docs/decisions.md` entry —
  the task that introduced filtering. This reverses that mechanism while keeping its goal.

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## Testing

- A directory item mid-transfer under a prefix: its children appear under the **logical** rel_path,
  with correct sizes, and no `.downloading-*` node exists in the output.
- The same item's `item.local_size` is non-zero and matches what is physically there.
- A stale prefixed directory with no live job still surfaces (the leftovers case) — assert a row
  exists that a user could delete.
- `_UNPACK_`/`_FAILED_`/`_FAILED_.downloading-*`/`.lftpweb-mount-ok` are still excluded.
- An old-prefix directory still maps, via the unioned prefix set.
- Whatever rule you chose for hard case 1, tested directly.
- An end-to-end test against the fake seedbox in the shape of `tests/test_download_prefix_e2e.py`.
- **Run the whole suite and read it carefully.** This changes reconciler output; existing tests
  encoding the filtering behaviour will fail, and each such failure is a decision — is the old
  assertion now wrong, or did you break something? Report every test you changed and why. Do not
  bulk-update assertions to make things pass.
- `uv run pytest` (fake seedbox is already running — leave it), `ruff check` **and**
  `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, `docker compose config
  --quiet` on all three compose files.

## Conventions to honor

- `docs/decisions.md`, newest at top, naming the entry this reverses and what changed.
- `CHANGELOG.md`.
- Update `docs/concepts.md`/`docs/how-it-works.md` if either describes the prefix behaviour.
- `DESIGN.md` §4.4b describes the prefix — if it says the directory is hidden from scanning,
  **draft the correction in `docs/decisions.md` and ask**; do not edit `DESIGN.md` directly.
- Close what this resolves in `prompts/open-issues.md`, keeping the reasoning.
- **You cannot see the UI** — no browser exists here.

## If this turns out bigger than it looks

Stop and report. A clear "here is why this cannot be done safely in one pass, and here is the
seam" is a good outcome. A half-migrated reconciler is not — everything in this application reads
its output.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
