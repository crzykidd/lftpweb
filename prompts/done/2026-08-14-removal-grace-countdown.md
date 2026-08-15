---
name: 2026-08-14-removal-grace-countdown
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: |
  Added a synthetic MISSING state-chip substitution (StateChip.tsx, FileTree.tsx's Row) mirroring
  the settle gate's SETTLING pattern exactly, for the removal grace window (DESIGN.md §3.2 rule
  3 / §7.3). New GET-only /api/settings/removal-grace endpoint exposes
  core/mount_sentinel.DEFAULT_GRACE_S read-only (models.RemovalGraceSettingsOut), same pattern
  as SettleSettingsOut.required_scans/min_age_s. Pure derivation functions in lib/format.ts
  (isRemovalGracePending, removalGraceRemainingS, removalGraceShortLabel, removalGraceLabel),
  keyed off an exported REMOVAL_GRACE_ELIGIBLE_STATES set mirroring core/mount_sentinel.py's
  _COMPLETE_PREV_STATES (deliberately duplicated across the language boundary, same as
  core/itemview.py's own fork of the same set). Item drawer (ItemDrawer.tsx) gets a
  RemovalGraceNotice panel with the full sentence plus the absolute first-missing timestamp.
  Lifecycle icons (core/itemview.py) untouched, as required. Frozen-clock edge: frontend cannot
  see per-row mount_ok on the Files page's WebSocket-driven tree (mount_ok only reaches GET
  /api/files, never snapshot/queue_delta/item_delta) without new backend plumbing, so took the
  simpler capping option per the prompt's own fallback instruction -- capped the countdown at a
  bare "Missing" label once elapsed reaches the grace window, rather than showing a stuck/lying
  number. Recorded in docs/decisions.md, including the rejected "permanent presence chip"
  alternative. 16 new frontend unit tests (format.test.ts) plus one backend test
  (test_settings_api.py). docs/concepts.md gained a 7th "things that trip people up" section;
  README.md and docMarkdown.test.ts updated for the new section count. Not click-tested -- no
  browser in this environment; a human should confirm the MISSING chip's amber shade reads as
  visually distinct from SETTLING's, not merely differently worded.
---

# Task: Show a countdown while an item's removal grace period is running

An item whose local copy disappears keeps showing its last known-good state — `VERIFIED`,
`DOWNLOADED`, `EXTRACTED` — for the whole ~10-minute grace window before it resolves to
`REMOVED_LOCAL`/`REMOVED_BOTH`. Nothing on screen says a clock is running, so the row reads as
stale or broken rather than as a decision in progress.

Reported live: a `move`-mode release whose local copy was moved out sat at `VERIFIED` with both
presence icons dim, no size, and **22 children already at `REMOVED_BOTH`**. It looked like a bug.
It was `DESIGN.md` §3.2 rule 3 working exactly as specified, 76 seconds from resolving.

## The framing — this is the fix, don't re-derive it

The row is showing **a past fact** (this was verified) and **a present fact** (both sides are
gone) with nothing indicating **a future transition is scheduled**. The missing information is the
pending change, not the presence.

**The lifecycle icons are already correct and must not change.** This project's rule — presence
icons (R/L) read the world and may go dark, milestone icons (V/E) read timestamps and stay lit —
is deliberate and load-bearing (`core/itemview.py`; collapsing the distinction reinstates a whole
bug class). `V` staying green while `L` goes dim is right: the item genuinely *was* verified.

So the change belongs on the **state chip**, which is the field carrying the transient reading.

## What to build

A synthetic countdown chip for the grace window, **mirroring the settle gate's existing
substitution exactly** — same mechanism, same visual language, so a user learns one idea rather
than two.

| Situation | Chip today | Chip after |
|---|---|---|
| Remote still arriving | `Remote · 23 GB` (amber, synthetic `SETTLING`) | unchanged |
| Local copy vanished, grace clock running | `VERIFIED` / `DOWNLOADED` / `EXTRACTED` (green) | **`Missing · 1m`** (amber) |
| Grace expired | `REMOVED_LOCAL` / `REMOVED_BOTH` | unchanged |

Hover title and the item drawer get the full sentence, e.g.
*"Local copy gone since 17:35. Treated as removed in 1m unless it comes back."*

`components/StateChip.tsx` already carries two synthetic labels (`REMOVING`, `SETTLING`) with the
`FileTree.tsx` `Row` substitution pattern and comments explaining it. Follow that pattern; do not
invent a third mechanism.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §3.2 rule 3 and §7.3.
- Read `core/mount_sentinel.py` — `resolve_absence`, `DEFAULT_GRACE_S`, `_COMPLETE_PREV_STATES`,
  `_STICKY_PREV_STATES` — and `core/engine.py`'s vanished-row sweep, which is the *other* call
  site and the one that produced the reported case.
