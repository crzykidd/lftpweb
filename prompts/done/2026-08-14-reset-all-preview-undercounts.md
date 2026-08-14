---
name: 2026-08-14-reset-all-preview-undercounts
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Added core/local_delete.py.reset_queue_targets, the single enumeration reset_queue's execute
  path and a new POST /api/queues/{id}/reset-all-preview endpoint both call, so the All scope's
  preview can never drift from what a confirmed reset does. QueueResetControls.tsx now fetches
  that preview automatically on scope selection instead of reading `nodes`; resetComposition.ts's
  describeResetTargets gained an unpublishedCount clause so a row the Files page no longer shows
  is explained rather than silently included. Regression test (queue with one live item + one
  REMOVED_BOTH item not published) asserts the preview and reset-all counts are exactly equal, at
  both the local_delete and api layers. Selected scope left untouched, per the prompt. The
  per-item scope's own related gap (a terminal removed row has no UI path to an individual
  reset) was named in prompts/open-issues.md rather than fixed, per the prompt's own boundary.
  Full verification run: uv run pytest (1023 passed), ruff check + ruff format --check (clean),
  npm run lint/test/build (clean, 254 frontend tests passed), docker compose config --quiet on
  all three compose files (all OK).
---

# Task: The whole-queue reset preview undercounts what the reset will actually do

**Reported live 2026-08-14.** Reset item tracking → Pattern `*` shows 2 items. Reset item
tracking → **All** shows *none* — then resets those 2 anyway.

`All` is supposed to be a superset of any pattern match. It previews a strict subset, and then
acts on the superset.

## The cause

The two scopes read different sources:

| Scope | Preview reads | Executes against |
|---|---|---|
| Pattern | `core/local_delete.py.reset_pattern_matches` → the `item` **table** | the same function |
| **All** | `nodes` (the published tree, via `QueueResetControls`' `nodes` prop) | `reset_queue` → `SELECT id, rel_path FROM item WHERE queue_id = ? AND instr(rel_path, '/') = 0` |

`a4a626d` deliberately **stops publishing a vanished row once it reaches a terminal removed
state** — correct for the Files tree, which should not show ghosts. But the All scope reads that
filtered stream and treats it as "everything this queue tracks", which it is not. A
`REMOVED_BOTH` row is in the database and off the wire, so the preview cannot see it and the
execute path resets it regardless.

**There is no `reset-all-preview` endpoint.** `api/jobs.py` has `reset-preview` (pattern only),
`reset-all`, `reset-by-pattern`, and per-item `reset`. The frontend improvised the All preview
from data it already had.

Note what the pattern scope got right, and copy it: `reset_pattern_matches`' own docstring says
the preview and execute paths share one query *"so 'what the preview showed' and 'what got reset'
can never drift apart (the same reason `delete_local`'s `dry_run` reuses every real guard rather
than approximating them)."* That invariant is the fix.

## Why this matters more than a wrong number

The unified reset control (`4b15fcc`) made **the preview the confirmation** — that was the
explicit argument for softening the ceremony, and `api/jobs.py.reset_by_pattern`'s docstring makes
the same argument for the pattern scope. A preview that under-reports the blast radius of the
action the app itself calls "the most destructive action in the app" undermines the whole design.

## Before you start

- Read `CLAUDE.md`.
- Read `core/local_delete.py`'s `reset_queue`, `reset_pattern_matches`, `reset_item`, and
  `_reset_targets` — and the long section comment above them about which three tables a reset
  clears and why.
- Read `api/jobs.py`'s four reset endpoints.
- Read `frontend/src/components/QueueResetControls.tsx` (the unified control) and
  `frontend/src/lib/resetComposition.ts`.
- Read `prompts/done/2026-08-14-reset-panel-counts-and-layout.md` — the task that built this
  control. Its reasoning is sound; this is a source-of-truth defect it did not anticipate, not a
  design reversal.

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## What to do

### 1. Give the All scope a real preview endpoint

Add a whole-queue preview that enumerates from the **same query `reset_queue` executes against**,
so the two cannot drift. Extract that enumeration into one shared function — the shape
`reset_pattern_matches` already uses for its own pair — rather than writing a second `SELECT` that
happens to match today.

Shape it like the existing `reset-preview` so the frontend has one consistent contract for all
three scopes.

### 2. Make the frontend use it

`QueueResetControls`' All scope must preview from that endpoint, not from `nodes`. Keep the
existing preview → confirm flow and the typed-name stage; only the data source changes.

**Consider what `nodes` is still legitimately for.** The Selected scope reads it correctly — you
can only select rows you can see, so it is inherently limited to published rows and that is
right. Do not "fix" Selected.

### 3. Say what the extra rows are

Rows that are in the database but no longer published are, by definition, ones the user cannot
see on the Files page — so a preview listing them needs to explain itself, or it reads as the app
inventing items. Something like *"3 of these are already-removed items still tracked in the
database"* is enough. `resetComposition.ts`'s existing directories/files breakdown is the natural
place; extend it rather than adding a second summary.

Whether a removed row is distinguishable from a live one in the preview list is your call —
`state` is available. Say which you chose in `docs/decisions.md`.

### 4. Check the per-item scope for the same defect

`reset_item` takes an `item_id` from the frontend, which can only come from a published row. If a
terminal removed row can never be reset individually, that is a real gap worth naming — say so in
your report and in `prompts/open-issues.md` rather than silently widening scope to fix it here.

## Testing

- **The regression test that matters:** a queue with one live item and one `REMOVED_BOTH` item
  (in the database, not published). Assert the All preview reports **both**, and that
  `reset-all`'s own outcome count matches the preview's count exactly. That equality is the
  invariant this task exists to restore — assert it directly, not by eyeballing two numbers.
- A test that Pattern `*` and All report the same set for the same queue.
- Frontend tests for the new preview wiring and whatever wording step 3 lands on.
- Run `uv run pytest` (fake seedbox likely already running — if so, leave it), `ruff check`
  **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top.
- `CHANGELOG.md` entry — this is a user-visible correctness fix on a destructive action.
- Update `docs/concepts.md` if its "Dismiss vs Clear vs Reset" section describes what the preview
  shows; it is the single source the in-app Docs render from.
- **You cannot see the UI** — no browser exists here.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
