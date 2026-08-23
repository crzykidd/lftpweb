---
name: 2026-08-23-settle-gate-skip
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Shipped `settle.SettleSettings.client_skip_enabled` (site-wide, default off) plus
  `settle.find_client_completion`/`_client_content_path_matches` (pure, component-boundary
  path matching), `ClientSyncScheduler.completed_transfers()` (reads the stage-2a
  `_full_estate` cache, restricted to currently-enabled instances), and the skip itself wired
  into `AutoQueue.on_scan`, auditing as `settle_client_skip`. `QueueAutoConfig.remote_path`
  and `AutoQueue.client_sync` (plain-attribute wiring, main.py) added to make the match
  possible. Fixed a latent PUT-merge bug in `/api/settings/settle` while adding the second
  field. Frontend: a second checkbox in Settings -> Transfer's settle-gate section. Gates:
  pytest 1910 passed, ruff check clean, ruff format clean, frontend build/lint/test all
  green. Correction recorded in spec §9.1: the skip reads the slow/full-estate cadence, not
  the fast one the table names. See docs/decisions.md (2026-08-23) for full reasoning.
---

# Task: Stage 2b — a client's completion verdict can satisfy the settle gate

`docs/download-client-framework-spec.md` §4.3: the settle gate waits for a remote directory to
stop growing. **A download client reporting the release finished *is* that same fact**, reported
by the process that did the writing — an inference replaced by a direct observation.

This is the first task in the whole feature that changes when a transfer starts. Treat it
accordingly.

## Before you start

1. **`docs/download-client-framework-spec.md`** — §4.1 (**advisory only**), §4.2 (**absent is not
   a verdict** — the rule this task is most able to violate), §4.3 (what the verdicts buy), §7.2
   (never predict a path from a name), §9.2 (precedence), **§13.4 — especially guess #2**.
2. **`core/settle.py`** — the gate itself: what it waits for, how `matched_scans` accumulates,
   what "settled" currently means.
3. **`core/autoqueue.py`** — the caller, and where a settled item becomes a queued transfer.
4. **`core/clientsync.py`** — the poller and its cache, built in stage 2a. Its cache is what this
   task consults; do not add a second polling path.

## The thing that makes this risky, stated plainly

**The skip depends directly on an unverified guess.** §13.4 #2 — SABnzbd history
`Completed`→`COMPLETED`, `Failed`→`FAILED` — is doc-derived, ranked **High**, and has never been
seen against a live instance. rTorrent's phase derivation (§13.6) is no better.

If that mapping is wrong, this feature transfers a half-written directory. The settle gate exists
precisely to prevent that.

So:

- **Ship it OFF by default**, as a setting. This project's rule is that every new capability ships
  off (`arr_instance.enabled`, `download_client.enabled`, and every post-processing toggle follow
  it). A behaviour change gated on an unverified vocabulary is the strongest case yet for it.
- **The fallback must be the current behaviour, exactly.** No verdict, an unknown phase, an
  unreachable client, a disabled setting, a client that never heard of this release — every one of
  those means *run the settle gate as it runs today*. There is no path through this code where a
  missing answer produces a faster decision.
- **Only a terminal, explicit completion skips the gate.** A queue-side status must never satisfy
  it — only a history/terminal `COMPLETED`. `TransferPhase.UNKNOWN` never satisfies anything.
- **Matching must be exact.** Bind on the client's reported `content_path` (spec §7.2: SAB renames
  on unpack, and a predicted path is a guess that will sometimes be wrong). Path equality or a
  component-boundary containment check against the item's own remote path — **never** a name
  heuristic, never a prefix match that can straddle a directory-name boundary.

## What to build

1. **A setting, default off.** Decide site-wide vs per-queue and justify it — note that
   `docs/transfers-redesign-spec.md` §4.5 makes a client instance site-level, while the settle
   gate is a per-queue concern. Follow the existing settings patterns rather than inventing one.

2. **The skip itself.** Where `core/settle.py` would otherwise require more scans, a positive
   terminal verdict from `core/clientsync.py`'s cache satisfies the gate immediately.

3. **An audit event every time the gate is skipped**, naming the client instance and the verdict
   that permitted it. This is not optional decoration: when this feature eventually transfers
   something half-written, that event is the only way anyone will work out why. Follow the
   existing event `kind` conventions (`snake_case`, see `core/events.py` and its neighbours).

4. **A withheld case is NOT in scope.** Stage 3 handles explicit failure verdicts blocking a
   transfer. This task only ever makes a wait *shorter*, never makes a transfer *not happen*.

## Tests

- The setting off → byte-identical behaviour to today, asserted directly.
- A terminal `COMPLETED` verdict with an exact path match → gate satisfied, event written.
- **A queue-side (non-terminal) status → gate NOT satisfied.**
- `UNKNOWN` phase → gate not satisfied.
- Unreachable client / empty cache / blank queue response → gate not satisfied, current behaviour
  preserved. Use `tests/fake_sabnzbd.py`'s blank-queue mode — the v0.2.4 production incident.
- A near-miss path (`/complete/ar-tv/Show.S01` vs `/complete/ar-tv/Show.S01.EXTRA`) does **not**
  match. Assert the component-boundary rule directly; this is the test that catches a prefix bug.
- An item with no client involvement at all is untouched.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. Five agents on this feature have stalled on exactly this. **Run backend gates from the
REPO ROOT**; if you `cd`, `cd` back.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. Frontend if touched: `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`.
Update the spec's §14 staging table to reflect that 2b has landed.
**Do not commit or push.** Report: files, every exit code, test counts, a proposed one-line `feat:`
message, where you put the setting and why, and anything in the spec found wrong or
underspecified.
