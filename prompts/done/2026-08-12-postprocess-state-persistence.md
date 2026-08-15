---
name: 2026-08-12-postprocess-state-persistence
status: completed        # pending | completed | failed
created: 2026-08-12
model: opus              # mixed: a §3.2 state-machine decision plus the code to implement it
completed: 2026-08-12
result: >
  Post-processing states now survive the periodic rescan, as a precedence rule rather than
  stickiness. Outcomes (VERIFIED/CORRUPT/EXTRACTED/EXTRACT_FAILED) win over a freshly
  recomputed DOWNLOADED (new pure predicate `core/postprocess.outcome_survives_rescan`,
  applied in `core/engine.py._persist`), lose to PARTIAL (§3.2 rule 2 stays absolute), and
  all six states joined `core/mount_sentinel.py`'s sticky set so absence still reaches
  REMOVED_LOCAL through §7.3's grace period — which also fixes a latent auto-queue
  re-download of any release an importer moved out. Transient states (VERIFYING/EXTRACTING)
  are protected by the pipeline's in-memory `in_flight_item_ids()`, never by the state
  string, so a worker that dies mid-extract un-wedges itself on the next scan with no startup
  sweep and no timeout. New `tests/test_state_persistence.py` (28 tests) plus additions to
  `test_mount_sentinel.py` and `test_postprocess.py`; 458 passed, ruff format + check clean.
  DESIGN.md §3.2 wording proposed in the report, not written (user decides doc changes).
---

# Task: Stop the periodic rescan from erasing post-processing states

Every post-processing outcome is silently overwritten within ~30 seconds of being set.

`core/engine.py._persist` recomputes each item's structural state (`REMOTE_ONLY`/`PARTIAL`/
`DOWNLOADED`, §3.2) on every scan pass and writes it, except for items in
`_protected_rel_paths` — which covers only items with a `queued`/`running` job or
`auto_queue_suppressed` set. None of the six post-processing states
(`VERIFYING`/`VERIFIED`/`CORRUPT`/`EXTRACTING`/`EXTRACTED`/`EXTRACT_FAILED`, §3.2 lines 238–239)
sets either condition, and none appears in `core/mount_sentinel.py._STICKY_PREV_STATES`. So the
next scan stomps them with a structural state.

The user-visible damage: a verified, extracted release reads as a plain `DOWNLOADED` item within
half a minute, and — worse — **`CORRUPT` and `EXTRACT_FAILED` vanish on their own**, so a failure
the user needs to see disappears from the UI before they look at it. Pre-existing since phase 5;
found while building the `_UNPACK_` extraction change (see `docs/decisions.md`, top entry).

## Before you start

- **Read `DESIGN.md` §3.2 in full** — it is the state-rule source of truth and the thing this
  task is judged against. Also §6 (post-processing) and §7.3 (the mount gate and the
  `REMOVED_LOCAL` grace period).
- Read `core/engine.py._protected_rel_paths`'s docstring end to end. This gap is the direct
  descendant of a decision recorded there: it calls itself "the smallest reasonable call,
  surfaced rather than silently decided", made in phase 4 when the post-processing states did
  not yet exist. You are revisiting that decision with the states that phase 5 added — treat the
  docstring's reasoning as the baseline to extend, not as an obstacle.
- Read `core/mount_sentinel.py.resolve_absence` and `_STICKY_PREV_STATES`, and
  `docs/decisions.md`'s phase 4 and phase 5 entries plus the newest `_UNPACK_` entry.

## Working tree check

Run `git status --porcelain` first. Expect a dirty tree from an in-flight local session:
dev-environment fixes (`docker/Dockerfile`, `docker-compose.dev.yml`, `docker-compose.test.yml`,
`frontend/vite.config.ts`), the `_UNPACK_` extraction change (`core/extract.py`,
`core/postprocess.py`, `core/local_scan.py`, and their tests), and quite possibly a
Settings → Transfer tab change (`pages/settings/TransferTab.tsx`, `models.py`, `core/queue.py`,
`TransfersPage.tsx`). **None of them are yours — do not revert, refactor, or tidy them.**
`CHANGELOG.md`, `standards.md`, `prompts/startnewsession.md`, `.claude/commands/release-prep.md`
were dirty before the session; leave them alone. Append to `docs/decisions.md` at the top
without disturbing existing entries. If a file you need to modify is dirty, list it and ask.

## The decision you have to make (and record)

