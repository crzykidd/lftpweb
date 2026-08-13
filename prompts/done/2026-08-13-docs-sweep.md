---
name: 2026-08-13-docs-sweep
status: done
created: 2026-08-13
model: opus
completed: 2026-08-13
result: >
  Applied the whole DESIGN.md wordings backlog (§3.1, §3.2 rules 3 and 9, §6, §7.3, §9, §9.2,
  §12, §13, and the status line), corrected four stale "wording not applied" code comments,
  documented the `resolve_absence`/`REMOVED_BOTH` gap without fixing it, and reconciled
  README.md and CHANGELOG.md. One draft-vs-code conflict found: `re_download_externally_removed`
  is documented as a no-op for `move` queues and is not — a `move` row at bare `REMOVED_LOCAL`
  becomes eligible when it is on. Nothing renumbered; every `§N.M` citation in the repo
  re-verified. 701 tests pass, both lint gates clean.
---

# Task: Closing documentation sweep for the 2026-08-12/13 session

Sixteen commits landed across one long session driven by the user running the app for real.
`DESIGN.md`, `README.md`, and `CHANGELOG.md` have drifted, and several agents drafted
`DESIGN.md` wording into `docs/decisions.md` rather than applying it. **The user has given
standing approval to update `DESIGN.md`** ("design.md you can update", 2026-08-12).

The orchestrating session is concurrently rewriting `prompts/open-issues.md` and
`prompts/startnewsession.md`. **Do not touch either of those two files.**

## Before you start

- Read `DESIGN.md` in full. It is the architectural source of truth; other documents and many
  code comments cite its sections by number.
- Read `docs/decisions.md` **completely**, newest first. It is long and has grown a lot today.
- `git log --oneline` since `81aa73c` is this session's work.

## Working tree check

`git status --porcelain`. Only `prompts/open-issues.md` should be dirty (the orchestrator's).
If anything else is, stop and report.

## 1. Apply every drafted DESIGN.md wording

Several tasks drafted wording and deliberately did not apply it. Search `docs/decisions.md`
for the markers — "proposed wording", "drafted", "not applied", "awaiting", "not yet applied".
**Do not trust this list as complete; search for yourself.** Known drafts:

- **§3.2 rule 9** — a new bullet for the `LOCAL_ONLY` + `remote_deleted_at` refinement,
  analogous to the existing `DOWNLOADED` bullet.
- **§7.3** — a `rel_path` can leave *both* trees at once, and `_persist` now resolves such rows
  through the same grace period.
- **§3.2 rule 3's parenthetical** ("in move… it reaches `REMOVED_BOTH` instead") — annotate: it
  does not match the implementation. See item 3 below.
- **§6** — archive cleanup after extraction (`4533617`).
- **§9.2** — the Files row revamp: lifecycle icons, inline progress, sorting, the detail drawer.

Integrate them into the document's existing structure and voice — not pasted blocks. **Where a
draft conflicts with what the code actually does, the code wins**; it has tests and the doc
does not. Flag any such conflict in your report.

**If you renumber or restructure anything, grep the whole repo** for citations of the sections
you touch — code comments cite `DESIGN.md` sections constantly — and update them. A stale
`§4.5` reference is worse than an awkward insertion point. Prefer appending subsections over
renumbering.

Mark each applied entry in `docs/decisions.md` with a short "**Applied to DESIGN.md
2026-08-13**" line. Do not delete or rewrite the original reasoning; the rejected alternatives
are the valuable part.

## 2. Also fix these stale pointers

- **`core/settle.py`, `core/metrics.py`, `migrations/005`** contain comments saying "DESIGN.md
  wording proposed, not applied". Those wordings *have* since been applied. Correct the
  comments. (This is the one place you may edit code — comments only, no logic.)
- **§12's file list** omits every module added since phase 4: `verify`, `extract`, `audit`,
  `itemview`, `mount_sentinel`, `settle`, `metrics`, `local_delete`, `logtail`. Bring it
  current.
- **§9's "TanStack Query for REST" has never been true** — the project has always used a
  hand-rolled `fetch` client and poll hook, flagged since phase 3b and never resolved. Correct
  the doc to describe what exists, and note in your report that the alternative (actually
  adopting the library) remains an open choice nobody has made.

## 3. Record a known implementation gap, do not fix it

`core/mount_sentinel.py.resolve_absence` always writes the literal `"REMOVED_LOCAL"`, taking
neither `sync_mode` nor `remote_deleted_at` as input — so a fully-completed `move`-mode item
that leaves both trees lands there rather than the `REMOVED_BOTH` that `DESIGN.md` and
`core/autoqueue.py`'s comments both claim. It was latent until `56ec523`, because such items
never reached that function at all.

Widening it is a real design decision (it would also have to decide about
`auto_queue_suppressed`), so **do not implement it**. Make `DESIGN.md` honest about the
current behaviour and note the discrepancy where rule 3 asserts otherwise.

## 4. `README.md`

Reconcile "What works today", "What doesn't yet", and "Known gaps" against reality. Landed
today, among others: rar extraction now actually works (it never had), the settle gate,
`state_changed_at`, manual + retention local deletion, archive cleanup, per-queue scan
intervals, lifecycle icons, inline progress, sorting, and the detail drawer.

**Add the gaps this session created or revealed**, honestly:

- **No frontend test runner exists in the project** — no vitest, no jest. Sorting, the collapse
  preference, and the progress-fraction logic are pure functions with zero automated coverage.
  This is now a real gap; name it.
- **Almost none of the new UI has been seen by a human**, and none by any agent.
- Encrypted-rar password retry is implemented but untestable (no compressor exists to build a
  fixture); only old-style `.r00` multi-volume has a real fixture, not `.partNN`.

## 5. `CHANGELOG.md`

Many agents appended to `## [Unreleased]` independently today. Read the whole section for
**coherence, not just completeness**: entries that contradict each other, describe a detour
rather than the net result, or duplicate one another. Precedent from `6d3bd95`: where a change
was made and then reversed the same day and never released, the changelog describes the **net
result**, not the journey. Nothing here has shipped in a release.

## Conventions to honor

- **Documentation only**, except the code *comments* named in item 2.
- `docs/decisions.md` gets one new entry at the top recording that the backlog was applied and
  what landed where.
- Run `uv run pytest -q` once at the end to confirm you changed no behaviour.
- Do not touch `prompts/open-issues.md` or `prompts/startnewsession.md`.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/`.
3. **Do not commit, do not push.** Report: every wording applied and where, any draft-vs-code
   conflicts, any renumbering and what you updated to match, what you changed in README and
   CHANGELOG, and the proposed one-line `docs:` commit message.
