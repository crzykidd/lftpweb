---
name: 2026-08-11-phase3b-transfers-ui
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: >
  Success. WS delta fix (queue_delta/item_delta replacing full-tree queue_snapshot) landed
  first and is proven proportional by test (20 vs 5,000 item trees) and measured live
  (~152-189 bytes/message vs. a 2,754-byte full snapshot). Transfers page, item drawer
  (virtualized side drawer), and Files page actions (virtualized tree, multi-select
  shift-range, per-row/bulk Queue/Stop) all built and wired to the phase 3a API, plus two
  small API additions the UI needed (list_jobs() broadened to include an item's most recent
  terminal job; POST /api/items/{id}/stop). Phase-2 scan-abort bug fixed (one unreadable
  subdirectory now produces a warning, not a vanished tree) and verified live against the
  fake seedbox. Full end-to-end verified via the real API + a real WebSocket client against
  the fake seedbox: queue, live progress, stop -> STOPPED with no refresh, all confirmed. Not
  verified: actual browser click-through (no browser available in this environment) -- build
  and lint are clean and the API/WS contract the frontend consumes was exercised for real,
  but the React components themselves were never clicked by a human or an automated browser.
  Two design deviations surfaced rather than silently absorbed: DESIGN.md §9's "TanStack
  Query for REST" was never adopted in phases 1-3a and this phase continues that; a new
  @tanstack/react-virtual dependency was added for virtualization (deferred from phase 2).
  Also: this session found the working tree already dirty with an unrelated concurrent
  session's GitHub-repo-bootstrap work partway through, including to prompts/startnewsession.md
  -- see docs/decisions.md's note. Not committed (spawned-agent instruction); see the final
  report for the proposed commit message and exact file list.
---

# Task: Phase 3b — Transfers UI, item drawer, and the WebSocket delta fix

Phase 3a built the engine and its API; nothing renders it yet. Make the transfer engine
*visible*: the Transfers page, the per-item drawer, working actions on the Files page, and the
WebSocket change that 1 Hz progress forces.

**Done when:** you can queue an item from the Files page, watch it progress live on Transfers,
open the drawer to see per-file status, stop it, and see it go `STOPPED` — all without a page
refresh, and without the WebSocket re-sending whole queue trees every second.

## Before you start

- **Read `DESIGN.md`** §9 in full (§9.1 shell and stats header, §9.2 pages and the drawer, §9.3
  knob placement), §2 (the WebSocket contract), §3.2 (states), §4.5–4.6 (what the actions mean).
- Read `prompts/startnewsession.md` and `docs/decisions.md` — phase 3a added several entries
  that explain why the engine behaves as it does.
- Phases 1–3a are committed. The API you need already exists: `GET/POST /api/jobs`,
  `POST /api/jobs/{id}/{stop,move-to-top,start-now}`, `POST /api/items/{id}/retry`,
  `GET /api/files`, `GET /api/stats`, `GET/PUT /api/settings/transfer`.

## Working tree check

Run `git status --porcelain` and cross-reference. If files this plan touches have uncommitted
changes, list them and ask first. Surface unrelated dirty files once. This file is exempt.

## What to do

### 1. The WebSocket fix — do this first, it constrains everything else

Phase 2 shipped `ws.py` sending a **full per-queue snapshot** on every change. That was fine for
a read-only tree that changed every 30 s. It is not fine now: phase 3a's progress sampler
updates at ~1 Hz, and a queue holding a few thousand files would re-serialise and re-send the
entire tree every second, to every connected browser.

Send **actual deltas**: a full snapshot on connect (per §2), then only the rows that changed,
keyed so the client can merge them. Progress ticks touch a handful of rows — the payload should
be proportional to what changed, not to the size of the tree.

**Prove it with a test**, not by inspection: build a queue with many items, mutate a couple, and
assert the emitted payload contains only those and does not grow with total tree size. This is
the kind of regression that silently returns.

### 2. Transfers page (§9.2)

Rows stay deliberately plain:

```
Some.Release.S03E04.2160p    [downloading]   18 files   62%   4.1 MB/s   ETA 12m
```

- **Visible status vocabulary is `queued` / `downloading` / `downloaded`.** Other internal
  states (§3.2) surface only on rows where they actually apply. Do not expand everyone's mental
  model to twelve chips.
