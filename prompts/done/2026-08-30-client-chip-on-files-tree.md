---
name: 2026-08-30-client-chip-on-files-tree
status: completed        # pending | completed | failed
created: 2026-08-30
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-30
result: FileNode now carries client_instance_name/client_instance_kind (item.download_client_id
  joined via core/itemview.py, the two callers that already LEFT JOIN item_settle); FileTree.tsx
  reads them straight off each node into a new dedicated 'client' column, no prop threading. All
  six gates green.
---

# Task: draw the download-client chip on the Files tree too

The user's ask: *"We should show the chip for SAB in all if it was a SAB process."*

Three surfaces render a provenance chip. Transfers (active/pending and complete, one shared row
renderer) and Preflight both draw the download-client chip as of 2026-08-30. **The Files tree does
not**, because `FileNode` carries no client field at all. This task closes that gap so the chip
appears on every surface that shows an *arr chip.

## Before you start

Read, in this order:

1. `core/itemview.py` — around line 75, the `item` column list, and around line 468 where
   `arr_status`/`arr_status_at` are projected onto the node payload. This is what you extend.
2. `backend/lftpweb/core/queue.py` / `backend/lftpweb/api/jobs.py` — how `client_instance_name`/
   `client_instance_kind` were joined onto `JobOut` on 2026-08-30 (commit `003d103`).
   **Mirror those field names exactly**; do not invent a third naming for the same fact.
3. `frontend/src/components/FileTree.tsx` around line 913 — the `ArrRowChip` call site.
4. `frontend/src/components/LifecycleIcons.tsx.ClientBrandMark` and
   `frontend/src/lib/clientBrandMark.ts` — the component and pure function you are reusing.
   **Do not write a second chip.**
5. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## Working tree

`frontend/src/components/PreflightBox.tsx` is **already modified and uncommitted** — that is the
Preflight half of this same change, made in-session. Leave it exactly as it is: do not revert it,
do not reformat it, do not "fix" it. It ships in the same commit as your work. Every other file is
clean apart from this prompt.

## What to do

### 1. Carry the client onto `FileNode`

`item.download_client_id` (migration 033) already exists and is already written by the poller.
Join it through `core/itemview.py` to the client's own `name` and `client_type`, and project them
as `client_instance_name` / `client_instance_kind` on the node payload — the identical names
`JobOut`/`HistoryJobOut` already use.

`null` for both whenever the item has no recorded client. Note the `_optional(row, ...)` pattern
already used for the *arr fields and follow it if the same pre-existing-shape concern applies.

Add the two optional fields to `FileNode` in `frontend/src/api/types.ts`.

### 2. Render it

In `FileTree.tsx`, render `ClientBrandMark` immediately beside the existing `ArrRowChip`, in the
same order the Transfers row uses so the two surfaces read identically.

**This is simpler than the *arr case and must not copy its shape.** `ArrRowChip`'s `instanceKind`
is threaded down from `FilesPage.tsx` as a prop because `FileNode` has no *arr kind and it has to
be resolved from the item's *queue* binding. Your field is **per-item, straight off the wire** —
read it from the node. Do not add a prop, do not thread anything through `FilesPage.tsx`, and do
not resolve it from the queue. Say so in a comment so the next reader doesn't "unify" the two.

Renders nothing at all when `client_instance_kind` is null — same "no data, no mark" rule the chip
already follows everywhere else.

## Layout — read this before you write JSX

`FileTree.tsx` rows are **virtualized** (`@tanstack/react-virtual`, this project's only added
frontend dependency). Two bugs in this repo were pure layout problems invisible to every test,
because **jsdom performs no layout**: an `overflow-hidden` wrapper clipping a wide row, and a
`w-full` crushing a sibling chip.

- The new mark must not change row height. A virtualized list measures rows; a taller row
  desynchronizes the scroll window from its own estimate.
- It must not push the name cell's truncation or shift the columns to its right.
- Do not write a test that appears to cover layout. Say in a comment that layout is untested here
  and why, as the other components already do.

If anything looks wrong on screen, **ask for a screenshot rather than guessing** — guessing from
reported text has been wrong twice in this repo and one image settled it immediately.

## Tests

- Backend: `core/itemview.py` projects `client_instance_name`/`client_instance_kind` for an item
  with a recorded client, and `null` for one without. Find the existing itemview tests and extend
  them rather than starting a new file.
- Frontend: `clientBrandLabel` is already covered by `lib/clientBrandMark.test.ts` — do not
  duplicate those cases. This repo has **no component-rendering tests anywhere** (`vitest run`
  over pure functions only, no `@testing-library/react`), so if the only thing left to test is the
  JSX, say that in your report rather than inventing a component test harness for one chip.

## Conventions to honor

- Match the surrounding docstring style — these modules explain *why* at length.
- Doc updates ship in the same change set: `DESIGN.md` §17 and
  `docs/download-client-framework-spec.md` where they describe which surfaces show the chip;
  `docs/decisions.md` newest at top, recording that the Files tree reads the client per-item off
  the wire while the *arr kind is still queue-resolved, and why those two stay different.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `npm --prefix frontend run lint`, `npx tsc -b --noEmit` (from `frontend/`; there is no
  `typecheck` script), `npm --prefix frontend test`.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. The orchestrating session
   surfaces the `y/n` to the user. Never `git add -A`, never push.
