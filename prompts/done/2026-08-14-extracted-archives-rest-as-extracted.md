---
name: 2026-08-14-extracted-archives-rest-as-extracted
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Part 1 shipped: `core/engine.py._persist`'s vanished-row sweep now short-circuits to
  `EXCLUDED`/`first_missing_at=None` for any rel_path in `deleted_archive_paths`, reusing the
  already-loaded set rather than a second query, so a cleaned-up archive volume never starts
  the removal-grace clock on either sync mode. A new `deleted_archive_at` wire field
  (`core/itemview.py`, joined in `core/engine.py._project` and `api/files.py.get_files`) lets
  the Files page tell that `EXCLUDED` apart from a pattern-excluded file and render a
  greyed-out `Extracted` chip (`FileTree.tsx`'s existing synthetic-chip substitution pattern,
  `StateChip.tsx`'s `FALLBACK_STYLE` by omission) instead of the misleading `Excluded`;
  `LifecycleIcons.tsx` and `ItemDrawer.tsx` got the matching truthful-tooltip/chronology
  update. Verified: `uv run pytest` (1032 passed), `ruff check`/`ruff format --check` clean,
  `npm run lint`/`npm test` (258 passed)/`npm run build` clean, all three compose files valid.
  Part 2 (the collapsible summary row) was not built -- it does not fall out cleanly against
  `FileTree.tsx`'s virtualization/sorting/persisted-collapse-preference, per the task's own
  "stop rather than force it" instruction; recorded as still open in `prompts/open-issues.md`.
  The grey-vs-emerald chip distinction has not been seen in a real browser -- no UI access in
  this environment -- and needs a human to confirm.
---

# Task: A cleaned-up archive volume should read "Extracted", not run a missing-file countdown

When archive cleanup deletes a release's rar volumes after a successful extraction, every volume
row starts a removal grace clock and shows an alarming `Missing · 9m` countdown for ten minutes —
for files this codebase deleted **on purpose**, as the successful conclusion of the thing that
just worked. They then resolve to `REMOVED_BOTH` and vanish.

Live evidence (production-test, 2026-08-14):

```
Show.1.S16E13…H264            EXTRACTED    extracted_at 22:03:16
  …defenestrate.rar           DOWNLOADED   local_size null   first_missing_at 22:03:25
  …defenestrate.r00 … .r10    DOWNLOADED   local_size null   first_missing_at 22:03:25
```

Nine seconds after extraction succeeded, cleanup removed the twelve volumes, and the very next
scan started a grace clock on every one of them.

## This is already half-recorded

`prompts/open-issues.md` has it under *"A cleaned-up archive rests in a different state depending
on sync mode"*: a `copy` queue leaves the remote volumes in place so the node stays and the counts
predicate marks it `EXCLUDED`, while a `move` queue loses both copies and the row goes through
§7.3's grace period instead. **Same event, two different readings.** That entry also notes the
countdown work (2026-08-14) made the wrong reading louder rather than quieter.

## The signal already exists — it just isn't consulted

`deleted_archive` (migration 010) records exactly which paths archive cleanup removed, and
`core/engine.py.build_scan_counts_predicate` already folds it into *completeness accounting* so
the parent doesn't read `PARTIAL`. Nothing consults it when deciding whether a row is **missing**.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §3.2 rule 3, §6, §7.3.
- Read `core/local_delete.py`'s `delete_extracted_archives` and `load_deleted_archive_paths`,
  `core/engine.py`'s `build_scan_counts_predicate` and its vanished-row sweep, and
  `core/mount_sentinel.py.resolve_absence`.
- Read `core/itemview.py`'s module docstring and its R/L/V/E facet computation — that is the
  established pattern this task follows.
- Read `frontend/src/components/StateChip.tsx` and `FileTree.tsx`'s `Row` substitution for the
  synthetic `SETTLING`/`REMOVING`/`MISSING` chips.

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## Part 1 — the fix (do this)

### 1. A cleaned-up archive must never enter the grace period

A row in `deleted_archive` was not lost; this codebase removed it. `first_missing_at` must never
be set for one, so it can never resolve to `REMOVED_LOCAL`/`REMOVED_BOTH` through §7.3's clock.

Consult `deleted_archive` where the absence decision is actually made. The queue's set is already
loaded once per scan for the counts predicate — reuse that load rather than adding a second query
per pass.

### 2. Display, not a new state

**Do not add an `ARCHIVE_REMOVED` state and do not overload `EXCLUDED`.** This project's own
documented answer to exactly this problem (`core/itemview.py`, and `prompts/open-issues.md`'s
"State and reality are different things") is that `item.state` already carries five orthogonal
facts, and the fix is *a truthful display projection beside it*, never another enum value.

Publish the `deleted_archive` membership on the item view — the same way the settle-progress and
prefix fields already ride along — and let the chip render from it.

### 3. The chip is a **greyed-out `Extracted`**

The user's own call, and it is the right one visually: the parent release carries an **emerald**
`EXTRACTED` chip (`StateChip.tsx:29`) meaning *present and unpacked*; a consumed volume should
read **grey** — the zinc tone `FALLBACK_STYLE` (line 57) already uses — meaning *gone, and this is
why*. Same word, different weight, no alarm.

Follow the existing synthetic-chip substitution pattern in `FileTree.tsx`'s `Row`; do not invent a
third mechanism. Hover/title should say what happened plainly — the volume was removed after its
contents were extracted — and the item drawer is the natural place for the longer form.

### 4. Both sync modes must read the same

The whole point of the open-issues entry: the reading must come from the `deleted_archive` fact,
not from whether the remote copy happens to survive. Verify a `copy` queue and a `move` queue
produce the identical chip for a cleaned-up volume, and test both.

## Part 2 — the summary row (optional; drop it if it is at all shaky)

`prompts/open-issues.md` records this as blocked on Part 1: the user floated collapsing archive
volumes into one row — *"14 archive volumes · removed after extraction"* — expandable, so the
screen stays clean but the provenance of the `.mkv` is still visible. Their own note at the time:
*"Not sure on this."*

Part 1 unblocks it by giving the volumes one consistent resting state. Build it **only if** it
falls out cleanly:

- It must be expandable, never hiding rows outright.
- It must not fight `FileTree.tsx`'s virtualization, sorting, or the persisted collapse
  preference — a synthetic row that is not a real tree node interacts with all three.
- If it complicates any of those, **stop and report** rather than forcing it. Part 1 is the fix;
  Part 2 is a nicety, and a half-working collapse is worse than twelve honest grey chips.

## Testing

- A cleaned-up volume never gets `first_missing_at` set, on both a `copy` and a `move` queue.
- It never resolves to `REMOVED_LOCAL`/`REMOVED_BOTH` via the grace path.
- The chip renders grey `Extracted` for it and the parent still renders emerald `EXTRACTED` —
  assert they are visually distinct, not merely differently worded.
- A row that is genuinely missing (not in `deleted_archive`) still gets the countdown — this task
  must not blunt the mechanism it is carving an exception out of.
- Run `uv run pytest` (fake seedbox likely already running — if so, leave it), `ruff check`
  **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top.
- `CHANGELOG.md` entry.
- Update `docs/concepts.md` — its removal-grace section describes what the countdown means, and
  this carves a documented exception out of it. Same file is the in-app Docs source.
- Close the open-issues entry this resolves ("A cleaned-up archive rests in a different state
  depending on sync mode"), keeping the reasoning.
- **You cannot see the UI** — no browser exists here. Say plainly that the grey-vs-emerald
  distinction needs a human to confirm.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