- Per-row actions: **Move to top**, **Start now at max bandwidth**, **Stop**, and **Retry** on a
  failed row. Start-now deliberately oversubscribes the ceiling (§4.5) — explain that inline the
  first time it's used rather than silently doing something surprising.
- Failed rows show the error class and the captured lftp output tail.
- Show each job's **allocated rate**, not just its current speed. Under admission control a job
  allocated 5 MB/s that is pulling 2 still *holds* 5 — without that number the scheduler looks
  broken when it is working correctly (§9.1).

### 3. Item drawer (§9.2)

Clicking a row opens a **side drawer, not a modal** — file lists get long and the queue should
stay visible. Per-file: name, size, transferred, progress, status. **Virtualized** — a release
can carry hundreds of files.

### 4. Files page actions

The Files tree is read-only from phase 2 because there was no engine behind it. There is now:
add **Queue**, **Stop**, and multi-select with shift-range plus bulk actions (§9.2). Manual
queueing always wins and clears suppression (§4.7) — the UI should not filter what the user
explicitly selected.

**Also add virtualization to the Files tree.** §9.2 requires smooth at 10k+ rows; phase 2
deferred it with a code comment. You are already building a virtualized list for the drawer, so
do both and retire the gap.

### 5. Stats header

Phase 1 stubbed `/api/stats` with zeros; phase 3a made it real. Wire the header to live values —
current speed, **allocated vs. ceiling**, queued count and bytes, 24 h transferred — and confirm
they move during a transfer.

### 6. Named carry-over: the scan-abort bug from phase 2

Phase 3a found, and correctly left alone, a phase-2 bug: **a single permission-denied
subdirectory aborts the entire remote scan** in `core/remote.py`. One unreadable directory on
the seedbox and the user sees an empty or truncated tree with no explanation.

It is in scope here because it determines whether the UI shows anything at all. Fix it: skip
what cannot be read, keep scanning, and surface the skipped paths (a queue-level warning is
enough — the tree should render with a note, not vanish). Add a test with an unreadable
subdirectory in the fake seedbox.

## Verify before reporting — actually run these

1. `uv run pytest` passes, including the WebSocket delta-size test and the scan-skip test.
2. `npm run build` clean, no TypeScript errors; `npm run lint` clean.
3. **End to end against the fake seedbox** (`docker-compose.test.yml`; password auth is
   `seeduser` / `testpass123` on port 2222): queue from the Files page, watch progress on
   Transfers, open the drawer, stop, confirm `STOPPED`. Report what you observed.
4. **Measure the WebSocket payload** during a transfer and report actual bytes per tick — the
   number is the point, not the assertion that it improved.
5. `docker compose config --quiet` clean on all three files.
6. **Tear down everything**; confirm with `docker ps -a`. Ports 8087/5187/2222/2223 free.

If you cannot verify something (a real browser click-through, for instance), **say so plainly**
rather than implying it works. An admitted gap is worth more than an unverified claim.

## Surfacing design decisions

Report prominently anything where `DESIGN.md` is wrong, ambiguous, or silent on a hard-to-reverse
choice. Make the smallest reasonable call and keep moving. **Do not edit `DESIGN.md`** — it gets
corrected deliberately, in conversation with the user. All three previous phases found real doc
errors this way.

## Conventions to honor

- Build into the existing frontend structure (layout / router / api client / hooks / pages);
  don't restructure it.
- Type-annotated, no `any`. Keep components small enough to test.
- No secrets in logs or tracked files.

## When done

1. Record decisions in `docs/decisions.md` (newest at top).
2. Update **`prompts/startnewsession.md`** — "Where we are", the phase table, traps if new ones
   appeared. Phase 3 is the last one being built for now; say so, and note that phases 4–9
   remain.
3. Update this file's frontmatter: `status`, `completed`, `result`.
4. `git mv` to `prompts/done/` (success) or `prompts/failed/` (failure).
5. **You are a spawned agent: do NOT commit.** Prepare the tree and report the file list plus a
   proposed one-line commit message (`feat:` prefix, no `Co-authored-by:`; branch `dev`).
   Never `git add -A`, never push.
