---
name: 2026-08-29-preflight-poll-freshness
status: completed        # pending | completed | failed
created: 2026-08-29
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-29
result: Widened the fast-tick `_full_estate` merge (phase filter, plus made it unconditional on
  `cheap_history` -- the real reason rTorrent's SEEDING verdict was still stranded) and added
  `ACTIVE_POLL_INTERVAL_S` (4.0s) so an instance with Preflight work in flight polls faster while
  a quiet one stays at `FAST_INTERVAL_S`; backoff still wins regardless. 5 new tests, all green;
  gates clean.
---

# Task: make a client's "finished" fact reach lftpweb in seconds, not minutes

Two defects keep Preflight and the settle-gate skip working from a stale picture. The user's
words: *"when things are in preflight we should update from SAB or rtorrent more often."*

## Before you start

Read, in this order:

1. `core/clientsync.py`'s **module docstring** — the two-cadence design (`FAST_INTERVAL_S` = 10s,
   active-only; `SLOW_INTERVAL_S` = 300s, full estate), why it is split cheap-vs-expensive rather
   than active-vs-inactive, and the backoff ladder.
2. `core/clientsync.py` lines ~505-570 — `run_once`'s per-instance body, `slow_due`,
   `cheap_history`, `_update_preflight`, and the `_full_estate` merge you are fixing.
3. `core/settle.py.FINISHED_TRANSFER_PHASES` and `core/clientsync.py.finished_transfers()`.
4. `CLAUDE.md` — commit rules; gates in the **foreground**, from the repo root.

## Defect 1 — the fast-tick merge never learned about SEEDING

`core/clientsync.py` ~line 566:

```python
if transfer.phase in (TransferPhase.COMPLETED, TransferPhase.FAILED):
    cache[transfer.client_id] = transfer
```

Yesterday (commit `cc5f75d`) `settle.FINISHED_TRANSFER_PHASES` was widened to
`{COMPLETED, SEEDING}` precisely because `core/clients/rtorrent.py._classify_token` maps a
finished, actively-seeding torrent to `SEEDING` and never to `COMPLETED`. **The reader was
widened and this writer was not.** So for the ordinary rTorrent case the "finished" fact still
waits out `SLOW_INTERVAL_S` (300s) before `finished_transfers()` can see it — and the 5s verified
settle skip that same commit shipped sits idle that whole time.

Fix the filter so it admits exactly the phases the consumers actually read —
`settle.FINISHED_TRANSFER_PHASES` for `finished_transfers()`, plus `TransferPhase.FAILED` for
`failed_transfers()`. Derive it from those constants rather than restating the set by hand; a
third copy of this set is how the bug happened in the first place. **Check for a circular import
before reaching for `from . import settle`** — if `core/settle.py` already imports from
`core/clientsync.py`, put the shared constant where both can reach it rather than creating a
cycle, and say in your report which way you resolved it.

Add a regression test that fails against the current filter: a SEEDING transfer observed on a
**fast** tick (not a slow one) must be visible to `finished_transfers()` immediately. Write the
test, watch it fail, then fix it — and say so in your report.

## Defect 2 — no faster cadence while work is actually in flight

The user asked for a shorter interval while items are in Preflight. Add one.

- A new module-level constant (the user suggested ~3-5s; pick one and justify it in its own
  docstring, in this module's established style — say what it costs as well as what it buys).
- It applies **only to the fast, active-only call**. The slow full-estate call keeps
  `SLOW_INTERVAL_S`; do not make the expensive call more expensive. Say this in the docstring so
  a later reader cannot mistake the intent.
- The trigger is "this instance currently has something in flight." `_update_preflight` already
  computes exactly that per instance every pass — reuse what it knows rather than adding a second,
  independently-drifting notion of "busy." When nothing is in flight, fall back to
  `FAST_INTERVAL_S` unchanged.
- **The backoff ladder wins.** An instance that is failing and backing off must not be dragged
  back to a 3s poll because it happens to have Preflight rows cached from before it broke. Test
  this explicitly — it is the one interaction most likely to go wrong.
- Not a settings knob, matching `FAST_INTERVAL_S`/`SLOW_INTERVAL_S`'s own stated reasoning.

## Tests

Extend `tests/test_clientsync.py`:

- The SEEDING-on-a-fast-tick regression above.
- With something in flight, the next tick is due after the new short interval, not `FAST_INTERVAL_S`.
- With nothing in flight, cadence is unchanged at `FAST_INTERVAL_S`.
- An instance in backoff stays in backoff regardless of Preflight state.
- The slow cadence is untouched by any of this.

Drive the clock through the existing overridable `now` parameter (`run_once`'s own docstring
covers it). Do not write a test that sleeps real wall-clock seconds.

## Conventions to honor

- Match the surrounding docstring style — this module explains *why* at length, including which
  earlier decision a line reverses and what caused the reversal. Defect 1 is a miss in a commit
  from the previous day; write that down plainly rather than making it look intentional.
- Doc updates ship in the same change set: `docs/download-client-framework-spec.md` §9.1 owns the
  cadence design and must describe the new interval; `DESIGN.md` §17 if it states the cadence;
  `docs/decisions.md`, newest at top, for the new interval and for deriving the merge filter from
  the consumers' own constants.
- Gates, each its own **foreground** command from the repo root, reading each exit code:
  `uv run pytest` (~2.5 min, generous timeout), `uv run ruff check .`, `uv run ruff format
  --check .`. If you touch no frontend file, no npm gates are needed — say so rather than
  skipping silently.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record non-obvious decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the working tree, then report back to the orchestrating session:
   the file list, a one-line commit message, and the final test counts. The orchestrating session
   surfaces the `y/n` to the user. Never `git add -A`, never push.
