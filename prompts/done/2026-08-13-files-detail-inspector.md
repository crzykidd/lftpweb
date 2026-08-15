---
name: 2026-08-13-files-detail-inspector
status: done
created: 2026-08-13
model: sonnet
completed: 2026-08-13
result: >
  Generalised ItemDrawer.tsx (itemId/rootRelPath/nodes) rather than building a second inline
  expansion; wired a new, visually-quieter info icon (FileTree.tsx row, LifecycleIcons.tsx) as
  the primary entry point, plus a free native-title hover tooltip. Added migration 011
  (local_mtime, files-only, mirrors remote_mtime), threaded it through local_scan -> reconcile
  -> engine._persist -> itemview -> models -> frontend types, and surfaced first_seen_at on the
  wire for the first time. Drawer now shows both-sides size/mtime, a sorted lifecycle
  chronology, and a bounded (10-row) on-open history fetch per item, backed by a new item_id
  filter on GET /api/history/jobs. 701 backend tests pass (16 new), both ruff gates clean,
  frontend lint/build clean. See docs/decisions.md (2026-08-13, "One detail surface, not two")
  for the full rationale and DESIGN.md §9.2 draft wording pending a nod.
---

# Task: Files page — see an item's full detail, both sides, without leaving the page

User request, 2026-08-13:

> If I mouse over a directory or a file or click one of the two I should get a tooltip or a
> detail expand underneath that shows the file info. Size, modified date etc. and I should get
> that for both sides if it exists on both sides, and maybe a little history info.

Part of a Files-page revamp whose framing is *"easy, all the info at my fingertips, a clean
sexy look."* **The row-level half — lifecycle icons, inline progress bars, sorting — is a
separate task (`prompts/2026-08-13-lifecycle-icons.md`) and should land first.** This task is
the detail surface.

## Do not build a third surface

**`ItemDrawer.tsx` already exists** (phase 3b, a side drawer rather than a modal) and already
shows a per-file breakdown: size, transferred bytes, percent complete. Adding an inline
expand-underneath *and* keeping the drawer means two places showing overlapping information
that will drift apart.

**But today it is unreachable from the Files page, and nearly unreachable at all.** It is
imported in exactly one place — `TransfersPage.tsx:257` — and opened by clicking a transfer row
(`TransfersPage.tsx:79`, `onOpenDrawer(job)`). It is keyed off a **job**, so once a transfer
completes and ages out of that page's list, the drawer can never be opened for that item again.
The user found this directly: *"How do I get to the drawer? I don't have anyway to load it in
the UI."*

Fortunately its props are already item-agnostic — `title`, `rootRelPath`, `nodes`, `onClose`.
It does not need a job at all, only a path and the queue's nodes, both of which `FileTree.tsx`
already has. So wiring it from Files is small; the work is in **what it shows**, which is
currently only the transfer breakdown and none of the lifecycle timestamps or history below.

Keep the Transfers-page entry point working exactly as it does — do not regress it while
generalising the component.

**A per-row info icon is the primary affordance** — the user's refinement, and it resolves a
real conflict rather than being a style preference:

> I think it is important to be able to see actual file details, it could be a small info
> icon...

**Row click is already taken.** `FileTree.tsx:181`'s `onClick` calls `onToggleSelect`, so
clicking a row *selects* it — and selection drives bulk Queue / Stop / **Delete**. Opening a
drawer on row click would collide with the affordance behind a destructive bulk action. An
explicit icon also works on touch, where hover does not exist at all.

So:

- **Small info icon per row → opens the extended drawer.** The explicit, discoverable,
  touch-safe path. Must not toggle selection — stop propagation, and test that clicking it
  leaves the selection unchanged.
- **Hover → lightweight tooltip** (optional, secondary): size / modified / percent. Cheap, no
  layout shift, no fetch, no re-render of the virtualized list.

Match the icon to whatever set the row-revamp task established (inline SVG, no new dependency —
it will have added a local icon module and a `NOTICE` entry). Keep it visually quieter than the
lifecycle icons: this is a control, they are status.

If you instead build the inline expansion the user originally floated, say what happens to the
drawer — do not leave both.

## The gap you have to close first: local modified time is not persisted

