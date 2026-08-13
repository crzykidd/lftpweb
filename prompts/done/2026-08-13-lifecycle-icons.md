---
name: 2026-08-13-lifecycle-icons
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >-
  Facets derived in core/itemview.py (_remote_facet/_local_facet/_verified_facet/
  _extracted_facet), reaching all three projection consumers through the shared item_view().
  Frontend: LifecycleIcons.tsx (inline Lucide SVG, ISC, NOTICE updated), StateChip.tsx inline
  progress fill, FileTree.tsx sorting (siblings-only) and a localStorage-backed
  default-plus-exceptions collapse preference. 42 new backend tests, 685 total passing; both
  ruff gates and npm run lint/build clean. See docs/decisions.md's
  "2026-08-13 -- Lifecycle icons" entry for the presence-vs-milestone design record.
---

# Task: Files page row revamp — lifecycle icons, inline progress, sorting

Three related pieces of one visual change, deliberately in a single task because they all
rewrite the same row renderer and doing them separately means rebuilding it three times:

1. **Lifecycle icons** (R / L / V / E) — the sections below.
2. **Inline progress bars** — see "Also in this pass: inline progress".
3. **Sorting** — see "Also in this pass: sorting".

Plus the persisted expand/collapse preference. The user's framing: *"Think easy, all the info
at my fingertips and a clean sexy look."*

**A separate, later task covers the detail inspector** (hover tooltip, extended `ItemDrawer`,
`local_mtime`, per-item history). Do not build that here.

## Part 1 — Show item reality as lifecycle icons (R / L / V / E), not one state word

User design decision, 2026-08-13, after a night of state bugs:

> we need to rethink the display. Moving to small icons. Where we have a R icon for Remote, a
> L icon for local and a D icon for downloaded … these aren't just one or the other, we would
> have multiple flags … **This shows the steps we made through the lifecycle.**

This supersedes an earlier "missing flag" prompt, now deleted — *missing* falls out of this
design as "no **L** on an item that has a `downloaded_at`."

## Why this is the right change, not just a prettier one

`item.state` is a single enum carrying at least five orthogonal facts: does a remote copy
exist, does a local copy exist, was it verified, was it extracted, and what is happening to it
right now. Collapsing those into one slot is the **root cause** of most defects found on
2026-08-12/13:

- `LOCAL_ONLY` clobbering `EXTRACTED` after a `move`-mode remote delete.
- `REMOVED_BOTH` being "knowingly overloaded" to also mean "local deleted, remote untouched".
- A deleted item unable to say "gone locally, still on the seedbox".
- `DOWNLOADED` rows claiming files that are not on disk.

Each is two independent facts fighting over one slot.

**This task does NOT rewrite the state machine.** `item.state` keeps driving decisions —
auto-queue eligibility, post-processing triggers, the precedence rules — and it has real test
coverage. Keep it. Add a truthful *display* projection beside it. Every facet needed is
already persisted:

| Facet | Source | Kind |
|---|---|---|
| **R** — exists on the remote | `remote_size IS NOT NULL` | presence |
| **L** — local bytes present | `local_size` vs `remote_size` | presence |
| **V** — verified | `verified_at IS NOT NULL` | milestone |
| **E** — extracted | `extracted_at IS NOT NULL` | milestone |

## The distinction that makes this work

**Presence icons (R, L) are true *now* and may go dark.** In `move` mode R extinguishes when
the remote is deleted — that is the display being honest, not losing information.

**Milestone icons (V, E) record that something *happened*** and stay lit once earned. They are
read from **timestamp columns**, never inferred from `item.state`, so a rescan cannot clobber
them. That property is the point: the bug class disappears at the display layer even before it
is fixed in the state layer.

Worked example — a completed `move`-mode release reads **R** dark, **L** filled, **V** lit,
**E** lit: *"we had it remotely, we still have it locally, we verified it, we unpacked it, and
the remote is gone now."* Today that same item collapses to the single word `LOCAL_ONLY`.

## Before you start

- `core/itemview.py` — the one projection behind `GET /api/files`, `queue_delta`, `snapshot()`,
  and the queue/postprocess item deltas. **Derive the facets here**, so all consumers agree.
  Deriving them in React would put the rule in a fourth place; that duplication has already
  caused one bug this week.
