---
name: 2026-08-14-files-page-speed-column
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Added the Speed column between Size and Status, driven by the existing `progress` WS
  message's `speed_bps` (no backend change) -- and fixed the column-resize handles first, per
  step 1b, by moving each handle to its column's left edge and flipping the pointer-drag sign.
  Live rate only, never a derived average; blank (not `0 B/s`) unless `state === 'DOWNLOADING'`.
  Sort key added with non-transferring rows sorting last via the existing null-last rule.
  Lint/tests/build/pytest/ruff/compose all green. The resize fix could not be visually
  confirmed (no browser in this environment) -- needs a human to drag-test every column,
  especially against a persisted pre-upgrade width layout.
---

# Task: Add a Speed column to the Files page, between Size and Status

The Files page shows size and state for a downloading item but never says how fast it is going,
even though the backend already publishes exactly that number over the WebSocket. Add a **Speed**
column, positioned **between Size and Status**.

## The data already exists — do not add a new endpoint or a new publish path

`core/queue.py._sample_and_publish_progress` (~line 1058) publishes a `progress` message on every
sampler tick carrying, per running job: `job_id`, **`item_id`**, `bytes_done`, `bytes_total`,
**`speed_bps`**, `eta_s`. The frontend already handles it — `hooks/useLiveModel.ts:168` has a
`msg.type === 'progress'` branch.

So the plumbing exists end to end. **Read what `useLiveModel` currently does with that message
before writing anything.** If it already retains speed keyed by item, this is a pure display
change. If it discards it, retain it in the live model rather than adding a second subscription,
a poll, or a REST call.

`speed_bps` is the EMA-smoothed instantaneous rate from `core/progress.py` — what the item is
pulling *right now*. **Prefer it.** It is already computed, already smoothed, and already on the
wire.

### The alternative derivation, and the trap in it

The user proposed deriving speed from data the Files row already carries: `local_size` (bytes) and
`state_changed_at` (when the item moved to `PARTIAL`). Both are present in the item view, and
`state_changed_at` is trigger-enforced (`57f7ce9`), so it is trustworthy as a timestamp.

**But dividing one by the other is wrong in a specific, already-known way.** `local_size` is the
*cumulative* bytes on disk, while `state_changed_at` is the *most recent* state transition. They
have different origins. A resumed transfer — 18 GB already on disk, state changed two minutes ago
— computes as ~150 MB/s, a rate nothing ever achieved. This is exactly the non-monotonicity trap
`core/metrics.py`'s module docstring already documents for `job.bytes_done`, and the reason
`job.bytes_start` exists: *"track `bytes_done - bytes_start` per job, never `bytes_done` alone."*

So if you implement the derived average at all — as a fallback when no live `speed_bps` is
available for a row — it **must** subtract the byte count as of the state change, not divide the
cumulative total. If no such baseline is available on the Files row, **render nothing rather than
a fabricated rate**, and say so in your report. A blank cell is honest; a phantom 150 MB/s is not,
and this project has spent a full debugging session tonight chasing a number that turned out to
mean something other than what it looked like.

Label an average and an instantaneous rate distinctly wherever both can appear. Never silently
substitute one for the other.

**Coordinate with `prompts/2026-08-14-transfer-timing-and-throughput-display.md`** (queued, may
run before or after this). That task adds elapsed/average-speed to the *Transfers* page and the
item drawer, and puts its derivations in `frontend/src/lib/` as pure functions. Reuse those
helpers if they exist by the time you run; if they don't, put yours in `lib/` in the same shape so
that task can reuse them. **Two different speed formatters in this codebase would be a defect,
not a detail.**

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §9.2.
- Read `frontend/src/components/FileTree.tsx` (the column model), `hooks/useLiveModel.ts`, and
  `lib/format.ts` (existing byte/rate formatters — **reuse them**).

## Working tree check

Run `git status --porcelain` first. Other queued work touches `FileTree.tsx`, `FilesPage.tsx`, and
`QueueResetControls.tsx` — notably the reset-controls unification, which lifts selection state
into `FilesPage.tsx`. If any file this plan needs is dirty, list it and ask before editing. This
prompt file is exempt.

## What to do

### 1. The column itself

Insert **Speed** between the existing Size and Status columns.

