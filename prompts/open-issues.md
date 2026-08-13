# Open issues — found 2026-08-12 (post-phase-9, first real-use session)

A living list, not a handoff prompt. It never moves to `done/`. Items are struck through
and annotated when they ship, so the reasoning stays findable after the fix.

Most of these came from the user actually running the app against their production
seedbox — the same pattern the 2026-08-12 session already recorded: nine phases of green
CI while real defects sat in plain sight on the first screen a human opened.

---

## Bugs

| # | Summary | Where | Status |
|---|---|---|---|
| 1 | No `busy_timeout` on the shared connection — any `SQLITE_BUSY` fails instantly rather than waiting | `db.py:32-40` | prompt written |
| 2 | **No settle gate on remote items** — a partial upload reads as complete | `core/reconcile.py`, `core/autoqueue.py` | open |
| 3 | `verify_hash_on_disk` returns `VERIFIED` for a partial file, which can authorize a `move`-mode remote delete | `core/verify.py:188-194` | open |
| 4 | `REMOVED_LOCAL` is not in `ELIGIBLE_STATES` — an item whose local copy was moved away can never be re-queued | `core/autoqueue.py:47` | open, coupled to 7/8 |
| 5 | `EXTRACTED` claimed for items containing no archives; false `extracted_at`; overwrites a real `VERIFIED` | `core/extract.py:189`, `core/postprocess.py:536` | prompt written |
| 9 | Expand/Collapse all give no reason when disabled for having no directories — reads as broken | `FileTree.tsx:380-397` | prompt written |
| 10 | "Rescan now" spinner is a 1-second timer, not a completion signal | `FilesPage.tsx:15-20` | prompt written |
| 12 | Child files under a mirroring directory only update every 30s | `core/queue.py:545-590` | open |
| 13 | `_sample_and_publish_progress` hand-builds an item dict with `"state": "DOWNLOADING"` hardcoded instead of using `core/itemview.py` | `core/queue.py:580-589` | open |
| 14 | Extraction has no completeness precondition — no volume-set check, ungated on a default `copy` queue | `core/postprocess.py:405-421` | prompt written |
| 15 | `_FAILED_` directories accumulate forever, hidden from scans, consuming disk | `core/extract.py` `_staging_dirs` | prompt written |

## Enhancements

| # | Summary | Status |
|---|---|---|
| 6 | `state_changed_at` column + trigger; Files column showing relative time labeled by state | open |
| 7 | Local retention cleanup — delete local files older than N days | open |
| 8 | Manual delete-local in Files (per-item + bulk) | open |
| 11 | Per-queue scan interval (10/30/60/none), replacing the single global | open |

---

## Detail on the ones that need it

### 2 — the settle gate

The seedbox may still be writing an item when a scan sees it. The user's landing paths:
hardlink for torrents (atomic, safe), move for nzbs (**only atomic if it stays on one
filesystem** — a cross-device move is a copy that grows), and sometimes a direct copy.

**Single files self-heal.** Scan catches a 4 GB file at 1.5 GB → queued → lftp pulls a
prefix → next scan sees remote 4 GB / local 1.5 GB → `PARTIAL` → re-queued → resumes.
Wasteful, not corrupting. Confirmed live by the user (Anna.Pigeon).

**Directories do not.** A release uploads 8 files, the scan catches 3, each of those 3 is
whole → the rollup in `core/reconcile.py` reads the directory as **`DOWNLOADED`**. Not a
boundary race — the normal outcome. Post-processing then runs on a half release, `move`
relocates it, an `*arr` imports 3 of 8 files, and the stragglers arrive to find the local
copy gone → `REMOVED_LOCAL` → excluded by issue 4 from ever being re-queued.

**Comparing size between scans does not catch this.** Nothing about those 3 files changes.

Agreed design: fingerprint the **top-level item's whole subtree** as
`(file_count, total_bytes, max_mtime)` and require it unchanged across 2 consecutive
scans. Top-level granularity matches auto-queue's granularity and §4.7's notion of an
item; children inherit their root's verdict.

- `remote_mtime` is **already captured** (`find -printf '%T@'`), persisted, and published
  to the frontend — and read by nothing. This is a decision to make, not plumbing to build.
- **mtime alone is unreliable**: rsync/scp/torrent clients preserve or preset it, and a
  directory's own mtime only moves on entry add/remove, never on a child growing.
- UI: keep the item `REMOTE_ONLY` and set **`substate = 'settling'`**. The `substate`
  column exists in `001_initial_schema.sql:86` and is used **nowhere** in the codebase —
  free, already migrated, and avoids touching the state `CHECK` constraint or §9.2's
  three-word visible vocabulary. Needs adding to `ITEM_VIEW_COLUMNS`.
- **A scan carrying a partial-scan warning must not advance the settle counter.** GNU
  `find` exits nonzero on one unreadable subdirectory and still prints what it found; two
  consecutive partial scans returning the same truncated subset would otherwise read as
  settled and be exactly wrong.
- A flat 2-scan rule costs the atomic hardlink path 30–60s of latency it does not need.
  Accepted: predictable beats clever, and it is nothing against a multi-GB transfer.