- `core/reconcile.py` — how `local_size`/`remote_size` are computed, including directory
  rollups and the `remote_file_totals` distinction.
- `frontend/src/components/FileTree.tsx` — the row renderer and the existing `settling` badge,
  which is the visual precedent.
- `prompts/open-issues.md` and `prompts/startnewsession.md`'s traps list.

## Working tree check

`git status --porcelain`. Other tasks are in flight around `core/local_delete.py`,
`core/postprocess.py`, `core/extract.py`, and `core/itemview.py`. If files you need are dirty,
list them and ask.

## What to build

1. **Derive the facets in `core/itemview.py`** and add them to `ITEM_VIEW_COLUMNS` /
   `item_view()`. Write the predicates as small pure functions with the reasoning in their
   docstrings, and unit-test each branch.

2. **L is three-valued, not boolean**: absent / partial / complete. Filled vs hollow vs nothing.
   Complete means `local_size >= remote_size`, the same leaf rule `core/reconcile.py` uses —
   **do not invent a second completeness rule.**

3. **Directories roll up.** `local_size` on a directory is a sum. Decide what L shows for a
   directory with some children present, state it in the docstring, and test it.

4. **Handle the cases where absent local content is correct, not a problem:**
   - **`EXCLUDED`** files never arrive by design — a `file_exclude` match. Must not read as a
     failure.
   - **A directory whose children are all excluded** is vacuously `DOWNLOADED` with zero local
     presence and is **not** missing. `remote_file_totals` is how `core/reconcile.py` tells
     that apart from a genuinely empty remote directory — see the traps list, and **do not key
     on local presence alone**.
   - **`REMOTE_ONLY`/`settling`** never claimed local content.

5. **Activity stays out of the icons.** Queued / downloading / stopped / failed are transient
   and genuinely mutually exclusive — the one place an enum is right. Keep them as the row's
   primary treatment. The icons are for the accumulated lifecycle, not the current verb.

6. **Real icons, colour-coded, with per-icon tooltips.** The user's decision:

   > standard icons that are color coded. Green complete and good, Yellow in progress and Red
   > failed. A mouse over over each icon gives a little detail. There should be standard svg
   > icons we can use for common things like network/local/download etc.

   **Use inline SVG, not an icon package.** This project has added exactly one frontend
   dependency since phase 1 (`@tanstack/react-virtual`) and flagged it as a deliberate
   deviation; `DESIGN.md` §9's TanStack Query was never adopted at all. Six icons do not
   justify a package. Copy the paths from a permissively-licensed set — **Lucide (ISC)** or
   Heroicons (MIT) — into a small local component module, and add a `NOTICE` entry in the same
   style as the existing third-party records. Suggested glyphs: network/cloud (remote),
   hard-drive (local), download arrow, shield-check (verified), package/archive (extracted).

   **Four colour treatments, not three.** The user named green/amber/red, but there is a
   fourth and it matters:

   | Treatment | Meaning |
   |---|---|
   | **Green** | done and good |
   | **Amber** | in progress |
   | **Red** | failed (`CORRUPT`, `EXTRACT_FAILED`, `FAILED`) |
   | **Dim / neutral** | not applicable, or **intentionally gone** |

   **A `move`-mode item's absent remote must be dim, never red.** The remote is gone because we
   deleted it on purpose after verifying — that is the *success* path. Rendering it as failure
   would call the best outcome a fault. `remote_deleted_at` is how you tell "we removed it" from
   "it vanished"; use it.

   **Colour must never be the only signal.** Distinct glyph shapes carry the meaning; colour
   reinforces it. Red/green is the most common colour-vision deficiency and this is a status
   display. Every icon needs a `title` **and** an accessible label.

   **Tooltips carry the detail**, per the user's request — not just the facet name but the fact
   behind it: sizes, and the relevant timestamp where one exists (`verified_at`,
   `extracted_at`, `remote_deleted_at`, `first_missing_at`). `first_missing_at` is what turns
   "missing" into "missing since 20s ago".

   **Both themes.** The UI has light and dark treatments throughout (`dark:` variants in
   `FileTree.tsx`). Every colour must be legible in both; check the contrast rather than
   assuming a palette works inverted.

