---
name: 2026-08-19-queue-position-order-model
status: completed        # pending | completed | failed
created: 2026-08-19
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-08-19
result: >
  Replaced rank/queued_at ordering with a dense `queue_position` model (migration 023). The
  v0.2.6 startup rescue was re-derived via `_rescue_position` (natural-zone-only neighbour
  search, excluding boosted jobs) and proven by a written-first regression test, including a
  counterexample where a naive full-queue comparison would have let a rescued job outrank an
  explicit Move to top. Migration backfill preserves the exact pre-migration order (relaxed
  mid-task to a nice-to-have, kept because it was cheap: a single `ROW_NUMBER()` `UPDATE`).
  1409/1409 backend tests pass (was 1399), 0 skipped; ruff check and ruff format --check both
  clean. `rank` kept in schema, unread for ordering, still written by move_to_top and read only
  by `_rescue_position` as a boosted/natural discriminator.
---

# Task: replace the queue's boost-based ordering with a dense position model

**Phase 1, stage 1 of `docs/transfers-redesign-spec.md` — read §3.4 of that spec first.** This is
the prerequisite for per-row "move up one / down one" reordering. It ships **no UI**: this task
changes the ordering model and nothing a user can see except that the queue order is unchanged.

The whole point is that the queue order must come out **identical** to today for every existing
scenario. This is a refactor with a migration, not a behavior change.

## Why today's model can't support "move up one"

Ordering is `rank DESC, queued_at ASC`. `rank` defaults to `0`; "Move to top"
(`core/queue.py`, ~line 850) sets `rank = MAX(rank) + 1` over queued jobs. So the queue is two
zones: a boosted zone (rank > 0, most-recently-boosted first) and the natural zone (rank 0,
oldest `queued_at` first).

"Move to top" fits that. "Move up one" cannot:

1. Two adjacent rank-0 jobs can only be swapped by swapping `queued_at` — which corrupts the
   queued-wait readout. `core/queue.py` (~line 508) backdates `queued_at` *specifically* so that
   readout stays truthful; the v0.2.6 rescue depends on it.
2. At the zone boundary "up one" isn't up one — promoting a rank-0 job means rank ≥ 1, vaulting
   it above the entire backlog at once.
3. Inside the boosted zone, rank encodes *how recently you boosted*, not *where it sits*.

## Before you start

- **`docs/transfers-redesign-spec.md` §3.4 and §3.5** — the design and its reasoning.
- `backend/lftpweb/core/scheduler.py` — the pure `admit()` function. Note `QueuedJob` carries
  `rank`/`queued_at` today, and that `queue_id` appears **zero** times in this module: admission
  is global and queue-agnostic. Keep it that way.
- `backend/lftpweb/core/queue.py` — the admission query (~line 1819), `enqueue_item`, the
  move-to-top implementation (~line 850), `_insert_job` (~line 2199), and the two docstrings at
  ~508 and ~735 that explain why `rank` is deliberately left alone by the backdating paths.
- `backend/lftpweb/migrations/` — `022_start_now_rate_fraction.sql` is the most recent; yours is
  **`023_`**. Read 022 first: it documents that SQLite cannot retype a column, and the
  parallel-column pattern this project uses as a result.
- `DESIGN.md` §4.5 — the ordering is specified there and must be updated to match.

## Working tree check

Run `git status --porcelain` first and cross-reference. If any file this plan touches is dirty,
list it and ask before proceeding. This prompt file is exempt. Baseline: branch `dev`, clean tree,
**1399 backend tests passing, 0 skipped**, in sync with `origin/dev`.

## What to do

### 1. The column and the migration (`023_queue_position.sql`)

Add `job.queue_position REAL`. Fractional, not integer — a move between two neighbours takes the
midpoint, so a reorder is one `UPDATE` with no renumbering of anything else.

**Backfill is the load-bearing part of this migration.** Every currently-queued job must come out
of the migration in *exactly* the order it would have run under `rank DESC, queued_at ASC`.
Assign ascending positions in that order (1.0, 2.0, 3.0, …). A user with a deep backlog must not
have their queue reshuffled by an upgrade.

