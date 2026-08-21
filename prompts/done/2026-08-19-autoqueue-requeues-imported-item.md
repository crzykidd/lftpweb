---
name: 2026-08-19-autoqueue-requeues-imported-item
status: completed        # pending | completed | failed
created: 2026-08-19
model: opus              # opus = research/planning, sonnet = coding (this is investigate-then-fix)
completed: 2026-08-19
result: >
  Trigger state established as PARTIAL (two independent lines of evidence, not the
  hypothesis alone); fixed in two narrow halves -- §7.3's grace period extended to
  "was complete, remote unchanged, local shrank", and auto-queue skipping any item
  whose arr_status is past the *arr hand-off. Cleanup-blocking deferred with reasons.
---

# Task: auto-queue re-queues an item the *arr has just imported, producing a doomed job that blocks cleanup

**Investigate first, then fix.** A production bundle shows auto-queue re-queueing an item seconds
after that item's own transfer succeeded and post-processing completed. The re-queued job sits in
the queue until a slot frees, by which time `move` mode has deleted the seedbox source on
confirmed import — so the job fails `REMOTE_GONE`. Worse, while it waits, it blocks `arr_cleanup`
for the whole time, because cleanup withholds whenever "an active job exists for this item."

The **root cause is not yet established.** Do not start editing until step 1 answers it.

## The evidence

From `private_data/debug_logs/lftpweb-support-0.2.6-20260819T205145Z.zip` (production, v0.2.6,
build `f90ed70`, queue `dc-tv`, `move` mode, bound to Sonarr). Times are UTC; the app log is
UTC−7, Sonarr's log is UTC.

**Item 3354306** — `Married.At.First.Sight.S12E15.720p.WEB.h264-BAE[rarbg]-xpost`:

| Time (UTC) | What |
|---|---|
| 18:15:23 | job 391 **succeeds**, 1,651,731,114 bytes |
| 18:15:24 | verify `SKIPPED`, extract none, `download_prefix_removed` (renamed to final name), `arr_notified`, `remote_delete_deferred` |
| **18:16:06** | **`auto-queue: queued 1 item(s) for queue 1`** → job 395, `queued_at` 18:16:06.587Z |
| 18:17:31 | `arr_imported` (2 import history events, two consecutive passes) |
| 18:17:32 | `remote_delete` — seedbox source deleted |
| 18:17:32 → 18:21:33 | `arr_cleanup_withheld` ×5 — *"an active job exists for this item"* |
| 18:21:44 | job 395 finally admitted → **`REMOTE_GONE`**, 0 bytes |
| 18:22:33 | `arr_cleanup` finally runs |

**Item 3304447** — `Married.At.First.Sight.S15.720p.WEBRip.AAC2.0.x264-BAE`: identical shape, but
job 336 succeeded 16:30:14, auto-queue re-queued at **16:44:12**, the job waited **97 minutes**
for a slot, and `arr_cleanup_withheld` fired **92 times** before it failed at 18:21:41.

