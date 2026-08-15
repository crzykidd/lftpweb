---
name: 2026-08-14-per-file-speed-inside-a-mirror
status: done
created: 2026-08-14
model: sonnet
completed: 2026-08-14
result: >
  Added a third, item-keyed WS message (`child_progress`) carrying a real, EMA-smoothed
  per-child rate computed from a real elapsed timestamp (never tick_s * throttle ticks),
  reusing `core/progress.py`'s EMA formula via a newly extracted `ema_step`. Gating chosen:
  freshness on the frontend (a sample newer than CHILD_SPEED_FRESHNESS_MS = 10s), not a
  state check or job-liveness lookup -- closed by construction because the backend already
  stops emitting a sample for a child that stops changing. All verification green: 992
  backend tests (incl. 35 new), 200 frontend tests (incl. new gating/stale-sample cases),
  ruff check + format clean, npm lint/build clean, all three compose files valid. See
  docs/decisions.md (2026-08-14 entry) for the full reasoning and rejected alternatives.
---

# Task: Show a live transfer rate on each file inside a mirroring directory, not just the parent row

The Files page's Speed column (2026-08-14, `f728373`) shows a rate only on the **top-level**
directory row of a `mirror` job. Its children — the individual files actually being transferred —
show nothing, because the backend never publishes a per-child rate. User request: show it per
file.

## The data already exists; only the rate does not

`core/queue.py._publish_child_progress` already:

- receives `JobProgress.children` — every child file's effective on-disk size, from
  `core/progress.py`'s existing walk (`.lftp`-suffix stripped by `local_scan.scan_local`), so
  **no new I/O is needed**
- diffs it against `self._prev_child_sizes` (the previous run of *this* method, not every
  `tick_s`) and acts only on files whose size changed
- is bounded by `MAX_CHILD_PROGRESS_UPDATES_PER_TICK` and by
  `mirror_parallel_transfer_count` — never by release size

So the byte delta per child is already computed and then discarded. What is missing is the
**elapsed time** between those two measurements and a place to put the resulting rate on the wire.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §4.4 and §9.2.
- Read `core/queue.py._sample_and_publish_progress` and `_publish_child_progress` **in full**,
  including their docstrings — they explain the throttling and the persist→read-back→publish
  invariant, both of which constrain this task.
- Read `core/progress.py` (the EMA smoothing the parent rate already uses) and
  `frontend/src/hooks/useLiveModel.ts` (the `speedByItemId` map, added by `f728373`).
- Read `frontend/src/components/FileTree.tsx`'s Speed cell and its sort handling.

## Working tree check

Run `git status --porcelain` first. If a file this plan needs is dirty, list it and ask. This
prompt file is exempt.

## What to do

### 1. Compute a per-child rate from the existing diff

Alongside `_prev_child_sizes`, record **when** each measurement was taken, and derive
`bytes_delta / seconds_elapsed` per child. Use a real timestamp — do **not** assume the interval
equals `tick_s * CHILD_PROGRESS_THROTTLE_TICKS`, because a slow pass makes that assumption
silently wrong and this project has already been burned once by a rate computed from a wrong
denominator (`bytes_done` vs `bytes_total`, `6e6b217`).

**Smooth it the same way the parent rate is smoothed.** `core/progress.py` EMA-smooths job speed
specifically because a raw per-tick delta on a file being written in bursts reads as violent
flicker. A per-child raw delta will be worse, not better, since a single file's writes are
burstier than a whole job's aggregate. Reuse that approach rather than inventing a second
smoothing scheme; if you genuinely cannot reuse it, say why in `docs/decisions.md`.

Prune a child's stored measurement when its job leaves `self._running`, so a finished transfer
does not leave a rate behind (see item 3 — this is the same staleness trap the Files column
already had to work around).

### 2. Get it onto the wire without corrupting either existing message

Two existing messages, and this belongs in **neither** as-is:

