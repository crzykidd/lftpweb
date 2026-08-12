---
name: 2026-08-12-files-expand-collapse-all
status: completed        # pending | completed | failed
created: 2026-08-12
model: sonnet            # small, self-contained frontend change
completed: 2026-08-12
result: >
  Added Expand all / Collapse all buttons to FileTree.tsx next to the existing filter
  controls. collapseAll fills `collapsed` from `fullFlat` (existing full-tree flatten)
  filtered to directories; expandAll clears it -- both O(tree size), pure Set updates.
  Both buttons disabled while a text/state filter is active (filters ignore `collapsed`
  entirely, so acting on invisible state would be confusing) and disabled when the tree
  has no directories. Selection needed no changes -- `selected`/`byPath` are already
  derived from the collapse-independent `fullFlat`, verified by reading, not assumed.
  npm run build and npm run lint both clean. Decision recorded in docs/decisions.md.
---

# Task: Expand all / Collapse all on the Files page

The Files tree can only be opened and closed one directory at a time. Add **Expand all** and
**Collapse all** controls so a whole queue's tree can be opened or folded in one action.

## Before you start

- Read `frontend/src/components/FileTree.tsx` in full before changing anything — the collapse
  state interacts with virtualization, filtering, and multi-select, and all three have
  non-obvious behaviour documented in comments there.
- Read `DESIGN.md` §9.2 (the Files page) and `docs/decisions.md`'s phase 3b and phase 9 entries.

## What already exists (do not rediscover this the hard way)

- `collapsed` is a `useState<Set<string>>` of collapsed directory `rel_path`s (~line 187), with a
  per-directory `toggleCollapse` (~line 245). **Default is expanded** — the set starts empty, so
  "collapse all" means filling it with every directory path, and "expand all" means clearing it.
- `flatten(roots, collapsed)` (~line 56) is what feeds the virtualizer; a collapsed directory's
  children are simply not walked.
- **Filters deliberately ignore `collapsed` entirely** (phase 9): while a text/state filter is
  active, the list is computed from a fully-expanded flatten so a match inside a collapsed
  directory still surfaces, and collapse state is restored the instant both filters clear. Your
  controls must not break that. Decide — and say in your report — what Expand/Collapse all should
  do while a filter is active: acting on hidden state the user cannot see is confusing, and
  disabling the buttons may be the honest answer.
- The tree is virtualized with `@tanstack/react-virtual`. Only rendered rows exist in the DOM, so
  neither control may depend on rows being mounted.

## What to do

1. Add **Expand all** and **Collapse all** controls near the existing filter controls, matching
   their visual weight and idiom — these are peers of the filters, not primary actions.
2. Collapse all must fold **every** directory in the tree, including ones inside currently
   collapsed parents (walk the full tree, not the flattened list). Expand all clears the set.
3. Keep both O(tree size) and pure state updates — no DOM walking, no per-row effects. A 5,000-node
   tree (the size phase 3b tested against) must not stutter.
4. **Selection must survive.** Multi-select with shift-range already exists; collapsing a
   directory whose children are selected must not silently drop or corrupt the selection, and the
   bulk action bar must keep reporting the true count. Say what you found and what you chose.
5. Disable or hide the controls when there is nothing to act on (no directories in the tree), the
   same way the page handles other empty states.

## Conventions to honor

- Match the surrounding component's structure, naming, and Tailwind idiom. Comments explain
  **why**, matching the file's existing density.
- **Do not introduce TanStack Query or any new dependency.**
- Both themes: the app has light and dark; follow how neighbouring controls handle it.
- Gates: `npm run build` and `npm run lint` clean. If you touch Python (you should not need to),
  `uv run ruff format --check` **and** `uv run ruff check`, plus `uv run pytest`.
- **No browser exists in this environment.** You can build, type-check, and lint — you cannot see
  the page. **Do not claim it renders or behaves correctly**; state exactly what you verified.

## Working tree check

Run `git status --porcelain` first. The tree is dirty on purpose with several unrelated completed
changes (dev-environment fixes, `_UNPACK_` extraction, Settings → Transfer, post-processing state
persistence, empty-directory reconcile, a logging change, and very likely a metrics/Dashboard
feature touching `frontend/src/**`, `nav.ts`, `api/client.ts`, `api/types.ts`, `lib/format.ts`).
**None are yours — do not revert, refactor, or tidy them.** `CHANGELOG.md`, `standards.md`,
`prompts/startnewsession.md`, `.claude/commands/release-prep.md` were dirty before the session;
leave them alone. Append to `docs/decisions.md` at the top without disturbing existing entries.
If `FileTree.tsx` itself is dirty, stop and ask before touching it.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record any non-obvious decision in `docs/decisions.md` — particularly the filter-active
   behaviour and anything you found about selection.
4. **Do not commit. Do not push.** Prepare the tree, then report back with the file list and a
   proposed one-line commit message (`feat:` prefix, no `Co-authored-by:` trailer).
