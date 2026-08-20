---
name: 2026-08-19-queue-short-display-name
status: completed        # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: Added nullable path_queue.short_name (migration 024), save-time trim/normalize/length
  validation (cap 10, no uniqueness), a resolve_queue_display_name fallback helper, and the
  Settings → Queues form field + a matching frontend lib helper. Not rendered anywhere yet
  (stage 4's job). Backend 1426 -> 1439 passed (13 new), frontend 511 -> 513 passed (2 new),
  0 skipped either side. UI ships browser-unverified.
---

# Task: a short display name per path queue

**Phase 1, stage 3 of `docs/transfers-redesign-spec.md` — read §3.6 first.**

Stage 4 drops the Transfers page's per-queue grouping in favour of one globally-ordered list. Once
grouping is gone, each row still needs to say which queue it belongs to — but cheaply, because
"per-row queue labels make the page busy" was the real finding that motivated grouping in the
first place (`prompts/done/2026-08-16-transfers-group-by-queue.md`).

A short display name is the answer: `DC-Movies` → `MOV`. This task adds the field and its setting.
**It does not render it on Transfers rows** — that is stage 4's job, once there is a single list
to render it into.

Icons were considered and **deliberately deferred** (spec §3.6, decided with the user
2026-08-19): a curated icon set drags in a `NOTICE` licence entry, bundling, and dark/light
legibility work, and a fixed set will never cover every user's categories — so the short name has
to exist regardless. Do not add icons here.

## Before you start

- `docs/transfers-redesign-spec.md` §3.6.
- `backend/lftpweb/migrations/` — most recent is `023_queue_position.sql`; yours is **`024_`**.
- `backend/lftpweb/api/settings_queues.py` and `backend/lftpweb/models.py` — the queue settings
  endpoints and their request/response models, including the existing **save-time path
  validation** (v0.2.1) whose shape your validation should match.
- `frontend/src/pages/settings/` — the Settings → Queues form.
- The existing `FieldHelp` per-field help component — this project applies it "starting with the
  fields whose wrong answer costs you data." A cosmetic label is not that, so a help popup here is
  optional; use judgement.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1426 backend / 511 frontend tests passing, 0 skipped**.

## What to do

### 1. Migration `024_queue_short_name.sql`

Add a nullable `path_queue.short_name TEXT`. **Nullable with no backfill** — `NULL` means "no
short name set," and every read falls back to the full `name`. Do not invent short names for
existing queues by truncating: `DC-Movies` and `DC-Music` would both become `DC-M`, and a silently
wrong label is worse than a long one.

### 2. Backend

- Expose `short_name` on the queue response and accept it on create/update, following the exact
  optional-field conventions the surrounding models already use.
- **Validate on save**, in the same spirit as the existing path validation: trim whitespace;
  empty-after-trim normalizes to `NULL` (not `""`), so "cleared" has one representation;
  enforce a maximum length. Pick the cap from what the UI can actually show without wrapping and
  say what you chose and why — somewhere in the region of 8–12 characters is the intent, not 200.
- **Uniqueness is NOT required.** Two queues may share a short name. It is a display hint, not an
  identifier, and rejecting a duplicate would be a surprising failure while typing. If you
  disagree after looking at the code, say so rather than silently adding a constraint.
- A single helper resolving "what do we display for this queue" (`short_name or name`) so the
  fallback lives in exactly one place rather than being re-derived at each call site.

### 3. Frontend

- A "Short name" field in the Settings → Queues form, next to the queue name, clearly optional,
  with placeholder or helper text making the fallback explicit (something like "defaults to the
  full name").
- The pure display-resolution helper belongs in `lib/` and should be unit-tested, matching how
  this codebase keeps logic out of components.

### 4. Tests

Backend: round-trip through create/update; whitespace-only normalizes to `NULL`; over-length is
rejected; the fallback helper returns `name` when `short_name` is `NULL`; existing queue tests
still pass unchanged (this field is additive and optional). Frontend: the display-resolution
helper.

### 5. Docs

`CHANGELOG.md` under `[Unreleased]`; `docs/concepts.md` or the Settings → Queues user-facing docs
if they enumerate the queue fields; tick stage 3 off in `docs/transfers-redesign-spec.md` §7.
`docs/decisions.md` only if you hit something not already settled here — the three decisions above
(nullable-no-backfill, no uniqueness, icons deferred) are already recorded, don't re-log them as
yours.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — running from there collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script** in this repo.
  Do not background the test run.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** State plainly that the UI ships browser-unverified.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