**Leave the `rank` column in place.** Do not drop it. Stop *reading* it, mark it vestigial in the
migration comment and in `models.py`/schema docs, and leave it for a later cleanup once the new
model has run in production. Dropping a column that the admission path used until this release is
not a risk worth taking in the same change that introduces the replacement.

### 2. Ordering

New order is `queue_position ASC` (with `id ASC` as a stable final tiebreak). Update:

- the admission query in `core/queue.py` (~1819)
- `core/scheduler.py`'s `QueuedJob` — replace `rank` with `queue_position`
- any other `ORDER BY job.rank DESC, job.queued_at ASC` (there is at least one more, ~2339)

`queued_at` stays exactly as it is and keeps its current meaning — it is the queued-wait readout's
source and is no longer an ordering input. **Do not change any backdating behavior.**

### 3. Insert path

`_insert_job` / `enqueue_item` assign `queue_position = MAX(queue_position) + 1` over currently
queued jobs (or `1.0` when none). New work lands at the back, which is what `queued_at ASC` did.

### 4. Move to top, reimplemented

`MIN(queue_position) - 1` over queued jobs. Behaviorally identical to today from the user's point
of view. Its existing API endpoint and tests should keep passing with minimal change.

### 5. Preserve the v0.2.6 rescue ordering — this is the acceptance criterion

The startup rescue re-queues an interrupted item **carrying its original `queued_at` forward**, so
it returns to its old place in line rather than the back, and it deliberately leaves `rank`
untouched so it can never outrank an explicit Move-to-top. See `core/queue.py` ~735 and
`prompts/done/2026-08-19-*` for the original work. **The user verified this behavior on a real
production restart on 2026-08-19 — it must not regress.**

Under a position model, "carry `queued_at` forward" no longer places anything. You must
re-derive the behavior: the re-queued job needs a `queue_position` that puts it back where its
original `queued_at` would have placed it — i.e. between the neighbours it sat between — while
still never landing ahead of a moved-to-top job.

**Write the test for this first, before implementing it.** It is the one thing in this task that
can silently break a feature that is already in production.

### 6. Fast lane

Positions are **global across both lanes** (spec §3.5) — the lanes admit from independent pools,
but the ordering key is one sequence. Do not partition positions per lane. `forced_rate_fraction`
("Start now") bypasses ordering entirely and is unaffected; confirm that still holds.

### 7. Tests

- Backfill: a fixture with a realistic mix (several rank-0 jobs of differing `queued_at`, plus two
  or three moved-to-top jobs with ascending ranks) comes out of migration 023 in the identical
  order the old `ORDER BY` produced. Assert the full sequence, not a spot check.
- Insert lands at the back.
- Move-to-top lands at the front and beats everything including a previous move-to-top.
- The rescue-ordering test from §5.
- A midpoint insertion between two adjacent positions orders correctly (this is what stage 2's
  chevrons will use — prove the primitive now even though no caller exists yet).
- Every existing ordering/admission test must still pass. If one needs changing, that is a signal
  worth reporting, not a formality — say which and why.

### 8. Docs

`DESIGN.md` §4.5 (the ordering spec), `CHANGELOG.md` under `[Unreleased]`, and `docs/decisions.md`
(newest at top) recording the model change, the fractional-key choice, and the decision to leave
`rank` in place rather than drop it.

Also tick stage 1 off in `docs/transfers-redesign-spec.md` §7's phase-1 table.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — `testpaths` lives in the root `pyproject.toml` and `tests/` is a
  sibling of `backend/`; running from `backend/` collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`. Three separate gates. Do not
  background the test run; a subagent never receives the completion notification and will stall.
- Report backend test counts before and after, and confirm 0 skipped.
- Conventional-Commit prefix (`refactor:` or `feat:` — your call, justify it). No
  `Co-authored-by:` trailer.
- **Surface, don't silently resolve.** If `DESIGN.md` §4.5 turns out to be wrong or
  underspecified, say so rather than quietly diverging.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`).
3. Record the decisions in `docs/decisions.md`.
4. **You are a spawned agent: do NOT commit.** Prepare the tree, then report the file list, how
   you re-derived the rescue ordering, and a proposed one-line commit message back to the
   orchestrating session. Never `git add -A`, never push.
