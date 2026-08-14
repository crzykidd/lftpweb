---
name: 2026-08-14-transfer-timing-and-throughput-display
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Added frontend/src/lib/transferTiming.ts (elapsedSeconds, queuedWaitSeconds,
  isNotableQueuedWait, averageSpeedBps, postprocessNote) with full test coverage. Wired
  elapsed/average-speed/queued-wait/postprocess-note into TransfersPage.tsx's Row, and
  per-attempt elapsed/average into ItemDrawer.tsx's history list. Item 3 (post-processing state
  label) resolved without new plumbing: TransfersPage already loads the item's FileNode via
  useLiveModel's nodesByQueue, looked up by job.item_id. Deliberately deviated from the prompt's
  literal "bytes_done / elapsed_seconds" formula for TransfersPage in favor of
  (bytes_done - bytes_start) / elapsed, matching core/metrics.py's documented non-monotonic-trap
  fix -- see docs/decisions.md. ItemDrawer's history rows use bytes_done / elapsed (bytes_start
  isn't on HistoryJobOut) with the imprecision flagged in the figure's own tooltip, not treated
  as a required backend change. No backend touched. npm run lint/test/build, uv run pytest,
  ruff check/format --check, and docker compose config all clean.
---

# Task: Show how long a transfer took and what speed it achieved, on Transfers rows and in the item drawer

Every job already carries `queued_at`, `started_at`, `finished_at`, `bytes_done`, and
`bytes_total`, but nothing derives the two numbers a person actually wants: **how long it took**
and **what speed that works out to**. Surface them.

**This is a display task. No backend change is expected** — `api/jobs.py`'s `JobOut` and
`api/history.py` both already serialize every field needed. If you conclude a backend change is
required, stop and report rather than making one.

## Why this is worth doing

A real debugging session on 2026-08-13/14 had to reconstruct "49 seconds, ~34 MB/s" by hand from
two ISO timestamps in the API response, because nothing in the UI said it. The same session spent
significant effort on "why does this look like it's hanging?" when the answer was a post-transfer
step doing exactly what it should. Both are display gaps, not behaviour gaps.

## Before you start

- Read `CLAUDE.md` and `DESIGN.md` §9.2 (the Transfers page's own spec).
- Read `frontend/src/pages/TransfersPage.tsx`, `frontend/src/components/ItemDrawer.tsx`, and
  `frontend/src/lib/format.ts` (the existing byte/duration formatters — **reuse them, do not add
  a second formatting vocabulary**).
- Note `frontend/src/pages/TransfersPage.test.ts` already exists (2026-08-14) — extend it.

## Working tree check

Run `git status --porcelain` first. Other queued work touches `components/QueueResetControls.tsx`,
`components/FileTree.tsx`, `pages/FilesPage.tsx`, and the docs pages — if any file this plan needs
is dirty, list it and ask before editing. This prompt file is exempt.

## What to do

### 1. Elapsed time and achieved speed on every Transfers row

For each job row, derive and show:

- **Elapsed** — `finished_at - started_at` for a terminal job; `now - started_at` for a running
  one, ticking with the existing render cadence rather than a new timer if one is already
  available.
- **Average speed** — `bytes_done / elapsed_seconds`. Label it as an average, distinct from the
  existing live `speed_bps` (which is the EMA-smoothed instantaneous rate from
  `core/progress.py`). **Both are useful and they are not the same number**; do not replace one
  with the other, and do not let a reader mistake which is which.
- **Time spent queued** — `started_at - queued_at`. Usually trivial, occasionally the entire
  explanation for "why did this take so long" when the scheduler was holding it behind
  `max_concurrent_transfers`. Show it when it is non-trivial rather than always.

Put the derivation in `frontend/src/lib/` as **pure functions** so they are unit-testable without
mounting anything — that is the existing convention and the reason `lib/*.test.ts` exists.

Guard the arithmetic: `started_at` can be null for a `queued` job, `finished_at` is null while
running, and a sub-second elapsed time must not produce a divide-by-zero or an absurd rate.
Cover each of those in tests.

### 2. Per-attempt detail in the item drawer

`ItemDrawer.tsx` already shows a bounded job history. Add the same three figures per attempt, so
a retried item shows what each attempt actually achieved. This is where the numbers matter most —
a job that failed after 40 minutes at 2 MB/s tells a very different story from one that failed in
3 seconds, and today both render identically.

### 3. Say what a finished-but-still-working row is doing

**Context — this is a regression in perceived behaviour introduced 2026-08-14 by
`prompts/done/2026-08-14-exit-zero-is-not-completion.md`.** `list_jobs()` now keeps a recently
`succeeded` job on the Transfers page. That is deliberate and correct (a transfer used to run for
seven minutes and complete with no visible trace). But post-processing then runs for a while
afterwards — a real, measured example: a 1.7 GB `pget` finished at `05:19:16.872`, and `verify`'s
hash-on-disk whole-file read did not finish until `05:19:24.563`, **7.7 seconds later**.

During that window the row sits at 100% with **0 B/s**, which reads as a stalled transfer. It is
not: the transfer is done and verification is reading the file end to end.

Make the row say so. The item's own state (`VERIFYING`/`EXTRACTING`, written by
`core/postprocess.py` and published as an item delta) is the honest source.

**Check first whether the Transfers page can already see item state.** If it can, use it. If it
cannot without new plumbing, **report that and stop** — say what would be needed rather than
adding a backend field or a second polling call on your own initiative. A wrong guess here is
worse than the current ambiguity.

Do not fabricate a progress bar for post-processing; a state label is enough.

## Testing

Extend `frontend/src/lib/*.test.ts` and `pages/TransfersPage.test.ts`. Cover: null `started_at`,
null `finished_at`, zero/sub-second elapsed, a multi-hour transfer's formatting, and the
average-vs-instantaneous distinction.

Run `npm run lint`, `npm test`, `npm run build`. Run `uv run pytest` to confirm nothing backend
moved; if a backend test fails, stop and report rather than adapting it.

## Conventions to honor

- Reuse `lib/format.ts`'s existing byte and duration formatters.
- Non-obvious decisions go in `docs/decisions.md`, newest at top.
- **You cannot see the UI** — no browser exists in this environment. Every claim means "builds,
  type-checks, and lints cleanly", never "renders correctly". Say so plainly.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