- **`progress`** is job-centric — one entry per running job, consumed by the Transfers page via
  `progressByJobId`. Children are not jobs and have no `job_id`. Do **not** add pseudo-entries
  here; it would collide in `progressByJobId` and put fictional rows on the Transfers page.
- **`item_delta`** carries `item_view()` projections read back from the `item` table. A transfer
  rate is **not** a persisted column and must not become one — it is a live sample, and
  `core/itemview.py` exists to project persisted truth (`DESIGN.md` §2/§9's invariant).

Add a **third, item-keyed message** (e.g. `child_progress`, shape `[{item_id, speed_bps}]`),
published from `_publish_child_progress` on the same throttled pass. The frontend's existing
`speedByItemId` map is already keyed exactly this way, so consuming it should be a small change
in `useLiveModel.ts` rather than new plumbing.

Keep it bounded the same way the rest of that method is: never more than the existing per-tick
cap, never proportional to tree size.

### 3. The gating problem — this is the part that needs thought, not just code

`FileTree.tsx`'s Speed cell currently renders **only when `entry.state === 'DOWNLOADING'`**. That
was deliberate: the WS speed map is never pruned when a job completes, so gating on "a value
exists" would leave a stale rate displayed on a finished row.

**A child file mid-transfer is not `DOWNLOADING`.** `_publish_child_progress`'s own child-state
rule is the leaf rule from `core/reconcile.py`: `local >= remote → DOWNLOADED, else PARTIAL`. So
every actively-transferring child sits at `PARTIAL`, and the current gate hides exactly the rows
this task is meant to light up.

Do **not** solve this by writing `DOWNLOADING` onto child rows. That would be a lie about
persisted state, it would collide with the reconciler's own leaf rule on the very next scan, and
this codebase has a documented history of exactly that class of bug (`_sample_and_publish_progress`
used to hardcode `"state": "DOWNLOADING"`; see `docs/decisions.md`, 2026-08-12).

Solve it on the **display** side instead. Options, pick one and justify it:

- Render a child's speed when its **parent's job is live** and a fresh sample exists for it —
  liveness comes from the parent, which the tree already knows.
- Have the backend prune a child from the map the moment it stops changing, and gate on freshness
  (a sample newer than N seconds), so staleness is impossible by construction rather than by a
  state check.

The second is more robust; the first is less code. Either is acceptable if the stale-rate case is
actually closed and tested.

### 4. Don't let the numbers read as additive

The parent row shows the **job's** rate; children now show their own. With
`mirror_parallel_transfer_count` files in flight, the children's rates sum to roughly the
parent's — they are the same bytes counted at two granularities, not extra throughput. Make sure
nothing in the UI implies otherwise (no "total" row that adds them, no sort that mixes parent and
child rates as peers without the tree structure making the relationship obvious).

## Testing

- Pure-function tests for the rate derivation: a normal delta, a zero delta, a *negative* delta
  (a file replaced or truncated mid-transfer — must not produce a negative rate), and a
  zero/sub-second elapsed.
- A test that a child's rate disappears once its parent job is gone.
- A test that `progress` and `item_delta` shapes are unchanged.
- Frontend tests for the new gating rule, including the stale-sample case.
- Run `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`, `gen_key.sh` first;
  if it is already running, leave it), `ruff check` **and** `ruff format --check`,
  `npm run lint`, `npm test`, `npm run build`, and `docker compose config --quiet` on all three
  compose files.

## Conventions to honor

- Non-obvious decisions in `docs/decisions.md`, newest at top, with rejected alternatives.
- Update `docs/concepts.md` if it describes what the Speed column shows — it is the single source
  the in-app Docs render from.
- `CHANGELOG.md` entry.
- **You cannot see the UI** — no browser exists here. Claims mean "builds, type-checks, lints,
  endpoints verified over HTTP", never "renders correctly."

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` it to `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record decisions in `docs/decisions.md`.
4. Prepare ONE commit; **do not commit**. Report the file list and a proposed one-line message
   back to the orchestrating session, which surfaces the `y/n`. Never `git add -A`, never push.
