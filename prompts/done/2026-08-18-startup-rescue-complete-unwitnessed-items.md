---
name: 2026-08-18-startup-rescue-complete-unwitnessed-items
status: completed
created: 2026-08-18
model: sonnet
completed: 2026-08-18
result: startup sweep now re-queues jobs it just interrupted and rescues rows stranded by
  earlier restarts, mount-gated per queue; all gates green (1300 backend tests, frontend
  lint/test/build)
---

# Task: Startup recovery re-queues interrupted items instead of stranding the complete ones

Production incident (2026-08-18, diagnosed live + support bundle
`lftpweb-support-0.2.4-20260818T192004Z`): during an NFS half-outage, job 195 (a 74 GB
36-episode season pack, `move` queue) **actually finished** — its lftp ground through
the slow mount, completed every file byte-exact (verified against the seedbox: 70G both
sides), and exited — but the supervisor was frozen in D-state disk-wait and never
reaped the exit. The restart's orphaned-job sweep did the only honest thing it could:
marked the job `failed / INTERRUPTED` with its last-sampled 29.3 GB. Result: the item
correctly re-derives `DOWNLOADED` from the filesystem (§1.3 working as designed) but is
**permanently wedged** — the success pipeline only fires on an *observed* job success,
so the `.downloading-` prefix stays on (the *arr can't see it), no verify/notify runs,
the remote copy is never handed to the delete ladder, and auto-queue ignores
`DOWNLOADED` items so nothing ever rescues it. Its sibling (job 193, genuinely partial)
self-healed within seconds of restart precisely because `PARTIAL` is auto-queue
eligible — the asymmetry is the bug.

The fix: the startup sweep re-queues what it interrupts. A re-queued complete item's
`mirror -c` finds nothing to transfer, exits 0 almost immediately, and that observed
success triggers the pipeline — the exact recovery the user performed by hand (one
Queue click) for the live item. A re-queued partial item resumes, same as auto-queue
would have done but without depending on auto-queue being enabled.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §1.3 (filesystem is truth), §4.6 (stop/suppression
  semantics — note an INTERRUPTED item is *not* suppressed, by design), §7 (the
  ladder that must eventually run for a `move` item).
- Read before editing:
  - `backend/lftpweb/core/queue.py` — the startup orphaned-job sweep (find it by the
    `"clearing %d job(s) left 'running' by a previous run"` log line; note its
    docstring already says the item "stays eligible to be picked up again" — an
    aspiration that only holds for `PARTIAL`), `enqueue_item` (idempotent — safe to
    call for an item that auto-queue also grabs later), and where `output_tail` was
    given the interruption explanation (2026-08-17, `INTERRUPTED_OUTPUT_TAIL`).
  - `backend/lftpweb/core/mount_sentinel.py` and `core/autoqueue.py.on_scan`'s
    blanket mount-gate check — the safety rule the re-queue MUST honor (below).
  - `tests/test_queue_orphans.py` — the existing sweep coverage you'll extend.

## Working tree check

Run `git status --porcelain` before editing; cross-reference the files this plan
touches; ask before touching any that are dirty. This prompt file is exempt.

## What to do

1. **The sweep re-queues every item it just marked INTERRUPTED** — after the existing
   UPDATEs, for each affected item, call the normal `enqueue_item` path (never a
   hand-rolled INSERT — the guards exist for a reason). Uniform for partial and
   complete items: partial resumes, complete no-ops into the pipeline. One info event
   per re-queued item (kind e.g. `interrupted_requeued`, message naming the incident
   pattern: "job interrupted by a restart/crash — re-queued; a completed transfer
   no-ops straight into post-processing, a partial one resumes from its bytes").
2. **Mount-gate the re-queue.** Auto-queue refuses to act for a queue whose mount
   sentinel fails, precisely so nothing writes into an unmounted directory — a
   restart with a broken mount (the exact incident this task comes from!) must not
   have the sweep spawn lftp processes into the void. Check
   `core/mount_sentinel.py.check()` per queue before re-queueing its items; a gated
   queue's items are left as today (marked INTERRUPTED, one warning event naming the
   gate as the reason they were not re-queued) — the next healthy scan's auto-queue
   still picks up the `PARTIAL` ones, and rule 3 below covers the complete ones.
3. **Also rescue rows stranded by *earlier* restarts** — the live production item was
   already `INTERRUPTED` before this fix ships, so the sweep must not only cover jobs
   it transitions right now. At startup (one bounded pass, same sweep), also re-queue
   any item whose **most recent** job is `failed/INTERRUPTED`, whose state reads
   `DOWNLOADED`, and whose physical directory still carries the `.downloading-`
   prefix (`core/local_delete.py._physical_local_root` is the one resolver — do not
   write a second one) with no active job and no postprocess outcome state. Same
   mount gate, same event. Keep it narrow: `PARTIAL` stranded rows are already
   auto-queue's job; this clause exists only for the complete-but-unwitnessed shape
   auto-queue structurally cannot see.
4. **Known limitation, named not solved:** a complete-local item whose *remote* has
   meanwhile vanished will fail its no-op re-queue `REMOTE_GONE` and still not reach
   the pipeline. Rare (requires a crash after completion AND an external remote
   delete); record it in `docs/decisions.md` as accepted, don't build for it.
5. **Tests** (extend `tests/test_queue_orphans.py`):
   - Interrupted job + complete local dir (still prefixed) → restart sweep →
     re-queued → job succeeds as no-op → postprocess pipeline fires (prefix removed).
   - Interrupted job + partial local dir → re-queued (don't assert the transfer
     itself; the enqueue is the contract).
   - Mount sentinel failing for the queue → NOT re-queued, warning event written,
     INTERRUPTED marking unchanged.
   - The rule-3 stranded shape (job already terminal INTERRUPTED before startup,
     item DOWNLOADED, dir prefixed) → re-queued on the next startup.
   - A stopped/cancelled job's item is NOT re-queued (user intent, §4.6).
6. **Docs, same commit:** `CHANGELOG.md` under Unreleased (### Fixed, user-voiced: a
   transfer that finished during a crash/hang no longer strands as
   downloaded-but-never-processed; interrupted items re-queue themselves on restart).
   `docs/decisions.md`: the re-queue-not-direct-pipeline choice (reusing observed
   job success as the single pipeline trigger vs. adding a second pipeline entry
   point — state why), the mount-gate rule, and the rule-4 limitation.

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from the repo root, ~3.5 min — timeout 400000ms; never background a
  gate or wait on a Monitor notification), exit codes read: `uv run --project
  backend ruff check`, `uv run --project backend ruff format --check`,
  `uv run pytest`; frontend untouched — re-verify anyway (`npm run lint`,
  `npm test`, `npm run build`).
- Comment style: dated, incident-citing (this bundle has a name — use it).
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Frontmatter: `status: completed` (or `failed`), `completed` date, one-line
   `result`.
2. Move this file into `prompts/done/` (or `prompts/failed/`).
3. Hand off ONE commit (prompt file + changes + move). Present file list + one-line
   message. **You are a spawned agent: do not commit, never `git add -A`, never
   push.** Branch is `dev`.
