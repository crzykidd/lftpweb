---
name: 2026-08-17-logs-search-and-lookback
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: >
  Backend: MAX_LINES_CAP 2000->10000, DEFAULT_MAX_BYTES 2MB->5MB (mirrors logsetup.MAX_BYTES
  via comment, not import). Frontend: Lines options extended to 10000; new pure
  lib/logFilter.ts (filterLogLines/logFilterSummary, Vitest-covered) wired into LogsTab.tsx
  as a client-side-only, instant substring filter over the fetched window, with a "showing N
  of M" readout. Left the lines view un-virtualized -- it's a single <pre> text node, not 10k
  row elements. Build-run table row lettered X, not W (row W already existed from an earlier
  same-day task). All verification gates green.
---

# Task: Logs page — text filter + 5k/10k lookback options

User request, design settled 2026-08-17: Settings → Logs needs (1) a search/filter over the
displayed raw log lines, and (2) a deeper lookback than today's 2000-line ceiling — the *arr
integration's per-minute poller HTTP lines (`httpx: HTTP Request: GET .../api/v3/queue`) now
dominate the log, so 2000 lines covers well under an hour on a busy install.

## Current numbers (verified — re-verify, don't trust)

- `core/logtail.py`: `DEFAULT_MAX_LINES = 200`, `MAX_LINES_CAP = 2000`,
  `DEFAULT_MAX_BYTES = 2 MB` (the per-tail read ceiling); a rotated file is at most
  `logsetup.MAX_BYTES` (5 MB).
- `api/logs.py.tail` clamps `lines` to the cap; the `level` filter is applied server-side
  after the tail.
- `frontend/src/pages/settings/LogsTab.tsx`: line-count options `[100, 200, 500, 1000, 2000]`
  and a truncation note when the read window didn't cover the file.

## What to do

### 1. Backend — raise the window

- `MAX_LINES_CAP` 2000 → **10000**.
- Per-tail byte ceiling 2 MB → **`logsetup.MAX_BYTES`** (5 MB) — reference the constant, with
  a comment saying why (the window should be able to cover one whole live log file); if
  importing it there creates a layering problem, mirror the value with a comment naming the
  linkage. 10k lines at typical line lengths (~150–200 B) fits inside 5 MB.
- `tail_file`'s memory bound is `max_bytes` plus a chunk (its own docstring) — confirm the
  instrumented byte-budget test still passes with the new ceiling and extend it if it pins
  the old number.
- The level filter is unchanged. No new server-side text filter — see the settled call below.

### 2. Frontend — options + text filter

- Line-count options become `[100, 200, 500, 1000, 2000, 5000, 10000]`.
- A text filter input beside the existing level filter: case-insensitive substring match over
  the fetched lines, applied client-side, instant (no refetch), with an easy clear and a
  "showing N of M lines" readout while active. The existing truncation note must remain
  accurate alongside it.
- **Settled call, record it:** the filter searches the *fetched window only* — at 10k lines
  that window can span an entire live file, which is the point of pairing these two features.
  A server-side grep across rotated files is deliberately NOT built (name it in the decisions
  entry as the rejected-for-now alternative). No match highlighting in v1 — filter only.
- Filtering logic (predicate + the N-of-M label) goes in a pure `lib/` module (new or an
  existing suitable one) with Vitest coverage, per the repo pattern; the component stays thin.
- With 10k lines rendered, check whether the lines container needs virtualization —
  `@tanstack/react-virtual` is already a dependency and the page currently renders a plain
  list. If 10k plain rows is plausibly janky, virtualize the same way
  `HistoryEventsSection.tsx` does; if you leave it plain, say so in your report and why.

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan touches. If
any have uncommitted changes, list them and ask before touching. This file is exempt.

## Docs, same commit

- `CHANGELOG.md` `[Unreleased]` → Added: one user-facing entry covering both.
- `docs/decisions.md`: entry only if you make a non-obvious call (the client-side-only filter
  scope and the byte-ceiling linkage qualify).
- `prompts/startnewsession.md`: add row **W** to the build-run table (after row V), same
  style, same commit. Note the UI is unviewed (no browser here).

## Verify — each gate separately, read each exit code

`uv run ruff check backend tests` · `uv run ruff format --check backend tests` ·
`uv run pytest` (full) · `npm test -- --run` · `npm run lint` · `npm run build`.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit (prompt file + changes + prompt move). **You are a spawned agent: do
   not commit.** Prepare the tree, then report the file list + proposed `feat:` message back
   to the orchestrating session, which surfaces the `y/n`.
