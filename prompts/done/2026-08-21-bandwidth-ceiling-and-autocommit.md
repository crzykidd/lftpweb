---
name: 2026-08-21-bandwidth-ceiling-and-autocommit
status: completed        # pending | completed | failed
created: 2026-08-21
model: opus              # changes the bandwidth data model; §4.5-adjacent
completed: 2026-08-21
result: >
  Bandwidth split into a Settings-owned ceiling (`max_bandwidth_bps`) and a Queue-slider throttle
  (`throttle_bandwidth_bps`, clamped to the ceiling on every write, no migration needed), with
  `effective_bandwidth_bps()` feeding the scheduler, the fast-lane reserve and Start-now fractions
  at the call site -- `core/scheduler.py` and §4.5's invariant untouched. The slider now
  auto-commits 5 visible seconds after the last change behind an "Apply to new items only"
  checkbox (checked by default); Apply/Cancel and the amber confirm dialog are gone, replaced by
  one in-place banner that counts down and then reports the server's real restart count, saying
  plainly that nothing was restarted while paused. 1675 backend / 663 frontend, 0 skipped.
---

# Task: bandwidth — a real ceiling, and a slider that commits itself

Findings **1 and 4** of `prompts/test-findings-2026-08-21.md`, from the user's browser test of
`764aaa7`. Both are about the same control, and finding 4's slider bounds depend on finding 1's
model, so they ship together.

## Part 1 — the ceiling (a data-model change, not a bounds tweak)

> *"The max on this should never exceed the max set in transfer settings. That is the max."*

Today there is **one** value, `max_bandwidth_bps`, owned by `SchedulerSettings`/`TransferSettings`
and edited by *both* Settings → Transfer and the Queue slider. That was deliberate
(`prompts/done/2026-08-21-bandwidth-from-the-queue-page.md` — "one control, one setting"), and the
user has now rejected it.

**Why the obvious fix does not work:** capping the slider at "the value the slider itself edits" is
circular — lower it once and the ceiling drops with it, so it can never be raised back from the
Queue page. A ratchet. The user's own test expectation (*"if I drag to 10mb then it stays till I
move back to max"*) is impossible under the current single-value design.

**So: two values.**

- **Hard max** — Settings → Transfer's `max_bandwidth_bps`. Meaning unchanged: the ceiling.
- **Effective limit** — new, edited by the Queue slider, bounded to `[min_share_floor_bps, hard
  max]`. **This is what the scheduler allocates against.**

**Keep the blast radius small.** `core/scheduler.py.admit()` takes `SchedulerSettings.max_bandwidth_bps`
as its budget `B`. Do **not** rename or re-plumb that — feed the *effective* value into it at the
call site. The scheduler's contract and §4.5's worked examples stay exactly as written.

Requirements:

- **The effective limit persists across restarts** — confirmed by the user. It is a stored setting,
  not session state. Nothing resets it on restart, on an empty queue, or on unpause.
- **Lowering the hard max below the effective limit clamps the effective limit.** Test it directly.
- **Raising the hard max does not raise the effective limit** — it only raises the ceiling the
  slider can reach.
- **Default when unset:** effective == hard max (so an upgrade changes nothing about behaviour).
- **Start-now fractions** (migration 022) are `fraction × site limit` at admission. They must use the
  **effective** limit — that is the limit actually in force. State this in your report and in §4.5.
- Say whether a migration was needed and why/why not.
- The two surfaces are no longer the same number, so the existing "both surfaces reflect each other"
  behaviour must be **restated, not deleted** — Settings → Transfer edits the ceiling, the Queue
  slider edits the throttle, and the slider's maximum must track the ceiling live.

## Part 2 — the slider commits itself

> *"Too many clicks… probably a check box 'apply to new items only'… and when I drag it, after an x
> second wait to make sure it isn't changed more, we just execute the change."*

Today: drag → two buttons + Cancel appear → click one → the in-progress path shows an amber
confirmation → confirm. **Four interactions to change a number.** Replace all of it:

- **A checkbox beside the slider: "Apply to new items only." Default CHECKED.** It replaces the
  two-button fork. Checked = future admissions only. Unchecked = also re-admit running transfers.
- **No Apply button, no Cancel, and the amber confirm dialog is removed.** The checkbox *is* the
  consent for the interrupting path — that was settled with the user explicitly, on the reasoning
  that a deliberate uncheck is a better guard than a dialog on every drag.
- **Auto-commit 5 s after the last change.**

**One banner, two states, in place** — it must not appear twice or stack:

| When | Text |
|---|---|
| On slider change, immediately | "Bandwidth update applied in **5** seconds…", counting down |
| On commit, checked | "Bandwidth set to **10 MB/s** for all new transfers." |
| On commit, unchecked | "Bandwidth set to **10 MB/s** — **N** running transfers restarted." |
| On commit, unchecked, queue paused | Must say **nothing was restarted**. |

- **The real count**, not a generic phrase — `POST /api/queue/bandwidth` already returns it.
- **The paused case is not hypothetical.** The backend deliberately skips re-admission while paused
  and returns `skipped_because_paused` (that is what stops a bandwidth change cancelling a timed
  pause). Reporting a restart that did not happen would be worse than the dialog being removed.
- **Moving the slider again restarts the countdown** — that is the cancel affordance, and it is why
  no Cancel button is needed. Drag back to the original value and nothing should commit at all.
- **Toggling the checkbox mid-countdown restarts the timer** — it changes what is about to happen.

## Before you start

- `prompts/test-findings-2026-08-21.md` findings 1 and 4 — the full record including what the user
  accepted and why.
- `prompts/done/2026-08-21-bandwidth-from-the-queue-page.md` — what exists and why.
- `DESIGN.md` §4.5 — **allocations are never re-shaped.** This task does not change that. Re-admit
  remains the only way a running job gets a new number.
- `backend/lftpweb/core/queue.py.set_site_bandwidth`, `core/scheduler.py`, `frontend/src/components/BandwidthControl.tsx`,
  `frontend/src/lib/bandwidth.ts`.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and
ask before proceeding. This prompt file is exempt.

## Tests

Clamp on lowering the hard max; effective persists across a restart; effective defaults to the hard
max when unset; the slider cannot exceed the hard max (server-side, not only in the UI); scheduler
allocates against effective; start-now fractions compute against effective; the existing
invariant tests still pass unchanged; the paused case still skips re-admission and reports it. For
the frontend, the debounce/countdown/banner-state logic should be **pure and unit-tested** — this
repo has no component-rendering harness, so put the logic in `lib/` and test it there.

## Docs

`DESIGN.md` §4.5 (the two-value model and which one the scheduler uses); `CHANGELOG.md`;
`docs/concepts.md`; `docs/decisions.md` — record *why* one-setting-two-surfaces was reversed, since
it was an explicit decision only a day earlier and a future reader will otherwise re-derive it.
Update `prompts/test-findings-2026-08-21.md`: mark findings 1 and 4 **done**. Also append a one-line
entry to `prompts/startnewsession.md`'s "On `dev` since the release" section — same commit.

## Conventions to honor

- **Never background a verification gate.** Foreground, `timeout` 600000 ms for pytest (~4 min),
  read each exit code. A spawned agent receives no background completion notification and will stall
  forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test -- --run`.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `feat:`. No
  `Co-authored-by:`.
- **Surface, don't silently resolve.** If the two-value model cannot be built without weakening
  §4.5, say so rather than weakening it.
- **You cannot render a page.** Say what a human should check.

## When done

1. Update frontmatter: `status`, `completed`, `result`.
2. `git mv` into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
