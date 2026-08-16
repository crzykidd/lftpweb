---
name: 2026-08-16-files-brand-logo-icons
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: |
  Files' *arr column now renders `ArrRowChip` (same component Transfers/History already used),
  with `arrInstanceKind` threaded down through `FileTree`/`Row` alongside the existing
  `arrInstanceName`, resolved by `FilesPage.tsx` from `listArrInstances()` the same way. `gone`
  now reads red on Files too (was amber). `ArrIcon`/`ArrMarkIcon` kept -- `TransfersPage.tsx`'s
  job-detail drawer still consumes `ArrIcon` directly, checked before considering deletion.
  docs/arr-integration-spec.md's "UI" section collapsed into one chip-based table covering all
  three surfaces; CHANGELOG.md Unreleased/Fixed and startnewsession.md (row Q) updated same
  commit. All gates green: frontend lint/test(363)/build, ruff check/format, pytest (1174
  passed, 0 skipped). No agent can see the rendered UI -- unviewed, per usual.
---

# Task: Files view uses the same brand-logo *arr chip as Transfers/History

User feedback (2026-08-16): the real Sonarr/Radarr logos (from
`2026-08-16-arr-chip-on-row-lines.md`, committed `f546595`) show on Transfers and
History rows, but the Files view still renders the old generic *arr mark. Unify: the
Files tree's *arr column renders the **same `ArrRowChip`** (brand logo + status
overlay), one visual language everywhere.

## What to do

1. `frontend/src/components/FileTree.tsx` (and `FilesPage.tsx` where instance data is
   resolved): the *arr column's `ArrIcon` is replaced by `ArrRowChip` from
   `LifecycleIcons.tsx`. The page already fetches `listArrInstances()` and resolves each
   queue's bound instance for the hover — thread `kind` through the same way
   `arrInstanceName` already flows. Unknown kind → the existing `ArrTextChip` fallback.
2. **Status colors unify on the chip's mapping** — including `gone` = red dot (the
   Files view's old amber ⚠ goes away; the chip component is the single source). Hover
   text unchanged (the existing `arrHoverLabel` helpers).
3. The "Processed · Xm" countdown chip and all facet/filter behavior stay exactly as
   they are — this task changes the icon rendering only, not the states, filters, or
   the removal-grace machinery.
4. Delete `ArrIcon`/`ArrMarkIcon` if nothing else consumes them after the swap (check
   first); dead icon components shouldn't linger.
5. Tests: update Files icon tests to the chip component/variant expectations; the
   `gone` red-not-amber change is asserted.
6. Docs same commit: `docs/arr-integration-spec.md` icon table (one table, chip-based,
   covering all three surfaces); `CHANGELOG.md` Unreleased; startnewsession.md table
   row.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt. (`prompts/2026-08-16-manual-delete-local-and-remote.md` is an
unrelated untracked sibling — leave it alone.)

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report.
- `fix:` prefix (unifies an inconsistent shipped UI). No new dependencies.
- If a background command finishes while you wait, read its result directly and
  continue — do not park indefinitely on a monitor.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
5. `uv run pytest` — note skip counts honestly.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `fix:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
