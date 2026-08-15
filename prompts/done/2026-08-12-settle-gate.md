---
name: 2026-08-12-settle-gate
status: done
created: 2026-08-12
model: sonnet
completed: 2026-08-12
result: >
  Built the fingerprint-based settle gate (migration 007, core/settle.py) with both gates
  (auto-queue eligibility and job-success completion in core/queue.py._reap_one), the
  substate='settling' surfacing, and the verify.py hash-on-disk truncation fix. Defaults off.
  542 tests pass (539 + this task's own additions), both lint gates clean, npm run
  build/lint clean. See docs/decisions.md's 2026-08-12 "settle gate" entry for full reasoning,
  rejected alternatives, and drafted DESIGN.md wording (not applied). Not committed -- left
  for the orchestrating session per this prompt's own instruction.
---

# Task: Don't treat a still-arriving remote item as complete

The seedbox may still be writing an item when a scan sees it. Today nothing notices, and
for a **directory** the consequences are permanent, not self-healing. This is the largest
correctness gap found in the 2026-08-12 real-use session.

## Before you start

- Read `DESIGN.md` §3.2 (state rules), §4.7 (what an "item" is), §5 (scanning), §7.3.
- Read `core/reconcile.py` in full — the rollup logic and `counts_predicate` seam.
- Read `core/autoqueue.py` — `ELIGIBLE_STATES`, and `on_scan`'s blanket mount gate.
- Read `core/remote.py` — `RemoteEntry`, `parse_find_records`, and
  `interpret_primary_scan_result`.
- Read `prompts/open-issues.md` § "2 — the settle gate" for the agreed design and the
  reasoning behind each choice. **That section is the specification; this prompt is the
  execution plan.**
- Read `prompts/startnewsession.md`'s "Traps worth knowing" — especially
  `relevant == 0` meaning two different things, and the publish invariant.

## Working tree check

Run `git status --porcelain`. This task touches widely-shared files
(`core/engine.py`, `core/itemview.py`, `core/reconcile.py`, `FileTree.tsx`). Earlier
agents worked in several of them. If any are dirty, list them and ask before touching
them — for this task, do block on it rather than working around it.

## Use migration number 007

`005` is metrics, `006` is `state_changed_at`, `008` is reserved for the deletion cluster.
Use **`007`**.

## The problem, precisely

`core/reconcile.py:238` decides completeness by size:
`state = DOWNLOADED if local_entry.size >= remote_entry.size else PARTIAL`, recomputed
every scan.

**Single files self-heal.** A 4 GB file caught mid-upload at 1.5 GB gets queued, lftp
pulls a prefix, the next scan sees remote 4 GB / local 1.5 GB → `PARTIAL` → re-queued →
resumes. Wasteful, not corrupting. Confirmed live by the user.

**Directories do not.** A release uploads 8 files; the scan catches 3; each of those 3 is
whole. The rollup reads the directory as **`DOWNLOADED`** — not a boundary race, the
normal outcome. Post-processing then runs on a half release, `move` relocates it, an
`*arr` imports 3 of 8 files, and when the stragglers arrive the local copy is gone →
`REMOVED_LOCAL` → excluded from `ELIGIBLE_STATES` → never re-queued.

**Comparing size between scans does not catch this.** Nothing about those 3 files changes.

## What to build

### 1. The fingerprint

Per **top-level item**, over its whole remote subtree:
`(file_count, total_bytes, max_mtime)`. Top-level granularity matches auto-queue's
granularity and §4.7's notion of an item; children inherit their root's verdict.

`remote_mtime` is **already captured** (`find -printf '%T@'` → `RemoteEntry.mtime` →
`ReconciledNode.remote_mtime` → the `item.remote_mtime` column → the frontend) and is
currently read by **nothing**. You are giving existing data a consumer, not adding
plumbing.

Do **not** use mtime alone: rsync/scp/torrent clients preserve or preset it, and a
directory's own mtime only moves when entries are added or removed, never when a child
grows. Do not use size alone either — that is the directory case above.

### 2. Settled = fingerprint unchanged across 2 consecutive scans

Persist it (migration 007). In-memory would need no migration but loses counters on
restart and — decisively — **cannot be published**, which collides with the invariant that
nothing publishes a state it did not read back from `item`.

**A scan carrying a partial-scan warning must not advance the settle counter.** GNU `find`
exits nonzero the instant it cannot read one subdirectory and still prints what it found
(`interpret_primary_scan_result` treats that as partial success). Two consecutive partial
scans returning the same truncated subset would otherwise read as "settled" and be exactly
wrong. Reset or hold — decide which and say why — but never advance.

Make the required count a named constant with a comment, defaulting to **2**. Note in the
comment that "2 scans" is a different wall-clock duration once the per-queue scan interval
lands (a separate task; `scan_interval_s` is one global 30s today).

### 3. Both gates, not just one

- **Auto-queue eligibility** — an unsettled item is not queued. The cheap half.
- **Completion** — an unsettled item must not reach `DOWNLOADED` and so must not trigger
  post-processing. **This is the half that actually fixes the directory case**, and
  skipping it leaves the bug open for manually-queued and mid-upload-arriving items.

**A manual Queue click overrides the gate** — explicit user action beats a heuristic. The
completion gate still applies, so the worst case is a wasted partial transfer that
resumes, never a bad import or a bad delete. Make sure the UI does not silently do
nothing when a user clicks Queue on a settling item.

### 4. Surfacing it

Keep the item `REMOTE_ONLY` and set **`substate = 'settling'`**. The `substate` column
exists (`001_initial_schema.sql:86`) and is used **nowhere** in the codebase — free,
already migrated, and it avoids touching the state `CHECK` constraint or §9.2's
three-word visible vocabulary. Add it to `ITEM_VIEW_COLUMNS`.

Files-page treatment: a small badge on a `REMOTE_ONLY` row indicating we are waiting for
another scan, per the user's explicit request. Keep it quiet — most items pass through
this state on every first sighting.

### 5. Also fix: `verify_hash_on_disk` can bless a partial file

`core/verify.py:188-194`. With no `.sfv`/`.md5` sidecar and the hash-on-disk fallback
enabled, verification proves only "readable end to end" — **a partial file passes** and
returns `VERIFIED`, which is the sole gate on `move` mode's irreversible remote delete
(`core/postprocess.py._maybe_delete_remote`).

At minimum, the fallback must also confirm the item's local bytes match its remote bytes
before returning `VERIFIED`. Consider whether a fallback that cannot detect truncation
should be allowed to authorize a delete at all — if you conclude it should not, make it
`SKIPPED` for `move` queues and say so loudly in the audit event. Record your reasoning.

## Safety rule this project holds to

**Every new capability ships defaulting OFF unless there is an explicit, reasoned
exception.** The settle gate is a judgement call: it makes the app *safer* but it also
delays every transfer by 30–60s, including the user's atomic hardlink path where it is
unnecessary. Decide, and record the reasoning either way. If you default it on, it must be
switchable off from settings, and the changelog must say plainly that existing installs
will see transfers start later than before.

## Tests

- **A real growing file across scan boundaries** against the fake seedbox — write a remote
  file in chunks, scan between chunks, and assert the item is not queued and does not
  reach `DOWNLOADED` until it settles. This is the reproduction; do not ship without it.
- **The directory case explicitly**: a release directory that gains files between scans
  must not read `DOWNLOADED` off the partial set.
- A partial-scan warning does not advance the counter.
- An atomic arrival (all files present at first sighting) settles after exactly the
  configured number of scans and no more.
- No regression in the existing reconcile tests, including the `relevant == 0`
  all-excluded-children case which must stay vacuously `DOWNLOADED`.

## Conventions to honor

- `docs/decisions.md`, newest at top, with rejected alternatives named.
- `CHANGELOG.md` under `## [Unreleased]`.
- Both lint gates: `uvx ruff@0.8.4 check --config ruff.toml .` **and**
  `uvx ruff@0.8.4 format --config ruff.toml --check .`.
- `npm run lint` / `npm run build` in `frontend/`.
- `uv run pytest` with the fake seedbox up; tear it down afterward, confirm `docker ps -a`.
- **You cannot see the UI.** No browser here.
- **If the build reveals `DESIGN.md` is wrong or underspecified, say so** — draft the
  wording in your report and in `docs/decisions.md`. Do not silently diverge, and do not
  edit `DESIGN.md` yourself; three proposed wordings are already awaiting the user's
  approval.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` it into `prompts/done/` (or `prompts/failed/`).
3. Record decisions in `docs/decisions.md`.
4. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   message, test count, lint results, your on/off default and why, your call on the
   hash-on-disk fallback, and anything found but not fixed. Never `git add -A`, never push.
