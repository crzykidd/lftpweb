---
name: 2026-08-31-surface-inert-download-clients
status: completed          # pending | completed | failed
created: 2026-08-31
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-31
result: Added PreflightResponse.unbound_clients (backend, derived by set difference over source_configured's own client-half query, invariant-tested) and a fourth Preflight banner (frontend) for an enabled client with zero enabled bindings; docs updated (README, spec §8.3 round 8, DESIGN.md §17.5, decisions.md).
---

# Task: say so when a download client is configured but can contribute nothing

A live diagnosis on 2026-08-30/31 (see `docs/decisions.md`'s 2026-08-31 entry and spec §8.3 round
7) lost an hour to this: SABnzbd was polling perfectly, every field `native`, `last_poll_ok:
true` — and contributing **zero** Preflight rows, because all four of its observed categories were
`excluded: true, queue_id: null` (migration 032 records every newly observed category as excluded).

Nothing said so. `unattributed_clients: []`, `gated_queues: []`, `source_configured: true`. The
Preflight rows still appeared — sourced from Sonarr, naming SAB in `download_client` — so the UI
read "downloading from SAB" while every number was second-hand and refreshed on the *arr's
cadence. The user reasonably reported it as a refresh bug.

This task makes that state visible where the confusion happens.

## Before you start

Read, in this order:

1. `docs/decisions.md`'s **2026-08-31** entry and `docs/download-client-framework-spec.md` **§8.3
   round 7** — the incident, written up yesterday. This task is its named follow-up.
2. `backend/lftpweb/api/jobs.py.get_preflight` (~line 789) — specifically the live
   `source_configured` query for the download-client source: *"an enabled download-client
   instance with at least one enabled category->queue binding."* **That predicate is the whole
   task.** A client failing it can never contribute a row, however well it polls.
3. `backend/lftpweb/models.py.PreflightUnattributedClientOut` and `PreflightResponse` — the
   existing shape to mirror.
4. `frontend/src/components/PreflightBox.tsx` — how `unattributed_clients` is rendered today,
   and `frontend/src/lib/preflight.ts.unattributedClientDetail` plus
   `frontend/src/lib/clientEditLink.ts.clientEditHref` — the banner + deep-link pattern to reuse.
5. `backend/lftpweb/migrations/032_category_observed_and_acknowledged.sql` — why categories
   default to excluded (finding #16: two lftpweb instances can share one seedbox). **That default
   is correct and is not being changed.**
6. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## What to do

### 1. Report it on the Preflight response

Add a field to `PreflightResponse` listing every **enabled** download-client instance that
currently satisfies **no** enabled category→queue binding. Suggested name `unbound_clients`; pick
better if you can justify it, but do not reuse `unattributed_clients` — that field means something
different (transfers that *did* reach attribution and found no home) and conflating them would
recreate exactly the ambiguity this task exists to remove.

Per entry, carry what the banner needs to be specific and actionable: the instance id and name,
how many categories it has observed, and how many of those are excluded. Enough for a sentence
like *"ultracc SAB: 4 categories observed, all excluded — none bound to a queue."*

**Derive it from the identical predicate the `source_configured` gate already uses — one query,
two consumers.** Do not hand-write a second "is this client usable" rule. If the two ever drift,
the banner starts lying in one direction or the other, and a banner that lies about this is worse
than no banner. If the existing query isn't reusable as-is, factor it out so both call it; say in
your report which way you went.

### 2. Show it in the Preflight box

Render it the way `unattributed_clients` is already rendered — same visual language, not a new
one — with a deep link to that instance via `clientEditHref`.

Wording rules:

- Say what is true and what to do: the client is connected, nothing is bound, so it contributes
  nothing. Avoid "inert"/"unbound" as user-facing jargon.
- **Never imply the exclusion default is a mistake.** It is a deliberate safety boundary. The
  problem is that no category has been bound *yet*, not that excluding was wrong.
- Plain language for the state, consistent with the disk-review copy conventions
  (`Not monitored here`, `Unassigned`) rather than internal field names.

### 3. Self-clearing, no dismissal

It is derived live from configuration on every request, so it disappears the moment a binding
exists. **No dismiss button, no acknowledgement, no confirmation dialog** — this project's house
style is fewer clicks, not more, and `categories_acknowledged_at` already covers the different
"new since you last looked" signal on the Settings tab. Do not touch that mechanism.

## Explicitly out of scope

- Do **not** change the exclusion default, migration 032, or `categories_acknowledged_at`.
- Do **not** warn about a client that has *some* bindings — a deliberately excluded category on
  an otherwise-configured client is the safety boundary working, and warning there would nag
  forever about the other instance's categories.
- Do **not** touch the disk review page, `core/disk_review.py`, or `api/disk_review.py`.
- Do **not** change attribution, matching, or any poller cadence.

## Tests

Backend:

- An enabled client with zero enabled bindings is reported, with correct observed/excluded counts.
- A client with **one** enabled binding is **not** reported, even if its other categories are all
  excluded. Name this test so it is obvious it protects against nagging the safety boundary.
- A **disabled** client is never reported.
- The reported set and `source_configured`'s own client half agree — assert them against each
  other in one test, so a future drift between the two fails loudly. This is the invariant test.

Frontend: the banner's text/visibility decision as a pure function in `lib/preflight.ts`, tested
there. This repo has **no component-rendering tests** (`vitest run` over pure functions only, no
`@testing-library/react`) — do not invent a harness for one banner.

## Layout

The Preflight box already stacks banners. Adding another must not push the row list around or
introduce a wrapper that clips. **jsdom performs no layout**, so no test will catch it — say so in
a comment rather than writing one that pretends to. If it looks wrong, ask for a screenshot.

## Conventions to honor

- Match the surrounding docstring style — these modules explain *why* at length, including which
  decision a line reverses.
- Doc updates ship in the same change set: `README.md`'s Known gaps entry for this now **closes**
  — replace it with a pointer to the behavior rather than deleting the history; spec §8.3 and
  `DESIGN.md` §17 where the diagnostic surfaces are described; `docs/decisions.md` newest at top.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `npm --prefix frontend run lint`, `npx tsc -b --noEmit` (from `frontend/`), `npm --prefix
  frontend test`.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. Never `git add -A`,
   never push.