Both re-queues are confirmed as auto-queue from the app log (`lftpweb.core.autoqueue: auto-queue:
queued N item(s) for queue 1` at timestamps matching each job's `queued_at` to the millisecond).

**Ruled out — do not chase these:**

- *A second NZB.* Sonarr did re-grab this episode from a different NZB (14:49:50Z `...BAE-xpost`
  failed on the SAB side, 15:13:52Z `...BAE[rarbg]-xpost` grabbed instead). But that was **three
  hours before** the re-queue, only the `[rarbg]` release ever landed on the seedbox, and only one
  lftpweb item and one `downloadId` ever existed for it. The re-grab explains the item's
  provenance, not the re-queue.
- *A restart.* No restart occurred in this window; the two `INTERRUPTED` jobs in the same bundle
  (334/335, 14:43:28) are a separate, correctly-handled event — the v0.2.6 startup rescue
  re-queued both and both completed and imported normally.

**The unanswered question, and the whole point of step 1:** `core/autoqueue.py`'s
`ELIGIBLE_STATES = ("REMOTE_ONLY", "PARTIAL")`, while `core/mount_sentinel.py.resolve_absence`
only intercepts `structural_state == "REMOTE_ONLY"`. So a `PARTIAL` reading has no grace-period
protection at all. The plausible story is that Sonarr's import moved the media file out of the
release directory within seconds of lftpweb renaming it, leaving a residual directory that
reconciles as `PARTIAL`. **That is inference, not evidence** — the Sonarr logs covering
18:15–18:17Z were among six files dropped from the bundle for its size budget (see
`prompts/2026-08-19-support-bundle-log-recency.md`), and the bundle carries no local-tree listing.

Relevant supporting fact: Sonarr had been retrying the import of this path **every ~90 seconds
since 15:15:27Z** — three hours — each attempt failing `path does not exist or is not accessible`,
because SAB finished on the seedbox within two minutes of the grab while lftpweb's queue didn't
reach the transfer until 18:13. So an import attempt landing within seconds of the 18:15:24 rename
is entirely expected.

## Before you start

- `DESIGN.md` §3.2 (the state rules, especially rules 3 and 8), §4.6, §7.3.
- `core/autoqueue.py` — `ELIGIBLE_STATES` and its long explanatory comment, and `on_scan`'s
  eligibility query.
- `core/mount_sentinel.py` — `resolve_absence` and `COMPLETE_STATES`; read the module docstring,
  it explains precisely which failure directions the grace period exists to prevent.
- `core/engine.py._persist` — where `resolve_absence` is applied and where a job-lifecycle state
  is protected from being recomputed.
- `core/arrsync.py._maybe_cleanup` — the "an active job exists for this item" withhold.
- `docs/decisions.md` — search for the existing accepted limitation, recorded 2026-08-18:
  *"complete-local + remote-vanished re-queue fails `REMOTE_GONE` and still misses the pipeline."*
  This defect is that limitation's more damaging cousin, and that entry will need updating.

## Working tree check

Run `git status --porcelain` first and cross-reference. If files this plan touches are dirty, list
them and ask before proceeding. This prompt file is exempt.

**Note:** other tasks may be in flight on `frontend/`, `backend/lftpweb/api/jobs.py`,
`models.py`, `core/queue.py`, and `core/supportbundle.py` / `core/arrclient.py`. Coordinate — if
your fix needs one of those, stop and ask rather than editing across another task's work.

## What to do

### 1. Establish the actual trigger state — do not skip this

Determine what `structural_state` the reconciler produced for item 3354306 at ~18:16:06 and why
auto-queue's query matched it. Acceptable ways to establish it:

- Write a focused test that reproduces the shape: an item in a complete post-processing state
  (`VERIFIED`, or whatever verify=`SKIPPED` actually leaves), remote fully present, local content
  partially or wholly removed by an external actor — then assert what `_persist` writes and
  whether auto-queue's query selects it.
- Read the code path end to end and prove which branch fires.

Report which of `PARTIAL` / `REMOTE_ONLY` / something else it is, **with the evidence**. If it
turns out to be `REMOTE_ONLY` after all, the grace period should have held it and the bug is a
different one — follow it where it leads and say so.

### 2. Choose the fix, and justify the choice

Two candidate directions were sketched with the user. Neither is mandated; pick with reasons.

- **Extend the grace period to partial absence.** Cover the case where a previously-complete item
  loses *some* local content, the same way `resolve_absence` already covers losing all of it.
  **The risk to weigh explicitly:** `PARTIAL` being auto-queue-eligible is load-bearing — it is
  how a genuinely interrupted transfer resumes. A fix that suppresses re-queue for real partials
  would be a serious regression, far worse than the defect. Any narrowing must key on
  "was complete, then shrank," never on `PARTIAL` alone.
- **Make auto-queue skip an *arr-tracked item whose import is in flight** (e.g. `arr_status` past
  `notified` and not yet terminal). Narrower blast radius; does nothing for the same shape on an
  untracked queue.

A combination is legitimate. Whatever you choose, state what it does *not* cover.

### 3. Address the cleanup blocking, separately

Even with the re-queue fixed, `_maybe_cleanup`'s "an active job exists" withhold means any stray
queued job parks cleanup indefinitely — 97 minutes here, unbounded on a busy queue. Consider
whether a *queued-but-never-started* job for an already-imported item should block cleanup at all.
This may be its own defect worth fixing in the same pass, or worth deferring — decide, and say
which.

### 4. Tests

A regression test that fails against today's code and passes after: the exact production sequence
— transfer succeeds → post-processing completes → external actor removes local content → next scan
→ assert auto-queue does **not** re-queue. Plus a guard test in the opposite direction: a
genuinely interrupted partial transfer **is** still re-queued. That second test is the one that
stops this fix becoming a worse bug.

### 5. Docs

- Update the existing `docs/decisions.md` accepted-limitation entry rather than adding a
  contradictory new one — it currently frames this as cosmetic; production shows it also stalls
  cleanup.
- `DESIGN.md` if the fix changes what §3.2 or §7.3 assert.
- `CHANGELOG.md` under `[Unreleased]`.
- If the fix does not fully close the case, say so in `README.md`'s "Known gaps" — this project
  names its gaps rather than quietly narrowing them.

## Conventions to honor

- **Run the gates in the FOREGROUND with a generous timeout and read each exit code.** From the
  **repo root** (not `backend/` — `testpaths` lives in the root `pyproject.toml` and `tests/` is a
  sibling of `backend/`; running from `backend/` collects zero tests and looks like a pass):
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` — three separate gates. Do
  not background the test run; a subagent never receives the completion notification and will
  stall forever.
- Report backend test counts before and after, and confirm 0 skipped.
- Conventional-Commit prefix (`fix:`). No `Co-authored-by:` trailer.
- **Surface, don't silently resolve.** If the investigation shows `DESIGN.md` is wrong or
  underspecified here, say so — the doc gets corrected, not quietly diverged from.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`).
3. Record the decision — including the rejected alternative — in `docs/decisions.md`.
4. **You are a spawned agent: do NOT commit.** Prepare the tree, then report the file list, what
   step 1 established, and a proposed one-line commit message back to the orchestrating session.
   Never `git add -A`, never push.
