---
name: 2026-08-12-per-queue-scan-interval
status: pending
created: 2026-08-12
model: sonnet
completed:
result:
---

# Task: Make the scan interval per-queue and settable from the UI

`scan_interval_s` is a single global (30s, `config.py:33`, env-overridable). The user asked
for a 10 / 30 / 60 / none choice. This has been an open request since phase 2, which
collapsed `DESIGN.md` §5's separate 30s remote / 10s local cadences into one global.

## Before you start

- Read `DESIGN.md` §5 (scanning) and §9.3 (settings).
- Read `core/engine.py` — the scan loop, `request_rescan()`, and
  `asyncio.wait_for(self._wake.wait(), timeout=self.scan_interval_s)` around line 242.
- Read `api/settings.py` and the Settings → Queues frontend tab for the existing
  per-queue-field conventions (phase 4 added two toggles there; follow that shape).
- Read `prompts/open-issues.md` § "11 — per-queue scan interval".

## Working tree check

`git status --porcelain`. Other agents have been working across `core/`, `api/`, and the
frontend. If files you need are dirty, list them and ask.

## Use migration number 009

`005` metrics, `006` `state_changed_at`, `007` settle gate, `008` deletion/retention. Use
**`009`**.

## Context the user does not have, and should not be given wrongly

They asked for a **refresh dropdown on the Files page**. That would be the wrong build:
the Files page does not poll — it renders off one WebSocket. Measured cadences:

| Change | Cadence |
|---|---|
| DOWNLOADING progress | ~1s (`transfer_tick_s`, `config.py:42`) |
| Lifecycle transitions | immediate, pushed on transition |
| Anything the scanner discovers | up to 30s (`scan_interval_s`, `config.py:33`) |

A client-side interval would re-read the same persisted data. The real knob is server-side
scan cadence. `POST /api/files/rescan` and a "Rescan now" button already exist, and a
`scan_complete` WebSocket message plus a "scanned Xs ago" readout landed in `cd74f91`.

**So: build the interval as a per-queue setting, not a client-side timer.**

## What to do

1. **Migration 009** — a per-queue interval column on `path_queue`. Nullable, where NULL
   means "use the global default", so every existing row keeps behaving exactly as it does
   today. That is this project's rule: a new capability changes nothing for an existing
   install until someone opts in.
2. **A per-queue next-due in the engine loop.** Today one global timeout drives every
   queue. The loop must wake at the earliest next-due across queues and scan only what is
   actually due — not scan everything on the shortest queue's cadence, which is the easy
   wrong implementation.
3. **"None" = on-demand only.** The queue is never scanned on a timer; `request_rescan()`
   and the Rescan button still work. Make sure a "none" queue does not silently stop
   auto-queue in a way the user cannot see — if auto-queue depends on scan passes (it runs
   at the end of each one), say so in the UI next to the option.
4. **Confirm an overrunning scan cannot stack.** At a 10s interval against a slow shared
   seedbox, a scan can take longer than its own interval. Verify what happens today and
   make it correct: a pass still running when the next is due must not start a second
   concurrent scan of the same queue. **Test this explicitly** — it is the failure mode a
   10s option introduces.
5. **UI** — Settings → Queues, following the existing field conventions. Put a plain
   warning next to 10s: a scan is an SSH round trip running `find` over the entire remote
   tree, and on a shared seedbox that is real load.

## Conventions to honor

- `docs/decisions.md`, newest at top: the NULL-means-global choice, and how you made the
  loop multi-cadence.
- `CHANGELOG.md` under `## [Unreleased]` → `### Added`.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build`.
- `uv run pytest` with the fake seedbox up; tear down afterward, confirm `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Report back: file list, proposed one-line `feat:` message, test
   count, lint results, what you found about overrunning scans, and anything not fixed.
   Never `git add -A`, never push.
