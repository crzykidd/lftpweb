---
name: 2026-08-13-files-ux-pass
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: |
  All five items done. Sortable column headers replace the separate sort widget; a lifecycle
  facet filter replaces "Missing only" (state filter kept -- not redundant, see docs/decisions.md);
  the settle-gate wait is now a readable countdown on the Status chip plus an amber R icon (found
  and fixed a real bug along the way: the settle-progress fields had to be gated on
  substate === 'settling' in core/itemview.py, or item_settle's endless per-scan churn on
  long-finished items would have defeated the WebSocket delta's "only publish what changed"
  property -- tests/test_ws_deltas.py caught it); Transfers shows each queued job's run
  position; Dashboard remembers its timeframe. 10 new backend tests (748 total, up from 738),
  both ruff gates clean, npm run lint and npm run build clean. Query-join cost measured directly
  (+3.4ms/query on a 20,800-row synthetic tree, indexed lookup confirmed via EXPLAIN QUERY PLAN).
---

# Task: Files/Transfers/Dashboard UX pass from live use

Five presentation changes the user asked for on 2026-08-13 after using the revamped Files page
for real. None of these are correctness bugs — the data is right, the presentation is not.

**A separate task (`prompts/2026-08-13-delete-state-truthfulness.md`) is fixing four state
bugs in `FileTree.tsx` and the backend and should land first.** Do not race it.

## 1. Sorting: click the column headers

> Sorting might look cleaner if it is just a click on the columns that are sortable vs a box.
> Not sure on where to put asc/desc. maybe leave it.

The previous task added a static header row and a separate sort control. Make the **sortable
headers themselves the control** — click to sort, click again to reverse — and remove the
separate widget. Keep the current sort keys (name, size, last change, percent) and the existing
null-last ordering.

Indicate the active column and direction on the header itself (a caret is conventional and
enough). The user is unsure where asc/desc belongs and said to leave it — so do the
conventional thing rather than inventing an affordance. Headers that are *not* sortable must
not look clickable.

Keep the persisted sort preference working (`frontend/src/lib/storage.ts`).

## 2. Replace "Missing only" with a facet filter

> Like the missing filter but maybe we should have a drop down to filter based on types.
> Remote/Local/Downloaded/Extracted/not extracted. that might be better. Ohh I guess maybe we
> have that. So not sure what missing only is?

**That the user could not tell what "Missing only" meant is the verdict on it.** It filters to
items with `downloaded_at` set but no local presence — the `*arr`-import case. A real
diagnostic, badly named, in a checkbox nobody can interpret.

Replace it with a dropdown over the **lifecycle facets that already exist** in
`core/itemview.py` (`remote`, `local`, `verified`, `extracted`, each with `level` and
`reason`). Something like: has remote copy / has local copy / extracted / not extracted /
missing locally. Name the missing option so it explains itself — "Downloaded but missing
locally" says what the checkbox never did.

Compose with the existing text and state filters through the same `visiblePaths` mechanism.
Do not build a second filtering path. Check whether the existing **state** filter is now
redundant against a facet filter — if it is, say so in your report; do not remove it unasked.

## 3. Make the settle-gate wait visible

> we should give some indication in the UI for the time when we see a directory and are in the
> wait to see if it changes stage. maybe change the local icon to orange and show a "Waiting
> for changes" or something?

Today this is a **6-pixel dot** (`FileTree.tsx:377-382`) with a `title`. Effectively invisible.

- **Put the amber on the R facet, not L.** During settling it is the *remote* that is still
  changing; L is legitimately absent because nothing has downloaded yet, so amber there would
  imply activity that is not happening. R amber with a `settling` reason reads correctly.
  (Confirm this against `core/itemview.py`'s facet reasons rather than assuming.)
- Replace the dot with a readable label — "Waiting for changes" or similar.
- **Count down.** "Waiting" with no sense of duration is its own frustration. `item_settle`
  already carries `matched_scans` and `first_matched_at`, and `core/settle.py` exposes
  `REQUIRED_SETTLE_SCANS` and `SETTLE_MIN_AGE_S`. Surface enough to say "1 of 2 scans, 35s of
  60s". That means joining settle progress into `core/itemview.py` for top-level rows —
  `item_settle` is keyed by `(queue_id, rel_path)` and only exists for top-level items, so
  handle the absent case cleanly and do not make the projection query expensive. Measure the
  cost on a large tree and say what you found.

## 4. Show queue position on the Transfers page

> what is the proper way to see the priority of the download queue and move things up?

The capability exists and is invisible. Jobs are ordered `rank DESC, queued_at ASC`
(`core/queue.py`), each queued row has **"Move to top"** and **"Start now"**, and
`move_to_top` sets `rank = MAX(rank) + 1`. But nothing displays a position or labels the
ordering, so the user has to infer it from row order — and did not.

Show queued jobs' position (1, 2, 3… in the order they will actually run) and make it clear
the list *is* the queue order. A small ordinal is likely enough; do not add a new API — the
ordering already comes back sorted.

Note in your report whether `rank` monotonically increasing is a problem worth caring about
(every "move to top" raises the ceiling; there is no compaction). Do not fix it unless it is
genuinely a bug.

## 5. Dashboard: remember the selected timeframe

> On the dashboard page. Let's remember the timeframe the user last clicked so it stays the
> same per browser instance.

Persist it with the **existing** `frontend/src/lib/storage.ts` helper — the same safe
`localStorage` wrapper the sort and collapse preferences use. Do not write a second one, and
keep its failure handling (private browsing, quota) so a preference read can never break the
page. Read it synchronously in the initial `useState`, not in a `useEffect`, or the chart
renders one timeframe and then jumps.

## Before you start

- `frontend/src/components/FileTree.tsx`, `LifecycleIcons.tsx`, `StateChip.tsx`,
  `frontend/src/lib/storage.ts`, `frontend/src/pages/TransfersPage.tsx`, and the Dashboard page.
- `core/itemview.py` for the facets, `core/settle.py` for the settle constants and record.
- `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. The state-truthfulness task touches `FileTree.tsx` and the backend
heavily and should have landed first. If it has not, or those files are dirty, **stop and ask**
rather than racing it.

## Conventions to honor

- **No new frontend dependency.** Icons are inline SVG in `LifecycleIcons.tsx` with a `NOTICE`
  entry; extend that if you need another glyph.
- Colour must never be the only signal, and everything must work in both light and dark themes.
- `FileTree.tsx` is virtualized — no per-row timers, no per-row fetches. One shared ticker
  already exists; reuse it.
- `docs/decisions.md`, newest at top. `CHANGELOG.md`. `DESIGN.md` §9.2 — the user has given
  standing approval to edit it directly.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`; `uv run pytest` with the fake seedbox up.
- **There is no frontend test runner in this project.** Do not add one — it is a pending
  decision for the user. Write the logic as pure, testable functions anyway, and cover
  anything you add to `core/itemview.py` with backend tests.
- **You cannot see the UI.** Say "builds, type-checks, lints, and the endpoints it calls were
  verified" — never "renders correctly", never "looks clean".

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line message, whether the state
   filter is now redundant, the settle-progress query cost on a large tree, your view on `rank`
   monotonicity, test count, lint results, and anything not fixed. Never `git add -A`, never
   push.
