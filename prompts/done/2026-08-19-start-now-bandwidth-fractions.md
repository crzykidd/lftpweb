---
name: 2026-08-19-start-now-bandwidth-fractions
status: completed
created: 2026-08-19
model: sonnet
completed: 2026-08-19
result: "Start now" is a 10%/25%/50%/75%/Max menu of the site bandwidth limit end to end
  (scheduler, API, DB migration 022, frontend); all gates green.
---

# Task: "Start now" becomes a menu — 10% / 25% / 50% / 75% / Max of site bandwidth

User request (2026-08-19, design settled): the §4.5 "Start now at max bandwidth"
escape hatch becomes a dropdown. The button reads **"Start now"**; clicking it opens a
small menu with **10% · 25% · 50% · 75% · Max**; picking one admits the item
immediately at that fraction of the **site bandwidth limit** (Settings → Transfer's
total rate). Max is byte-for-byte today's behavior. Settled sub-decisions:

- The fraction is of the *site total limit*, computed **once at admission** — §4.5's
  allocations-are-never-reshaped rule is untouched, and other running jobs keep their
  existing allocations exactly as the current Max path already leaves them.
- **No site limit configured → fractions are meaningless**: the four percent options
  render disabled with a hint ("set a site bandwidth limit to use fractions"); Max
  remains enabled and behaves as today.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §4.5 in full (ordering, the floor loop, the fast
  lane, start-now, `forced_full_rate` — "set per-item by a dedicated action, not by
  raising rank"), §9.3 (transfer settings).
- Read before editing:
  - `backend/lftpweb/core/scheduler.py` — the pure `(settings, running, queue) ->
    admit` function, its `QueuedJob.forced_full_rate` field, and the §4.5 table test
    that pins every worked example — your change must extend that table, not break a
    row of it.
  - `backend/lftpweb/core/queue.py` — how start-now is stored/read (find the field
    the API sets), how the admitted job's rate cap flows into the lftp rc
    (`core/lftp.py` build path), and `TransferSettings` (the site total limit's
    actual field name).
  - The start-now API endpoint (grep `start-now`/`start_now` under `api/`) and the
    frontend control (`TransfersPage.tsx` — the button with the §4.5 one-time inline
    explanation from phase 3b; also check whether the Files page/item drawer offers
    it anywhere — every surface that offers it gets the same menu).
- Where a `forced_full_rate` boolean must become a fraction: prefer widening in
  place (e.g. `forced_rate_fraction: float | None`, `1.0` = Max) over a parallel
  field — and check whether it's persisted (migration needed, next is 022) or
  in-memory only before assuming either.

## Working tree check

Run `git status --porcelain` before editing; cross-reference; ask before touching
dirty files. This prompt file is exempt. **Coordination note:** two sibling tasks
(`…rescue-requeue-keeps-queue-position`, `…transfers-row-shows-eta`) may have just
landed — `CHANGELOG.md`/`docs/decisions.md` carry their entries; append alongside.
Both also touched `core/queue.py`/`transferPanel.ts` — read current versions.

## What to do

1. **Backend:** the start-now action accepts an optional fraction
   (`{rate_percent: 10|25|50|75|100}`, omitted = 100 = today, so existing callers
   are unaffected). Server-side validation rejects anything outside that set (422).
   At admission the job's rate cap = `fraction × site_total_limit` (rounded to whole
   B/s), flowing into the existing per-job rc cap exactly as the current allocation
   math does; `fraction = 1.0` must take the identical code path today's Max takes.
   A fraction request when no site limit is configured is a 409 with a reason (the
   frontend disables those options, but the server must not silently treat it as
   Max).
2. **Scheduler:** extend the §4.5 table test with fraction cases — a forced-fraction
   job admits with the same immediacy as forced-full, its cap reflects the
   fraction, and no other row of the existing table changes.
3. **Frontend:** every surface offering "Start now at max bandwidth" becomes a
   "Start now ▾" menu (10% / 25% / 50% / 75% / Max). Keyboard-accessible
   (Enter/Space opens, arrows navigate, Escape closes — match whatever menu idiom
   the codebase already has; if none exists keep it simple and native, e.g. a
   focus-managed listbox, not a new dependency). Percent options disabled + hint
   when the fetched transfer settings show no site limit. The §4.5 one-time inline
   explanation text updates to mention fractions. Pure decision logic (which
   options disabled, label per option, request payload) in a `lib/` helper with
   Vitest coverage; components render what it returns.
4. **Tests:** backend — API validation (422 set, 409 no-limit, omitted = Max),
   admission cap math, scheduler table additions; frontend — the pure helper's
   cases. An end-to-end fraction transfer against the fake seedbox is NOT required
   (the rc cap plumbing is already covered by existing tests — verify that claim;
   if the cap path is genuinely untested, add one bounded case).
5. **Docs, same commit:** DESIGN.md §4.5's start-now paragraph gains the fraction
   (this is a deliberate design extension — note it as such);
   `docs/concepts.md`/`docs/` wherever start-now is described; `CHANGELOG.md`
   Unreleased `### Added` (user-voiced); `docs/decisions.md` (fraction-of-site-limit
   base, 409-not-silent-Max, the widened-field-vs-parallel-field call, migration or
   not).

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from repo root, timeout 400000ms; never background a gate or wait
  on Monitor notifications), exit codes read: `uv run --project backend ruff
  check`, `uv run --project backend ruff format --check`, `uv run pytest`;
  frontend `npm run lint`, `npm test`, `npm run build`.
- Comment style: dated, §-citing.
- No browser here — the menu ships unviewed; say so and name what a human should
  check (menu keyboard nav, disabled-state hint, a real fraction transfer's speed).
- Conventional-Commit prefix `feat:`; no `Co-authored-by:` trailers.

## When done

1. Frontmatter: `status: completed` (or `failed`), `completed` date, one-line
   `result`.
2. Move this file into `prompts/done/` (or `prompts/failed/`).
3. Hand off ONE commit (prompt file + changes + move). Present file list + one-line
   message. **You are a spawned agent: do not commit, never `git add -A`, never
   push.** Branch is `dev`.
