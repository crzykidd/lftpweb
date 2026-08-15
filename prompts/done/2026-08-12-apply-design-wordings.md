---
name: 2026-08-12-apply-design-wordings
status: done
created: 2026-08-12
model: opus
completed: 2026-08-12
result: >
  Applied all nine pending DESIGN.md wordings from docs/decisions.md, including the rar-task
  wording that landed mid-execution. Three new sections (§2.2 publish invariant, §3.3 settle
  gate, §10.4 throughput metrics), a new §3.2 rule 9 (state ownership), and corrections to
  §3.1/§3.2/§4.6/§4.7/§5/§6/§7.3/§9/§11/§13/§14. Nothing renumbered; every § reference in the
  repo re-checked. Three draft-vs-code conflicts resolved in the code's favour, the largest
  being §3.2 rule 3's inversion (REMOVED_LOCAL is now auto-queue-eligible; suppression, not the
  state name, is what writes an item off). Documentation only.
---

# Task: Apply every pending DESIGN.md wording the user has now approved

Over several sessions, agents that found `DESIGN.md` wrong or underspecified drafted
replacement wording into `docs/decisions.md` and deliberately left `DESIGN.md` untouched,
per this project's rule that the doc gets corrected rather than quietly diverged from.
**The user approved applying all of them on 2026-08-12.** This task applies them.

## Before you start

- Read `DESIGN.md` in full. It is the architectural source of truth, sections numbered for
  reference, and every other document cites it by section. Treat its voice, structure, and
  level of detail as the constraint you are writing inside.
- Read `docs/decisions.md` **completely**, newest first. It is long. Every proposed
  wording lives there, in the entry of whichever task found the gap.
- Read `prompts/open-issues.md` and `prompts/startnewsession.md`'s "Traps worth knowing".

## Finding the wordings — do not trust this list as complete

Known drafts, from the sessions that produced them:

1. **§3.2 rule 9** — who wins between the three modules that write `item.state`. Its
   absence is why post-processing outcomes were erased for four phases.
2. **An empty-remote-directory clause** near §3.2 rule 1/8 — a directory with zero remote
   files reads `REMOTE_ONLY`, not vacuously `DOWNLOADED`. Distinct from the
   all-children-excluded case, which *is* vacuously `DOWNLOADED` and is load-bearing.
3. **§2/§9's publish invariant** — what is published is the persisted state, never the
   structural one; reconcile → persist → read back → diff → publish.
4. **A settle-gate section** near §5/§3.2.
5. **A §6 clause** on the post-processing trigger and the deliberately-not-built
   scan-driven re-trigger path.
6. **A §7.3 update** for the verify fallback's now-stronger guarantee (hash-on-disk now
   also checks size, so it can no longer bless a truncated file).
7. **`REMOVED_BOTH` is overloaded** to also mean "local deleted, remote untouched",
   diverging from §3.2's literal wording.
8. **§6's rar claim** — a wording may be added by the in-flight rar task. Check for it.

**Search `docs/decisions.md` yourself for any others.** Agents were told to draft wording
rather than edit, so phrases like "proposed wording", "not yet applied", "awaiting the
user's approval", and "draft" are the markers. There may be drafts this list misses.

## What to do

1. **Apply each one**, integrating it into `DESIGN.md`'s existing structure and voice —
   not pasted in as a block quote from `docs/decisions.md`. Some drafts were written as
   "here is roughly what it should say"; your job is to make them read as though they were
   always part of the document.
2. **Renumber or restructure only if you must**, and if you do, grep the whole repo for
   citations of the sections you touch (`§3.2`, `§7.3`, code comments, `README.md`,
   `prompts/`, `docs/`) and update them. A stale `§4.5` reference is worse than an ugly
   insertion point — this project cites DESIGN.md sections constantly, in code comments
   most of all.
3. **Where a draft conflicts with what the code actually does, the code wins** — it has
   tests and the doc does not. If you find such a conflict, apply the wording that matches
   reality and **flag the discrepancy in your report**.
4. **Mark each decisions.md entry as applied** — a short "**Applied to DESIGN.md
   2026-08-12**" line on the relevant entry, so a future session can tell settled from
   pending. Do not delete or rewrite the original reasoning; the rejected alternatives are
   the valuable part.
5. **Update §13 and §15** if any of this changes what they claim. §13's build order is
   annotated with what shipped; §15's risk table has a per-row status line.
6. **Do not invent new architecture.** If you find a gap nobody drafted wording for, note
   it in your report — do not fill it yourself.

## A specific thing to get right

§6 and §7.3 have both been touched by multiple drafts from different sessions. Read all
the relevant entries before writing either section, so you produce one coherent section
rather than three drafts stacked on top of each other.

## Conventions to honor

- **This is a documentation-only change.** Do not modify code. If applying a wording
  reveals a code bug, report it — do not fix it here.
- `docs/decisions.md` gets one new entry at the top recording that the backlog was applied
  and listing which wordings landed where.
- `CHANGELOG.md` — a `docs:` note is appropriate but keep it brief; this documents existing
  behaviour rather than changing any.
- Nothing to lint or test, but run `uv run pytest -q` once at the end anyway to confirm you
  changed nothing you did not mean to.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. **Do not commit.** Report back: the complete list of wordings you found and applied
   (including any not in the list above), any you could not apply and why, any conflict
   between a draft and the code, any section renumbering and what you updated to match,
   and the proposed one-line `docs:` commit message. Never `git add -A`, never push.