- Read `frontend/src/lib/format.ts`'s settle-countdown functions (`settleWaitLabel`,
  `settleWaitShortLabel`, `settleArrivingShortLabel`) and their module comments — this is the
  shape to copy, including the short-label-for-the-cell / full-sentence-on-hover split.
- Read `components/StateChip.tsx` and `FileTree.tsx`'s `Row` substitution for `SETTLING`.
- Read `core/itemview.py` — confirm `first_missing_at` is already in `ITEM_VIEW_COLUMNS` and on
  the wire (it is, as of the live API response that produced this report; verify rather than
  assume).

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## What to do

### 1. Expose the grace constant so the countdown can be accurate

`DEFAULT_GRACE_S` (600.0) lives in `core/mount_sentinel.py` and the frontend has no way to know
it. **Do not hardcode 600 in the frontend** — that is the same drift trap as a hand-maintained
settings list.

`SettleSettingsOut` already solves this exact problem: it ships `required_scans`/`min_age_s` as
read-only fields computed from `core/settle.py`'s own constants. Follow that precedent. Whether it
belongs on an existing settings response or a small new one is yours to decide — say which and why
in `docs/decisions.md`.

### 2. Derive the countdown state on the frontend

The signal is `first_missing_at != null` **and** the row's state being one that asserts local
content was present (the `_COMPLETE_PREV_STATES` set: `DOWNLOADED` plus the post-processing
outcomes). A row already at `REMOVED_LOCAL`/`REMOVED_BOTH` has *finished* — it must not show a
countdown.

Put the derivation in `frontend/src/lib/` as pure functions, testable without mounting anything —
the existing convention, and why `lib/*.test.ts` exists.

**Guard the arithmetic**, and prefer showing the plain state over a wrong number:

- `first_missing_at` in the future, or unparseable → no countdown.
- Elapsed already past the grace window but the row not yet rewritten (the scan hasn't run) →
  decide what to show. Recommendation: `Missing` with no number rather than a negative or `0s`,
  since the transition is imminent but hasn't happened.
- The mount sentinel freezing the clock (see step 4) → the number would be a lie; see below.

### 3. The item drawer gets the full sentence

`ItemDrawer.tsx` already shows a lifecycle chronology and `local_mtime`. Add the grace state
there in plain language, including the absolute timestamp — the drawer is where someone goes to
answer "what is actually happening to this item", and it is the natural place for the longer form
the chip cannot fit.

### 4. The honest edge: a frozen clock

`resolve_absence` deliberately **does not advance the grace clock when `mount_ok` is false** —
"never start the grace clock on a reading we can't trust to mean what it appears to mean." So on
an unmounted or flaky share the countdown can appear to run out client-side while the backend
never transitions the row.

A ticking countdown that reaches zero and does nothing is worse than no countdown. Decide how to
handle it and record the choice:

- Preferred: the queue's mount-gate status is already exposed to the UI
  (`Engine.mount_ok`, surfaced per queue). If the gate is failing, say *that* instead of a
  countdown — "local root unavailable" is the actual problem and the more useful message.
- Acceptable: cap the display at `Missing` once elapsed exceeds the grace window, so it never
  shows a negative or a stuck `0s`.

Verify what the frontend can actually see about the mount gate before designing around it; if it
cannot see it without new plumbing, **report that and take the simpler option** rather than adding
a backend field on your own initiative.

## Testing

- Pure-function tests: inside the window, just expired, `first_missing_at` null, in the future,
  unparseable, and a row already `REMOVED_LOCAL`/`REMOVED_BOTH` (no countdown).
- A test that the countdown appears for each state in `_COMPLETE_PREV_STATES` and for none outside
  it — keyed off the shared set, not a hand-copied list, so the two cannot drift.
- Run `npm run lint`, `npm test`, `npm run build`; `uv run pytest` (fake seedbox likely already
  running — if so, leave it); `ruff check` **and** `ruff format --check`; `docker compose config
  --quiet` on all three compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives. The
  rejected alternative worth recording: making the chip show presence (`Missing`) *permanently*
  and demoting the milestone to icons only — rejected because it loses "this item completed
  successfully" for a case that lasts ten minutes, which is the mistake the presence/milestone
  split exists to prevent.
- `CHANGELOG.md` entry.
- Update `docs/concepts.md` — it is the single source the in-app Docs render from, and the grace
  period is exactly the kind of thing its "things that trip people up" list is for. It currently
  covers the settle gate; this is its counterpart at the other end of the lifecycle.
- **You cannot see the UI** — no browser exists here. Say plainly that the chip needs a human to
  look at, particularly that it is visually distinct from the settle countdown rather than
  confusable with it.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
