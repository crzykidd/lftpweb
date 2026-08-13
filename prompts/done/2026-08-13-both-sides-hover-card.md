---
name: 2026-08-13-both-sides-hover-card
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Replaced the native `title` tooltip with a portal-rendered, remote/local two-column hover
  card, backed by a new shared formatter (`lib/format.ts.bothSidesRows`/`hasBothSides`) reused
  by `ItemDrawer.tsx`'s existing both-sides panel. Imperative-ref controller (`HoverCardHost`)
  keeps show/hide from re-rendering the virtualized row list. `npm run lint`/`npm run build`
  clean; `uv run pytest` 760 passed (backend untouched). Not verified against a real browser.
---

# Task: A hover card showing remote and local side by side

User request, 2026-08-13:

> on the tool tip for a file or directory I like.. but if the file or dir exists on both sides
> remote and local... the popup should have the file name and then 2 columns remote and local
> showing the details.

## What exists today, and why this is a component rather than a string change

`FileTree.tsx.hoverTooltip` (~line 369) returns a plain string joined with `\n`, applied as a
native `title` attribute. **Native tooltips are plain text** — no columns, no styling, no
control over timing or position. Delivering two columns means a real hover card.

**`ItemDrawer.tsx` already renders a both-sides size/mtime panel** (`de85753`). The hover card
is a quick preview of the same facts.

**Share one formatting helper between them.** Not optional. This project has been bitten three
separate times by the same duplication pattern — column widths declared in both the header and
the row, the item projection hand-copied into four publishers, `_LOCAL_CONTENT_ASSERTED_STATES`
forked from `mount_sentinel.COMPLETE_STATES`. A tooltip and a drawer showing the same numbers
in two independently-written formatters will disagree eventually. Extract something like
`bothSidesRows(entry)` returning label/remote/local triples, and render it in both places.

## Behaviour

- **Two columns only when both sides exist.** When only one side does (`LOCAL_ONLY`,
  `REMOTE_ONLY`, a deleted item), a two-column layout with an empty half is worse than a single
  column — degrade to one, and label which side it is.
- **Filename as the header**, per the request. `rel_path` may be long; decide how it wraps or
  truncates and make sure the card cannot become wider than the viewport.
- **Directories carry no mtime at all.** `local_mtime`/`remote_mtime` are files-only
  (`core/reconcile.py`, deliberate — see `de85753`'s decision entry), so a directory's card
  shows sizes and percent only. Do not invent a directory mtime to fill the column; say
  nothing, or say why there is nothing.
- Percent complete is only meaningful with both sides present — a `remote_size` of 0 or null
  must not produce `NaN%`. `percentValue` in `lib/format.ts` already handles this; use it.

## Mechanics — the parts that are easy to get wrong

- **No new dependency.** Hand-roll it. This project has added exactly one frontend dependency
  since phase 1 and flagged it as a deliberate deviation.
- **The list is virtualized and scrolls constantly.** Rows unmount underneath a hovering
  pointer. The card must be portal-rendered (not a child of a row that can vanish), must hide
  on scroll, and must clean up when its anchor unmounts. A card left floating over an unrelated
  row is worse than no card.
- **Position within the viewport** — flip above/below and left/right near edges rather than
  overflowing.
- **Do not fire on every pointer move.** A show delay (~300–500ms) and a hide delay; do not
  re-render the tree to show it. The card is one element, not per-row state.
- **Keyboard and touch.** A hover-only affordance is inaccessible and does nothing on a phone.
  Show it on keyboard focus of the row's name, and remember the per-row **info icon → drawer**
  path already exists for touch — do not try to make the hover card serve touch too, just make
  sure the drawer remains the full-detail route.
- **Do not block interaction.** The card must not swallow clicks meant for the row, the sort
  headers, or the column resize handles (`a4a626d`).

## Keep or replace the native title?

Removing the `title` attribute loses the only tooltip that works before JS hydrates and in
contexts the hover card cannot reach. Decide deliberately: keep the native `title` as a
fallback, or remove it to avoid a double tooltip appearing on hover. **Both are defensible;
what is not defensible is both firing at once.** Say which you chose.

## Before you start

- `frontend/src/components/FileTree.tsx` — `hoverTooltip`, the row renderer, the resize handles.
- `frontend/src/components/ItemDrawer.tsx` — the existing both-sides panel.
- `frontend/src/lib/format.ts` — `formatBytes`, `formatPercent`, `percentValue`.
- `de85753`'s `docs/decisions.md` entry for why mtime is files-only.

## Working tree check

`git status --porcelain`. A task is in flight in `FileTree.tsx` (delete-during-transfer) and
should land first. **If it is dirty, stop and ask** rather than racing it — that file has taken
four significant changes in a day.

## Conventions to honor

- **There is no frontend test runner in this project.** Do not add one — pending user decision.
  Write the formatting helper as a pure function so it is testable when one arrives.
- Both light and dark themes; colour must never be the only signal.
- `docs/decisions.md`, newest at top — the shared-formatter decision and the native-`title` call.
- `CHANGELOG.md`; `DESIGN.md` §9.2 (standing approval to edit directly).
- `npm run lint` / `npm run build`; `uv run pytest` to confirm nothing backend moved.
- **You cannot see the UI.** You cannot judge whether the card is well positioned, whether the
  delay feels right, or whether two columns read better than the current lines. Say so plainly.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, the shared
   helper's shape, the native-`title` decision, how you handled unmount-while-hovering, lint and
   build results, and anything not fixed. Never `git add -A`, never push.
