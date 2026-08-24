---
name: 2026-08-23-unclaimed-pile
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Third pile shipped. `core/disk_review.py`'s SuppressedDebrisItem (a count) became UnclaimedItem
  (a real, shown item, same shape as DebrisCandidate plus reason); ReconciliationResult.unclaimed
  replaces suppressed_debris. DiskReviewPage.tsx renders it grouped by directory like debris, with
  a link-aware reclaim figure, no checkbox anywhere (not selectable through the ordinary flow).
  A real defect was caught by the "no pile at all" test while implementing: an excluded-category
  claim's content_path is now folded into the same hard-exclusion set excluded_paths uses, before
  the claim is dropped, so it never falls through to "unclaimed" by accident. The gate itself is
  deliberately not built (stage 5 doesn't exist) -- recommendation recorded in spec §11.4 and
  docs/decisions.md: a distinct, separately-reachable action, not a confirm dialog. All gates
  green: pytest 2059 passed, ruff check clean, ruff format clean, frontend build/lint/test clean
  (805 tests). Not verified against the user's real two-instance deployment or in a real browser.
---

# Task: Show ambiguous items as a third "unclaimed" pile instead of suppressing them

Finding **#17**. **Read it in full first**, along with #16 (the two-instance deployment that makes
ambiguity permanent) and §11.1d (the existing two-pile split).

## The correction this makes

Fail-closed was implemented as *do not show*: a base path whose exclusions cannot be resolved to
paths had its debris suppressed, and the user saw only a line reporting the suppression.

**That is the same failure as finding #2.** Content that exists and is never surfaced is
indistinguishable from content that is not there. The user cannot act on what they cannot see, and
the suppressed material is precisely the most valuable output of the whole feature — the user's own
words: *"things can show up in weird categories, we might want to clean up."*

> **Fail-closed means "never act without an explicit gate", not "never display".**

## What to build

A **third pile**, extending §11.1d's two:

| Pile | What | Selectable |
|---|---|---|
| **Debris** | unclaimed by any client, unused by lftpweb, in a resolvable path | yes (as today) |
| **Seeding estate** | claimed and seeding — informational | no |
| **Unclaimed** *(new)* | ownership genuinely undeterminable: unclaimed, in a tree where exclusions cannot be resolved to paths | **not by the ordinary flow** — see below |

- **Everything currently suppressed appears here instead.** Nothing is hidden. Whatever "N items
  suppressed" reporting exists becomes this pile's own header/explanation.
- **The pile explains why it is abnormal**, concretely, not as a generic warning. In a
  single-instance setup it should be empty or near-empty; a populated one usually means debris from
  an interrupted operation, or **another lftpweb instance's content** (finding #16). Say both.
- Group it the way debris is grouped (by directory — a genuinely unclaimed item has no torrent to
  group under, §11.1d).
- Its reclaim figure must be **link-aware** like the others (§10.5) — a naive sum reintroduces the
  lie that section exists to prevent.

## On the gate — build the right amount, and no more

**Stage 5 does not exist**, so nothing acts on any pile yet. Do **not** build a confirmation flow
for an action that has not been written — that is speculative UI, and it would be designed against
guesses about stage 5's shape.

What this task must do:

- Make the unclaimed pile **not selectable by the ordinary select-and-remove flow** that debris
  uses. It is visible and inert.
- **Record the gate's intended shape in `docs/decisions.md` and spec §11**, so stage 5 implements it
  deliberately rather than inventing one. Note the tension worth resolving there: this project has a
  **standing preference against confirmation dialogs** (the pause and bandwidth controls were built
  as a checkbox plus debounced auto-commit plus a result banner, deliberately *not* a confirm
  dialog), while the user's request here was "a confirmation dialog **or something**". The likely
  right answer is a **distinct, visually separated action** that cannot be reached by the normal
  flow — accident-proof without being repetitive — rather than a modal. **Write the recommendation
  down; do not implement it.**

## Non-negotiable

- Finding #16's safety property is unchanged: a file claimed by an **excluded** category is still a
  hard exclusion — it does **not** appear in the unclaimed pile, it does not appear anywhere. The
  unclaimed pile is for items whose ownership is *unknown*, never for items known to belong to
  another instance.
- No `client_type` branching.

## Tests

- An item previously suppressed now appears in the unclaimed pile, with its reason.
- A file claimed by an **excluded** category appears in **no** pile — assert this directly; it is
  the line between "unknown" and "known to be someone else's".
- The unclaimed pile is not selectable through the debris flow.
- Its reclaim total is link-aware (one of two hardlinks selected reports zero bytes).
- The seeding estate is unaffected.
- A single-instance-shaped fixture (no exclusions, everything resolvable) produces an **empty**
  unclaimed pile — the normal case must look normal.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — explicit timeout of at least 600000 ms on every gate Bash call.
**Run backend gates from the REPO ROOT**; use a subshell `( cd frontend && … )`.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md` (including
the deferred gate recommendation), update spec §11/§11.1d and the §14 stage-5 row (stage 5 must now
also implement the unclaimed pile's gate), and append a resolution under finding #17.
**Do not commit or push.** Report: files, every exit code, both test counts, a proposed one-line
message, the gate shape you recommended for stage 5, and what you could not verify without a real
browser.
