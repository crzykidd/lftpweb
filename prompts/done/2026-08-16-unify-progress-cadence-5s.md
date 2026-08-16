---
name: 2026-08-16-unify-progress-cadence-5s
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: "core/queue.py.PROGRESS_SAMPLE_TICKS = 5 (replacing CHILD_PROGRESS_THROTTLE_TICKS = 3)
  gates job-level ProgressSampler.sample, the per-tick item_delta publish, and
  _publish_child_progress on one shared counter in _sample_and_publish_progress; the 1s
  tick() loop itself (reap/admit/stop) is untouched. DESIGN.md §4.4 corrected;
  docs/decisions.md, CHANGELOG.md Unreleased/Changed, startnewsession.md row K updated.
  tests/test_queue_child_progress.py rewritten for the unified gate plus 3 new tests (job+child
  same tick, 5s elapsed-time speed math, reap/stop latency unaffected). All gates green: ruff
  check/format clean, 1162 backend tests passed (0 skipped), frontend lint/343
  tests/build all green."
---

# Task: one 5-second progress cadence for job and per-file speeds

User decision (2026-08-16, from watching a live transfer): job-level speed (sampled ~1 Hz)
and per-file speed (sampled every 3rd tick) run on different schedules with independent
EMA lags, so a one-file directory shows two speeds that never agree (46 vs 40 MB/s live).
Unify BOTH onto a single **every-5th-tick (~5 s)** sampling schedule — same instants, same
effective smoothing, and a longer delta window that averages the underlying rate better.

## The shape of the change

1. **The tick loop stays at 1 s** (`transfer_tick_s`). It also drives admission, reaping,
   and stop handling — a Stop click must keep taking effect in ~1 s. Only the *progress*
   work moves to every Nth tick.
2. One constant (e.g. `PROGRESS_SAMPLE_TICKS = 5` in `core/queue.py`), replacing
   `CHILD_PROGRESS_THROTTLE_TICKS = 3` — job-level sampling (`ProgressSampler.sample` via
   `_progress_tick`), the per-tick `item_delta` progress publish, and
   `_publish_child_progress` all gate on the same counter, so job and child deltas are
   measured over the identical interval. Not a new setting — a code constant, like the
   old throttle.
3. EMA math unchanged (`DEFAULT_EMA_ALPHA` stays 0.3); the elapsed-time-based rate
   derivations (`child_speed_bps`'s real-elapsed measurement) already handle a variable
   interval — verify job-level sampling does too rather than assuming a 1 s step
   anywhere (grep for any `tick_s`-assuming rate math; the docstrings already warn about
   `tick_s * N` assumptions).
4. Keep first-sample behavior sane: a fresh job's speed reads 0 until its second sample
   (~5–10 s in) — that's acceptable and existing behavior at a longer delay; don't
   invent a fast-start special case.
5. The child-progress write-pressure cap (`MAX_CHILD_PROGRESS_UPDATES_PER_TICK`) and the
   "persist only changed children" behavior stay exactly as they are — this change slows
   writes further, never speeds them up.

## Docs, same commit

- **`DESIGN.md` §4.4 asserts ~1 Hz sampling — correct it** to describe the 1 s tick loop
  vs the 5 s progress-sampling cadence split, briefly and in place. This is a sanctioned
  design change (user decision), not a silent divergence.
- `CHANGELOG.md` Unreleased (Changed); `docs/decisions.md` entry (why 5 s, why the loop
  stays 1 s, the two-EMA-lags mechanism that motivated it); startnewsession.md arr
  build-run table row.

## Tests

- Adjust existing sampler/tick tests that assume per-tick sampling; add: job and child
  samples occur on the same ticks; a job's speed after simulated ticks matches the
  delta/elapsed math at the 5 s interval; stop/reap latency unaffected (a stop mid-window
  is handled on a non-sampling tick).

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- `feat:` or `fix:`? Use **`feat:`** (user-visible behavior change by request).
- No new dependencies, no migration.

## Verification gates — run each separately and read its exit code

1. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
2. `uv run pytest` — note skip counts honestly.
3. `cd frontend && npm run lint && npm test && npm run build` (likely untouched; prove it).

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   commit message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
