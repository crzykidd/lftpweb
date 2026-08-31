---
name: 2026-08-31-document-category-binding-requirement
status: completed        # pending | completed | failed
created: 2026-08-31
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-31
result: Documented the category-binding requirement and its silent-failure mode in README.md (setup note, troubleshooting entry, known gap), docs/download-client-framework-spec.md (§8.3 round 7, §9.2 cross-reference, §13.4 row 14), DESIGN.md §17.5, and docs/decisions.md; corrected two stale provenance labels (usePreflight.ts poll-interval comment, sabnzbd.py's paused-flag docstring). No behavior changed; gates unchanged from baseline.
---

# Task: document the category-binding requirement, and the silent failure when it is missing

**Docs-and-comments only. No behavior changes.** (One stale code comment is corrected; see §4.)

A live diagnosis on 2026-08-30/31 burned a long session on a failure mode the docs do not cover.
The user's ask: *"We need to make sure docs cover this well cause it gets confusing."*

## What actually happened — the facts to document

The user's SABnzbd instance was polling fine (`last_poll_ok: true`, all fields `native`), but
**every one of its categories was `excluded: true, queue_id: null`** — auto-recorded that way by
migration 032's "every newly observed category defaults to excluded" rule. Observed live:

```
=== ultracc SAB                       === ultracc rtorrent
   dc-movies  queue_id=None excl=True    ar-tv      queue_id=2    excl=False
   ar-movies  queue_id=None excl=True    ar-movies  queue_id=1    excl=False
   dc-tv      queue_id=None excl=True    dc-tv      queue_id=None excl=True
   ar-tv      queue_id=None excl=True    ...
```

Consequences, none of which announce themselves:

- The client contributed **zero** Preflight rows.
- Preflight still showed the downloads — sourced from the *arr, with `source: "arr"`,
  `contributors: []`, and `download_client: "SABnzbd-ultracc"` (the *arr's own tooltip fact). So
  the UI said "downloading from SAB" while every number came from Sonarr.
- Progress therefore refreshed on the *arr's cadence instead of the client's, appearing to
  "freeze for 60 seconds then jump."
- SABnzbd's global pause never appeared, because there was no client row to mark paused.
- The download-client icon never appeared for those items.
- **`unattributed_clients: []` and `gated_queues: []` the whole time** — an excluded category is
  filtered out of the unattributed count, so every diagnostic read healthy.

After binding `ar-tv`/`ar-movies` to their queues and un-excluding them, rows immediately became
`source: "client"`, `contributors: ['arr', 'client']`, and remaining-bytes moved every ~5s
(578M → 532M → 441M → 396M → 303M over 27s).

## What to write

### 1. `README.md` — setup and troubleshooting

A short, user-facing section: **after adding a download client you must bind each category you
want lftpweb to act on to a queue and un-exclude it.** Newly observed categories are recorded
excluded by design (safety: more than one lftpweb instance can share a seedbox — finding #16), so
a brand-new client does nothing until configured.

Then a troubleshooting entry named for the symptom, because that is what someone will search for
— something like *"Preflight progress only updates every ~60s / my download client's pause or
icon never shows."* Give the check and the fix: Settings → Clients, look for categories with no
queue and `excluded`, bind them.

Add to **Known gaps**: an excluded, unbound category is currently invisible to every diagnostic —
it is filtered out of `unattributed_clients`, so a fully-working connector can contribute nothing
while the UI reports no problem. Name it as a known gap; **do not fix it in this task.**

### 2. `docs/download-client-framework-spec.md`

Where the category→queue binding and Preflight contribution rules live (§9.2 and the attribution
sections), state plainly that **an excluded or unbound category contributes no Preflight rows**,
and that when the *arr also tracks the release the row still appears — sourced from the *arr,
with the client absent from `contributors`. Say what that looks like on the wire, since it is the
fastest way to tell the two apart:

| Client contributing | `source` | `contributors` |
|---|---|---|
| yes | `"client"` | `['arr', 'client']` |
| no | `"arr"` | `[]` |

### 3. `DESIGN.md` §17

One paragraph in the same voice: binding is a prerequisite for every client-derived feature —
Preflight freshness, the settle-gate skip, the pause state, the row icon. Not a separate toggle
for each; they all die quietly together.

### 4. Two corrections

- `frontend/src/hooks/usePreflight.ts` — its comment says the *arr poll default is 60s. It has
  been **10s** since 2026-08-21 (`core/arrsync.py.ArrSettings.poll_interval_s`). Fix the comment
  only; leave `POLL_INTERVAL_MS` alone.
- `core/clients/sabnzbd.py` and `docs/download-client-framework-spec.md` §13.4 row 14 — the
  top-level `queue["paused"]` flag was labelled **doc-derived, UNVERIFIED**. It was **confirmed
  against the user's live SABnzbd on 2026-08-31**: pausing the queue produced the paused state on
  Preflight rows. Update both labels to measured, dated, in the same style §13.4's other measured
  rows use. Do not change any mapping or logic — only the provenance label.

### 5. `docs/decisions.md`

Newest at top: record the diagnosis, why it took so long (every cadence measured correct while the
real question was whether the client produced rows at all), and the wire-level tell above as the
first thing to check next time.

## Explicitly out of scope

- **Do not** add a UI banner, change the exclusion default, or make excluded categories visible in
  `unattributed_clients`. That is the real fix for the underlying gap and is a separate task —
  README names it as a known gap; leave it there.
- Do not touch any behavior, mapping, threshold, or default anywhere.

## Gates

Docs and comments only, but run them anyway, each its own **foreground** command from the repo
root, reading each exit code: `uv run pytest`, `uv run ruff check .`, `uv run ruff format
--check .`, and — because §4 touches a `.ts` file — `npm --prefix frontend run lint`,
`npx tsc -b --noEmit` (from `frontend/`), `npm --prefix frontend test`. Baselines: backend 2040
passed / 49 skipped, frontend 843 passed / 41 files. **Both should be unchanged** — if a count
moves, you changed behavior and must explain why.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. **Do not commit.** Prepare the working tree, then report back: the file list, a one-line
   `docs:`-prefixed commit message, and the gate results. Never `git add -A`, never push.
