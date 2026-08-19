---
name: 2026-08-19-transfers-row-shows-eta
status: completed
created: 2026-08-19
model: sonnet
completed: 2026-08-19
result: "transferLineValue now appends a '<duration> left' figure for a running row once eta_s is known (live sample preferred, job's own eta_s as fallback, omitted when null); TransfersPage.tsx's row widened w-32 to w-44 to fit it. Tests extended in transferPanel.test.ts; all six gates green."
---

# Task: The collapsed Transfers row shows time-to-complete for a downloading item

User request (2026-08-19): the Transfers page is the queue view, and a downloading
row's collapsed line should answer "how long until this finishes" without expanding.
Today the collapsed row deliberately shows percent + live rate (2026-08-15, the
single-line-rows task: "the two figures that answer 'is this actually moving'"); the
ETA already exists on the wire (`JobOut.eta_s`, live-updated via the progress ticks)
and is already rendered inside the expand panel's Transfer group as
"`<rate>` (ETA `<eta>`)" — this task surfaces it one level up.

## Before you start

- Read `CLAUDE.md`. Read before editing:
  - `frontend/src/lib/transferPanel.ts` — the collapsed row's live-figure helper
    (~line 33's "percent complete plus the live rate" docstring — find the function
    it documents), the expand panel's existing ETA composition (~line 149,
    `eta = running ? (opts.live?.eta_s ?? job.eta_s) : null` — reuse that exact
    null/live-fallback discipline), and `lib/format.ts.formatEta`.
  - `frontend/src/pages/TransfersPage.tsx` — where the collapsed row renders the
    live figures, and how live WS progress (`opts.live`) is threaded so the ETA
    ticks with the rate rather than going stale.
  - `frontend/src/lib/transferPanel.test.ts` — the existing collapsed-row tests
    you'll extend.

## Working tree check

Run `git status --porcelain` before editing; cross-reference; ask before touching
dirty files. This prompt file is exempt. **Coordination note:** the sibling task
`2026-08-19-rescue-requeue-keeps-queue-position` may have just landed —
`CHANGELOG.md` will carry its entry under Unreleased; append alongside.

## What to do

1. **A `DOWNLOADING` row's collapsed live figures gain the remaining time** —
   reading like "45% · 40 MB/s · 25m left" (match the page's existing separator
   idiom rather than inventing one; the "left" suffix — or an equivalent that
   unambiguously reads as remaining-time, e.g. "ETA 25m" — is your wording call
   against what fits the row, state it). Rules:
   - Only for an actively downloading row (the same `running` condition the expand
     panel's ETA uses); queued/terminal rows are unchanged.
   - Same live-value discipline as the panel: prefer the live WS sample's `eta_s`,
     fall back to the job row's, render nothing when null (a just-started transfer
     has no ETA yet — no placeholder, no "∞").
   - Pure-function first: the figure composition lives in `lib/transferPanel.ts`
     (extend the existing helper, don't fork a parallel one), `TransfersPage.tsx`
     only renders what it returns.
2. **Don't disturb the row's economy** — the 2026-08-15 task exists because rows had
   too many figures; this adds exactly one, only while downloading. No other fields
   move or appear.
3. **Tests** (`transferPanel.test.ts`): downloading + eta present → figure includes
   it; downloading + eta null → absent; queued/succeeded/failed rows → unchanged
   output; live-sample eta overrides job eta.
4. **Docs, same commit:** `CHANGELOG.md` Unreleased `### Added` (user-voiced: a
   downloading transfer's row now shows how long until it completes, next to its
   percent and speed). `docs/decisions.md` only if something genuinely non-obvious
   comes up.

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts, exit codes
  read: frontend `npm run lint`, `npm test`, `npm run build`; backend untouched —
  re-verify anyway (`uv run --project backend ruff check`, `uv run --project
  backend ruff format --check`, `uv run pytest` from the repo root, timeout
  400000ms; never background a gate or wait on Monitor notifications).
- Comment style: dated, matching the file's existing docstrings.
- No browser here — the row ships unviewed; say so.
- Conventional-Commit prefix `feat:`; no `Co-authored-by:` trailers.

## When done

1. Frontmatter: `status: completed` (or `failed`), `completed` date, one-line
   `result`.
2. Move this file into `prompts/done/` (or `prompts/failed/`).
3. Hand off ONE commit (prompt file + changes + move). Present file list + one-line
   message. **You are a spawned agent: do not commit, never `git add -A`, never
   push.** Branch is `dev`.
