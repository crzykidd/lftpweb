---
name: 2026-08-11-phase4-autoqueue-and-patterns
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: |
  core/patterns.py (one evaluator), core/autoqueue.py, core/mount_sentinel.py (mount gate +
  REMOVED_LOCAL grace period), counts_predicate wired into core/reconcile.py (EXCLUDED
  state), exclude_globs wired into core/queue.py, migration 002 (auto_queue_patterns_only,
  default off), pattern CRUD + live preview + autoqueue-status API, Settings -> Queues UI.
  Verified: uv run pytest (215/215 with the fake seedbox up, including a new end-to-end test,
  tests/test_autoqueue_e2e.py), both ruff gates, npm run build/lint, docker compose config on
  all three files. Not committed -- see the final report for the proposed commit. Every
  decision made unattended is recorded in docs/decisions.md's phase 4 entry.
---

# Task: Phase 4 — auto-queue and patterns

Make lftpweb pick things up on its own: three kinds of pattern, one shared evaluator, and an
auto-queue that respects everything phases 3a/3b put in place.

**Done when:** a `file_exclude` of `*.nfo` leaves its release `DOWNLOADED` — **not** permanently
`PARTIAL` — and a stopped item is never resurrected by an auto-queue pass whose patterns still
match it.

## Before you start

- **Read `DESIGN.md` §4.7 in full** (three pattern kinds, matching semantics, what counts as an
  item, the completeness trap), plus §3.2 (states, especially rules 3, 7, 8), §7.3 (the mount
  sentinel), §13 phase 4, and §15.
- Read `prompts/startnewsession.md` — the traps list and the "what real hardware taught us"
  section. Phases 1–3 are proven against a live seedbox; don't regress them.
- Read `docs/decisions.md`. Several entries constrain this phase directly.

## Working tree check

Run `git status --porcelain` first and cross-reference what this plan touches. If anything is
dirty, list it and ask before proceeding. This file is exempt.

## Non-negotiables

- **Auto-queue defaults OFF, per queue.** Enabling it is an explicit user action. A capability
  that turns itself on is a bug.
- **Do not modify the user's live data or config.** No migrations that rewrite existing rows'
  `sync_mode`, no enabling anything on an existing queue.
- **Do not commit.** Prepare the tree and report back.

## What to do

### 1. `core/patterns.py` — one evaluator, used in two places

This module exists because the same compiled pattern set must build the lftp `--exclude-glob`
arguments **and** tell the reconciler what an item is supposed to contain. Two copies of that
logic drifting apart is the bug in §3.2 rule 8.

Three kinds (§4.7), matched **case-insensitively**:

| Kind | Matches against | Effect | Enforced by |
|---|---|---|---|
| `select` | the item name | which items auto-queue picks up | us |
| `skip` | the item name | items auto-queue never picks up | us |
| `file_exclude` | paths inside an item | files never transferred | lftp `--exclude-glob` |

Semantics, all from §4.7:
- **Glob (`fnmatch`) when the pattern contains `*`, `?`, or `[`; plain substring otherwise.** So
  `1080p` matches without demanding `*1080p*`, while `*.nfo` stays strict.
- **Skip beats select.** Evaluated after.
- **No select patterns ⇒ everything matches**, unless *patterns-only* is on for that queue.
- Patterns are per-queue, or global with `queue_id NULL`.
- **`file_exclude` also applies to a loose top-level file item** — otherwise `*.nfo` suppresses
  nfos inside releases while happily downloading a stray `notes.nfo` at the queue root.

### 2. The completeness trap — the highest-value part of this phase

`--exclude-glob '*.nfo'` means those files never arrive. If the reconciler counts them as
missing, every filtered release is permanently `PARTIAL`: never `DOWNLOADED`, never
post-processed, never deleted under `move`, and re-queued on every pass. One pattern would
quietly break the pipeline for every item it touches.

- Excluded children are marked **`EXCLUDED`** and do not count toward completeness.
- **A directory whose children are all excluded is vacuously `DOWNLOADED`**, and its local
  directory may legitimately not exist — lftp does not create a directory it has nothing to put
  in. Completeness must not require it (§3.2 rule 8).
