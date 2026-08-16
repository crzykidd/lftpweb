---
name: 2026-08-16-history-jobs-group-collapse
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: >
  History's jobs section now groups by queue like Transfers -- single-click collapse per queue,
  remembered separately from Transfers (own `history.collapsedQueues` storage key), header shows
  queue name + outcome counts (succeeded/failed/cancelled) + total size. Because the jobs list is
  paginated, aggregates are computed server-side: a new `queue_summaries` block on
  `GET /api/history/jobs` (`HistoryQueueSummaryOut`, one bounded GROUP BY honoring the same
  filters as the list), inlined rather than a second endpoint (docs/decisions.md explains why).
  `lib/transferPanel.ts` gained History-specific helpers reusing the Transfers ones where the
  shape matched (formatQueueGroupCounts) and adding variants where it didn't
  (groupHistoryJobsByQueue, historyQueueGroupCounts, decrementHistoryQueueSummary,
  read/writeHistoryCollapsedQueues). Events section untouched. All 5 gates green: lint, 343
  frontend tests (+14), build, ruff check+format, 1159 backend tests (+5, 0 skipped). Unviewed --
  no browser in this environment.
---

# Task: History page jobs section — collapsible queue groups with summary headers

User request (2026-08-16): the same grouping treatment just applied to the Transfers page
(`2026-08-16-transfers-group-by-queue.md`, see its `done/` result) goes on the **History
page's jobs section**: single-click collapse per queue group, remembered state, and a
queue-summary header line.

## Before you start

- Read the Transfers grouping task's result in
  `prompts/done/2026-08-16-transfers-group-by-queue.md` and the helpers it created —
  **reuse them** (grouping, aggregate formatting, the localStorage collapse-state
  helper); do not fork parallel implementations. Extend a helper if History needs a
  variant, in place.
- Read `frontend/src/components/HistoryJobsSection.tsx`: it already groups by queue by
  flattening headers + rows into one virtualized array — keep the virtualizer (History's
  row set is unbounded, unlike Transfers).
- Read `backend/lftpweb/api/history.py`: the jobs list is `LIMIT`/`OFFSET` paginated and
  filterable. **This is the key difference from Transfers**: client-side aggregates over
  the loaded page would show wrong totals whenever more rows exist than are loaded.

## What to do

1. **Backend — honest aggregates**: add a bounded aggregate to the history jobs API — a
   per-queue `GROUP BY` summary (job counts by state, summed `bytes_done`) honoring the
   **same filters** as the list query (queue/state/error class/date range). Either a
   small `GET /api/history/jobs/summary` endpoint or an optional inlined `summaries`
   block on the existing response — pick what fits `api/history.py`'s conventions best
   and say why. One cheap SQL aggregate, no per-row blobs (the phase-6 trap).
2. **Frontend**: queue group headers in the jobs section become single-click
   collapse/expand (collapsing filters that queue's rows from the flattened virtualized
   array); header shows queue name + the server-side aggregates (counts by outcome with
   zero counts omitted, total size). Collapse state persisted per queue via the shared
   helper — **same storage shape, different key namespace** than the Transfers page (a
   queue collapsed on Transfers is not implicitly collapsed on History).
3. Leave the Events section alone — jobs section only, per the user's ask.
4. Tests: backend — the aggregate honors filters, groups correctly, and stays bounded;
   frontend — collapse filtering of the flattened array, header assembly from the
   summary payload, persistence round-trip, default expanded.
5. Docs same commit: `CHANGELOG.md` Unreleased entry; startnewsession.md arr build-run
   table row; `docs/decisions.md` if a non-obvious call comes up (the
   endpoint-vs-inlined choice may deserve one).

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report; ships unviewed.
- No new dependencies. `feat:` prefix.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` — CI's exact pinned commands and
   scope.
5. `uv run pytest` — note skip counts honestly.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
