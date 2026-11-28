---
name: 2026-11-27-preflight-retire-finished-and-gzip
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: F1 retire-on-terminal-signal and gzip middleware done; ruff check, ruff format --check and pytest (2446 passed) all exit 0; frontend untouched
---

# Task: retire finished downloads from Preflight immediately (F1), and gzip API responses (F2 part 1)

This task fixes two findings from a live test round on the test system. Both are written up in
`prompts/test-findings-2026-11-27.md`; read F1 and F2 there first.

**The user's hard rule is that nothing breaks for an existing install.** That memory and
standard come from C3–C5 (`b251d35`, `c95d1a9`, `62a0d81`). Both changes here must be invisible
apart from being faster or more accurate.

## Before you start

- Read `CLAUDE.md`, then the relevant `DESIGN.md` sections:
  - Preflight, §17 / the download-client framework
  - the middleware stack
- Read `docs/download-client-framework-spec.md` §4.2 and §9.2. They cover the blank-queue
  incident the hold exists for.
- Work in **this checkout on `dev`**, and do not create a worktree. Ignore `.claude/worktrees/`.
- The findings file `prompts/test-findings-2026-11-27.md` is untracked and you will update it
  (below).

## Working tree check

Run `git status --porcelain`. The only expected dirty paths are `.claude/worktrees/`, this
prompt, and `prompts/test-findings-2026-11-27.md`. If anything else is dirty, stop and report.

## Part A: F1, a finished download's Preflight row lingers for up to 150 s

**Today's behaviour:** `core/clientsync.py` builds `seen` from Preflight-eligible-phase
transfers and calls `PreflightHold.update(seen, now=now)` without a retirement set. The hold is
in `core/preflight.py`, with `PREFLIGHT_HOLD_S = 150`. A transfer that **finishes** drops out of
`seen`, and its row keeps its last label for 150 s. That label is "Extracting" for SAB, or
"downloading, 285 MB left" for rTorrent. It was seen three times live, and there is no data
risk; only the label is stale.

**Fix:** on each poll pass, compute a `retired` set of `client_id`s that this same pass
**positively** reports as terminal. Pass it to `hold.update`; `PreflightHold.update` already
supports `retired` for arrsync.

- **SAB:** a history entry for that `client_id` whose phase is terminal (`COMPLETED`/`FAILED`,
  or whatever terminal phases the model defines). The cheap history call already runs on every
  fast tick when `LIST_HISTORY` is NATIVE. A history entry in post-processing (Extracting,
  Verifying, Moving, Running) is **not** terminal, so its row should keep updating as it does
  now.
- **rTorrent:** the transfer present in this pass's result with a terminal or finished phase
  (`SEEDING`, `COMPLETED`, `FAILED`).
- Work this out per connector from what the pass actually fetched, using the phase model
  (`core/clients/models.py`, `settle.FINISHED_TRANSFER_PHASES`). Don't special-case names.

**Keep the anti-flicker guarantee.** A blank, partial, or failed response carries no positive
terminal signal, so the row stays held exactly as today. This guards the v0.2.4 incident and
`tests/fake_sabnzbd.py`'s `queue_empty_for_requests`. Existing hold tests must keep passing
unchanged.

**Tests:**
- A SAB transfer goes downloading, then extracting, then appears in history as completed. Its
  row disappears on that pass and doesn't linger.
- A blank queue response with no history entry keeps the row held.
- An rTorrent transfer going from downloading to seeding retires immediately.
- A post-processing history entry keeps the row, with its label updated.

**Docs:**
- Rewrite the `core/clientsync.py` module docstring paragraph that calls this an "accepted
  cost".
- Update DESIGN, or the spec section that describes the hold.
- `docs/decisions.md`: why a positive terminal signal is safe to act on when a missing row is
  not.

## Part B: gzip HTTP responses

`GET /api/files` returns 3.1 MB of uncompressed JSON on the test system, and the 1 MB JS bundle
is also uncompressed. Add Starlette's `GZipMiddleware`, with `minimum_size` around 1000.

Make sure all of the following hold:
- Only clients sending `Accept-Encoding: gzip` get gzip. Starlette does this by default; confirm
  it.
- **WebSockets are unaffected.**
- **Streaming and already-compressed responses are not broken or double-compressed.** Find every
  `StreamingResponse`/`FileResponse` in the app: log tail and download, support-bundle zip,
  backup download, job output, anything SSE-like. Check the installed Starlette version's
  behaviour, including `text/event-stream` and whether it skips responses that already have
  `Content-Encoding`. Exclude anything that must stream live, or is already compressed (zip,
  gz), if Starlette wouldn't skip it on its own.
- **Middleware order.** Place it so C3/C5's `TrustedProxyMiddleware`, `RequestGuardMiddleware`,
  `AuthMiddleware` and the Host allow-list still see and answer requests exactly as before.
  Gzip should wrap the outside of the response path. Explain the placement in a comment.
- **The image healthcheck** (`curl -fsS` without `--compressed`) still passes. curl sends no
  `Accept-Encoding` by default, so confirm that.

**Tests:**
- `/api/files` with `Accept-Encoding: gzip` comes back gzip-encoded, and decodes to the same
  JSON.
- The same request without the header comes back plain.
- A WebSocket still connects.
- A streaming or zip endpoint is unaffected.

Measure the before and after size of a large `/api/files` fixture response and report it.

**Docs:**
- CHANGELOG `[Unreleased]` under Changed or Fixed, whichever fits. Put it in user words: "pages
  load faster: responses are compressed".
- DESIGN, where the middleware stack is described.

## Findings file

Update `prompts/test-findings-2026-11-27.md`:
- Mark F1 **done**.
- Mark F2 part 1 (gzip) **done**, with the measured sizes.
- Leave F2's other parts and the minor items **open**.

This file goes in the same commit.

## Conventions to honor

- Run every verification gate in the **foreground** with an explicit timeout; for pytest use
  600000 ms. You will never receive a background-task notification, so read each exit code
  yourself.
- From the **repo root**, run and read the exit code of each of these separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`. The fake seedbox containers are up, so expect **0 skipped**. Baseline:
    2437 passed.
- From `frontend/`, run `npm run lint`, `npx tsc -b`, and `npm test` (baseline: 867 passed),
  if you touch the frontend.
- Report every gate's exit code and the counts.

## When done

1. Set this file's frontmatter, then move it into `prompts/done/`. It is untracked, so a plain
   `mv` is fine.
2. **Do not commit.** Report back with:
   - what changed in Parts A and B
   - the gzip size numbers
   - anything that could behave differently for an existing install
   - the gate results
   - the full explicit list of paths for a single commit, including this prompt in
     `prompts/done/` and `prompts/test-findings-2026-11-27.md`
   - a proposed one-line `fix:` message, with no `Co-authored-by` trailer
