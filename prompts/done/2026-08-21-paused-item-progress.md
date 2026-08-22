---
name: 2026-08-21-paused-item-progress
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: A queued row already carried bytes_done/bytes_total (JobOut) but bytes_done was stale
  (0) for a fresh retry job row -- fixed by joining item.local_size into list_jobs() and
  preferring it in _job_out for a queued row only, mirroring the existing bytes_total ->
  item.remote_size fallback. Frontend: stateProgressPercent (lib/fileTree.ts, shared by Files
  and Transfers) gained a QUEUED branch with a zero-guard against a bogus 0%; StateChip.tsx's
  FILL_STYLES.QUEUED got its own indigo pair (its own base hue, not PARTIAL/WAITING's amber).
  1608 backend / 621 frontend tests, 0 skipped. Browser-unverified.
---

# Task: a paused item should still show how much of it is already downloaded

The second half of **[issue #14](https://github.com/crzykidd/lftpweb/issues/14)**. The first half
(pause for a fixed duration) has its own prompt, `2026-08-21-pause-for-duration.md`, and lands first —
**check whether it is already in `prompts/done/` and read what it built** before starting, since both
touch the same rows and the same pause machinery.

## Why

"Pause now" SIGTERMs the running lftp and returns the job to `queued`, deliberately keeping its
`queue_position`, `attempt` and **partial bytes** — the whole point being that pause is
*non-destructive*. But the row then renders as plainly queued, losing every sign that the item is 45%
downloaded and will resume from exactly there. The user's words on the issue: showing something like
**`Queued 45%`** would make pause visibly non-destructive.

## What to build

A queued row that has partial bytes on disk shows its progress — in or under its chip, matching the
page's existing economy. `StateChip` already supports a progress fill (`WAITING`/`PARTIAL` share a
confirmed amber pair; `82197a5` added the ticking fill to the Queue row's chip), so this is very likely
a matter of **feeding a `percent` to a chip that already knows how to draw one**, not new chip
machinery.

## The question to answer first, from the code

**Does a `queued` row already carry the numbers?** `JobOut` / the jobs list projection may or may not
expose `bytes_done` (or the item's local bytes) and the known remote size for a job that is not running
— the live progress path is `_publish_child_progress` over the WebSocket, which only speaks for
*running* transfers. Find out before designing:

- If the numbers are already on the row → **frontend only.** Prefer that.
- If they are not → the smallest honest backend addition to the existing jobs projection. Do **not**
  add a new endpoint, and do **not** put a filesystem stat on the request path — progress is derived
  from the filesystem by the scanner (§1.3), and the scan already records local bytes. Reuse what the
  scan persisted rather than measuring afresh.

If it turns out to need more than a small projection change, **stop and report** rather than growing
this into a data-plumbing task.

## Behaviour to get right

- **Only where it is true.** A queued row with genuinely no local bytes must look exactly as it does
  today — no `0%`, no empty fill. The signal is "there is already something on disk", so absent that,
  say nothing.
- **This is not only about pause.** A job queued after an interrupted attempt (`attempt > 1`, partial
  bytes on disk) is in the same situation and should read the same way. Don't gate the display on "the
  queue is paused" — gate it on "this row has partial bytes". That is both simpler and more honest.
- **Don't imply motion.** A paused/queued item is not downloading. If the fill or wording suggests
  active transfer, that is worse than showing nothing — `SETTLING` deliberately has no fill for exactly
  this reason. Match that judgement.
- **Percent of what.** Use the same denominator the running row uses (known remote size), so a row does
  not appear to jump when it starts. If the remote size is unknown, show nothing rather than a
  percentage of a guess.

## Before you start

- `frontend/src/components/StateChip.tsx` — the fill, and which states have one and why.
- `frontend/src/pages/TransfersPage.tsx` — the collapsed row line (`45% · 40 MB/s · 25m left`, `31216a8`)
  and the queued-row rendering.
- `prompts/done/2026-08-20-queue-pause.md` — what pause-now leaves behind on the job row.
- `DESIGN.md` §1.3 — progress is derived from the filesystem, never asked of lftp.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and ask
before proceeding. This prompt file is exempt.

## Tests

A queued row with partial bytes renders the percentage; one with none renders exactly as before; the
denominator matches the running row's so there is no jump at start; an unknown remote size renders
nothing rather than a bogus figure. If the projection changed, a backend test that a queued job's row
carries the bytes.

## Docs

`CHANGELOG.md`; `docs/concepts.md`'s pause section (pause being visibly non-destructive is the point of
this change). `docs/decisions.md` if the "gate on partial bytes, not on paused" call needs recording.
Also append a one-line entry to the "On `dev` since the release" section of `prompts/startnewsession.md`
so a crashed session can pick up where this left off — same commit as the code.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to 600000 ms
  for pytest (~4 min), reading each exit code. A spawned agent receives no background completion
  notification and will stall forever — a written rule in `CLAUDE.md`.
- From the **repo root** (not `backend/`): `uv run pytest`, `uv run ruff check`, `uv run ruff format
  --check`. From `frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. There is **no `typecheck` npm
  script**.
- Report backend and frontend test counts before and after; confirm 0 skipped. Prefix `feat:`. No
  `Co-authored-by:`.
- **You cannot render a page.** Say plainly what a human should check.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (plain `mv` if git refuses — it is untracked).
3. **You are a spawned agent: do NOT commit.** Prepare the tree, report the file list and a proposed
   one-line commit message. Never `git add -A`, never push.
