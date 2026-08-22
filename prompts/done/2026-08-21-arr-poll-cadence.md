---
name: 2026-08-21-arr-poll-cadence
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: >
  Verified the four bullets against core/arrsync.py and core/arrclient.py (all held). Dropped
  ArrSettings.poll_interval_s default 60.0 -> 10.0 (5s floor unchanged), exposed it via
  GET/PUT /api/settings/arr/poll-interval (server-validated 5-3600s) and a new Settings ->
  Integrations "Poll cadence" section, added observational arr_queue_multi_page event for a
  queue that grows past one page (no adaptive backoff), left existing installs alone (no
  migration -- save_arr_settings had zero call sites before this, so no real install had a
  persisted value), and updated docs (CHANGELOG, DESIGN, arr-integration-spec, decisions.md,
  startnewsession.md). 1663 backend / 654 frontend tests, 0 skipped. Not committed -- prepared
  the tree for the orchestrating session to commit.
---

# Task: expose the *arr poll interval and drop its default to 10s

Closes **[issue #16](https://github.com/crzykidd/lftpweb/issues/16)** — Preflight progress updating
in one-minute jumps, and import detection lagging 30–60 s.

## The design question is already answered — do not re-open it

Issue #16 asks whether a single poll dial is the wrong shape, and floats splitting the cadence
(queue vs history), an adaptive cadence, or answering from local state instead of polling harder.

**All of those were investigated in-session on 2026-08-21 and rejected, because the premise behind
them is false.** The issue says dropping to 5 s is "12× the request rate against Sonarr/Radarr." It
is not. Read `core/arrsync.py` and `core/arrclient.py` and confirm this for yourself before
building — if any of it turns out to be wrong, **stop and report** rather than proceeding:

- **The queue costs one HTTP request per instance per pass**, not one per item.
  `ArrClient.queue_records()` walks `/api/v3/queue` at `PAGE_SIZE = 250`, so any normal queue is a
  single page.
- **History is already by-id and already event-triggered.**
  `ArrClient.import_events(download_id=…)` is an exact lookup, and it is called only for items that
  need it — items awaiting import confirmation, `dropped` rows, and `gone` heal retries (which have
  their own backoff). It is **not** called per-item per-pass.
- **Both symptoms are gated on the queue poll.** `_check_pending`'s requirement 1 is that the queue
  record is gone or reports `trackedDownloadState: imported` — only once the *queue* poll observes
  that does the history lookup happen. So polling the queue faster fixes Preflight progress **and**
  import detection, and history volume rises only when something actually transitions.
- **Import confirmation needs two consecutive passes** observing both signals, so 60 s means up to
  ~120 s to confirm an import. At 10 s it is ~20 s.

Net effect of the change: **1 → 6 queue requests per minute per instance**, no increase in history
calls. That is trivial for Sonarr/Radarr, and it is why no cadence split, adaptive logic, or
local-observation trick is warranted.

## What to build

1. **Default `poll_interval_s` 60.0 → 10.0.** `ArrSettings` in `core/arrsync.py`.
2. **Keep the 5 s floor** (`ArrSyncScheduler.MIN_POLL_INTERVAL_S`) exactly as it is. 10 s is the new
   default, not the new minimum.
3. **Expose it in the API and the UI.** It is currently DB-only — stored as JSON in a `setting` row
   (`SETTING_KEY`), read via `data.get("poll_interval_s", 60.0)`. It needs to reach the *arr
   settings surface the same way that page's other settings do. **Follow the existing idiom on that
   page rather than inventing a new one**; validate server-side, not only in the browser.
4. **Guard the multi-page case.** The "one request per pass" property holds only while the queue fits
   in one 250-record page. A queue past that walks multiple pages, and doing that every 10 s is a
   genuinely different cost. Decide how to handle it — the cheapest honest option is to *observe* it
   (count pages walked, log or event once when a pass goes multi-page) rather than to build adaptive
   backoff nobody has needed yet. **Do not build an adaptive cadence.** State what you chose.

## The one call you have to make, and must state explicitly

**Existing installs.** The stored value wins over the code default, so an install with
`poll_interval_s: 60` already persisted keeps 60 unless something moves it.

**The relevant fact: this setting has never been exposed in the API or UI, so any stored value is not
a user choice** — it is a default that got written down. Decide whether to (a) leave stored values
alone and let the new UI control handle it, or (b) actively move existing installs to the new
default. **(b) is defensible precisely because no user ever chose 60**, but it is a behavior change
on upgrade and must be deliberate, not incidental. Pick one, say which, and record the reasoning in
`docs/decisions.md`.

## Before you start

- `backend/lftpweb/core/arrsync.py` — `ArrSettings`, its load/save helpers, `MIN_POLL_INTERVAL_S`,
  the sleep at the end of the poll loop, and `_check_pending`'s two requirements.
- `backend/lftpweb/core/arrclient.py` — `queue_records()`, `import_events()`, `PAGE_SIZE`.
- The Settings → *arr page and its API, for the existing idiom.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

The new default; the 5 s floor still clamps a smaller configured value; the setting round-trips
through the API with server-side validation rejecting out-of-range input; whatever you chose for
existing installs, asserted directly. If you added multi-page observation, a test that a
multi-page walk is detected.

## Docs

`CHANGELOG.md`; `docs/concepts.md` if it describes the poll cadence; `DESIGN.md` if it states the
60 s figure anywhere (**grep for it — the number is quoted in more than one place**);
`docs/decisions.md` for the existing-installs call and for *why the cadence was not split*, since
issue #16 argues for splitting and a future reader deserves to know that was considered and why the
premise was wrong. Also append a one-line entry to the "On `dev` since the release" section of
`prompts/startnewsession.md` — same commit as the code.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`. From `frontend/`: `npm run lint`, `npx tsc -b`,
  `npm test -- --run`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `feat:`. No
  `Co-authored-by:`.
- **You cannot render a page.** Say plainly what a human should check.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
