---
name: 2026-08-21-preflight-row-columns
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Preflight rows now mirror Transfers' column order (queue tag, title, StateChip, *arr chip, w-44 figure), gained remaining-time (arr timeleft) and lftpweb-perspective chip wording with an *arr detail tooltip, chips route through StateChip's amber, and a handed-over release now evicts from the Preflight hold immediately instead of lingering up to 150s.
---

# Task: Preflight row presentation — columns, queue tag, chips, remaining time — and evict on handover

From the user's first browser look at the shipped Preflight box (2026-08-21), on `e785de0`:

> *"we missed the remaining time on the preflight list. and we moved the columns around. it should
> still have the tag and the column for status on arr icon. arr icon is at the first of the line
> now"*

Two problems, one root cause: **the Preflight row invented its own column order instead of
mirroring the rows above it.**

## What is actually wrong

`frontend/src/components/PreflightBox.tsx`'s `PreflightRowView` renders:

```
[*arr chip] [title] [status] [size]
```

while `frontend/src/pages/TransfersPage.tsx`'s `Row` renders (read it, this is approximate):

```
[queue badge] [title …] [*arr chip] [right-aligned figure column, w-44]
```

So on a Preflight row the *arr icon **leads the line** instead of sitting in its usual column, and
the **queue badge is missing entirely** — `PreflightRow` carries `queue_id` but no queue name or
short name, so the row cannot show the tag every other row on the page shows.

Three rows stacked in one view should read as one table. They currently do not.

## What to do

### 1. Match the column order to the main rows

Read `Row` in `TransfersPage.tsx` and mirror its order and its column widths — including the
right-aligned figure column's `w-44` (widened from `w-32` when the ETA figure was added; the same
reason applies here). Reuse the same components: `queueDisplayName` for the tag, `ArrRowChip` (or
whatever `SourceChip` should delegate to for an *arr-sourced row) in its usual slot.

**Do not** make Preflight rows carry affordances they must not have — no chevrons, no `#N`
position, no Dismiss/Start now/Stop. They stay inert. This is about **column alignment**, not
adding actions.

For a **settle-sourced** row there is no *arr chip; leave that column empty rather than shifting
everything left, so the columns still line up down the page.

### 2. The queue tag needs data that isn't there yet

`PreflightRow` (`backend/lftpweb/core/preflight.py`) has `queue_id` but no name. Add the queue's
name and short name so the row can render `queueDisplayName(short_name, name)` exactly as the other
rows do.

**Keep the source boundary intact** — `core/preflight.py` must stay source-agnostic (its docstring
is explicit, and the settle source landed through that boundary without reshaping it). Queue
identity is common to every source, so it belongs on the shared row shape, not in either source's
own module. Both sources must populate it.

### 3. Remaining time

The user's *"we missed the remaining time"*. For an **\*arr-sourced** row the *arr's own queue
record already carries this — `QueueRecord.raw` keeps the full response dict, and the v3 queue
record has `timeleft` and `estimatedCompletionTime`. **Read it from `raw`; do not add a request.**

- Decide which field to use and say why. `timeleft` is a duration string; `estimatedCompletionTime`
  is an absolute timestamp. Whichever you pick, render it in the **same shape and formatting
  helper the Transfers row already uses for its own ETA** (`transferLineValue` / `lib/format.ts`)
  rather than a second time-formatting idiom.
- **It is frequently absent or meaningless** — a paused or stalled SAB item, a queue record with no
  estimate. Omit the figure entirely when absent. Never render `null`, `0`, or a fabricated
  estimate.
- For a **settle-sourced** row there **is** a meaningful remaining figure, and it already exists —
  see the next section. Do not leave it blank.

### 3b. The chips bypass `StateChip` — that is the real bug, not the wording

The user, seeing it in the browser: *"I actually like settling but all those badges are [grey]
now.. so it just looks different"* and *"the settling is just a soft grey chip now"*.

**Keep the word "Settling"** — the user likes it and it matches `core/itemview.py`'s
`FileNode.substate` already. Do **not** rename it to the Files tree's "Waiting for changes".

The actual defect: `PreflightBox.tsx` hand-rolls its chips as
`bg-zinc-100 ... text-zinc-500` spans instead of using **`frontend/src/components/StateChip.tsx`**,
which already exists and already has:

```
SETTLING: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
```

added deliberately — its own comment says it replaced a nearly-invisible 6px dot with "a readable,
amber chip." So the correct vocabulary already existed and the new box bypassed it, which is why
Preflight reads as a different kind of thing rather than the top of the same table.

**Use `StateChip`.** Settling becomes amber for free and stays consistent with the Files tree and
the lifecycle icons.

### 3c. An *arr row's chip says what it means to lftpweb; the tooltip carries the *arr's own words

The user, on the `downloading` chip (2026-08-21):

> *"that downloading clip in preflight is arr telling it is downloading from the client — it is arr
> status but there is no details about that or mouse over... so I think we should spell that out.
> **waiting for download** that might be better. and tooltip maybe we should show the arr details.
> Downloading from "download client name" from arr"*

This is the resolution to the "whose vocabulary is this?" problem. Today the chip shows the *arr's
raw state (`downloading`), which reads as though **lftpweb** is downloading — when in fact lftpweb
is doing nothing at all and is *waiting* for someone else to finish.

**So:**

- **The chip is lftpweb's perspective, not the *arr's.** `downloading` becomes **"Waiting for
  download"** — what the row means *to this app*. Decide whether other *arr states worth showing
  (paused/stalled/queued at the client, etc.) get their own lftpweb-perspective wording or all
  collapse to the one label; **check the actual `TRACKED_DOWNLOAD_STATE_*` vocabulary in
  `core/arrclient.py`** (verified against a live Sonarr, per its own comment) rather than
  inventing states. Do not invent a state the *arr does not report.