Protecting these states is necessary but not sufficient, and the naive fix creates a worse bug.
**A permanently sticky state can never be un-stuck**: if `EXTRACTED` is simply added to the
protected set, an item whose local files are later deleted by the user, an `*arr` importer, or a
failed mount stays `EXTRACTED` forever, and §3.2 rule 3's `REMOVED_LOCAL` transition — the whole
point of the §7.3 grace period — never fires for it again.

So the two halves have to be designed together:

1. **While the content is present**, a post-processing state must win over the recomputed
   structural state. `core/postprocess.py` owns `state` for these items the same way
   `core/queue.py` owns it for an active job.
2. **When the content goes absent**, these items must still reach `REMOVED_LOCAL` through the
   existing `resolve_absence` grace-period machinery, exactly as `DOWNLOADED` does today —
   which likely means the terminal ones belong in `_STICKY_PREV_STATES` (or whatever
   generalisation you judge correct), not merely in the protected set.

Decide deliberately, and consider at least these distinctions rather than treating all six the
same:

- **Transient vs terminal.** `VERIFYING`/`EXTRACTING` are held only while a worker is mid-run;
  `VERIFIED`/`EXTRACTED`/`CORRUPT`/`EXTRACT_FAILED` are outcomes that must survive.
- **A crashed worker must not wedge an item forever.** If the process dies mid-extract, an item
  left `EXTRACTING` that is now permanently protected can never be recovered by a rescan. Phase
  3 hit exactly this shape of bug with jobs left `running` by a restart (see
  `prompts/done/` and `docs/decisions.md`) — do not reintroduce it. Say explicitly how a
  stuck transient state gets resolved.
- **Failure states are the ones the user most needs to keep.** Whatever you choose, `CORRUPT`
  and `EXTRACT_FAILED` must not silently disappear.

Record the decision in `docs/decisions.md` with the alternatives you rejected and why.

## What to do

1. Implement the decision above in `core/engine.py` (and `core/mount_sentinel.py` if the
   absence path needs it). Prefer extending the existing seams — `_protected_rel_paths`,
   `resolve_absence`, `_STICKY_PREV_STATES` — over adding a parallel mechanism.
2. Keep it one owner per concern: `core/queue.py` owns job-lifecycle states, `core/postprocess.py`
   owns post-processing states, `core/reconcile.py` owns structural ones. If your fix blurs that,
   say so in the report.
3. **Tests are the deliverable here as much as the fix.** At minimum: each of the six states
   survives (or correctly does not survive) a full scan pass; an `EXTRACTED` item whose local
   files are then removed still reaches `REMOVED_LOCAL` via the grace period; an item stuck in a
   transient state recovers rather than wedging. Table-driven if that fits the existing style —
   `tests/test_scheduler.py` is the project's model for that shape.
4. If `DESIGN.md` §3.2 is silent or wrong about who wins between a post-processing state and a
   structural one — it currently is — **say so in your report and propose the wording**. Do not
   edit `DESIGN.md` yourself in this task; the user decides doc changes. (`docs/decisions.md` is
   yours to write, and is the right home for the decision itself.)

## Explicitly out of scope

- The **delete-before-extract ordering** (`move`-mode deletes the remote copy before extraction
  runs, so a failed extract has no remote source left). Real, reported by the `_UNPACK_` task,
  and its own decision — do not reorder the pipeline here.
- Any change to what post-processing *does*. This task is solely about whose write to `item.state`
  survives a rescan.

## Conventions to honor

- Comments explain **why**, matching the surrounding density and voice — `_protected_rel_paths`'s
  own docstring is the standard to match, since you are extending its reasoning.
- Cite `DESIGN.md` sections (`§3.2`, `§6`, `§7.3`) where a decision traces to one.
- `uv run ruff format --check` **and** `uv run ruff check` (run the format check explicitly — it
  has caught files `check` alone missed four times in this project), plus the full `uv run pytest`.
- The dev stack and fake seedbox are **running and in use by the user** (`lftpweb-backend-1`,
  `lftpweb-frontend-1`, `lftpweb-test-seedbox-gnu`, `lftpweb-test-seedbox-busybox`). Leave them
  running; do not disturb `/data/pickup`. `docker compose -f docker-compose.dev.yml restart backend`
  picks up backend changes.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Record the decision and its rejected alternatives in `docs/decisions.md`, newest at top.
4. **Do not commit. Do not push.** Prepare the tree, then report back to the orchestrating
   session with the file list and a proposed one-line commit message (`fix:` prefix, no
   `Co-authored-by:` trailer).