- Phase 2 left a `counts_predicate` seam in `core/reconcile.py` for exactly this. Use it.
- Changing `file_exclude` patterns **retroactively changes completeness in both directions**.
  That is correct; surface it in the preview rather than letting it be discovered.

### 3. `core/autoqueue.py`

- Evaluated against newly-seen remote items on each scan; per-queue enable, **default off**.
- **Skips anything with `auto_queue_suppressed`**, plus `STOPPED`, `FAILED`, `REMOVED_LOCAL`,
  `REMOVED_BOTH`. Phase 3a writes that flag precisely so this phase honours it — without it,
  auto-queue restarts a stopped job ~30 s later, forever (§4.6).
- **Retroactive:** adding a pattern re-evaluates the whole known model, not just future scans.
- Manual queueing always wins and clears suppression; never filter an explicit user action.

### 4. The mount sentinel and grace period — required here, not with `sync`

§7.3 writes these up under `sync` mode, but `docs/decisions.md` records the decision to require
them in **phase 4**: auto-queue is the first feature that takes *action* on local absence, and a
network mount that drops makes every item look locally absent at once.

Both failure directions are destructive:
- items read `REMOTE_ONLY` ⇒ auto-queue **re-downloads the entire library** off one blip;
- items read `REMOVED_LOCAL` ⇒ auto-queue **permanently skips** them.

Implement: lftpweb writes `.lftpweb-mount-ok` at a queue's local root after its first successful
scan. Before auto-queue acts on *any* absence for that queue, the root must exist, be readable,
and contain that sentinel. Failing that, auto-queue is disabled for the queue and the condition
is surfaced in the UI and the log. The user runs this against an NFS share, so this is live risk,
not theory.

Grace period: absence must persist across several consecutive scans (default ~10 minutes,
tracked via `item.first_missing_at`) before it counts.

### 5. API and UI

- Pattern CRUD, per queue and global.
- **Live "what would this match" preview** against the current remote tree (§9.2): which items
  would be *selected*, which *skipped*, and within a sampled item, which files *excluded*.
  Patterns are the feature most likely to be subtly wrong and the only cheap fix is showing the
  answer before it is saved.
- Per-queue auto-queue toggle and *patterns-only* switch, both defaulting off/false.

## Verify before reporting — actually run these

The fake seedbox is `docker-compose.test.yml` (run `docker/test-seedbox/gen_key.sh` first;
password auth `seeduser`/`testpass123`, ports 2222 GNU / 2223 busybox). **Tear everything down
when finished** and confirm with `docker ps -a`.

1. `uv run pytest` passes. New tests must include:
   - the pattern evaluator: glob-vs-substring dispatch, case-insensitivity, skip-beats-select,
     empty-select with and without *patterns-only*;
   - **a `*.nfo` exclude leaves its release `DOWNLOADED`, not `PARTIAL`** — the single highest
     value test in this phase;
   - a directory whose children are all excluded is `DOWNLOADED` even though the local
     directory was never created;
   - a `STOPPED` item is not resurrected by an auto-queue pass whose patterns match it;
   - the mount gate: an unmounted/sentinel-less root propagates **no** auto-queue action.
2. **End-to-end against the fake seedbox**: enable auto-queue on a queue with a `*.nfo`
   `file_exclude`, confirm the release transfers, the `.nfo` does not, and the item reaches
   `DOWNLOADED`. Report what you observed.
3. `npm run build` and `npm run lint` clean.
4. **Both lint gates exactly as CI runs them**, repo-wide — `ruff check` alone is not enough,
   `format --check` is a separate gate and has broken the build before:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
5. `docker compose config --quiet` clean on all three compose files.

State plainly what you could not verify. An admitted gap beats an unverified claim.

## Surfacing decisions

The user is asleep and has asked that **every decision made without them be documented**. Record
each in `docs/decisions.md` (newest at top) with the alternatives considered, and repeat them in
your final report. If `DESIGN.md` is wrong or silent, make the smallest reasonable call, record
it, and **do not edit `DESIGN.md`** — that gets corrected in conversation.

## When done

1. `docs/decisions.md` entries for every decision.
2. Update `prompts/startnewsession.md` — phase table and "Where we are".
3. Update this file's frontmatter (`status`, `completed`, `result`).
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list plus a proposed one-line commit message (`feat:`
   prefix, no `Co-authored-by:`; branch `dev`).