- **The tooltip carries the detail and its provenance**, roughly the user's own shape:
  `Downloading from "<download client name>" — reported by <instance name>`. The download client
  name is in `QueueRecord.raw` (`downloadClient`) — **read it from `raw`, do not add a request**;
  the instance name is already on the row as `source_label`. Handle the field being absent
  gracefully: fall back to naming just the instance rather than rendering an empty quote.
- Because the chip now speaks lftpweb's own vocabulary rather than a foreign one, the earlier
  worry about borrowing lftpweb's state colours for another system's states **no longer applies**.
  A Preflight row is a *waiting* row whatever its source, which argues for the same amber the
  settle rows get — Preflight as the waiting room, one colour. **Confirm that reads sensibly
  against `StateChip`'s existing palette and say what you chose**; if amber-for-everything makes
  the box monotonous or collides with `MISSING`'s deliberately-distinct amber, say so.

The figure column should read naturally with both pieces where both exist — follow whatever
`transferLineValue` already does for combining figures rather than inventing a separator.

### 4. Also fix: a handed-over release lingers in Preflight for up to 150s

Folded in from a separate prompt at the user's direction — small, real, and cheaper to fix while
someone is already in these files.

**Confirmed by reading the code, not observed in a browser.** `core/arrsync.py._update_preflight`
calls `hold.update(seen, ...)` where `seen` is built from `_preflight_candidates`, which correctly
excludes any record matching a real `item`. So when a release lands and becomes real, it drops out
of `seen` — and `PreflightHold` cannot tell **"dropped because it was handed over"** from **"the
source blipped this pass."** Both look identical to `update`. The row is therefore held for up to
`PREFLIGHT_HOLD_S` (**150 s**) while the item is *also* showing in Active/pending.

Severity is modest — a self-correcting stale row, no data loss, no wrong state written — but the
cache contract is wrong and worth correcting.

**The fix: let a source distinguish RETIRED from MERELY ABSENT.**

- **Retired** — gone for a known reason (it matched a real item; it was handed over). Terminal.
  **Evict from the hold immediately**; there is nothing transient to smooth over.
- **Merely absent** — not in the source's report this pass, reason unknown. Keeps today's
  `PREFLIGHT_HOLD_S` behaviour, because that is precisely the SABnzbd blank-queue blip the hold
  exists for (`core/arrsync.py`'s module docstring, the 2026-08-18 incident).

Fix it **in the hold's contract in `core/preflight.py`**, not by special-casing inside
`core/arrsync.py` — that module owns "seen-vs-held-vs-expired bookkeeping once, so every source
gets the identical flap tolerance without re-deriving it" (its own docstring), and any future
source hits the same distinction. Either `update` gains a retired set alongside the seen set, or
there is an explicit `retire(identity)`; pick one and justify it.

**Do not simply shorten `PREFLIGHT_HOLD_S`.** That trades this for a worse bug — it re-exposes the
blank-queue flap the hold exists to absorb, and still leaves a duplicate window.

`_preflight_candidates` is where a record is excluded for matching an existing item, so that is
where retirement must be signalled from.

**The settle source is unaffected** — it replaces its rows wholesale each pass and does not use the
hold at all. Note in your report that this bug is the evidence for that earlier choice being right.

**Tests for this part specifically:** a record present in one pass and matched to a real item in
the next must be gone from `rows` **immediately**, not after the hold — write it so it fails
against today's code and confirm that it does. And the flap case must still work: a record that
merely goes missing is still held for the full window and returns without blinking. Assert both,
or the fix could silently disable flap tolerance altogether.

## Before you start

- `frontend/src/pages/TransfersPage.tsx` — `Row`, and the comment near the `w-44` figure column
  explaining why it widened.
- `frontend/src/components/PreflightBox.tsx` — `PreflightRowView`, `SourceChip`.
- `frontend/src/lib/preflight.ts` — `preflightSizeLabel`, `preflightStatusLabel`.
- `backend/lftpweb/core/preflight.py` — the shared row shape and its source-agnostic contract.
- `backend/lftpweb/core/arrsync.py` — `_preflight_candidates`, where an *arr row is built.
- `backend/lftpweb/core/autoqueue.py` — where a settle row is built.
- `backend/lftpweb/core/arrclient.py` — `QueueRecord.raw` and its docstring on reading unnamed
  fields.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it
and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean, in sync with
`origin/dev`, **1566 backend / 573 frontend tests passing, 0 skipped**.

## Tests

Pure logic in `lib/` and backend row construction, per this codebase's convention: the remaining-
time field present, absent, and unparseable; a settle row carrying size but no remaining time; and
the queue tag resolving through `queueDisplayName`'s existing short-name fallback for both sources.

## Docs

`CHANGELOG.md` under `[Unreleased]`. `DESIGN.md` §9.2 only if it describes the row layout.
`docs/decisions.md` only if you hit a decision not already settled here.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to
  600000 ms for pytest (~4 min), reading each exit code. A spawned agent receives no background
  completion notification and will stall forever — a written rule in `CLAUDE.md`, and it has cost
  two manual nudges already.
- From the **repo root** (not `backend/` — running from there collects zero tests and looks like a
  pass): `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. From `frontend/`:
  `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm script**.
- Report backend and frontend test counts before and after; confirm 0 skipped.
- Conventional-Commit prefix (`fix:`). No `Co-authored-by:` trailer.
- **You cannot render a page.** The whole point of this task is visual alignment you cannot see —
  be explicit about that, and say exactly what a human should compare.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a
   proposed one-line commit message. Never `git add -A`, never push.
