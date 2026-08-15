---
name: 2026-08-12-live-child-progress
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Surfaced per-file progress from core/progress.py's existing subtree walk
  (JobProgress.children), diffed and published it from core/queue.py throttled to every 3rd
  tick with a logged safety cap, using core/reconcile.py's local>=remote leaf rule for child
  state. Fixed the parent's hardcoded "state": "DOWNLOADING" to read back through
  ITEM_VIEW_COLUMNS/item_view instead. New tests/test_queue_child_progress.py (5 tests, no
  seedbox needed). Both ruff gates and the full pytest suite pass.
---

# Task: Publish live per-file progress inside a mirroring directory

Reported by the user watching a real multi-rar release download: individual files sit
frozen, then a whole batch flips to `DOWNLOADED` at once. The cause is specific and the
fix is cheap, because the expensive part is already being paid for and thrown away.

## Before you start

- Read `DESIGN.md` §2/§9 (WebSocket contract) and §4.4 (progress sampling).
- Read `core/queue.py._sample_and_publish_progress` (~line 545) and `core/progress.py` in
  full, including the module docstring.
- Read `core/itemview.py` — the single projection, and central to this task.
- Read `prompts/startnewsession.md`'s "Traps worth knowing", specifically:
  - **Never publish a full node list except on connect.** Every update must be
    proportional to what changed, never to tree size.
  - **Nothing may publish a state it did not read back from the `item` table.**
    `core/itemview.py` is the one projection; `ReconciledNode.structural_state` is only a
    *candidate*.
  - **Sparse files lie** (§4.4) — `pget` writes sparse files, so `st_size` is wrong; read
    the `.lftp-pget-status` sidecar, and account for the `.lftp` temp suffix.
  - **`job.bytes_done` is not monotonic** — a retry or resume resets it.

## Working tree check

Run `git status --porcelain`. Other agents have recently worked in `db.py`,
`core/engine.py`, `core/extract.py`, `core/postprocess.py`, `api/`, and the frontend. If
any file *you* need is dirty, list it and ask before touching it. Surface unrelated dirty
files once; they do not block. This prompt file is exempt.

## The defect

`_sample_and_publish_progress` iterates `self._running.values()` — **one entry per running
job, which is one per top-level item.** It updates `item.local_size` for `p.item_id` and
publishes one `item_delta` for that item. The child files inside a mirroring directory get
nothing.

So every `.rar` under a release directory has its `local_size` and `state` recomputed only
by `scan_queue`'s reconcile pass — **every 30 seconds** (`scan_interval_s`). Hence: dead
still, then a batch flips at once.

Two things compound it, and both matter to the fix:

- **lftp writes `foo.rar.lftp` while transferring** (`xfer:use-temp-file yes`) and renames
  to `foo.rar` on completion. A child does not exist under its final name until it is
  done, so even the 30s scan sees files *appear* in clumps. The quantization is real, not
  just perceived. Your child→item mapping must strip the `.lftp` suffix.
- **The per-file data is already computed and discarded.** `core/progress.py`'s own
  docstring notes `scan_local` on a mirror job's subtree "is still a real walk of that one
  job's files every tick" — it walks every file under the job once a second and keeps only
  the sum (`prog.bytes_done`). No new I/O is needed; only the detail is missing.

## What to do

1. **Surface per-file results from the sampler.** Return them from `core/progress.py`
   alongside the existing aggregate rather than replacing it — the aggregate is what the
   Transfers page and `job.bytes_done` use, and it must keep working exactly as it does.

2. **Diff against the previous tick and act only on children whose size actually
   changed.** This is what keeps the message bounded: `mirror` transfers only a few files
   concurrently, so the changed set per tick is naturally small — bounded by lftp's
   parallelism, not by the size of the release. **Add an explicit safety cap anyway** and
   `log()` when it truncates; a silent cap reads as "we published everything" when we did
   not.

3. **Derive child state with the rule the reconciler already uses** — `local >= remote ?
   DOWNLOADED : PARTIAL`, against the child's persisted `item.remote_size`. Do not invent
   a second completeness rule; if you find yourself needing one, that is a signal to stop
   and report rather than diverge from `core/reconcile.py`.

4. **Throttle child publishing to every N ticks, not every tick.** Pick N (3 is a
   reasonable default — ~3s) as a named module constant with a comment. The goal is smooth
   feedback, not 1 Hz precision on each `.rar`. Rationale to record: a 50-file release at
   1 Hz is up to 50 `UPDATE`s per second, and steady write pressure is exactly what turned
   the `VACUUM INTO` race from rare into routine (fixed in `209928d`). Batch the writes.

5. **Fix the projection violation in the same pass.** `core/queue.py:580-589` hand-builds
   the item dict with `"state": "DOWNLOADING"` **hardcoded**. The 2026-08-12 work collapsed
   four hand-written copies of that dict into `core/itemview.py` precisely so nothing
   publishes a state it did not read back from `item`; this looks like a fifth that
   survived.

   Make the order match `scan_queue`'s invariant — **persist → read back → publish** —
   using `ITEM_VIEW_COLUMNS`/`item_view()` for both the parent and the new children.

   **If, after reading the code, you conclude this hardcoding is a deliberate and correct
   fast-path exception** (e.g. the read-back cost per item per tick is unacceptable),
   then do NOT change it — say so in your report with the reasoning, and make the new
   child publishing consistent with whatever you conclude. Do not leave the two paths
   following different rules without saying which is right.

6. **Tests.** At minimum: a mirror job over a multi-file subtree where individual children
   are seen progressing between ticks and reaching `DOWNLOADED` independently; the `.lftp`
   suffix mapping; the changed-only filter (an unchanged child publishes nothing); the cap;
   and no regression in the existing aggregate/`job.bytes_done` behaviour. There are
   existing progress tests — extend that style rather than inventing a parallel one.

## Explicitly out of scope

- The 30s `scan_interval_s` itself, and making it per-queue. Separate task.
- Any change to how *top-level* item completeness is computed. That belongs with the
  settle-gate work.

## Conventions to honor

- Match the surrounding comment style — explain *why*, name the rejected alternative.
- `docs/decisions.md`, newest at top: the throttle value and why, and your call on item 5.
- `CHANGELOG.md` under `## [Unreleased]` → `### Fixed`.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `uv run pytest` with the fake seedbox up (`docker-compose.test.yml`) so nothing skips;
  tear it down afterward and confirm with `docker ps -a`.
- **You cannot see the UI.** No browser here.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: file list, proposed one-line `fix:`
   message, test count, lint results, the throttle you chose, your call on the
   hardcoded-state question, and anything found but not fixed. Never `git add -A`, never
   push.