`FileTree.tsx`'s columns are **drag-resizable** (`a4a626d`) and **sortable** (`8a54475`), and
column widths are persisted and clamped (there are existing unit tests for the clamping). A new
column must participate in all three — resizing, sorting, and width persistence — not be bolted on
outside that model. Check how a stored width preference from before this column existed behaves
on upgrade: a persisted layout that predates the new column must not corrupt the header or leave
it unrenderable.

### 1b. Fix the resize handles first — they are on the wrong edge

Reported live 2026-08-14: *"if I click and drag the line by Size it kind of works… Status moves
the left side of Status while the line to drag is on the right."*

**This is not an off-by-one in the handle-to-column mapping.** `RESIZABLE_COLUMNS` are five
fixed-width columns and **Name flexes to absorb all remaining space** (`FileTree.tsx:695-702`).
`ColumnResizeHandle` sits at each column's *right* edge (`-right-1`, ~line 873) and resizes that
column. Because Name absorbs the delta, widening a column does not move its right edge — Name
shrinks and everything from that column leftward shifts left. **The column grows leftward**, so
the handle never tracks the cursor and the boundary that visibly moves is the column's *left*
edge.

Fix it as part of this task — do not add a sixth column on top of a broken handle model. Two
options:

- **Move each handle to its column's left edge.** Minimal, and it places every handle exactly on
  the boundary that actually moves, which is also the set of dividers a user can see. Note the
  leftmost resizable column's handle then lands on the Name|Size boundary, which is correct.
- **Paired resize** — adjust the dragged column and Name together so the grabbed divider stays
  under the cursor. Conventional, more code, and it **reverses a deliberate decision**: the
  docstring at `FileTree.tsx:700` records that paired resize was considered during `a4a626d` and
  rejected. If you choose this, say so explicitly and record the reversal in `docs/decisions.md`.

Either way, keep the keyboard resize path (`handleKeyDown`) working and keep the drag off the
React render path — the existing implementation deliberately tracks the pointer imperatively
(`setPointerCapture`) so a drag does not re-render the tree on every move. Do not regress that
while moving the handle.

**You cannot see this fix work.** Reason it from the layout, state plainly that it needs a human
to confirm, and do not claim it is fixed.

### 2. What it shows, and when it is blank

- **Actively transferring item** — the live rate, formatted with `lib/format.ts`.
- **Anything else** — blank, or a neutral dash. Do **not** render `0 B/s` for an item that is
  simply not transferring; a zero rate and "not transferring" are different statements and this
  project has already been bitten tonight by a UI that made a completed job look like a stalled
  one.
- **A directory being mirrored** — decide whether the row shows the job's overall rate while its
  children show their own per-file rates (`_publish_child_progress` publishes live child progress
  inside a mirroring directory, `819b82c`). Whichever you choose, make it consistent and say so in
  `docs/decisions.md`. Do not show a parent rate that is the sum of child rates *and* the job rate
  double-counted.

### 3. Sorting semantics

Sorting by Speed must put non-transferring rows in a defined place (all at one end, not
interleaved by a coincidental zero). Cover it in a test — the existing sort tests assert a
sibling-preserving invariant on tree structure, so follow that shape rather than asserting a flat
ordering.

## Testing

Extend the existing Vitest suite. The column-width clamping, sorting, and any speed formatting or
selection logic belong in `lib/` as pure functions so they are testable without mounting a
component — that is the existing convention and the reason `lib/*.test.ts` exists.

Cover: a transferring item, a non-transferring item, a completed item, sort ordering with a mix of
the three, and a persisted column layout saved before this column existed.

Run `npm run lint`, `npm test`, `npm run build`. Run `uv run pytest` to confirm nothing backend
moved — **no backend change is expected here.** If you conclude one is needed, stop and report
rather than making it.

## Conventions to honor

- Reuse `lib/format.ts`; do not introduce a second rate/byte formatting vocabulary.
- Non-obvious decisions in `docs/decisions.md`, newest at top.
- **You cannot see the UI** — no browser exists in this environment. Column placement and width
  behaviour are reasoned from the code, not observed. Say plainly that a human needs to confirm
  the layout, especially the resize behaviour with an upgraded persisted width preference.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
