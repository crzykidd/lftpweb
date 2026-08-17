---
name: 2026-08-17-orphaned-spent-archive-rows
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Fixed. `core/engine.py._persist`'s `deleted_archive_paths` vanished-sweep branch now checks
  whether the row's top-level ancestor is still in `written` before resting at `EXCLUDED`;
  once the ancestor has itself landed on a terminal state, the row falls to `REMOVED_BOTH` and
  a new `core/archive_cleanup.py.purge_deleted_archive_paths` (re-exported from
  `core/local_delete.py`) clears its `deleted_archive` registry entry. Grace interplay is free
  (ancestor-before-descendant sort order + `written` membership); retroactive self-heal is a
  consequence of the same mechanism, no migration needed. A defensive carve-out (`"/" not in
  rel_path`) preserves the existing self-referential top-level-file test shape. 5 new engine
  tests added in `tests/test_state_persistence.py`. Full backend suite (1246), frontend suite
  (410), lint, and build all green. Docs: CHANGELOG `[Unreleased]` Fixed entry,
  `docs/decisions.md` 2026-08-17 entry, `prompts/startnewsession.md` row V.
---

# Task: Spent-archive rows must leave with their parent (production bug)

Found live on the user's production system, 2026-08-17, fully diagnosed from its logs + this
codebase. A rar'd release (`Dont.Say.Good.Luck.2026...-ETHEL`, 29 volumes) ran the entire
pipeline correctly: verify `VERIFIED` → extract → `delete-archives-after-extract` removed the
29 spent volumes → *arr import confirmed → remote copy deleted (move ladder) → *arr cleanup
removed the whole local directory. The parent row rode the removal grace to `REMOVED_BOTH`
and left the Files page — but the 29 rar child rows stayed behind **forever**: orphaned rows
with no parent directory, a grey "Extracted" chip, and no delete affordance. The user cleared
them by hand with Reset item tracking; this task removes the need to.

## Root cause (verified in code — don't re-derive, verify my line references still hold)

`core/engine.py`'s vanished sweep (`_persist`, the `vanished = set(previous) - written -
protected` loop, ~line 1426): a rel_path in `deleted_archive_paths` resolves to
`vanished_state = "EXCLUDED"` **unconditionally, every pass** (the branch commented "the live
bug this closes: nine seconds after deletion..." ~line 1480–1500). That exemption
(2026-08-14, commit `9975223`, "a cleaned-up archive volume never gets a missing-file
countdown") was designed for a spent volume *inside a still-present release* and never
accounts for the parent itself leaving both trees. Once the parent is gone there is no path
out of `EXCLUDED`: `resolve_absence` has no opinion about it (the `else` branch below), the
registry row (`deleted_archive` table, `core/local_delete.py`) never expires, and the rows
rest orphaned in the DB and the projection.

## The fix (settled design)

**The spent-archive exemption lapses when the row's top-level ancestor is itself gone from
both trees.** In the vanished sweep's `deleted_archive_paths` branch:

- Determine whether the rel_path's top-level ancestor (first path segment; DESIGN.md §4.7's
  "item" notion) is still present in either tree this pass. While it is — current behavior,
  byte-for-byte: rest at `EXCLUDED`.
- When the ancestor is gone from both trees too: fall through to the same `REMOVED_BOTH`
  resolution the sweep already applies to ordinary vanished rows, **and purge the
  `deleted_archive` registry entries** for those rel_paths (extend `core/local_delete.py`'s
  existing registry helpers — one narrow delete function; do not hand-roll SQL in engine.py)
  so a later same-named release can never inherit stale exclusions.
- **Grace interplay, pinned:** while the parent is still riding its removal grace
  (`first_missing_at` ticking, "Processed · Xm" on the Files page), the archive rows should
  keep resting `EXCLUDED` — they only flip once the parent actually reaches `REMOVED_BOTH` /
  leaves the model. A parent mid-grace is still "present" for this purpose. Implement
  whichever way is least invasive (checking the parent row's persisted state via `previous` /
  this pass's written values is likely enough), but the *behavior* above is the contract —
  encode it in tests, and note the mechanism chosen in `docs/decisions.md`.
- **Retroactive self-heal, required:** rows already orphaned in an existing database (parent
  long gone, rows resting `EXCLUDED`) must be carried out by the normal sweep within a scan
  pass or two after upgrade, with their registry entries purged — no migration, no manual
  reset. Add a test that seeds exactly the production shape (registry entries + `EXCLUDED`
  child rows + parent absent from both trees + parent row already `REMOVED_BOTH`) and shows
  the next scan passes clean them up.

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan touches. If
any have uncommitted changes, list them and ask before touching. This file is exempt.

## Before you start

- `DESIGN.md` §3.2 (state rules), §4.7 (item notion), §7.3 (grace) — required reading per
  `CLAUDE.md`.
- `docs/decisions.md` 2026-08-14 entries around the spent-archive/`EXCLUDED` design and the
  2026-08-15 "cleaned-item grace visibility" entry (phase G) — this fix sits directly on both.
- `tests/test_state_persistence.py` — the engine-level vanished-sweep tests this fix's tests
  should sit alongside (the phase G tests are the closest model).
- The one-assumption warning in `prompts/startnewsession.md` (logical vs physical path):
  archive registry rel_paths are logical; keep it that way.

## Tests (beyond the two named above)

- Existing behavior pinned: parent present, spent volumes deleted → rows rest `EXCLUDED`,
  no countdown (existing tests must pass unmodified; add one if none pins this directly).
- Parent mid-grace → archive rows still `EXCLUDED`; grace expiry → parent `REMOVED_BOTH` and
  archive rows follow within the same or next pass, registry purged.
- A `copy`-queue item (remote copy survives) is untouched by all of this — its spent volumes
  are still in `all_paths` and never reach the sweep.
- Full backend suite green.

## Docs, same commit

- `CHANGELOG.md` `[Unreleased]` → Fixed: user-facing one-liner (orphaned "Extracted" rows
  after a fully cleaned-up release now leave with it; self-heals existing orphans).
- `docs/decisions.md`: newest-at-top entry — the lapse rule, the grace interplay, the
  registry purge, the retroactive sweep, rejected alternatives if you weigh any.
- `prompts/startnewsession.md`: add row **V** to the build-run table (after row U — row U is
  the what's-new popup task, landing just before this one), same style, same commit.

## Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full) · frontend untouched but re-verify anyway: `npm test -- --run`,
`npm run lint`, `npm run build`.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit (prompt file + changes + prompt move). **You are a spawned agent: do
   not commit.** Prepare the tree, then report the file list + proposed `fix:` message back
   to the orchestrating session, which surfaces the `y/n`.