Open decisions: persist the fingerprint (a migration, and required by the invariant that
nothing publishes a state it did not read back from `item`) vs. in-memory; whether the
gate holds back **completion** as well as queueing (it must, to fix the directory case);
whether a manual Queue click overrides it (recommended: yes, with the completion and
delete gates still in force).

### 6 — `state_changed_at`

The user's ask: one timestamp meaning "when did this row last change state", whatever the
state. Cleaner than per-state columns, and the label comes free from the state.

`item.state` is written from three modules — `engine.py._persist` (two upserts),
`core/queue.py`, and `core/postprocess.py`. Requiring every writer to stamp a timestamp
guarantees one gets missed. **Enforce it in the schema instead:**

```sql
ALTER TABLE item ADD COLUMN state_changed_at TEXT;

CREATE TRIGGER item_state_changed_at
AFTER UPDATE OF state ON item
WHEN NEW.state IS NOT OLD.state
BEGIN
  UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
```

Fires on the `ON CONFLICT DO UPDATE` branch of `_persist`'s upserts, which is where most
transitions land. Only fires on an actual change, so the 30s rescan rewriting the same
state is a no-op. The trigger's own `UPDATE` touches a different column, so it cannot
re-enter. New rows need `DEFAULT (STRFTIME(...))` for the first-sighted `REMOTE_ONLY`
case. Backfill existing rows from
`COALESCE(extracted_at, verified_at, downloaded_at, first_seen_at)` and label it in the
migration as the approximation it is.

Frontend: `Intl.RelativeTimeFormat` (built in, no new dependency), absolute local time on
hover. `FileTree.tsx` is virtualized — use **one** shared ticker at page level, never a
`setInterval` per row.

**Retention (7) must still key on `downloaded_at`, not `state_changed_at`** — "when did it
complete" and "when did it last move" are different questions, and a `DOWNLOADED` item
that dips to `PARTIAL` and back would otherwise earn a fresh retention lease.

### 7 + 8 — the deletion cluster (one prompt, not two)

Second irreversible-delete feature in the codebase, and the first that touches the user's
own data. Build to the bar `move` mode already set: two-layer opt-in, an audit event on
every delete **and every withhold**, and a UI confirmation.

**They share a primitive.** One `delete_local(item)`; retention calls it on a schedule,
the Files button calls it on a click.

**Where they must differ: the `nlink > 1` guard.** Correct default for retention (a robot
deleting unattended should refuse when it cannot prove another copy exists — and if the
user's `*arr` hardlinks out of the download directory, `nlink > 1` makes deletion
provably non-destructive). Wrong for manual delete: `LOCAL_ONLY` junk with exactly one
link is precisely what you are trying to remove. **A guard that is right for the robot is
wrong for the human.**

**Coupled to issue 4, and the coupling is a trap.** Retention works *today* only because
`REMOVED_LOCAL` is excluded from `ELIGIBLE_STATES`. Fix 4 without handling this and
retention re-downloads everything it just deleted on a 30-second loop. Both deletion paths
must set `auto_queue_suppressed = 1` with a new `suppressed_reason` — reusing §4.6's
existing mechanism. That needs a migration: the column's `CHECK` allows only
`('user_stopped', 'retries_exhausted', 'permanent_error')`.

Other required guards: mount-sentinel gated like auto-queue; never touch an item with an
active job or one in `PostprocessPipeline.in_flight_item_ids()` (the live-worker check,
not the state string); resolve the target and **assert containment within the queue's
`local_path`** before any unlink — a `LOCAL_ONLY` directory can be a symlink, and `rm -rf`
through one pointing outside the download root is the worst outcome this feature has.

**Delete files, keep `item` rows.** Row lifetime is a genuinely separate open question and
History needs the rows; after the file is gone the path is in neither tree, so
`_project`'s `rel_paths` filter drops it and `diff_nodes` publishes it as `removed`
without anyone deleting anything. Set `REMOVED_BOTH` on the way out.

**Explicitly out of scope: "Delete remote."** Phase 9's gap list names both, but the only
remote deletion today is `move` mode's verification-gated pipeline, and a manual remote
delete button is a much larger safety conversation.

Needs from the user: does the `*arr` hardlink, copy, or move out of the local downloads
directory? Determines whether `nlink > 1` is a near-free safety net or a guard that never
fires. Buildable without the answer (retention defaults off), but it shapes the default.

### 11 — per-queue scan interval

The user asked for a 10/30/60/none refresh dropdown on the Files page. **The Files page
does not poll** — it renders off one WebSocket. Measured cadences:

| Change | Cadence |
|---|---|
| DOWNLOADING progress (bytes/speed/ETA) | ~1s — `transfer_tick_s = 1.0`, `config.py:42` |
| Lifecycle transitions | immediate, pushed on transition |
| Anything the scanner discovers | up to 30s — `scan_interval_s = 30.0`, `config.py:33` |

So a client-side refresh interval would change nothing; the bottleneck is server-side scan
cadence. `POST /api/files/rescan` and a "Rescan now" button already exist. What is missing
is the interval being **configurable per queue** — currently one global.

A 10s option means an SSH `find` over the whole remote tree every 10 seconds; on a shared
seedbox that is real load. Warn on it, and confirm an overrunning scan cannot stack.
