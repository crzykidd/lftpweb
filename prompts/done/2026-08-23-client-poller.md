---
name: 2026-08-23-client-poller
status: done
created: 2026-08-23
model: sonnet
completed: 2026-08-23
result: >
  Built core/clientsync.py (the poller, two cadences collapsed into one call per tick, capped
  backoff, one event per failure transition, ClientAuthenticationFailed gets its own audit kind
  on the same ladder), widened PreflightRow with an optional download_id (spec section 9.2's
  merge key), added a one-line additive download_id population to core/arrsync.py's existing
  PreflightRow(...) call, and extended api/jobs.py's _merge_preflight_rows to a three-way
  arr/client/settle merge with per-field client precedence. Wired into main.py/health.py.
  25 new tests (13 poller, 12 merge), full suite 1892 passed, ruff check and ruff format --check
  both clean.
---

# Task: Stage 2a — the download-client poller and its Preflight source

Make the connectors do something. Build `core/clientsync.py` — poll every enabled client instance,
cache what it reports, and feed the Queue tab's **Preflight** box as a third source.

**This is stage 2a. The settle-gate skip is stage 2b and is NOT in scope here.** This task changes
no transfer behaviour at all: it observes, caches, and displays. That separation is deliberate —
2a is safe to run against a live seedbox while its mappings are still unverified; 2b is not.

## Before you start

1. **`docs/download-client-framework-spec.md`** — §4.2 (absent is not a verdict), §9 (**the polling
   shape**), §9.1 (**two cadences**), §9.2 (**freshness and source precedence — the hardest part of
   this task**), §8.3 (category → queue attribution), §7.1/§7.2 (identity), §13.4 and §13.6 (the
   correction lists: everything these connectors report is still unverified).
2. **`core/arrsync.py`** — the shape to mirror: poll an external service, short timeout, capped
   exponential backoff per instance (60 s → 30 min), one warning plus one audit event on failure,
   keep last-known state, never let silence become a verdict.
   **DO NOT REFACTOR `arrsync.py`.** Spec §9 is explicit: it is battle-tested against real
   production incidents. Build alongside it; extracting the shared shape comes later, once both
   exist and the seams are obvious.
3. **`core/preflight.py`** — read its whole docstring. `PreflightRow` is **source-agnostic by
   construction** and has survived five tasks that way. `PreflightSource` is *widened*, never
   replaced. `PreflightHold` gives flap tolerance; decide deliberately whether this source needs it
   (the *arr source does, because a download client's queue can blank out for a beat — which is
   *literally this source*, so think carefully).
4. **`core/arrsync.py`'s `_preflight_candidates`** and `ArrSyncScheduler.preflight_rows` — how an
   existing source builds rows and handles request-time retirement.
5. **`api/settings_clients.py`** — how instances and their category→queue mappings are stored.

## The rule this task must not break

**A connector is handed no database handle (spec §4.1), but the poller has one.** The structural
guarantee that protected the connector layer does not protect this module.

> **The poller may never write `item.state`.** Not directly, not via a helper, not "just for
> this one case." It caches what clients report and projects it for display. Every decision that
> changes lftpweb's own state machine stays where it already lives.

If something in this task seems to want to write `item.state`, that is the signal the design has
gone wrong — stop and report it rather than doing it.

## What to build

### 1. `core/clientsync.py`

- One poll pass per **enabled** instance. A disabled instance is never contacted.
- **Two cadences (§9.1):** a fast pass (~10 s) calling `list_transfers(active_only=True)`, and a
  slow pass (minutes) for the full estate. Listing hundreds of seeding torrents every 10 s is
  waste; learning 60 s late that a download finished defeats the point.
- **Per-instance capped exponential backoff**, 10 s timeout, one warning + one audit event per
  failure transition — not per failed pass, or a dead client floods the event log.
- **Keep last-known state on failure.** An unreachable client never downgrades anything.
- **`ClientAuthenticationFailed` deserves its own handling.** A wrong credential is not a transient
  network problem: it will not fix itself by retrying, and it should be visibly distinguishable in
  the events from "host down." Decide the backoff treatment deliberately and document it.
- The cache is a **cache**: everything in it is re-fetchable, nothing else reads it for
  correctness, truncating it is always safe (spec §4.6's framing). Prefer in-memory, as
  `core/preflight.py` does — justify persistence if you choose it.

### 2. The Preflight source

Build `PreflightRow`s for what the clients are working on, attributing each to a queue through the
configured **category → queue** mapping (spec §8.3). A row that cannot be attributed is **silently
omitted** — `core/preflight.py`'s existing rule, and promising a release that never arrives is
worse than showing nothing.

Set a new `PreflightSource` value; **widen the Literal, do not replace it.** Populate
`source_label`/`source_kind` so the row can carry the client's identity, and `size_bytes`/
`size_remaining_bytes`/`remaining_s` where the connector actually reports them — **never a
fabricated or zero figure** for a connector that does not.

### 3. §9.2 — freshness and source precedence (the hard part)

A release grabbed by SAB will be reported by **both** the *arr Preflight source and this one, at
once, with different lag and different wording. Implement §9.2:

- **The direct client observation wins for fields both report — always, not "if newer."** This is
  deliberately not a timestamp comparison: the *arr never tells us when it last polled its own
  client, so a fetch-time rule would prefer a just-fetched relay of a two-minute-old fact over a
  ten-second-old direct reading.
- **Per-field, never per-record.** A client that reports no ETA must not blank the *arr's
  `timeleft`.
- **Only where the client actually reported.** §4.2 outranks this: silence from the client leaves
  the *arr's row standing, unchallenged. A blank SAB queue must never read as "not downloading."
- **An unreachable client keeps its last-known reading** rather than falling back to the *arr's.

Dedupe on `downloadId` — it **is** the client's own key (spec §7.1), so this needs no heuristics.
**Two sources holding the same identity must not produce two rows.**

**Write a test that a stale *arr field cannot overwrite a fresher client field.** That failure is
otherwise silent and presents as flicker.

### 4. Wire-up

Start the scheduler in `main.py` alongside the existing ones; surface health in `/api/health` the
way the *arr poller does. Extend the Preflight endpoint to include this source.

## Tests

The backoff ladder and its reset; one event per failure *transition*; last-known survival across a
failed pass; an auth failure distinguished from unreachability; unattributable rows omitted; the
two cadences firing independently; §9.2's precedence including the per-field and
silence-outranks-precedence cases; dedupe by download id producing exactly one row.

Use `tests/fake_sabnzbd.py` — including its **blank-queue** mode, which models the real v0.2.4
production incident. A blank queue must produce no verdict, not an empty-means-gone reading.

## Verification gates — read `CLAUDE.md`

**NEVER background a gate** — every gate Bash call MUST pass an explicit timeout of at least
600000 ms. **Five** agents on this feature have now stalled on exactly this. **Run backend gates
from the REPO ROOT**; if you `cd`, `cd` back first.

1. `uv run pytest` · 2. `uv run ruff check .` · 3. `uv run ruff format --check .`
4. Frontend, if you touch it: `npm run build`, `npm run lint`, `npm test`

## When done

Update frontmatter, `git mv` to `prompts/done/`, record decisions in `docs/decisions.md`.
**Do not commit or push.** Report: files, every exit code, test counts, a proposed one-line `feat:`
message, how you handled `ClientAuthenticationFailed`'s backoff and why, whether you used
`PreflightHold` and why, and anything in the spec found wrong or underspecified.