7. **Keep the existing state text.** Do not remove it in this pass. Icons prove themselves
   alongside it first; removing the words is a separate decision once the user has looked at
   it. **You cannot see the UI**, so you are not in a position to judge that trade.

8. **A "missing" affordance** — an item with `downloaded_at` set but no local presence is the
   `*arr`-import case and the most valuable thing this makes visible. Make sure it reads
   unmistakably (a dark L where the history says there should be one). Consider a Files filter
   for it if it fits the existing filter idiom; skip it and say so if not.

## Also in this pass: inline progress

User request:

> currently we list partial or downloading or downloaded. I wonder if we show a rough progress
> bar somehow. So it is a box with the word partial in it that shows a color background that
> keeps ticking up as we get 1% 8% 20% through etc. so we can see how much is left. Something
> sexy looking … Also the top level directory should show this info as well so if you have a
> 40gig directory with multiple files you can see the % completed.

Reference point they named: **SABnzbd** — an inline bar filling the row with the percentage
over it. Neither you nor the orchestrating session can see screenshots; build to the
description and say plainly that the visual is unverified.

**No new backend data is needed.** `local_size` and `remote_size` are already in
`core/itemview.py`'s projection, and `core/reconcile.py` already rolls both up for
directories — so a multi-file directory's aggregate percentage comes out correct for free.
`formatPercent(local_size, remote_size)` already exists (used by `ItemDrawer.tsx`); reuse it
rather than writing a second percentage.

- **The bar is the state pill's background**, not a separate column — the fill grows behind the
  state word. Text must stay legible across the whole fill range in **both themes**; check
  contrast at ~50% where the text straddles filled and unfilled, which is the case that breaks.
- **Only where a percentage means something.** `DOWNLOADING` and `PARTIAL` yes. A complete item
  does not need a 100% bar, and `REMOTE_ONLY`/`EXCLUDED` have no meaningful denominator.
  Decide, state it, and make sure a missing or zero `remote_size` cannot produce `NaN%` or a
  divide-by-zero.
- **Performance matters here.** `FileTree.tsx` is virtualized and a queue can hold thousands of
  rows. Use a CSS width transition — no JS animation loop, no per-row timer. Progress deltas
  arrive over the WebSocket only for active items (~1 Hz for the parent, every third tick for
  children since `819b82c`), so repaint is naturally bounded; do not add polling.
- **ETA is explicitly deferred** — the user said "that could come later". Note in your report
  what it would take: `core/progress.py` computes EMA-smoothed speed and ETA **per job**, and
  publishes them on the `progress` WebSocket message that the Transfers page consumes. They are
  not in the item projection, so the Files page would need either that message joined
  client-side or the fields added to `core/itemview.py`. **Do not build it.**

## Also in this pass: sorting

> we should also add some sorting on here. Size maybe.. and time status column?

**`FileTree.tsx` renders a tree, not a list.** Sorting must reorder **siblings within each
parent**, never the flattened array — flattening is what the virtualizer walks, and sorting it
directly would tear children away from their parents and produce nonsense. Sort the tree, then
flatten.

- Sortable at minimum: **name**, **size**, and **last state change** (`state_changed_at`, added
  in `57f7ce9`). Consider **percent complete** — cheap now that it is computed.
- Directory size means the rollup, which is what the user wants for a 40GB release.
- Ascending/descending, with the active column indicated.
- **Compose with the existing filters**, which already bypass `collapsed` entirely while active
  (see the traps list) — sorting must work in both the filtered and unfiltered views.
- Null/absent values (no `remote_size`, no `state_changed_at`) need a defined position; do not
  let them sort randomly.
- Persist the sort choice the same way as the collapse preference below — same storage helper,
  same failure handling. Do not write two.

## Also in this pass: persist the Expand all / Collapse all preference

User request, same page, so it rides along rather than becoming a second visit:

> expand all / collapse all should be a browser cache setting, so the default is what I last
> clicked.

