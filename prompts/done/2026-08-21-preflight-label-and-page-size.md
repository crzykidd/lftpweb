---
name: 2026-08-21-preflight-label-and-page-size
status: completed        # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Chip label -> "Waiting"; Settling chip now reuses settleWaitLabel via new generic
  PreflightRow.wait_scans/wait_since fields; "Show all" expand replaced by a persisted 5/10/20
  selector (PageSizeSelect extracted to a shared component). All gates green (1577 backend /
  591 frontend tests, 0 skipped).
---

# Task: Preflight gets a page-size selector, and the *arr chip says "Waiting for remote client"

Two small follow-ups from the user's browser review on 2026-08-21, both in the same component.
Baseline `d02fc0d`.

## 1. The chip label

> *"waiting for download shows up ... maybe the text 'Waiting for download' is the problem maybe"*
> → *"Maybe **waiting for remote client** might be better"*

`lib/preflight.ts.preflightChipLabel` maps the *arr's `downloading` state to **"Waiting for
download"**. That was ambiguous — it reads equally as "lftpweb is waiting to download it" and
"waiting for the download client to finish", and the point of the label is to say the work is
happening **somewhere that is not this app**.

The user considered "Waiting for remote client", then asked to shorten it, then chose from a set
of options.

**Change it to "Waiting."** Chosen deliberately over longer alternatives:

- **7 characters**, well under the 20 it replaces, so it cannot crowd the row.
- It matches its sibling chip **`Settling`** exactly in shape — one word, present tense, a state.
  The two are the only chips in this box and should read as one vocabulary.
- It says nothing about *where*, and does not need to: the **\*arr brand logo sits in the same
  row**, the box is called **Preflight**, and the tooltip already spells out
  `Downloading from "<client>" — reported by <instance>`. The context carries what the chip drops.

Rejected, with reasons worth keeping: "At client" (9, says where but mild jargon), "Downloading"
(the *arr's own word — precisely the wording that caused the confusion), "Grabbed" (Sonarr's term,
but describes a past event, so it sits oddly beside a present-tense `Settling`).

### 1b. The Settling chip needs a tooltip too

> *"The settling chip should have a mouse over that shows time details..."*

Right now only the *arr chip has a tooltip; `Settling` has none, so the box is asymmetric — and
"Settling" alone tells you nothing about how much longer.

**The data and the wording already exist.** `lib/format.ts` has a helper producing
**"Waiting for changes — 1 of 2 scans, 35s of 60s"**, already shared by the Files tree's state text
and the lifecycle R-icon tooltip. Find it and reuse it, so the Preflight chip, the Files tree and
the icon tooltip all say the same thing rather than a third variant appearing.

- The settle gate has **two** conditions — N consecutive unchanged scans **and** a wall-clock
  minimum — and the existing wording counts down both. Keep that; a tooltip showing only one would
  mislead when the other is the binding constraint.
- The settle source will need to carry whatever that helper takes as input (scan count, elapsed
  time — check what it actually needs) rather than a pre-baked string, so the countdown stays live
  between polls instead of freezing at whatever the backend last rendered.
- **Keep `core/preflight.py` source-agnostic** — this is a generic "detail for the tooltip" on the
  shared row shape, not a settle-specific field bolted onto it. The *arr source leaves it unset.
- If the helper's shape makes reuse genuinely awkward, **say so rather than duplicating its logic**
  — a third copy of this wording is exactly what this section exists to prevent.

**This also settles an open question from the previous task**: `remaining_s` stays `None` for settle
rows (a seconds estimate cannot honestly be derived from an uncertain scan cadence). The countdown
belongs in the tooltip, not the figure column — so the figure column keeps showing size alone.

- Leave the `importing` → "Importing" mapping alone.
- Leave the tooltip alone — it already carries
  `Downloading from "<client>" — reported by <instance>`.
- Leave the unmapped-state fallthrough alone (raw word, never invented).

**Watch the width.** This is 25 characters against the previous 20, in a row that also carries a
queue tag, a title, an *arr logo and a `w-44` figure column. If it visibly squeezes the title
column, **say so in your report** and propose a shorter label rather than silently letting the row
get cramped — the tooltip already carries the detail, so a shorter chip loses nothing. Do not
change the `w-44` figure column to make room; that width is deliberate and shared with the rows
below (it was widened from `w-32` when the ETA figure landed).

## 2. A page-size selector on the Preflight box

> *"we should have a drop down on preflight like the rest show 5/10/20 etc."*

The Preflight box currently shows 5 rows with a **"Show all (N)"** expand into a paged view at
`PREFLIGHT_EXPANDED_PAGE_SIZE`. The previous task deliberately skipped a selector, reasoning that a
5-row box does not share a growing job history's "see more at once" need. **The user has now used
it and disagrees — that settles it.**

- Add a selector offering **5 / 10 / 20** (not the other boxes' 10/20/50 — this box is smaller by
  intent), defaulting to **5**.
- **It should replace the "Show all (N)" expand, not sit beside it.** Two controls doing
  overlapping jobs on one small box is worse than either alone. If you believe both should stay,
  argue it — but the default is to remove the expand.
- **Persist per browser**, validated on read with a fallback to the default, exactly as
  `transfers.activePageSize` / `transfers.completePageSize` already do
  (`lib/pagination.ts.isPageSize`, `lib/storage.ts`). Use a matching key name. **A stale or
  hand-edited value must never be trusted.**
- Reuse the existing `Pager` and `pageReadout` — `components/Pager.tsx` was extracted precisely so
  all three boxes share one implementation. Do not add a third pagination idiom.
- **Changing size resets to page 1**, same as the other two boxes.
- **The box must still scale to content** — this is a standing user requirement: height follows the
  row count up to the selected size, and zero rows collapses to the single "Nothing in preflight."
  line with no reserved empty space. Selecting 20 must not make an empty box twenty rows tall.
- The selector should still render when the box is showing (per the existing rule: hidden entirely
  only when no source is configured at all).

## Before you start

- `frontend/src/components/PreflightBox.tsx` — the box, its expand toggle, `PreflightRowView`.
- `frontend/src/lib/preflight.ts` — `preflightChipLabel`, `PREFLIGHT_DEFAULT_ROWS`,
  `PREFLIGHT_EXPANDED_PAGE_SIZE`.
- `frontend/src/lib/pagination.ts` — `PAGE_SIZE_OPTIONS`, `isPageSize`, `pageReadout`, and how the
  two Transfers boxes wire their persisted selectors in `TransfersPage.tsx`.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1574 backend / 586 frontend tests passing, 0 skipped**.

## Tests

Pure logic in `lib/`, per this codebase's convention: the new label mapping (including that
`importing` and the unmapped fallthrough are unchanged), and the size-option validation including
the bad-stored-value fallback. Update any existing test asserting the old label rather than leaving
it green against dead wording.

This is very likely frontend-only. If you find you need a backend change, stop and explain why
before making it.

## Docs

`CHANGELOG.md` under `[Unreleased]`. `docs/concepts.md` only if it quotes the old label verbatim —
check. `docs/decisions.md` only if you diverge from anything decided above.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`, already hit
  repeatedly.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`feat:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** Say plainly what a human should check — especially whether the
  longer label crowds the row.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