The `item` table has `remote_size`, `local_size`, and `remote_mtime`. **There is no
`local_mtime`, in any migration.** So "modified date … for both sides" cannot be answered
today. You will need:

1. A migration — **use 011**; 001–010 are taken.
2. `core/local_scan.py` to capture it, and `core/reconcile.py` to carry it through to
   `ReconciledNode`.
3. `core/engine.py._persist` to store it.
4. `core/itemview.py` — `ITEM_VIEW_COLUMNS` / `item_view()`, the one projection behind
   `GET /api/files`, `queue_delta`, and `snapshot()`. Add it there and all three agree.
5. `models.py` and `frontend/src/api/types.ts`.

**Directories:** decide what `local_mtime` means for one (newest child? the directory's own
mtime, which changes only on entry add/remove?) and say why. `remote_mtime` is currently only
set for files, not directories — check that and stay consistent rather than inventing a
different convention on the local side.

## What to show

**Both sides where both exist**, clearly labelled as remote vs local — that is the core of the
request. Size and modified time each side, and the difference where it matters (a local file
short of its remote size is mid-transfer or truncated; that is worth seeing plainly).

**The lifecycle facts already persisted**, which are currently invisible on this page:
`first_seen_at`, `downloaded_at`, `verified_at`, `extracted_at`, `remote_deleted_at`,
`first_missing_at`, `state_changed_at`. These are exactly "the steps we made through the
lifecycle" the icons show as glyphs — here they get their timestamps. Render them as a
chronology, not an unordered field dump.

**A little history**, per the request. `event` rows carry `item_id`, and `job` rows carry
`item_id`. `api/history.py` already serves both with filters and a `MAX_LIMIT` cap.

- Check whether it can already filter by `item_id`. If not, add it rather than fetching
  everything and filtering client-side.
- **Keep it bounded.** `api/history.py` deliberately does *not* inline `output_tail` because
  its row set is unbounded (see `docs/decisions.md`'s phase 6 entry). Respect that reasoning:
  fetch a small number of recent rows on demand when the drawer opens, never eagerly for every
  row in the tree.
- Delete-audit events (`remote_delete`, `remote_delete_withheld`, `local_delete`,
  `archive_cleanup`) are the most valuable thing here — they explain why bytes vanished.

## Before you start

- `frontend/src/components/ItemDrawer.tsx` and `FileTree.tsx` — both are virtualized with
  `@tanstack/react-virtual`.
- `core/itemview.py`, `core/local_scan.py`, `core/reconcile.py`, `core/engine.py._persist`.
- `api/history.py` and its `docs/decisions.md` phase 6 entry.
- `prompts/open-issues.md`.

## Working tree check

`git status --porcelain`. The row-revamp task touches `FileTree.tsx` and `core/itemview.py`
heavily and should have landed first. If it has not, or those files are dirty, stop and ask —
do not race it.

## Performance

`FileTree.tsx` is virtualized and a queue can hold thousands of rows.

- A hover tooltip must not trigger a re-render of the list, and must not fetch anything.
- History loads **on drawer open**, for one item, never per row.
- No per-row timers. A single shared ticker already exists for relative times (`57f7ce9`);
  reuse it rather than adding a second.

## Tests

- `local_mtime` survives the whole path: scan → reconcile → persist → all three projection
  consumers.
- The migration backfills sanely and does not disturb existing rows.
- Both-sides rendering when only one side exists, when both do, and when they disagree.
- Per-item history returns only that item's rows, and is bounded.
- A directory's `local_mtime` follows whatever rule you chose, tested.
- No history request fires from hovering, or from rendering the tree.

## Conventions to honor

- `docs/decisions.md`, newest at top — especially the directory-`mtime` convention and the
  tooltip-vs-drawer split.
- `CHANGELOG.md` under `### Added`.
- `DESIGN.md` §9.2 — draft wording, do not apply without a nod in the report loop.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up.
- **You cannot see the UI.** Say "builds, type-checks, lints, and the endpoints it calls were
  verified" — never "renders correctly", and never claim a layout looks good.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: file list, proposed one-line `feat:` message, the
   tooltip-vs-drawer decision and why, the directory `local_mtime` rule, whether
   `api/history.py` needed a new filter, test count, lint results, and anything not fixed.
   Never `git add -A`, never push.
