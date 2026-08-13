---
name: 2026-08-13-resizable-file-columns
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: |
  Shortened the settle countdown's in-cell text (new `settleWaitShortLabel`, full sentence
  moved to the chip's new `title` hover prop) and one long STATE_AGE_LABELS entry
  ('Removed locally' -> 'Deleted'). Unified the header/row column widths into one
  `RESIZABLE_COLUMNS` definition. Added drag-to-resize (pointer events, CSS custom properties
  written via a ref during the drag, one `setState`+`localStorage` write on pointerup),
  keyboard resize (arrow keys, Shift for a bigger step), and double-click-to-reset, persisted
  via lib/storage.ts under 'files.columnWidths'. Lint and build clean; 747/748 backend tests
  pass (the one failure is unrelated, from a concurrently in-progress backend change under a
  different prompt). No UI verification possible in this environment -- unverified.
---

# Task: Drag-resizable Files columns, persisted per browser

User request, 2026-08-13:

> can we make columns in files draggable for sizing and save as a browser setting?

## First, the thing that prompted it — fix this even if you do nothing else

The request came from the settle countdown being **clipped**. The user's words:

> or shrink the font for the waiting txt so it is all displayed

The label added in `38efaaa` — "Waiting for changes — 1 of 2 scans, 35s of 60s" — is simply
too long for its cell. **Shorten the label; do not shrink the font.** The table is already
dense, a smaller font buys a few characters and costs legibility on every row, and the full
sentence already exists in the tooltip.

Something like `Waiting 1/2 · 35s`, with the complete phrasing retained on hover. Keep it
unambiguous — a bare `1/2 · 35s` with no verb reads as data rather than status.

Do the same audit for the other in-cell text while you are there (`removing`, the state chip
with a percentage, the relative-time column): anything that truncates at default widths should
be shortened at the source rather than relying on the user to widen a column. **This fix should
land even if you conclude the resizing work below is not worth it.**

## Two things to fix at once

**1. The widths are currently declared twice.** `frontend/src/components/FileTree.tsx` hardcodes
Tailwind widths in the header row (`w-24`, `w-28`, `w-20`, `w-32`, around lines 1105–1138) *and
again* in each data row (around lines 492–540). They are kept in sync by hand. Resizing is
impossible without unifying them, and unifying them removes an existing drift risk — the header
and the rows can silently disagree today.

**Introduce one shared column definition** (id, label, default width, min width, alignment,
sortable) and drive both the header and the row from it.

**2. The virtualization trap.** `FileTree.tsx` uses `@tanstack/react-virtual` and a queue can
hold thousands of rows. **Do not `setState` on `pointermove`** — that re-renders the whole
visible window on every frame of the drag and will feel terrible on a large tree.

Use **CSS custom properties on the container**: `--col-size`, `--col-state`, etc., with cells
sized `width: var(--col-size)`. During the drag, write the variable directly on the container
element via a ref — the browser reflows, React does not re-render. Commit to React state and
`localStorage` **once, on `pointerup`**.

## What to build

- **Drag handles** between resizable headers. Use **pointer events** (`pointerdown` /
  `pointermove` / `pointerup` with `setPointerCapture`), not mouse events, so it works on
  touch and does not lose the drag when the cursor leaves the handle.
- **The name column flexes** (`min-w-0 flex-1` today) and absorbs the remaining space; the
  others are fixed. Keep that model unless you find a reason not to — resizing a fixed column
  then just changes how much the name gets. Say what you decided.
- **Minimum widths** so a column cannot be dragged to nothing and become unrecoverable. A
  maximum probably is not needed; if you add one, say why.
- **Double-click a handle resets that column** to its default. Cheap, conventional, and the
  escape hatch when someone drags something to 4px.
- **Persist with the existing `frontend/src/lib/storage.ts` helper** — the same safe wrapper
  the sort, collapse, and dashboard-timeframe preferences already use. Do not write a second
  one. Keep its failure handling (private browsing, quota) so a preference read can never break
  the page, and read it **synchronously in the initial `useState`** or the table paints at
  default widths and then jumps.
- **Store by column id, not index**, so adding or reordering a column later does not silently
  apply someone's saved width to the wrong one. Ignore unknown ids on read.

## Accessibility

- The handle must be focusable and resizable from the keyboard (arrow keys, a larger step with
  shift). A drag-only affordance is unusable without a pointer.
- Give it `role="separator"` with `aria-orientation="vertical"` and an accessible label naming
  the column it resizes.
- Do not rely on hover alone to reveal the handles; they need a visible or focus-visible
  affordance.

## Interactions to get right

- **Sorting still works.** The headers became click-to-sort in `38efaaa` — a drag must not
  fire a sort. Distinguish them by movement threshold or by keeping the handle a separate
  element that stops propagation. **Test both**: dragging does not sort, clicking still does.
- **Both substates** (`settling`, `removing`) and the lifecycle icons live in fixed-width
  cells; make sure they degrade sensibly when narrowed rather than overflowing the row.
- The tree indentation lives in the name column — confirm depth padding still behaves when that
  column is squeezed.
- Horizontal overflow: decide whether the row can scroll horizontally or whether total width is
  clamped to the container. Either is defensible; say which and why.

## Before you start

- `frontend/src/components/FileTree.tsx` — the header row and `Row`.
- `frontend/src/lib/storage.ts`.
- `frontend/src/components/{LifecycleIcons,StateChip}.tsx` for what lives in the narrow cells.

## Working tree check

`git status --porcelain`. If `FileTree.tsx` is dirty, stop and ask — several tasks have landed
in it recently and racing one would be painful to untangle.

## Conventions to honor

- **No new frontend dependency.** `@tanstack/react-table` has column sizing built in and is
  *not* worth adopting for this — the project has added exactly one frontend dependency since
  phase 1 and flagged it as a deliberate deviation.
- **There is no frontend test runner in this project.** Do not add one; it is a pending
  decision for the user. Write the width-clamping and persistence logic as pure functions so it
  is testable when one arrives.
- `docs/decisions.md`, newest at top — especially the CSS-variable approach and why, since the
  obvious `setState`-per-move implementation is the one a future contributor will reach for.
- `CHANGELOG.md`; `DESIGN.md` §9.2 if it describes the Files layout (standing approval to edit).
- `npm run lint` / `npm run build`; `uv run pytest` to confirm nothing backend moved.
- **You cannot see the UI.** No browser here — you cannot verify that dragging feels right,
  that the handles are findable, or that anything looks correct. Say so plainly and do not
  claim otherwise.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, how you kept
   the drag off the React render path, the flex-vs-fixed decision, the overflow decision,
   lint/build results, and anything not fixed. Never `git add -A`, never push.
