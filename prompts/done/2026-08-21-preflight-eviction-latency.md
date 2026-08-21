---
name: 2026-08-21-preflight-eviction-latency
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Retirement moved to request time in ArrSyncScheduler.preflight_rows (now async), sharing
  _record_matches_any_item with _preflight_candidates; frontend poll dropped 15s->5s. Central
  test confirmed failing pre-fix (stash/run/restore), all gates green (1585 backend / 607
  frontend tests, 0 skipped).
---

# Task: a handed-over Preflight row should disappear at request time, not at the next *arr poll

From the user's browser review, 2026-08-21:

> *"it does take 20-30 seconds for items to be removed from preflight after it shows in active
> sometimes"* … *"sometimes it is fast and sometimes it is slow"*

The variance is the tell, and it identifies the cause precisely.

## The diagnosis

The **evict-on-handover** fix (commit `d02fc0d`) works — it removed the 150 s `PREFLIGHT_HOLD_S`
window. What remains is a different term that was always underneath it: **retirement is only
*decided* when the *arr poller runs**, and `ArrSettings.poll_interval_s` defaults to **60 s**.

So:

- item lands just **before** a poll → retired almost immediately → feels fast
- item lands just **after** a poll → waits nearly a full 60 s → feels slow
- plus up to **15 s** for the frontend's own `usePreflight` poll to fetch the change

Total 0–75 s, unevenly distributed. That is exactly "sometimes fast, sometimes slow."

## The fix, and why it is cheap

**Retirement does not need the *arr at all.** The question "should this row still show?" is really
"does a matching `item` row exist now?" — a purely local database query. It currently happens once
per *arr poll only because that is where the code lives (`_preflight_candidates`), not because it
depends on anything the poller fetches.

**Move that check to request time**, in `api/jobs.py`'s preflight endpoint — which already performs
live DB queries for `source_configured` and the gated-queue banner, and whose own docstring says
every query there is deliberately fresh "so a change ... hides immediately rather than waiting for a
cache to catch up." Filtering handed-over rows at serve time is the same principle applied to the
one case that currently does not follow it.

That collapses the 0–60 s term to zero. What remains is the uniform 15 s frontend poll.

### The care this needs

**Extract and share the match predicate — do not reimplement it.** `_preflight_candidates` already
decides "does this record match an existing item". A second, independent definition at the endpoint
would drift, and drift here means rows wrongly reappearing or wrongly vanishing. Pull the predicate
into a shared helper both call, and say in your report where you put it.

**Keep `core/preflight.py` source-agnostic.** It has stayed free of any `arr`/`settle` reference in
code through four tasks. If the shared predicate is *arr-specific (it matches *arr records against
items), it belongs in `core/arrsync.py` or its own module — **not** in `core/preflight.py`.

**The settle source needs no change** — it is recomputed wholesale from local state every scan pass
and already has no such latency.

**Do not raise the *arr poll rate** to fix this. That would hammer someone else's API to solve a
problem that is purely local, and the poll interval is a user-facing setting with its own reasons.

## Also: shorten the frontend poll

`frontend/src/hooks/usePreflight.ts` polls at **15 s**, chosen when the box's data only changed as
fast as the *arr poller behind it. With retirement now evaluated per request, the endpoint's
freshness is no longer bounded by that, so the frontend interval becomes the dominant remaining
delay.

Drop it to about **5 s**, matching `StatsHeader`'s health poll. The endpoint is a cached projection
plus two small queries — cheap enough. **Update the hook's own comment**, which currently explains
the 15 s choice in terms of the *arr's cadence and would otherwise become misleading.

## Before you start

- `backend/lftpweb/api/jobs.py` — the preflight endpoint, its live queries, and its docstring about
  freshness.
- `backend/lftpweb/core/arrsync.py` — `_preflight_candidates`, `_update_preflight`, `_record_identity`.
- `backend/lftpweb/core/preflight.py` — `PreflightHold`, `update`'s `retired` parameter, and the
  source-agnostic contract.
- `frontend/src/hooks/usePreflight.ts`.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

- **The latency itself**: a record held in the projection whose item now exists must be absent from
  the endpoint's response **without any poller pass having run in between**. Write it so it fails
  against today's code and confirm that it does — that is the whole point of the task.
- The shared predicate gives identical answers at both call sites (poller and endpoint) — a table
  of cases exercised through both paths is better than two separate tests that could drift.
- Existing behaviour must survive: flap tolerance still holds a merely-absent row for the full
  window; a genuinely present record still shows; the settle source is unaffected.

## Docs

`CHANGELOG.md` under `[Unreleased]`; `docs/decisions.md` recording that retirement is evaluated at
request time and why (local state, no *arr dependency) — the previous entry describes the
retired-vs-absent distinction and should be extended rather than contradicted.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`fix:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say what a human should watch: a release crossing from Preflight to
  Active should now vanish from Preflight within a few seconds, consistently — not sometimes fast
  and sometimes half a minute.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
