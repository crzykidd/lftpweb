---
name: 2026-08-23-docs-currency-for-0.4.0
status: done
created: 2026-08-23
model: opus
completed: 2026-08-23
result: >-
  Done. `DESIGN.md` gains §17 (the download-client connector framework) — eight subsections
  describing what exists, with an explicit "the spec is a proposal, this is reality" precedence
  rule and a stage table stating plainly that stage 5 is not built. `README.md` gains a
  user-facing download-client bullet, a "Two lftpwebs sharing one seedbox" subsection under the
  recommended layout (answering finding #16's second open question in the affirmative, labelled
  newly-supported and lightly tested), and seven new "Known gaps" entries. `docs/concepts.md`
  gains five entries (download clients, category → queue mapping, base paths, Disk review's three
  piles, the two verdict gates that ship off), 15 → 20. `prompts/startnewsession.md` rewritten
  above a new "everything below this line is history" divider — the stage-0-era #18 narrative was
  actively wrong and is gone; Operating rules and Traps moved above the divider; stage 5's gate
  restated with its second, time-based condition and the reasoning for it; the two correction
  lists named as the highest-value outstanding work. `docs/README.md` re-indexed. Decision
  recorded. Gates: pytest 0, ruff check 0, ruff format --check 0. No code, no tests, no behaviour
  changed.
---

# Task: Bring every document current for the 0.4.0 release, and make the brief clear-proof

**Documentation only. Change no behaviour, no code, no tests.** If you find a code defect, report
it — do not fix it.

Two goals:

1. **The docs describe what now exists.** `DESIGN.md` is this project's architectural source of
   truth and it is **completely silent** on the download-client connector framework — six stages,
   two connectors, migrations 027–032, ~500 new tests. That is the largest doc gap in the repo.
2. **`prompts/startnewsession.md` becomes self-sufficient**, because the user is about to `/clear`
   and start a fresh session for pre-release bug hunting. Everything a new session needs must be in
   the file, not in a transcript that will be gone.

## Context you must read first

- `docs/download-client-framework-spec.md` — the governing spec for all of this work. Note it
  carries several *corrections* (§8.2, §8.3, §9.1, §11.1c, §13.4/#9/#10) where an earlier decision
  was reversed with its cause recorded. That convention is deliberate; preserve it.
- `prompts/test-findings-2026-08-23.md` — **17 findings** from live use against a real seedbox, all
  resolved except where noted. Several changed the design, not just the code.
- `docs/decisions.md` — the reasoning log, newest first.
- `git log --oneline v0.3.1..HEAD` — everything shipped since the last release.

## What to write

### 1. `DESIGN.md` — a new numbered section for the connector framework

Match the existing sections' voice: numbered, opinionated, explaining *why* and naming the traps.
It must at minimum cover:

- The two vocabularies (operations vs fields) and why one flat capability list serves neither
  consumer.
- The tri-state capability declaration, its three layers, and **the rule that a transport failure
  must never degrade a capability**.
- **Advisory only**, and that it is enforced structurally — a connector is handed no database
  handle.
- **Deletion never goes through the client**: `remove` unregisters, lftpweb deletes bytes over SSH.
  This removed rTorrent's `erasedata` hook from the design entirely.
- Attribution: **path first, category as fallback** — and that rTorrent's `content_path` is its
  seeding directory, so path matching cannot work for it (spec §1.1).
- The disk review scan's three piles and **inode-based claiming**.
- **The two-instance deployment** (finding #16) and why "not used here" is a safety boundary rather
  than a preference.

**`DESIGN.md` must describe what exists, not the spec's aspirations.** Stage 5 (the delete
pipeline) is **not built** — say so plainly rather than describing it.

### 2. `README.md`

- The download-client integration in user terms: what it does, what it does not do yet.
- **"Known gaps"** brought current — including that stage 5 does not exist, that both connectors'
  wire vocabularies carry unverified guesses (§13.4 / §13.6), and that cross-seed handling ships
  unwitnessed against a real cross-seeding setup.
- Consider whether the **two-lftpweb-instances-on-one-seedbox topology** belongs in the
  "Recommended seedbox layout" section — finding #16 raises it as an open question. Decide, and if
  you add it, be honest that it is newly supported and lightly tested.

### 3. `docs/concepts` (or wherever the concept list lives — check `nav.ts`)

New concepts a user now meets and cannot look up: **download client**, **category → queue
mapping**, **base path** (content vs working), **debris / seeding estate / unclaimed**, **the
settle-gate skip**, **withhold**. Match the existing entries' length and tone.

### 4. `prompts/startnewsession.md` — the clear-proof brief

**This is the most important deliverable.** Assume the reader has no memory of this work at all.

- Rewrite the "Where we are" section so #18's state is accurate and complete: what is built
  (stages 0–4 plus the fix rounds), what is not (stage 5), what is gated and why.
- **Stage 5's gate, stated in full**, with its now-strengthened condition: it waits on findings
  #15/#16 being resolved **and** on **0.4.0 running in the user's two-instance setup for several
  days**. The dangerous case — the other instance's content losing its client claim — cannot be
  staged, only observed over time. Record that reasoning, not just the rule.
- **Point at the two correction lists** (§13.4, §13.6) as the highest-value outstanding work, and
  say plainly that a green test suite does not touch them because the fixtures encode the same
  assumptions as the code.
- The next release is **0.4.0** — decided by the user, 2026-08-23, on the grounds that SAB/rTorrent
  queue visibility is a substantial feature set. Note that `/release-prep` is **forbidden from
  touching `DESIGN.md`**, so section 1 of this task must be committed *before* the release prep runs.
- Note the immediate next session's purpose: **pre-release bug hunting**, not new features.
- Prune what is stale. The brief has accreted; a fresh reader should not wade through superseded
  detail to find the current state.

## Constraints

- **Documentation only.** No code, no tests, no behaviour.
- Preserve the reversal-with-cause convention where a decision changed — do not rewrite history to
  look like it was always right.
- Do not claim anything is verified that has not been. Where something is untested against reality,
  say so.

## Verification gates — read `CLAUDE.md`

Even for a docs-only change, run them; a stray edit can break a doctest or a lint rule.
**NEVER background a gate** — explicit timeout of at least 600000 ms. **Run from the REPO ROOT.**

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record any decision in `docs/decisions.md`.
**Do not commit or push.** Report: files changed, gate exit codes, a proposed one-line `docs:`
message, anything you found that is wrong in the code (report only), and **anything you could not
determine and had to leave vague** — that last one matters, since a fresh session will trust this
brief.
