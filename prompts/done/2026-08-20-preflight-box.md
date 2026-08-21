---
name: 2026-08-20-preflight-box
status: completed          # pending | completed | failed
created: 2026-08-20
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-20
result: Shipped a source-agnostic Preflight box (core/preflight.py's PreflightRow/PreflightHold,
  GET /api/queue/preflight) fed today only by the *arr poller's discarded queue records
  (arr_visible_path attribution, 150s flap-tolerance hold, no-duplicate-at-handover by
  construction) — 5-row-default/expandable/paged box, hidden with no source configured. Backend
  1544/1544 passed (+14), frontend 573/573 passed (+7), all gates green. Browser-unverified.
---

# Task: a "Preflight" box — releases an API source knows about that haven't reached the seedbox yet

User design, 2026-08-20. A **third, small box at the top of the Transfers → Queue tab**, called
**Preflight**: items we can see from an API source but which lftpweb has **no work to do on yet**,
because they have not landed in the seedbox's completed folder.

> *"maybe we add one more small box with like 5 rows by default that can be expanded and paged
> that is called 'Preflight' — these are items we see from API sources but don't actually have any
> work to do"*

The point (user's words): *"to not only understand the files feeding from your seedbox but also
the status of things being processed"* — full pipeline visibility, grabbed → downloading →
landed → transferring → verifying → imported.

## Why this is cheap: the data is already being fetched and discarded

`core/arrsync.py`'s poller already calls `/api/v3/queue` on every bound instance every ~60 s, and
`core/arrclient.py.QueueRecord` **keeps the full `raw` response dict** — its docstring says
explicitly that this exists "so a future caller (or a test) can read a field this projection
doesn't name yet without a client change." Today, records that **match** an lftpweb item drive
`arr_matched`; records that match nothing are **ignored**. Those ignored records are exactly this
box's contents.

**No new integration, no new credential, no new poll, no migration.**

## The architectural rule — Preflight rows are NOT `item` rows

Same reasoning as `docs/transfers-redesign-spec.md` §4.6: a preflight release is in neither the
remote tree nor the local tree, so `core/engine.py._project` (whose `rel_paths` filter is
load-bearing, not tidiness) would drop it every scan, and every §3.2 state rule is defined in terms
of remote/local presence.

**Make it a pure projection of the latest poll — no table, no persistence.** The poller already
holds the records in memory. This gives a property worth stating in code: **nothing can accumulate
in this box.** A release that fails in SAB and drops off the *arr's queue simply stops being
projected. No TTL, no cleanup sweep, and none of the "nothing blocks forever" machinery the Active
box needed (`core/pipeline_flight.py`).

A restart empties the box until the next poll (≤60 s). That is acceptable; do not add persistence
to avoid it.

## The sharp risk: not everything in the *arr's queue is coming here

The *arr's queue contains **everything that *arr is downloading** — other categories, other
download clients, direct-to-disk clients. Projecting those would have lftpweb promising files that
never arrive, which is worse than showing nothing.

**Attribution rule, and the data is already configured:** each queue has **`arr_visible_path`** —
its local path as that *arr sees it (set on the user's production queues after the v0.2.2
diagnosis). Prefix-match a record's `output_path` against each bound queue's `arr_visible_path` to
decide which queue a preflight row belongs to.

- A record whose `output_path` matches no queue's `arr_visible_path` is **not shown**. Silence is
  the correct answer for "this isn't coming here."
- A record with **no `output_path` at all** (the *arr does not always populate it) — decide and
  justify. Leaning: show it attributed to the instance's bound queue **only if that instance is
  bound to exactly one queue**, otherwise omit. Do not guess between queues.
- `raw` also carries `downloadClient`; a client/category filter is available if `arr_visible_path`
  proves insufficient. **Do not build a settings UI for that in this task** — note it if needed.

## Flap tolerance — the opposite discipline from the Active box

The v0.2.4 production incident: **SABnzbd intermittently returns a blank queue to Sonarr**, so the
*arr's queue empties for a beat and refills on the next refresh. That is why the amber `dropped`
state exists. Here it is display-only, so a wrong verdict is not at stake — but rows appearing and
vanishing every minute is its own problem.

**Hold a row briefly after it stops being reported** rather than dropping it the instant one poll
misses it. Pick a hold that comfortably spans a single missed poll, say so, and make sure it cannot
turn into accumulation (a held row still disappears; it just does so a little later).

## Before you start

- `docs/transfers-redesign-spec.md` §4 (the phase-2 client model this prefigures) and §4.6
  (why pending rows are not `item` rows).
- `core/arrsync.py` — the poller, `_match_items`, and the module docstring's account of `dropped`.
- `core/arrclient.py` — `QueueRecord`, `TRACKED_DOWNLOAD_STATE_*`, and the note that the
  `trackedDownloadState` vocabulary **is** verified against a live Sonarr.
- `frontend/src/pages/TransfersPage.tsx` — the two existing boxes, `pageReadout`, the page-size
  selector, and the empty-state shell.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt.

## What to do

1. **Expose unmatched, still-downloading queue records**, attributed per the rule above, via the
   existing *arr poller. Include what the row needs to be useful: title, the *arr instance and its
   kind (for the brand chip), the tracked download state, and a size/progress figure **if the raw
   record already carries one** — do not add a request to enrich it.
2. **The box**: top of the Queue tab, above Active/pending — it is first in the pipeline.
   **5 rows by default**, expandable, and paged once expanded. Reuse the existing pager and
   `pageReadout` rather than a third pagination idiom; decide whether the page-size selector
   belongs here too (a 5-row box may not want one) and say why.
3. **The box scales to its content — this is an explicit user requirement, not a nicety.**
   *"if 0 rows it just shows the box and 1 row that says nothing in preflight. Keeps interface
   tight."* So:
   - Height **follows the row count up to the 5-row default** — do not reserve five rows' worth of
     space and render four of them empty.
   - **Zero rows → the box header plus a single line**, e.g. "Nothing in preflight." One line, not
     a padded empty state.
   - This box sits above the two main ones, so any wasted vertical space here pushes the actual
     work off-screen. Tightness matters more here than anywhere else on the page.
   - **Separate case, and flag it if you disagree:** when **no API source is configured at all**
     (no bound, enabled *arr anywhere), hide the box entirely rather than showing "Nothing in
     preflight" forever — that line is informative for someone with Sonarr bound and meaningless
     for someone without.
4. **The rows are inert, and the box is what makes that structural**: no queue position number, no
   chevrons, no Dismiss, no Start now, no Stop. There is no job and no bytes. Make sure none of
   those affordances can reach a preflight row.
5. **Retirement**: when the release actually lands and becomes a real item, the preflight row must
   disappear — the item takes over. Verify there is no window where both are visible; a release
   appearing twice in one view is the failure mode to avoid.

## Tests

Attribution (matching and non-matching `arr_visible_path`, absent `output_path`, an instance bound
to several queues); a matched record is **not** projected; flap tolerance across one missed poll;
no-source-configured hides the box; and the no-duplicate property at handover.

## Docs

`CHANGELOG.md` under `[Unreleased]`; `DESIGN.md` §9.2; **`docs/concepts.md`** — this is a
user-visible concept people will ask about ("why is this in Preflight and not downloading?");
`docs/decisions.md` for the projection-not-persistence choice and the attribution rule; and add it
to `docs/transfers-redesign-spec.md` — it prefigures §4's client work and should be recorded as
such rather than looking like an unplanned addition.

## Conventions to honor

- **Never background a verification gate.** Foreground, explicit generous timeout (pytest ~3.5 min;
  set the tool timeout to 600000 ms), read each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`,
  `uv run ruff format --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There
  is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should check first.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
