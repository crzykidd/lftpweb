---
name: 2026-08-21-queue-tab-rescan-button
status: completed          # pending | completed | failed
created: 2026-08-21
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-21
result: Added a "Rescan now" button to the Queue tab (its own row below Pause/BandwidthControl), extracted the shared baseline-sequence logic into hooks/useRescan.ts (used by both Files and Queue pages), left out a "scanned Xs ago" reading as dishonest for an ungrouped list. 1654 backend / 654 frontend tests, 0 skipped.
---

# Task: add a "Rescan now" button to the Queue tab

Closes the first half of **[issue #19](https://github.com/crzykidd/lftpweb/issues/19)**. The sort-order
half of that issue is **already done and merely recorded there for context — there is nothing to build
for it.** Do not touch `sortTransferRows`.

## Why

v0.3.0 made the Queue tab the default landing page and the working surface, but it has no way to make
lftpweb look at the seedbox. The Files tab has had **"Rescan now"** since early on. Today you have to
switch tabs to trigger a scan, which is silly now that Queue is where you live.

## The decision already made — do not re-litigate

**It rescans every queue, exactly like the Files tab's button.** No per-queue choice, no dropdown. The
Queue tab is a single globally-ordered ungrouped list (v0.3.0 dropped grouping because admission is
global — `core/scheduler.py` has zero references to `queue_id`), so a per-queue rescan control would
imply a per-queue structure that the page deliberately no longer has. `DESIGN.md` already describes the
rescan button as instance-wide.

## The part that matters: reuse, don't reimplement

`FilesPage.tsx` already solves the hard part of this, and the comments there record **two earlier wrong
answers**: a `setTimeout(…, 1000)` fake that stayed "Rescanning…" for exactly one second even when the
scan failed, and the temptation to make the endpoint block. What exists instead:

- `POST /api/files/rescan` (`api/files.py`) only sets the engine's wake event and returns **202**, so
  completion is **not** observable from the response.
- `useLiveModel.ts` exposes `scanCompleteSeq`, bumped on every `scan_complete` WS message for any queue.
- The button captures the sequence in a ref **before** requesting, then clears the spinner on the first
  bump past that baseline. On request failure it clears immediately — there will be no `scan_complete`,
  because the wake event was never set.

**Extract that logic into one shared hook** (e.g. `hooks/useRescan.ts`) and have **both** pages use it,
rather than pasting ~30 lines of subtle baseline-ref handling into a second component. The Files page's
behaviour must not change — it is the reference implementation, so port it, don't redesign it. Carry the
explanatory comments with it; they are the record of why it is not a timer.

Reuse the existing `rescanFiles()` client function and the existing endpoint. **Do not add a second
endpoint** — if you find yourself writing `POST /api/jobs/rescan`, stop.

## Placement

Top of the Queue tab. The page already has a pause banner and `PauseMenu` at the very top (page-level
controls, deliberately above everything else) and per-box headers below. Put the rescan control with the
page-level controls, not inside the Active or Complete box header — it acts on the whole instance, not on
one box's rows. Match the Files tab's button styling so the two read as the same control.

Consider whether the Queue tab should also show the "scanned Xs ago" reading the Files tab carries — the
Files page shows it per queue section, which the Queue tab has no equivalent of. If there is no honest
single-instance version of it, **leave it out and say so in your report**; a rescan button with no
timestamp is fine, an invented or misleading timestamp is not.

## Before you start

- `frontend/src/pages/FilesPage.tsx` — the rescan block and its comments (~lines 77–128).
- `frontend/src/hooks/useLiveModel.ts` — `scanCompleteSeq` and its docstring.
- `frontend/src/pages/TransfersPage.tsx` — the page-level control area at the top (~line 1214 onward).
- `backend/lftpweb/api/files.py` — the endpoint. It almost certainly needs no change; confirm.

## Working tree check

Run `git status --porcelain` and cross-reference. If a file this plan touches is dirty, list it and ask
before proceeding. This prompt file is exempt.

## Tests

Frontend: the shared hook — request success then a `scanCompleteSeq` bump clears the pending state; a
request failure clears it immediately without waiting for a bump; a bump that arrives while not rescanning
does nothing. The Files page's existing tests must still pass unchanged (that is the evidence the port
preserved behaviour).

## Docs

`CHANGELOG.md`. `docs/concepts.md` / `docs/quick-start.md` only if either states where rescan lives.
`docs/decisions.md` only if you make a non-obvious call (e.g. the "scanned Xs ago" question above).
Also append a one-line entry to the "On `dev` since the release" section of `prompts/startnewsession.md`
so a crashed session can pick up where this left off — same commit as the code.

## Conventions to honor

- **Never background a verification gate.** Foreground, with the Bash tool's `timeout` set to 600000 ms
  for pytest (~4 min), reading each exit code. A spawned agent receives no background completion
  notification and will stall forever — a written rule in `CLAUDE.md` that has caught several agents.
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
