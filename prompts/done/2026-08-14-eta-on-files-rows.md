---
name: 2026-08-14-eta-on-files-rows
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Verified the parent item's ETA was already fully computed server-side
  (core/progress.py.JobProgress.eta_s, already on the `progress` WS message) -- no backend
  change. Added `etaByItemId` to useLiveModel.ts (same shape as speedByItemId), threaded it
  through FileTree.tsx's buildTree onto a new TreeEntry.eta_s field. Added two pure functions
  to lib/format.ts: transferEtaLabel (job-level, same DOWNLOADING gate as transferSpeedLabel)
  and childEtaS (client-side derivation for a leaf file: remote_size - local_size divided by
  its freshness-gated child_speed_bps, null on every degenerate case, uncapped on the high
  end). FileTree.tsx's effectiveEtaLabel picks job-level first, child-level fallback, same
  shape as effectiveSpeedLabel. Appended into the existing Speed cell ("34 MB/s · 3m") rather
  than a new column or hover-only -- rejected alternatives and the no-second-sort-key call
  recorded in docs/decisions.md. Widened the Speed column's defaultWidth 88px -> 128px
  (unverified against a real browser -- no UI access in this environment; needs a human check
  at a narrow viewport). New tests: lib/format.test.ts (transferEtaLabel, childEtaS -- normal
  case, remote_size null, zero/no/non-finite/negative rate, remaining <= 0, a very small rate
  producing a very large uncapped ETA) and FileTree.test.ts (buildTree's eta_s resolution,
  effectiveEtaLabel's job-first/child-fallback/stale-sample/completed-child cases). All
  verification green: npm run lint (0 errors, pre-existing warnings only), npm test (221
  passed), npm run build (tsc + vite clean), uv run pytest (996 passed, 1 failed in
  tests/test_delete_during_transfer_e2e.py -- unrelated, that file was already modified by a
  concurrent session's in-flight work, not touched by this task), ruff check (clean), ruff
  format --check (clean except one pre-existing, not-mine file in the same concurrent
  session's diff), docker compose config --quiet on all three compose files (clean). Did not
  commit or push, per instruction -- reported the file list and proposed commit message back
  to the orchestrating session. Flagged that CHANGELOG.md now carries both this task's edit
  and a concurrent session's unrelated edit interleaved in the same file, so a plain `git add
  CHANGELOG.md` would stage both.
---

# Task: Show an ETA on Files rows — both the top-level item and each file inside a mirror

The Files page shows a transfer **rate** but never says how long the thing will take. When you
are watching a 20 GB release land, "3m left" answers the question you actually have; "34 MB/s"
makes you do the arithmetic. Show an ETA on the top-level row **and** on each transferring child.

## Run this AFTER `2026-08-14-per-file-speed-inside-a-mirror.md`

That task establishes the per-child sampling, the smoothed per-child rate, and the item-keyed
wire message this one needs as its denominator. If that prompt has not landed yet, **stop and say
so** rather than duplicating its machinery. Check `prompts/done/` for it first.

## What already exists

- **The top-level item's ETA is already computed.** `core/progress.py` produces `JobProgress.eta_s`
  from its EMA-smoothed speed, and `core/queue.py._sample_and_publish_progress` already publishes
  it on the job-centric `progress` message alongside `speed_bps`. Nothing new is needed
  backend-side for the parent — **verify this yourself before writing code**, and if it is already
  displayed somewhere (check `TransfersPage.tsx`), reuse that formatting rather than adding a
  second one.
- **The frontend already has an item-keyed speed map** (`speedByItemId`, `f728373`) built from
  that same message. An ETA map is the same shape.
- **`lib/format.ts` already has `formatEta`.** Reuse it. Do not add a second duration vocabulary.

## What has to be built

**Per-child ETA.** A child's remaining bytes are `item.remote_size - local_size`, both persisted
(`_publish_child_progress` already compares exactly these two for its `DOWNLOADED`/`PARTIAL` leaf
rule). Divided by that child's smoothed rate from the preceding task, that is the ETA.

Guard every degenerate case, and prefer showing **nothing** over showing a number that is wrong:

- **`remote_size` is `NULL`** (remote size not yet known) — no denominator, no ETA. That column is
  deliberately left alone when unknown, since "unknown vs. 0" is not the progress path's call to
  make; respect that here too.
- **Rate is zero or has no fresh sample** — no ETA. Never render `∞`, `--:--`, or a huge number.
- **Remaining bytes ≤ 0** (local already meets or exceeds remote) — the file is done; no ETA.
- **An absurd result** (hours, from a rate that just collapsed to a trickle) — decide whether to
  cap the display or show it honestly, and say which in `docs/decisions.md`. Honest is usually
  right; a capped "> 1h" is acceptable if you justify it.

## The design decision: where does it go?

This is the part to think about rather than pattern-match. The Files columns are **already
narrow** — `a4a626d` trimmed the labels once specifically because they were clipping, and
`f728373` has since added a Speed column. A seventh fixed-width column squeezes the flexing Name
column further, and Name is the one carrying the information people actually scan by.

Options, in the order I would consider them:

1. **Inside the existing Speed cell** — `34 MB/s · 3m`. No new column, no additional width
   pressure, and rate and ETA are genuinely one thought. Risk: the cell gets busy at narrow
   widths, and the sort key becomes ambiguous (does the column sort by rate or by ETA? Keep
   sorting by rate and say so).
2. **A new ETA column.** Cleanest semantically, sortable in its own right, worst for width.
   If you pick this, check the persisted-column-width migration path — a layout saved before the
   column existed must not corrupt the header (the same concern `f728373` had to handle).
3. **Hover / drawer only.** Cheapest and safest, but it does not answer the question at a glance,
   which is the entire point of the request.

**Recommended: option 1.** Pick deliberately, implement one, and record the rejected alternatives.

Apply the same treatment to the top-level row and the child rows, so the page reads consistently
rather than having ETA mean different things at different depths.

## Testing

- Pure-function tests for the ETA derivation: normal case, `remote_size` null, zero rate, no fresh
  sample, remaining ≤ 0, and a very small rate producing a very large ETA.
- A test that a completed child shows no ETA.
- Frontend tests for whatever gating you land on, including the stale-sample case.
- Run `uv run pytest` (fake seedbox is likely already running — if so, leave it), `ruff check`
  **and** `ruff format --check`, `npm run lint`, `npm test`, `npm run build`, and
  `docker compose config --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives.
- Update `docs/concepts.md` if it describes what the Speed column shows — it is the single source
  the in-app Docs render from, so a stale description there is wrong in two places at once.
- `CHANGELOG.md` entry.
- **You cannot see the UI** — no browser exists here. Width and busyness are exactly the things
  you cannot judge from the code, so say plainly that the chosen layout needs a human to look at
  it, especially at a narrow window.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