**Do not naively persist the existing `collapsed` set.** `FileTree.tsx` currently holds
`collapsed: Set<string>` — the paths that *are* collapsed — starting empty, so the default is
expanded. Save and restore that set and "collapse all" silently breaks: the Files tree updates
continuously over the WebSocket, and a directory that appears *after* the set was saved is not
in it, so it renders expanded against the user's stated preference.

**Invert what is stored.** Persist a `defaultCollapsed` boolean plus a set of **exceptions**
(paths where the user has overridden the default). A directory's collapsed state becomes
`exceptions.has(path) ? !defaultCollapsed : defaultCollapsed`, so newly-arriving directories
inherit the preference automatically and per-row toggles still work. Clicking Expand all or
Collapse all sets `defaultCollapsed` and clears the exceptions.

Details that matter:

- **`localStorage`**, under a namespaced key. It must not throw where storage is unavailable
  or full (private browsing, quota) — wrap access, fall back to in-memory, never let a
  preference read break the page.
- **Read it synchronously in the initial `useState`**, not in a `useEffect`, or the tree paints
  expanded and then snaps closed on first load.
- **Do not fight the filter behaviour.** `FileTree.tsx` deliberately ignores `collapsed` while a
  text or state filter is active — a match inside a collapsed directory must still surface —
  and restores it when filters clear (see `prompts/startnewsession.md`'s traps list). The
  persisted preference must survive that round trip unchanged.
- **Per-row toggles**: persisting them was not requested. Whether exceptions survive a reload or
  reset to the default each time is your call — pick one, keep it consistent, and say which in
  your report.
- The Expand all / Collapse all buttons stay disabled when the tree has no directories or a
  filter is active, with their existing explanatory tooltips (`cd74f91`). Do not regress that.

## Tests

- Each facet predicate, every branch, as unit tests on the pure functions.
- Move-mode completed item: R dim, L filled/green, V green, E green. **The worked example above
  is the headline test** — and assert R is the *neutral* treatment, not the failure one. A
  successful move must not render as a fault.
- A `CORRUPT` / `EXTRACT_FAILED` item renders red on the relevant icon, and an item whose
  remote merely vanished (no `remote_deleted_at`) is distinguishable from one we deleted.
- An item within §7.3's grace period, files gone, state still `DOWNLOADED` → L dark. The
  `*arr`-import case.
- `EXCLUDED` → not rendered as missing.
- All-children-excluded directory, vacuously `DOWNLOADED` → not rendered as missing.
- Partially-present directory → whatever you decided in step 3.
- The facets reach all three projection consumers (`GET /api/files`, `queue_delta`,
  connect-time `snapshot()`) — one test per path, since the whole point of the shared
  projection is that they cannot disagree.
- **The collapse preference persists and applies to newly-arrived directories.** With
  `defaultCollapsed` saved as true, a directory that appears in a later `queue_delta` renders
  collapsed — this is the case the naive "persist the collapsed set" implementation gets wrong,
  so it is the test that matters.
- The preference survives a filter being applied and cleared.
- A `localStorage` that throws on read or write does not break the page.
- **Progress percentage on a directory** equals the rollup of its children, not the directory's
  own bytes — the 40GB-release case the user asked for.
- A zero or absent `remote_size` produces no bar and no `NaN`.
- **Sorting reorders siblings, not the flattened list**: a sorted tree still has every child
  under its own parent. Assert on the tree structure, not just the visible order.
- Sorting composes with an active filter, and with the collapse preference.
- Rows with a null sort key land in a defined position.

## Explicitly out of scope

- **Any change to `item.state`, its transitions, `resolve_absence`, or the grace period.** This
  is a display projection. If you find yourself editing the state machine, stop and report.
- Removing the state text from the UI (see step 7).

## Conventions to honor

- `docs/decisions.md`, newest at top — record the presence-vs-milestone distinction explicitly;
  it is the load-bearing idea and the next person must not collapse it back.
- `CHANGELOG.md` under `### Added`.
- `DESIGN.md` §9.2 — draft wording, do not apply without a nod in the report loop.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** Say "builds, type-checks, lints, and the endpoints it calls were
  verified" — never "renders correctly".

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, each predicate
   in one sentence, what you decided for partially-present directories, whether you added the
   filter, test count, lint results, and anything not fixed. Never `git add -A`, never push.
