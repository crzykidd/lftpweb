---
name: 2026-08-12-small-fixes-and-scan-visibility
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: |
  All three fixes landed. `db.py.connect()` sets `PRAGMA busy_timeout = 30000` (matching
  `core/backup.py`'s dedicated VACUUM connection), with every pragma cursor closed explicitly;
  a new test reads it back. `FileTree.tsx`'s Expand/Collapse all now give a distinct `title`
  for "no directories at all" vs. "filters active". `core/engine.py.scan_queue` publishes a
  new `scan_complete` WebSocket message (queue_id, finished_at, ok, warning) at the end of
  every pass, success or failure; `useLiveModel.ts` exposes a `scanCompleteSeq` counter, and
  `FilesPage.tsx`'s "Rescan now" clears on the first one after its own request instead of a
  1s timer, with a relative "scanned Xs ago" / absolute-on-hover readout that folds in a
  partial-scan warning. Existing `tests/test_ws_deltas.py` call sites updated for the new
  message interleaving; 2 new tests added there plus 1 in `tests/test_db.py`. 508 tests pass
  (fake seedbox up, torn down after), both ruff gates clean, `npm run build`/`npm run lint`
  clean. One pre-existing, unrelated test failure
  (`test_extract_no_archives_is_a_no_op_success`) was observed transiently mid-run, caused by
  a parallel agent's in-flight edit to `core/extract.py`/`tests/test_postprocess.py` (not
  touched by this prompt) — resolved on its own by the next full run.
---

# Task: Three small correctness fixes, plus make scan completion observable

Three unrelated small defects found by inspection and by the user running the real app on
2026-08-12, grouped into one prompt because each is small and they touch disjoint files.
The third one grows a little: fixing it properly needs the backend to say when a scan
actually finished, which is worth having on its own.

## Before you start

- Read `CLAUDE.md` (per-session rules) and `prompts/startnewsession.md`'s "Where we are"
  and "Traps worth knowing" sections.
- `DESIGN.md` §2/§9 for the WebSocket contract, §5 for the scan loop.
- **The WebSocket delta rule is load-bearing** (`prompts/startnewsession.md`, traps list):
  never publish a full node list except on connect. Anything you add must be a small,
  fixed-size message — never proportional to tree size.

## Working tree check

Run `git status --porcelain` and cross-reference the files below. If any have uncommitted
changes, list them and ask before touching them. **Another agent is working in
`core/extract.py` and `core/postprocess.py` in parallel with you — those files are not
yours; if you see them dirty, that is expected, leave them alone and do not report it as
a blocker.** This prompt file is exempt.

## Fix 1 — no `busy_timeout` on the shared connection

`backend/lftpweb/db.py:32-40`. `connect()` sets `journal_mode = WAL` and `foreign_keys =
ON` but no `busy_timeout`, so the shared application connection is at SQLite's default of
**0** — any lock contention fails instantly with `SQLITE_BUSY` instead of waiting.

The app now has several concurrent writers (the engine's scan persist, the transfer
queue's ~1 Hz tick, the metrics sampler's 30s heartbeat, the post-processing pipeline)
plus, since `209928d`, a second connection for `VACUUM INTO`.

- Set `PRAGMA busy_timeout` on the shared connection. **30000 ms**, matching what
  `core/backup.py.create_backup` already sets on its dedicated connection — one number,
  not two conventions.
- **Watch the cursor trap.** `core/backup.py` documents it: a PRAGMA returning a row whose
  cursor is never closed leaves an unfinalized statement on the connection, which made
  `VACUUM` fail with `cannot VACUUM - SQL statements in progress`. `db.py.connect()`
  currently uses bare `await conn.execute(...)` for its pragmas. Close the cursors, or at
  minimum verify the existing pragma calls aren't already leaving statements open — the
  pre-migration backup runs on this very connection's database.
- Add a test asserting the pragma is actually in effect on a connection from `connect()`
  (`PRAGMA busy_timeout` read back), not merely that the line exists.

## Fix 2 — Expand all / Collapse all give no reason when disabled

`frontend/src/components/FileTree.tsx:380-397`. Both buttons are
`disabled={!hasDirectories || filtersActive}` and carry a `title` that only explains the
**filter** case. When a queue's tree has no directories at all (`hasDirectories`, line
211) both render greyed at `opacity-50` with no explanation and read as broken. The user
hit exactly this and assumed the feature didn't exist.

Give the disabled state a reason in both cases — e.g. "Nothing to expand — this queue has
no directories" vs. the existing filter message. Keep it a `title`; don't add a tooltip
library.

## Fix 3 — "Rescan now" reports completion it knows nothing about

`frontend/src/pages/FilesPage.tsx:15-20`:

```js
setRescanning(true)
await rescanFiles()                              // POST /api/files/rescan -> 202
setTimeout(() => setRescanning(false), 1000)
```

`POST /api/files/rescan` (`api/files.py:58`) only sets the engine's wake event and returns
202 immediately. So the button says "Rescanning…" for exactly one second and then goes
idle, whether the scan took 200 ms or 90 seconds. On a large remote tree it is actively
misleading about the single thing the user pressed it to learn.

**Fix it by making scan completion a real signal, not a guess:**

1. **Publish a `scan_complete` WebSocket message** at the end of each `scan_queue` pass in
   `core/engine.py`. Small and fixed-size — `queue_id`, a finish timestamp, and whether
   the pass carried a partial-scan warning (`PrimaryScanOutcome.warning`; GNU `find` exits
   nonzero on one unreadable subdirectory and still returns usable output — see
   `interpret_primary_scan_result`). **Publish it on failed passes too**, or a scan that
   errors leaves the button spinning forever.
2. Follow the existing message conventions in `api/ws.py` / `frontend/src/hooks/
   useLiveModel.ts`. Do not invent a second socket or a poll.
3. The button's busy state clears on the first `scan_complete` after its request — not a
   timer.
4. **Surface "last scanned" on the Files page** — relative ("scanned 12s ago"), absolute
   on hover, from the same message. This is the readout the user actually wanted when they
   asked for a refresh dropdown, and it makes the 30s `scan_interval_s` visible instead of
   something they have to infer from rows not changing.
5. If a pass carried a warning, say so in that readout — a partial scan currently
   surfaces only in logs.

**Do not** add a client-side refresh interval or polling dropdown. The Files page is
WebSocket-driven; there is no poll to tune, and a client timer would re-read the same
persisted data. A configurable *server-side* per-queue scan interval is a separate,
already-identified task — not this one.

## Conventions to honor

- Match surrounding comment style: explain *why*, and name the rejected alternative.
- `docs/decisions.md`, newest at top — one entry covering the `scan_complete` message
  (why a WS message rather than making `/api/files/rescan` block until the scan finishes:
  the endpoint is deliberately fire-and-forget, and a blocking variant would tie up a
  request for the length of an SSH tree walk).
- `CHANGELOG.md` under `## [Unreleased]` → `### Fixed`.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .` — `format --check` has caught files
  `check` alone missed three separate times in this project.
- `npm run lint` and `npm run build` in `frontend/`.
- `uv run pytest` — 490 tests pass today; you should be at 493+. Bring the fake seedbox up
  (`docker-compose.test.yml`) so nothing skips, and tear it down afterward
  (confirm with `docker ps -a`).
- **You cannot see the UI.** No browser exists here. Say "builds, type-checks, lints, and
  the endpoints it calls were verified" — never "renders correctly".

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: the file list, a proposed one-line
   `fix:` message, the test count, lint results, and anything you found but did not fix.
   Never `git add -A`, never push.
