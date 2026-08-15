---
name: 2026-08-12-phase9-polish
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: |
  UI: Files-page text/state filters (client-side), honest bulk Queue/Stop partial-failure
  reporting (Promise.allSettled, failed entries stay selected), host_reachable/scheduler_alive
  surfaced in the stats header. Virtualization reviewed (no browser to measure), not changed.
  Deliberately NOT built, named instead: bulk Delete local/remote, Settings -> Transfer UI
  (placeholder text corrected to be honest about it).

  Docs (the larger half): README.md, DESIGN.md §13/§15, and prompts/startnewsession.md
  reconciled against reality across all 9 shipped phases. Consolidated "Known gaps" list added
  to README.md (7 items from the prompt plus 2 more found this phase). Corrected a factual bug
  in README's volume table (staging/downloads were backwards vs. what phase 5 actually built).
  Rewrote startnewsession.md's stale repo-bootstrap section using live gh/git checks (branch
  protection is active; that section described an empty unprotected repo). One-line truth fix
  to CLAUDE.md's stale phase-count status line.

  Verified: uv run pytest 367 passed/0 skipped (fake seedbox up), 357 passed/10 skipped
  without it, no backend code changed. Both ruff gates clean. npm run build/lint clean. All
  three compose files validate. Every § reference in DESIGN.md/CLAUDE.md/startnewsession.md/
  README.md/decisions.md resolves. No browser available -- UI never click-tested, stated
  plainly in README/DESIGN.md §13/startnewsession.md. Not committed per instruction; see
  docs/decisions.md's phase 9 entry and the orchestrating session's report for the file list
  and proposed commit message.
---

# Task: Phase 9 — polish, and an honest reconciliation of the docs

The last phase. Two jobs: finish the UI affordances §9 specifies, and make the documentation
tell the truth about what now exists.

**Done when:** the UI has the bulk operations and filters §9.2 promises, and `README.md`,
`DESIGN.md`'s build order, and `prompts/startnewsession.md` describe a nine-phase project that
is actually complete — with every known gap named rather than quietly dropped.

## Before you start

- **Read `DESIGN.md` §9 in full**, §13 phase 9, and §15 (risks — several were written when the
  code didn't exist and may now be stale or resolved).
- Read `prompts/startnewsession.md` end to end. It has grown across eight phases and now
  contains statements written when later phases were hypothetical.
- Read `docs/decisions.md` — eight phases of decisions, several flagged as deliberate scope
  reductions that this phase should either close or restate as permanent.

## Working tree check

`git status --porcelain` first. Anything dirty: list it and ask. This file is exempt.

## What to do

### 1. UI polish (§9.2)

- **Bulk operations** on the Files page: multi-select with shift-range already exists — make
  sure the bulk actions cover Queue / Stop, and that a bulk action reports partial failure
  honestly (7 of 10 queued, these 3 failed because …) rather than silently.
- **Filters** on Files: by state and free-text search, per §9.2.
- **Virtualization tuning**: the Files tree and History both use `@tanstack/react-virtual`.
  Confirm they stay smooth with a large tree; if you change anything, say what you measured.
- **Surface `host_reachable` and `scheduler_alive`** in the header or Settings — phase 7 added
  them to `/api/health` and explicitly deferred the UI to this phase.

### 2. Documentation reconciliation — the more valuable half

Eight phases of docs were written incrementally, several while later phases were still
hypothetical. Make them true:

- **`README.md`** — the "What doesn't yet" table is already partly corrected; verify the whole
  file against reality. The pre-release warning should stay (this is still `0.0.1` with a
  schema that changes), but it must not describe features that now exist as missing, or vice
  versa.
- **`DESIGN.md` §13** — the build order describes phases as future work. Mark what shipped.
  **Do not rewrite the design sections themselves** (§1–§12): those are the architectural record
  and several were deliberately corrected in conversation. §13 and §15 are the exceptions —
  §15's risk table should reflect which risks are now closed, live, or superseded, with the
  reasoning kept.
- **`prompts/startnewsession.md`** — this is what a fresh session reads. It must accurately say:
  what is built, what is not, what is deliberately unscheduled (`sync` mode, §7), and the
  standing traps. Prune anything that was true mid-build and is now misleading.

### 3. Name the gaps rather than closing them silently

Several deliberate scope reductions are recorded across `docs/decisions.md`. Collect them into
one honest list — in `README.md` or `startnewsession.md`, wherever a reader will find it:

- **No UI phase was ever click-tested** — no browser exists in this environment. Every UI phase
  said so; that caveat belongs somewhere permanent, not only in eight separate reports.
- Post-processing triggers only from job success, not from scans (phase 5).
- The `REMOVED_LOCAL` grace period is unit-tested but never exercised live across a real
  multi-scan window (phase 4).
- Date filters are UTC-only; no timezone handling exists anywhere (phase 6).
- API keys are SHA-256, not argon2id (phase 8, deliberate).
- Login timing is not normalized between unknown-user and wrong-password (phase 8, deliberate).
- **`password` mode with no user row is treated as open access** (phase 8) — the deliberate
  lockout-recovery route, which is also a fail-open. It deserves to be stated plainly in the
  README next to the "Locked out?" section, not only in the decision log.

Add any others you find. **Do not fix them silently** — this phase is about the docs matching
the code, and a known gap that is written down is worth more than one quietly closed at 3am
without the user's input.

## Verify before reporting — actually run these

1. `uv run pytest` passes; add tests for any behaviour you change.
2. `npm run build` and `npm run lint` clean.
3. **Both lint gates repo-wide, exactly as CI runs them** — `check` alone has missed unformatted
   files in three separate phases:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
4. `docker compose config --quiet` clean on all three compose files. Tear down anything you start.
5. **Grep `DESIGN.md` for `§` references and confirm every one still resolves** — the numbering
   is cited by `CLAUDE.md`, `startnewsession.md`, and dozens of code comments.

State plainly anything you could not verify. No browser is available.

## Surfacing decisions

The user is asleep and asked that **every decision made without them be documented**. Record each
in `docs/decisions.md` (newest at top), and repeat them in your report. **Do not edit `DESIGN.md`
§1–§12** — those are the architectural record; §13 and §15 are the exceptions noted above.

## When done

1. `docs/decisions.md` entries.
2. `prompts/startnewsession.md` fully reconciled — this is the single most important artifact for
   whoever picks this up next.
3. Frontmatter: `status`, `completed`, `result`.
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list and a proposed one-line commit message (`feat:` or
   `docs:` as fits, no `Co-authored-by:`; branch `dev`).
