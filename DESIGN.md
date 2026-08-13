# lftpweb — Design Document

*A containerized web interface for keeping a local directory in sync with a seedbox, using
lftp as the transfer engine.*

Sections are numbered so feedback can be given by reference (e.g. "§4.3 — no, do it this way").

**Status:** all nine build phases are built (§13), plus the post-phase-9 corrections real use
surfaced. Still pre-release — nothing has been tagged, and the first version will be **`0.0.1`**.
This line said "nothing implemented yet" until 2026-08-13; §13 is the authoritative record of
what is actually built and what is still open.

---

## 1. Context

### 1.1 What we're building

A web app that:
- Browses the remote seedbox tree and the local tree as **one unified view**
- Shows what's remote-only, queued, downloading (with live speed/ETA), partial, or complete
- Lets you queue/stop/delete items, individually or in bulk
- Auto-queues new remote items matching patterns
- Optionally extracts archives, verifies checksums, and moves finished items from a staging
  directory to a final destination
- Exposes lftp's advanced tuning knobs rather than hiding them

Constraints: **SSH + SFTP** to the seedbox; **Python/FastAPI + React**; **containerized**;
v1 = **core sync + post-processing**; auth **optional** (some users sit behind
Authelia/Tailscale and want it off).

### 1.2 Prior art: what SeedSync teaches us

lftpweb is a fresh codebase. **No SeedSync code is used or adapted** — not a file, not a
function. Its source and issue history were read as research, and every reference to it in this
document exists to justify a design choice, overwhelmingly a choice to do the thing
*differently*. Where the two are compared (§2.1), it is comparison, not lineage.

[SeedSync](https://github.com/nitrobass24/seedsync) works and is the right reference. But
reading its source and issue history surfaces one structural weakness that is worth designing
away rather than reproducing, because everything else in the app sits downstream of it.

SeedSync drives **one long-lived interactive lftp process per path-pair over a pexpect PTY**,
and reconstructs *all* transfer state by polling `jobs -v` every 0.5 s and regex-parsing
lftp's human-readable verbose output. That parser is roughly 15 interlocking regexes plus an
order-dependent line dispatcher, and it must cope with:

- ANSI/bracketed-paste escapes injected by readline (upstream #117)
- PTY line wrapping when the terminal width isn't honored — despite `COLUMNS=10000` and
  `setwinsize(24, 10000)`, Unraid and some SSH configs override it (fork #253, #260, #290)
- lftp's inconsistent progress grammar: `` `f' at 2976 (12%) 997b/s eta:22s [Receiving data] ``
  vs `` `f', got 13733 of 25165824 (0%) 4.0K/s eta:1h45m `` — where in the first form the
  number is *not* the local size and the percentage is *not* the local percentage
- Headers that vanish while lftp is "connecting" or "getting file list"
- A pget header with no data line swallowing the next job's header (fork #555)

The fork's maintainer summarized it in issue #294:

> "The lftp `jobs -v` parser is the most fragile part of the codebase… We've fixed these
> reactively (#253, #258, #260, #290, #293) but the root cause remains."

The resolution recorded on that issue was **"Do nothing for now"** — mitigated by a catch-all
that skips unrecognized lines and a counter that tolerates 10 consecutive parse failures.

Three consequences follow from the shared-process design, and they're the ones that actually
bite a user:

1. **Shared blast radius.** One parse failure or one pexpect timeout degrades *every*
   transfer on that pair, because they share one process and one job-id namespace.
2. **A kill race.** Stopping a job means `kill <id>`, but ids can shift between the `status()`
   read and the `kill` write. The source acknowledges this and concludes "there's nothing we
   can do about it." There is — don't share a process.
3. **Bad directory ETAs.** lftp never emits an ETA on a mirror header, so directory ETA had
   to be back-computed from filesystem scans lagging 10–30 s (fork #68).

### 1.3 The core idea of this rewrite

> **lftp is a transfer engine, not a status API.**
>
> Derive progress from the filesystem — local bytes on disk versus known remote size — and
> use lftp purely to move bytes.

This single decision cascades:

- The `jobs -v` parser disappears entirely. So does the whole class of bugs above.
- Each transfer becomes an independently supervisable OS process: liveness is an exit code,
  stopping is a SIGTERM to one PID, and a failure is contained.
- Per-file progress inside a directory transfer gets *better*, not worse — we see every file
  in the tree, not just the handful lftp happens to mention.
- ETA becomes ours to compute and smooth, uniformly, for files and directories alike.

The cost is honest and small: we must understand two lftp on-disk conventions (§4.4). Both
are short, stable, machine-oriented formats — unlike the verbose output, which is explicitly
formatted for humans and has never been a stable interface.

---

## 2. System architecture

Single container, single Python process (uvicorn + the asyncio engine share one event loop),
SQLite for state, React SPA served as static files from the same origin.

```
  browser
     │  REST (mutations, config)  +  one WebSocket (live model deltas)
     ▼
  FastAPI  ── api/{files,jobs,settings,auth,health}.py, api/ws.py
     │
     ▼
  Engine (core/engine.py) — owns the loop, holds the current model, publishes events
     │
     ├── RemoteScanner   (core/remote.py)      asyncssh → remote tree; also remote deletes
     ├── LocalScanner    (core/local_scan.py)  os.scandir → local tree
     ├── ProgressSampler (core/progress.py)    ~1 Hz stat of active files only
     ├── Metrics         (core/metrics.py)     ~30 s throughput samples + liveness heartbeat
     ├── Scheduler       (core/scheduler.py)   admission control: who starts, at what rate
     ├── TransferQueue   (core/queue.py)       the job queue: one lftp process per job
     ├── AutoQueue       (core/autoqueue.py)   pattern matching on newly-seen remote items
     ├── PostProcessor   (core/postprocess.py) verify → extract → move
     ├── Backup          (core/backup.py)      scheduled + pre-migration VACUUM INTO
     └── Reconciler      (core/reconcile.py)   merges all of the above into one tree
                                                        │
                                              EventBus → WebSocket
```

### 2.1 Departures from SeedSync, at a glance

| Concern | SeedSync | lftpweb |
|---|---|---|
| lftp process model | 1 long-lived PTY per pair, shared `queue` | 1 short-lived process **per job**, plain pipes — no PTY, no readline, no ANSI, no wrapping |
| Progress source | regex over `jobs -v` | local sizes vs remote sizes, sampled ~1 Hz |
| Job liveness | inferred from diffing the job list | process exit code |
| Stopping a job | `kill <id>`, with an id race | SIGTERM to one PID |
| Job queue & concurrency | delegated to lftp `cmd:queue-parallel` | owned in Python (asyncio semaphore) → reorder, priority, per-job settings |
| Remote scan | scp `scan_fs.py`, md5-compare, shell detection, pexpect `ssh` | one `find -printf` over asyncssh; script fallback |
| SSH layer | pexpect typing passwords at prompts | asyncssh (keys, agent, password, known_hosts) |
| Persistence | JSON persist files | SQLite — history, restart-safe, auditable |
| Failure blast radius | whole pair stalls | one job fails; others continue |

### 2.2 What is published is the persisted state, never the structural one

`core/reconcile.py` produces a *candidate* reading of every node — its **structural state**,
computed from remote-vs-local bytes alone. That reading is never what a client is shown,
because several rules override it: an active job's lifecycle state (§4.6), a post-processing
outcome (§6), §7.3's grace period, the settle gate (§3.3). So the scan pass runs in a fixed
order, and that order is the invariant:

> **reconcile → persist → read back → diff → publish.**

The `item` table is the single authority for item state; the in-memory model is a cache *of*
it. **Nothing publishes a value it did not read back from the row it just wrote**, and one
shared projection serves the WebSocket delta, the connect-time snapshot, and `GET /api/files`
alike, so those three cannot disagree with each other or with the database.

The cheaper alternative — have the persist step hand its arbitrated states back and patch them
onto the in-memory nodes before diffing — was rejected. It leaves two places computing what an
item's state is, kept in agreement by remembering to, which is precisely how the wire came to
publish `REMOTE_ONLY` (Queue button and all) for items the database recorded as
`REMOVED_LOCAL`.

Two consequences worth knowing:

- The reconciler's field is named `structural_state`, not `state`. Asking for the structural
  reading requires naming it; publishing it directly is the bug the name exists to make
  visible.
- The published node set is the *current scan's* nodes, not every persisted row. Nothing ever
  deletes an `item` row (§3.2 rule 6), so an unfiltered projection would resurrect rows that
  have left both trees and would make "removed" impossible to detect. `GET /api/files` returns
  every persisted row for a queue and is therefore the wider of the two views; that difference
  is a question about row lifetime, not about who owns `state`.

---

## 3. Data model

### 3.1 SQLite schema (`/config/lftpweb.db`)

```sql
-- Global settings live as typed key/value so the UI can edit anything
setting(key TEXT PK, value JSON, updated_at)

-- The seedbox, configured once. v1 has exactly one row and no multi-host UI, but the host is
-- a record with a stable id rather than fields inlined into `setting`, so a second seedbox is
-- a schema addition instead of a migration of every queue.
host(
  id, name, address, port,
  protocol TEXT,                     -- 'sftp' in v1
  username, auth_method TEXT,        -- 'key' | 'agent' | 'password'
  key_path NULL, password_enc NULL, known_hosts_policy,
  ssh_key_enc NULL,                  -- migration 014: a pasted key, encrypted like password_enc.
                                      -- Additional to key_path, not a replacement -- see §8.
  connection_overrides JSON,         -- net:connection-limit, net:socket-buffer, timeouts, retry
  created_at)

-- A named path queue: one remote path → one local path, plus the policy for that mapping.
-- Shown in the UI simply as a "Queue" with the user's own name ("TV", "Movies", "Music").
path_queue(
  id, host_id, name, remote_path, local_path, staging_path NULL, enabled,
  sync_mode TEXT,                    -- 'copy' | 'move' | 'sync'   (default 'copy', §7)
  auto_queue_enabled, auto_extract, auto_verify,
  scan_interval_s REAL NULL,         -- NULL = site default, 0 = on-demand only (§5)
  created_at)
-- Note: no bandwidth / concurrency / parallelism columns. Those are site-level (§4.5) —
-- a queue governs what and where, never how fast.

-- Three kinds, doing three different jobs (§4.7). Conflating them is the classic mistake:
--   'select'       — item name; which releases auto-queue picks up
--   'skip'         — item name; releases auto-queue never picks up
--   'file_exclude' — paths inside an item; files never transferred (lftp --exclude-glob)
-- queue_id NULL = applies to every queue.
pattern(id, queue_id NULL, kind TEXT,
        expr TEXT, enabled, created_at)

-- One row per item we have ever cared about; the durable lifecycle record
item(
  id, queue_id, rel_path, is_dir,
  remote_size, local_size, remote_mtime, local_mtime,   -- both mtimes files-only (§9.2)
  state TEXT, substate TEXT NULL,    -- substate carries 'settling' (§3.3); NULL otherwise
  first_seen_at, downloaded_at NULL, extracted_at NULL, verified_at NULL,
  state_changed_at NULL,             -- stamped by a trigger, not by each writer (§3.2 rule 9)
  first_missing_at NULL,             -- when local absence was first observed → grace period (§7)
  remote_deleted_at NULL,            -- when we deleted the remote copy, if we did
  auto_queue_suppressed BOOL,        -- set on user stop/dequeue and on exhausted retries (§4.6)
  suppressed_reason TEXT NULL,       -- 'user_stopped' | 'retries_exhausted' | 'permanent_error'
                                     --   | 'deleted_local'  (§3.2 rule 3)
  error_class NULL, error_detail NULL,
  UNIQUE(queue_id, rel_path))

-- Settle-gate bookkeeping (§3.3): one row per top-level item being watched, so a restart
-- doesn't lose an in-progress verdict and so nothing has to publish a state it can't read back.
-- first_observed_at/last_changed_at (migration 013) are NULL on any row that predates them and
-- hasn't been rewritten since -- "how long have we watched this" / "when did it last move" are
-- unknown for such a row, not fabricated as 1970 or "just now".
item_settle(queue_id, rel_path, file_count, total_bytes, max_mtime NULL, matched_scans,
            updated_at, first_observed_at NULL, last_changed_at NULL,
            PRIMARY KEY(queue_id, rel_path))

-- Archive files this codebase deleted after a successful extraction (§6). Read by the scan
-- pass and folded into the same completeness predicate §4.7's file_exclude patterns feed, so
-- a removed volume reads EXCLUDED rather than missing. Never garbage-collected.
deleted_archive(queue_id, rel_path, deleted_at, PRIMARY KEY(queue_id, rel_path))

-- Throughput history (§10.4). Two tables, so "idle" and "down" stay distinguishable
metric_sample(id, queue_id, ts, bytes_delta)   -- only when that queue moved bytes in a window
metric_heartbeat(id, ts)                       -- one row per sample tick, unconditionally

-- One row per transfer attempt — this is the audit trail SeedSync lacks
job(
  id, item_id, kind TEXT,           -- 'mirror' | 'pget'
  state TEXT,                        -- queued|running|succeeded|failed|cancelled
  lane TEXT,                         -- 'main' | 'small'   (§4.5 fast lane)
  rank REAL, attempt INT,            -- sortable rank; order is rank DESC, queued_at ASC
  queued_at,
  pid NULL, argv JSON, lftp_settings JSON,
  bytes_start, bytes_done, bytes_total,
  rate_limit_bps NULL,               -- the allocation this process was spawned with (§4.5)
  forced_full_rate BOOL,             -- admitted via "start now at max bandwidth"
  started_at, finished_at, exit_code NULL,
  error_class NULL, output_tail TEXT NULL)

event(id, ts, level, item_id NULL, job_id NULL, kind, message)
```

Rationale: SeedSync keeps five parallel name-sets in JSON (`downloaded`, `extracted`,
`extract_failed`, `validated`, `corrupt`). Collapsing those into one `item` row with a state
column plus timestamps removes a category of "sets disagree with each other" bugs and gives
the UI a real history view for free.

Three notes on the shape:

- **Connection details live on `host`, not on the queue.** Address, port, protocol,
  credentials, and connection tuning are set once. Speed and concurrency are site-level too
  (§4.5). Only what legitimately varies per path is per-queue: `sync_mode`, auto-queue,
  patterns, post-processing toggles, and staging path. §9.3 lists which knob sits where.
- **`rank` is a sortable real, not an integer priority level.** Default ordering is
  oldest-first (`rank DESC, queued_at ASC` with equal ranks); **Move to top** assigns a rank
  above every queued row. Storing a sortable value means arbitrary drag-reorder is a later UI
  change rather than a schema migration.
- **`sync_mode` subsumes the old `auto_delete_remote` boolean.** There is exactly one switch
  governing whether we touch the remote, with three values, not a mode plus an overlapping
  flag. `copy` is the default and never deletes anything remote.
- **Deletes are audited in `event`.** Every propagated remote delete writes an `event` row
  (`kind = 'remote_delete'`) naming the item, the queue, the mode, the verification result that
  gated it, and the outcome. A remote delete is irreversible; the minimum bar is being able to
  reconstruct exactly what was deleted and why (§7).

### 3.2 File states

```
REMOTE_ONLY    remote exists, nothing local
QUEUED         accepted into the job queue, no process yet
DOWNLOADING    an lftp process is running for it
PARTIAL        local < remote (or incomplete children), no active job — re-queueable
STOPPED        user stopped or dequeued it; partial data kept. Never auto-restarted (§4.6)
DOWNLOADED     complete
EXCLUDED       matched a file-exclude pattern; deliberately not transferred, not "missing" (§4.7)
VERIFYING / VERIFIED / CORRUPT
EXTRACTING / EXTRACTED / EXTRACT_FAILED
FAILED         retries exhausted or a permanent error; carries error_class + output_tail
LOCAL_ONLY     present locally, absent remotely, never tracked
REMOVED_LOCAL  was downloaded, now absent locally, remote still present
REMOVED_BOTH   deliberately removed by us — terminal, kept as history. See below.
```

`DELETED_REMOTE` used to cover the first of those two. It was ambiguous the moment remote
deletion became real: "gone locally" and "gone from both sides" are now distinct states with
distinct consequences, and a name that could be read either way is a bug waiting to happen.

**`REMOVED_BOTH` is deliberately broader than its name.** Its original meaning is "was
downloaded, absent locally, remote deleted by us" — a `move`-mode delete. It is *also* the
state lftpweb writes when it deletes only the **local** copy and never touches the remote: a
manual delete from Files, or the local-retention sweep, on a `copy`-mode queue. That is not
literally "both", and a `LOCALLY_DELETED` state was considered. It was rejected because
`REMOVED_BOTH` is already the one terminal "this row is finished, never act on it again" state
— excluded from auto-queue by construction, frozen against rescan, and legible in History as
"something deliberate happened here" — and a second state meaning the same thing to every
consumer would have bought a more accurate name at the cost of a `CHECK`-constraint migration
and a new branch everywhere the vocabulary is handled. What actually distinguishes the two
cases is `remote_deleted_at` (set only when we removed the remote copy) and the `event` row
that records every delete.

**A third reading, narrower still, added 2026-08-13.** `core/mount_sentinel.py.
resolve_vanished` also writes `REMOVED_BOTH` for a `PARTIAL`/`LOCAL_ONLY` row that leaves both
trees with no scan ever getting the chance to say anything else about it (rule 9's last bullet,
§7.3) — the safety net for a stale reading the throttled child-progress writer can otherwise
leave behind on a `move` queue. Unlike the two readings above, this one is **not** a delete
this codebase performed: `auto_queue_suppressed` is left clear and no `event` row is written,
because there was nothing to audit. The state text is the same "nothing here to compare, don't
invent a story" signal either way; only the suppression (and therefore auto-queue eligibility)
differs, which is exactly why `core/local_delete.py.reconsider_removed_state` treats a
`resolve_vanished`-produced `REMOVED_BOTH` no differently from a self-delete one when content
later returns — see that function's own docstring.

Nine rules that are easy to get wrong and that want review:

1. A **directory** is `DOWNLOADED` only when every non-directory descendant that has a remote
   size **and is not `EXCLUDED`** is itself complete. Otherwise `PARTIAL`. The exclusion clause
   is load-bearing — see rule 8.
   **A directory with no remote files under it at all is a different case and is not
   vacuously done.** "Every child excluded" and "no children" both leave nothing to compare,
   but they mean opposite things: the first is a filtered release that really is complete
   (rule 8), the second is an empty remote directory that has simply never been mirrored. So
   the two are told apart by counting remote files *before* the exclusion predicate runs — no
   remote files anywhere beneath it ⇒ `REMOTE_ONLY` until the directory exists locally, then
   `DOWNLOADED`. This must **not** be keyed on local presence instead: an all-excluded
   directory legitimately has no local presence either (rule 8), so keying on that would flip
   the load-bearing case back into the permanent-`PARTIAL` re-queue loop it exists to prevent.
2. `local_size < remote_size` with **no active job** ⇒ `PARTIAL`, never `DOWNLOADED`. This is
   what makes a stopped transfer resumable rather than silently "done".
3. Previously downloaded, now absent locally, still present remotely ⇒ `REMOVED_LOCAL`, once
   §7.3's grace period has elapsed. **There are exactly two ways a local copy goes away, and
   auto-queue must treat them oppositely.** Every delete lftpweb performs itself — a manual
   delete from Files, the retention sweep — writes `REMOVED_BOTH` *and* sets
   `auto_queue_suppressed` (`suppressed_reason = 'deleted_local'`) in the same write, so it is
   excluded from auto-queue for the same reason a `STOPPED` item is (rule 7, §4.6), under every
   setting, with no way to switch it back on — that is not a behaviour anyone should be able to
   enable. A bare `REMOVED_LOCAL`, by contrast, only ever means something *outside* lftpweb
   removed it — a human, or an importer moving a finished release into the media library
   (§7.2) — and by default it is **excluded from auto-queue** exactly like `REMOVED_BOTH`,
   not eligible again: on a `copy`-mode queue with auto-queue on, the remote copy is never
   touched, so an item an importer just moved out still matches its select pattern and would
   be fetched again on the very next scan, re-imported, and repeat forever — a live loop, not a
   theoretical one, for the common shape of a `copy`-mode queue feeding an `*arr` importer on
   one schedule while a separate script prunes the seedbox on another. `core/autoqueue.py.
   AutoQueueSettings.re_download_externally_removed` (site-level, default `False`) is the
   opt-in for anyone who genuinely wants that case re-fetched; it governs only the
   externally-removed path; it can never make a self-delete (`REMOVED_BOTH`) eligible. This
   is in practice a `copy`-mode concern — in `move` the remote copy is already gone by the time
   an item could read bare `REMOVED_LOCAL`, so there is nothing left for the setting to
   re-fetch, and (closed 2026-08-13, see below) it now genuinely reaches `REMOVED_BOTH` instead,
   the same as any other self-contained "gone from both trees" row.

   > **Gap closed 2026-08-13** (`prompts/2026-08-13-vanished-rows-should-leave-the-tree.md`;
   > previously recorded here as a known gap). This rule used to end "(it reaches `REMOVED_BOTH`
   > instead)" while `core/mount_sentinel.py.resolve_absence` — the only thing that ever writes
   > this transition — always wrote the literal `REMOVED_LOCAL`, taking neither `sync_mode` nor
   > `remote_deleted_at` as input, so a fully-completed `move`-mode item actually landed on bare,
   > **unsuppressed** `REMOVED_LOCAL`. `resolve_absence` itself is unchanged — its real call site
   > (an item genuinely reading structural `REMOTE_ONLY`, remote present) still means exactly
   > what it always did. The fix lives at its *other* call site instead: §7.3's leaves-both-trees
   > sweep (`core/engine.py._persist`) already fakes a `REMOTE_ONLY` reading there purely to reuse
   > `resolve_absence`'s grace-period clock ("the closest existing reading for 'there is nothing
   > here to compare'") for a `rel_path` it already knows is in *neither* tree — so that call site
   > now remaps a resolved `REMOVED_LOCAL` to `REMOVED_BOTH` itself before writing it, since only
   > it knows the remote is genuinely gone too. Left **unsuppressed**, deliberately — the same
   > choice `resolve_vanished`'s own `REMOVED_BOTH` output already made (rule 9 below): nothing
   > here asserts *who* removed the remote copy, and `REMOVED_BOTH` is excluded from
   > `core/autoqueue.py.ELIGIBLE_STATES` by state name, not by `auto_queue_suppressed`, so no flag
   > is needed to keep it out of auto-queue. `re_download_externally_removed` is therefore now
   > genuinely a no-op for `move` queues, as this rule's prose always claimed.
4. **Remote size is a moving target** — a torrent may still be downloading on the seedbox.
   Never latch it; recompute completeness on every scan.
5. **The same detection drives different actions by mode** (§7). `REMOVED_LOCAL` is one
   observation — "we downloaded this and it is no longer here" — and the queue's `sync_mode`
   decides what it means. In `copy` it means *no remote action, ever* — whether the item is
   re-queued locally is rule 3's question, not this one.
   In `sync` it is additionally the trigger to propagate the delete to the remote, subject to
   every rail in §7.3. `move` never reaches `REMOVED_LOCAL` for this purpose; it deletes on
   verified completion instead.
6. `REMOVED_BOTH` is terminal and is **never** re-queued or re-scanned into `REMOTE_ONLY`. If
   the same `rel_path` reappears remotely later it is a genuinely new item as far as the
   lifecycle is concerned; the old row stays as history.
7. **`STOPPED` and `FAILED` are never picked up by auto-queue** (§4.6). They carry
   `auto_queue_suppressed`, cleared only by a deliberate user re-queue. Without this rule the
   retry policy is decorative: auto-queue would restart a stopped job on its next pass.
8. **`EXCLUDED` is not "missing".** A file skipped by a file-exclude pattern (§4.7) is remote,
   absent locally, and *supposed* to be. The reconciler must evaluate the same patterns lftp
   is given, or every filtered directory stays `PARTIAL` forever — never `DOWNLOADED`, never
   post-processed, never deleted in `move` mode, and re-queued on every pass.
   The same holds one level up: a directory whose children are **all** excluded is vacuously
   `DOWNLOADED`, and its local directory may legitimately not exist, because lftp does not
   create a directory it has nothing to put in. Completeness must not require it. (An empty
   remote directory is the *other* reading of the same arithmetic and does not get this
   treatment — rule 1.)
9. **Three modules write `item.state`, and this rule says who wins.** The scan pass
   (`reconcile` → persist) recomputes a structural reading every ~30 s; the transfer queue
   writes the job lifecycle (`QUEUED`/`DOWNLOADING`/`DOWNLOADED`/`STOPPED`/`FAILED`);
   post-processing writes the six §6 states. Without a stated precedence the periodic rescan
   silently erases the other two — which is exactly what happened for four phases: a verified,
   extracted release read as plain `DOWNLOADED` again within a scan interval, and `CORRUPT`
   and `EXTRACT_FAILED` deleted themselves before anyone could look at them. The rule is
   **precedence with an explicit domain, never blanket stickiness** — a state that can only
   ever be protected is a state that can never be un-stuck:

   - **An item with a live claim on it is not recomputed at all — with one narrow exception.**
     A `queued`/`running` job, an item a post-processing worker (or, since 2026-08-13, a
     `core/local_delete.py.delete_local()` call — see §7.4) is *currently inside*, or a
     suppressed row (`STOPPED`, `FAILED`, a row we deleted ourselves — rule 3) keeps its state;
     only sizes and mtimes are refreshed. Note what the live-worker cases key on: the **live
     worker's existence**, never a string like `VERIFYING`/`EXTRACTING`/`substate = 'removing'`.
     A worker killed mid-extract (or mid-delete) leaves that string behind with nothing running,
     and a protection keyed on the string could never let go of it. The exception: a row this
     codebase itself marked `REMOVED_LOCAL`/`REMOVED_BOTH` (`suppressed_reason = 'deleted_local'`)
     may still have its `state` text corrected — never its suppression — when a fresh scan
     proves the removal claim half-false (content came back on one side; `core/local_delete.py.
     reconsider_removed_state` is the rule). This is not a second recomputation path: it fires
     only for those two states, so a `STOPPED`/`FAILED` row's protection stays exactly as
     absolute as before.
   - **Content present and complete** (structural `DOWNLOADED`): the post-processing outcome
     wins. `VERIFIED`/`CORRUPT`/`EXTRACTED`/`EXTRACT_FAILED` are *refinements* of `DOWNLOADED`
     — each says something about an all-bytes-present item that the byte comparison cannot.
   - **Content present, remote gone because we deleted it** (structural `LOCAL_ONLY`, *and*
     `remote_deleted_at` set): the outcome wins here too. This is the same refinement argument
     as the bullet above — the bytes are all here — reached from the other side: the scan after
     a `move`-mode queue's verified remote delete sees "remote absent, local present", which
     `core/reconcile.py` reads as `LOCAL_ONLY` with no way to know why. `remote_deleted_at` is
     the column that tells "we removed the remote copy on purpose, after verifying" from a
     genuinely untracked local file, and it is deliberately the *only* thing that opens this
     branch: gating on `LOCAL_ONLY` alone would hand the protection to any local-only file that
     happens to carry an outcome-shaped `state`. Found the first time `move` ran end to end
     against a real release — it downloaded, verified, deleted the remote, extracted, and then
     read `LOCAL_ONLY` within one scan interval, losing everything §6 had just recorded.
   - **Content partially present** (structural `PARTIAL`): the structural state wins and the
     outcome is dropped. Rule 2 is absolute, and an outcome is a stronger claim still; the item
     is genuinely re-queueable again.
   - **Content absent** (structural `REMOTE_ONLY`): §7.3's grace period decides, and all six
     post-processing states ride it exactly the way `DOWNLOADED` does — the item holds its
     outcome for the whole window and then lands on `REMOVED_LOCAL`. This half is not
     politeness: without it a `VERIFIED` item whose importer moved it out would persist as a
     fresh `REMOTE_ONLY` and be downloaded all over again.
   - **Content absent from *both* trees** — the path is in neither the remote scan nor the
     local one, so the reconciler produces no node for it at all. §7.3 covers what happens
     then. Every `prev_state` §7.3's grace-period function (`resolve_absence`) has an opinion
     about goes through the same grace period as if it were freshly absent — the point of
     that half — but its terminal output is remapped from `REMOVED_LOCAL` to `REMOVED_BOTH`
     before it is written, since this call site (unlike `resolve_absence`'s ordinary one) already
     knows the remote copy is gone too; see rule 3's gap note above for why. **A second, narrower
     fallback exists for two `prev_state`s `resolve_absence` has no opinion about at all**
     (2026-08-13, a real bug — see below): `PARTIAL` and `LOCAL_ONLY` both assert *some*
     concrete content was actually here, so a row that leaves both trees carrying either one
     rests at `REMOVED_BOTH` rather than being frozen forever, because the scan loop only
     visits nodes and a row nothing ever visits again is a row frozen on its last reading
     forever. `REMOTE_ONLY` (nothing was ever fetched) and `EXCLUDED` (never going to be, on
     purpose) are deliberately left out of that fallback and keep the older "simply drops from
     the published tree" behavior — see `core/mount_sentinel.py.resolve_vanished`'s own
     docstring for why widening it to cover them too would be a regression, not a fix.

     **Written every pass regardless, published only until terminal (2026-08-13,
     `prompts/2026-08-13-vanished-rows-should-leave-the-tree.md`).** This whole mechanism exists
     so a row that has left both trees is never *frozen* — `core/engine.py._persist` writes a
     fresh resolved state for it on every pass, without exception, so the History page (which
     reads `item` directly) always has the truth. That is a different question from whether the
     row still belongs in the *live* Files tree: once it lands on `REMOVED_LOCAL`/`REMOVED_BOTH`
     with nothing left in either tree, it is "kept as history" (the state table above, rule 6) —
     which means exactly that, not "kept in the tree forever." It stops being handed to
     `_project` the pass it turns terminal, `diff_nodes` reports it `removed` exactly once, and
     `GET /api/files`/History still see it because they read `item` directly, same as the
     write above. While it holds a non-terminal, content-asserting state during the grace window
     (any of the states rule 9's second bullet lists), it keeps publishing — the content could
     still come back, and hiding it early would be worse than the freeze this section fixes.

     **Found 2026-08-13** (`prompts/2026-08-13-delete-state-truthfulness.md`): a `move` queue's
     throttled per-child progress writer (`core/queue.py._publish_child_progress`) can leave a
     small file's row reading a mid-transfer `PARTIAL` — true for a fraction of a second — right
     as its job reaps, and post-processing can relocate the whole release out of both trees
     before any scan gets a chance to correct it. Before the fallback above existed, such a row
     was frozen at `PARTIAL` forever, and a rescan could not fix it: no fresh structural reading
     exists for a path in neither tree to overwrite it with. `core/queue.py._reap_one` also now
     flushes a final, un-throttled, accurate child reading the instant a job reaps successfully
     — the fix that stops the stale reading from forming in the first place; the fallback above
     is the safety net for whenever one forms anyway (a crash between two throttled writes,
     say).

   `state_changed_at` is stamped by a database trigger on an actual change of value, not by
   each of the three writers remembering to. Cross-cutting discipline over three modules is
   how a timestamp ends up silently wrong, and a wrong timestamp is worse than none — nothing
   downstream can tell the two apart.

### 3.3 The settle gate

The seedbox may still be writing an item when a scan observes it. Completeness is a
remote-vs-local byte comparison (§3.2 rule 1), and that comparison genuinely cannot tell a
finished item from one still arriving: if a release is uploading 8 files and a scan catches 3,
each of those 3 individually whole, the rollup reads the **directory** as `DOWNLOADED`. That is
not a boundary race, it is the normal outcome of uploading a multi-file release, and comparing
scan N against scan N−1 does not catch it either — nothing about those 3 files ever changes
again.

A single growing *file* self-heals: it is queued, lftp pulls a prefix, the next scan reads
`PARTIAL`, and it resumes. Wasteful, not corrupting. A directory does not self-heal —
post-processing runs on the half release, `move` relocates it, an importer takes 3 of 8 files,
and the stragglers arrive to find the local copy gone.

**The fingerprint.** Per top-level item (§4.7's granularity), over its whole remote subtree:
`(file_count, total_bytes, max_mtime)`, required to hold across **two consecutive scans**.
Each of the three closes a gap the others leave open — a new file changes the count, a growing
file changes the bytes, and the newest write changes the mtime even when a file happens to
arrive at its final size in one go. Neither signal alone is enough: mtime alone is unreliable
because rsync/scp/torrent clients routinely preserve or preset source mtimes and a directory's
own mtime never moves when a child merely grows in place; size alone *is* the bug above,
restated as a fix.

**Two gates, and both are needed.**

- **Eligibility** (§4.7): auto-queue skips an unsettled item and picks it up on a later pass.
  This is the cheap half and prevents most of the damage.
- **Completion**: an unsettled item must never reach `DOWNLOADED`. This half is what covers a
  manually-queued item, and an item that settles, gets queued, and then grows again while the
  job runs — a `mirror` job transfers whatever was visible when it started and can exit 0
  having moved every file it was asked for while the item itself is not done. Such an item is
  held at `REMOTE_ONLY` with `substate = 'settling'`, its suppression cleared so a later pass
  or a user click resumes it, and **post-processing is not triggered by that job's success**.
  An `event` row records the hold, so a job that visibly succeeded without completing anything
  is not a mystery. A **third, smaller consequence of this same gate** (§6): the scan pass
  that eventually finds the remote genuinely quiet and releases the hold to `DOWNLOADED` is
  itself a second, narrow post-processing trigger — so a job-originated hold does not depend on
  auto-queue or a manual click to ever get post-processed; it can also clear entirely on its
  own, the next time the scan pass runs.

**A manual Queue click overrides the eligibility gate and never the completion gate.** Explicit
user action beats a heuristic, so a click queues immediately; the completion check applies
regardless of how the job got queued. The worst case of clicking Queue on a settling item is a
wasted partial transfer that later resumes — never a bad import, never a bad delete.

**A partial scan holds the counter rather than resetting or advancing it.** GNU `find` exits
nonzero the moment one subdirectory is unreadable and still prints everything it did reach
(§5), so two consecutive partial scans can return an identical truncated subset — which would
read as "settled" under a naive recount. Holding is the honest reading of "no evidence anything
changed, only that this pass could not see all of it."

**The countdown's denominator stays fixed at 2 scans, and so does the counter's own arithmetic
— the display gets a second signal alongside it instead** (2026-08-13,
`prompts/2026-08-13-settle-progress-visibility.md`). A live copy onto the seedbox pinned the
Files page's countdown at "1 of 2" for the whole transfer, because every scan found the
fingerprint still growing, which resets `matched_scans` back to 1 every time — indistinguishable
from "already confirmed unchanged once," which also reads 1 for one scan. A climbing denominator
("2 of 3", "3 of 4"…) was proposed and rejected: the requirement genuinely is not growing, it is
always 2 consecutive unchanged scans, and a growing denominator would say something false about
what's actually being waited for. A same-shaped fix — starting `matched_scans` at 0 instead of 1
specifically when a fingerprint differs from a previous record, so the Files page could tell the
two cases apart by the persisted counter alone — was tried and reverted: it silently required 3
observations of the same fingerprint to settle instead of 2 for any item that had ever changed
once, the identical false-denominator problem relocated into the numerator, and
`tests/test_settle_gate_e2e.py`'s real fake-seedbox reproductions caught it. `matched_scans` is
therefore untouched by this task. Two new persisted timestamps (migration 013,
`item_settle.first_observed_at`/`last_changed_at`) carry the distinguishing signal instead:
`last_changed_at` moves to "now" on the same scan a fresh sighting or a changed fingerprint
resets the counter, and holds on every scan that merely confirms it — so the Files page reads
`matched_scans == 1` **and** a `last_changed_at` from the fingerprint's own most recent write as
"still arriving," and only switches to the ordinary countdown once a confirming scan has
actually landed (§9.2). `REQUIRED_SETTLE_SCANS` and `is_settled`'s threshold are both completely
unchanged — this task altered no settle *timing*, only what the Files page shows while a
`matched_scans == 1` row is waiting for its first confirmation.

The verdict is persisted (`item_settle`, §3.1) rather than counted in memory: it must survive a
restart, and — decisively — `substate = 'settling'` goes out over the WebSocket, so it has to
be read back from a table like everything else (§2.2).

**On by default — settling requires both a scan count *and* a wall-clock floor.** Two
independently load-bearing conditions, `SettleSettings.enabled` and `core/settle.py`'s own
constants: `REQUIRED_SETTLE_SCANS` (2) consecutive matching scans, **and** at least
`SETTLE_MIN_AGE_S` (60.0) seconds of wall-clock time since the current matching streak began.
The count alone cannot tell a fast-settling item from a slow-polling one that simply hasn't
been rescanned enough times yet; the floor alone cannot tell "genuinely unchanged" from "haven't
looked in a while" — a queue that gets disabled mid-settle and never rescanned again is not
"settled" just because a clock ran out with nobody checking. The floor exists because the
counter's whole meaning assumes every queue shares one scan interval — true today (one global
30 s value) but not guaranteed once a per-queue interval lands — and 60 s is the exact number
already documented everywhere in this project as what the gate costs at today's default, so a
30 s queue's worst case is unchanged by the floor while a faster future queue is held to the
same guarantee rather than quietly given a weaker one.

This is the **third reasoned exception** to this project's "every new capability ships off"
rule, after `move`-mode verification (§6) and the phase 7 scheduled backup (§10.2). Shipped off
originally; flipped once real use confirmed non-atomic remote copies (plain copies, and
cross-device moves that are copies in disguise) are a real path on this deployment's own
setup, at which point "off by default" stopped being the safe choice — it is the fix for a
real, confirmed-live directory-corruption bug, not a latency preference. **Existing installs
upgrading into this default will see transfers complete up to about a minute later than
before** — stated plainly (CHANGELOG.md's `### Changed` entry), not left for someone to notice.
Still switchable off (`GET`/`PUT /api/settings/settle`, Settings → Transfer) for anyone whose
seedbox landing path is atomic end to end and would rather not pay the delay at all.

---

## 4. The transfer engine

### 4.1 One process per job

Each queued item becomes exactly one subprocess, spawned with `asyncio.create_subprocess_exec`,
**stdin/stdout/stderr as pipes** (never a PTY):

```bash
lftp -c "
  source /run/lftpweb/job-<id>.rc ;      # settings + credentials, mode 0600, deleted after
  set cmd:fail-exit true ;
  open sftp://<host> ;
  mirror -c --parallel=<N> --use-pget-n=<M> [--exclude-glob '<pat>' ...] '<REMOTE>' '<LOCAL>/'
"
```

Single files use `pget -c '<REMOTE>' -o '<LOCAL>/'` instead. `-c` (continue) on both makes
every restart resumable.

Why pipes matter: with stdin not a tty, lftp disables readline. The bracketed-paste escapes
(upstream #117) and column-driven line wrapping (fork #260, #290) that break SeedSync's
parser **cannot occur**. We aren't relying on that for correctness anymore, but it also keeps
the captured error text clean.

### 4.2 Credentials

- **SSH key preferred**: `set sftp:connect-program "ssh -a -x -i /config/keys/id_ed25519"`.
- **Password**: written into the per-job rc file (mode 0600, `/run` tmpfs, unlinked on exit),
  never in argv — argv is readable by any process in the container.
- Anything that might echo a URL goes through a redactor before logging or storage:
  `scheme://user:pass@host` → `scheme://user:***@host`.

### 4.3 Success, failure, retry

- **Success is exit code 0**, guaranteed by `set cmd:fail-exit true`. No inference needed.
- On nonzero, classify the captured output into `AUTH_FAILED`, `HOST_UNREACHABLE`,
  `TLS_ERROR`, `PERMISSION_DENIED`, `DISK_FULL`, `REMOTE_GONE`, `UNKNOWN`, and store the last
  ~4 KB on the `job` row so the UI can show *why* rather than a red dot.
- **Retry only on transient classes**, with exponential backoff, bounded by `max_attempts`
  (default 3): `HOST_UNREACHABLE`, `TLS_ERROR`, timeouts, connection resets.
- **Never retry permanent classes.** `AUTH_FAILED`, `PERMISSION_DENIED`, `REMOTE_GONE`, and
  `DISK_FULL` surface immediately rather than hammering the seedbox or the disk.
- Attempts exhausted, or a permanent class ⇒ `FAILED`, **and no further automatic attempt from
  any path** (§4.6).
- Concurrency: the **job queue** is ours, so reordering, priority, and pause-all are trivial.
  ("Job queue" throughout this document means the pending-transfer queue owned by
  `core/queue.py`. A user-facing **queue** is a named path queue — see §3.1 and §9.)

### 4.4 Progress without parsing

`ProgressSampler` ticks at ~1 Hz and stats **only the active file set** — the files under
currently-running jobs — never a full tree walk. From that it computes transferred bytes,
instantaneous speed, and ETA, with EMA smoothing (α ≈ 0.3) so the UI doesn't jitter.

Two on-disk conventions must be honored, because raw `st_size` lies. Both are **lftp's**, not
SeedSync's — any program that lets lftp move the bytes inherits them, whatever inspired the
program:

**(a) `<file>.lftp-pget-status` sidecars.** With pget or `--use-pget-n`, lftp writes chunks
into a sparse file, so `st_size` reports the full allocation immediately. The sidecar records
what's actually filled:

```
size=<total>
0.pos=<n>          # next byte to write for chunk 0
0.limit=<n>        # end of chunk 0
1.pos=... 1.limit=...
```

Effective size = `size − Σ(limit − pos)`. Small, stable, machine-oriented format.

**(b) `xfer:use-temp-file` with `xfer:temp-file-name "*.lftp"`.** In-flight files carry a
`.lftp` suffix; strip it when matching a local file against its remote counterpart, and when
the final name doesn't exist yet, look for `<name>.lftp`.

If lftp ever changed either format, progress degrades to "raw size, still monotonic" — it
never crashes and never lies about completion, because completion is the exit code.

### 4.5 Bandwidth and concurrency

Bandwidth is not one number, it's a three-level hierarchy — and the constraint that actually
bites in practice isn't bandwidth at all, it's the connection count.

```
global cap                                              10 MB/s
├─ job 1  (one queued item, usually a directory)         cap 5 MB/s
│   ├─ mirror --parallel=4        → 4 files at once
│   └─ mirror --use-pget-n=4      → 4 connections per file   = 16 connections
└─ job 2                                                 cap 5 MB/s
                                                          = 32 connections worst case
```

A **job** is one queued item — one lftp process (§4.1). On a seedbox that item is nearly always
a release directory, so "2 concurrent streams" means *2 directories in flight*, not 2 files.
`--parallel` and `--use-pget-n` are the knobs *inside* a directory job: they multiply the
connections beneath a single process rather than adding a concurrency tier of their own, which
is why they sit at the job level in the diagram.

**Per-job caps compose, because `net:limit-total-rate` is process-wide.** It bounds the sum
across every connection that lftp process opens, so one number per job bounds the entire
subtree beneath it no matter how many files it runs in parallel or how many pget chunks each
file splits into. This is the knob we use. `net:limit-rate` — the *per-connection* limit — does
**not** compose: a 5 MB/s per-connection cap on a job running 16 connections is an 80 MB/s job.
We do not use `net:limit-rate` for global shaping, and the Settings UI should not present it as
if it were interchangeable.

#### The invariant

> **A running job's allocation is fixed at spawn and never re-shaped.**

`lftp -c` exits with its transfer and gives us no control channel, so we cannot retune a job
that is already running. Rather than working around that, the scheduler is designed so the
limitation never arises: bandwidth is handed out at **admission** time and held for the job's
lifetime. The missing control channel stops being a constraint and becomes the design.

#### Site-level settings

One container serves one site (one seedbox), so these are global to the instance — **not**
per-queue. A queue governs *what* and *where*, never *how fast*.

| Setting | Meaning | Default |
|---|---|---|
| `max_bandwidth` (B) | ceiling across everything | — |
| `max_concurrent_transfers` (N) | main-lane slots | — |
| `small_item_threshold` | fast-lane cutoff, by item's total remote size | 10 MB |
| `small_lane_concurrency` | fast-lane slots | 2 |
| `small_lane_reserve` | bandwidth carved out for the fast lane | 10% of B, min 1 MB/s, **capped at B/2** |
| `min_share_floor` | refuse to admit below this | ~500 KB/s |
| `net:connection-limit` | hard cap on concurrent connections | — |

#### Main-lane admission

Every scheduling pass:

```
slots    = N − running_count
ready    = min(slots, queue_depth)
headroom = B − small_lane_reserve − Σ(allocations of running jobs)

if ready == 0 or headroom <= 0:   admit nothing
share = headroom / ready
while share < min_share_floor and ready > 1:   ready--;  share = headroom / ready
admit the `ready` highest-priority items, each spawned with
    net:limit-total-rate = share,  fixed for its lifetime
```

The floor loop is what produces "run fewer, faster" rather than admitting eight transfers at
250 KB/s apiece.

Worked, with N=2 and B=10 MB/s:

| Situation | Result |
|---|---|
| 5 items queued at once, nothing running | 2 admitted at **5 MB/s** each; 3 wait |
| 1 item arrives alone | admitted at the **full 10 MB/s** |
| a 2nd arrives while that one holds all 10 | headroom is 0 → it **waits** for the running job to finish |
| one running at 5 (its partner finished), a new item arrives | starts at **5**; the running job stays at 5 |
| a job finishes, 3 still queued | headroom 5, ready 1 → next by priority starts at **5**, and so on |

Note the steady state: once you are in the "two at half" regime you stay there until the queue
drains. You do not drift back to one-at-full while work is waiting.

#### Queue order and priority

Ordering is `priority DESC, queued_at ASC` — so the **default is oldest-first**, and priority is
invisible until someone uses it. The one action exposed is **Move to top**, which sets that
item's rank above everything currently queued.

Store the rank as a sortable value rather than an integer level, so full drag-reorder becomes a
UI addition later instead of a schema migration.

#### The fast lane

An item whose total remote size (per the last remote scan) is under `small_item_threshold` runs
in a separate lane: its own concurrency cap, sharing `small_lane_reserve` between its active
jobs. It consumes **no** main-lane slot and does **not** enter the headroom calculation.

This exists to prevent one specific absurdity — **head-of-line blocking**. A 3 MB `.nfo`
arriving while a 40 GB release holds the entire ceiling would otherwise sit through an hour of
headroom-is-zero to move a file it could have finished, alongside, in under a second.

The reserve is carved off B rather than left unmetered, so the total stays bounded by B. The
cost is that the slice sits idle when no small items are running.

**The `B/2` cap on the reserve is load-bearing, not defensive.** The "min 1 MB/s" floor is
unconditional, so without the cap any ceiling at or below 1 MB/s yields a reserve ≥ B, hence
`headroom ≤ 0`, hence the main lane admits nothing — **ever**. Jobs queue and sit there with no
error and no log line. Found in build phase 3a by setting a 400 KB/s cap; the fast lane exists
to stop small items being blocked, and must never be able to block everything else instead.
When the scheduler admits nothing while work is waiting, it logs the arithmetic that produced
that decision, so this class of fault is visible rather than silent. Letting the fast lane run
unmetered was considered and rejected: queue 300 small files and it saturates the uplink at its
concurrency cap, starving the rate-limited main lane and blowing past the ceiling exactly when
the ceiling matters.

#### Start now at max bandwidth

A per-item action that admits immediately with allocation = the full B, **deliberately
oversubscribing** past the ceiling. While `Σ allocations > B − small_lane_reserve` the scheduler
admits nothing new; normal admission resumes once enough jobs finish to bring the total back
under. This is intentional, not a leak in the model: it is the "I want this one now" escape
hatch, and it pays for itself by freezing new work rather than by throttling what is running.

#### Residual inefficiency, stated plainly

Because allocations are never re-shaped, a job admitted at B/2 keeps B/2 after its partner
finishes and the queue empties — half the pipe idle with nothing to claim it. That is the
accepted cost of not needing a control channel.

The **Phase 3 experiment** (§4.1) is precisely what would close this gap: hold the lftp
process's stdin open as a pipe, start the transfer with `&` so the command loop stays live, and
write `set net:limit-total-rate <n>` mid-flight to retune a running transfer. **Unverified** —
it must be tested against a real running transfer before any part of the design leans on it.
The two ideas compose cleanly: admission control is the correct v1 design, live retune is an
optimization layered on top, and nothing has to be rebuilt to adopt it.

**The connection ceiling is the constraint that actually bites.** 2 jobs × 4 parallel × 4 pget
chunks = **32 concurrent SFTP sessions**. Plenty of seedboxes cap well below that and start
refusing connections, which surfaces as mysterious per-job failures rather than as "you asked
for too much". Therefore:

- `net:connection-limit` is a **first-class setting**, host-level, not an advanced afterthought.
- The Settings UI **computes and displays the worst-case concurrent connection count live** as
  the user moves the jobs / parallel / pget-n sliders (§9.3). These three numbers multiply, and
  they multiply silently — nothing in lftp's own output tells you that you just asked for 32
  sessions.
- `--parallel` and `--use-pget-n` are **site-level** too. They were originally sketched as
  per-queue knobs so `tv/` (many small files) and `movies/` (few large files) could be tuned
  differently — but they multiply into the same host-wide connection ceiling, so letting each
  queue raise them independently makes that ceiling unenforceable. One site, one set of
  transfer knobs.

### 4.6 Stopping, and what never restarts by itself

**Stopping is a user action, and it is terminal.** It is never a pause, and it never leads to
an automatic retry.

- **Running job:** SIGTERM to that one PID (§4.1). Not SIGKILL — SIGTERM lets lftp flush its
  `.lftp-pget-status` sidecar so the partial is resumable (§4.4). SIGKILL only after a grace
  timeout, ~10 s.
- **Queued but not started:** it is simply removed from the job queue.
- Either way the item lands in `STOPPED`, **partial data is kept**, and re-queueing later
  resumes via `-c` rather than restarting.

**The rule that makes this mean anything:**

> A stopped item, and an item whose retries are exhausted, carry `auto_queue_suppressed`.
> **Auto-queue skips them.** Only a deliberate user re-queue clears the flag.

Without it the retry policy in §4.3 is decorative. Auto-queue runs on a scan cadence; a stopped
job would match its pattern again on the next pass and restart 30 seconds later, forever —
which is both an infinite retry loop wearing a different hat and a UI that ignores the user.
The same applies to `FAILED`: a permanent `AUTH_FAILED` must not be retried every 30 s because
auto-queue never learned about the failure.

Manual re-queue always wins. It clears suppression, resets `attempt`, and puts the item back in
the job queue — that is the intended way to say "try again", and it is explicit.

**The same flag is what makes a delete stick, permanently, regardless of settings.** When
lftpweb removes an item's local copy itself — a manual delete from Files, or the retention
sweep — it sets `auto_queue_suppressed` with `suppressed_reason = 'deleted_local'` in the same
write that marks the row terminal (`REMOVED_BOTH`, never bare `REMOVED_LOCAL`). §3.2 rule 3's
`AutoQueueSettings.re_download_externally_removed` toggle only ever changes whether a bare
`REMOVED_LOCAL` — nothing lftpweb touched — is eligible; the flag is what tells "we removed it"
from "it went away" apart, and the first is never re-fetched, under either value of that
setting.

### 4.7 What enters the queue: manual add, auto-queue, and patterns

Two intake paths.

**Manual.** Select one or more items in Files and queue them. Always available, always wins:
it clears `auto_queue_suppressed` (§4.6) and ignores every pattern. An explicit user action is
never second-guessed by a filter.

**Auto-queue.** Per path queue, off by default, evaluated against every eligible top-level
remote item on each scan. Eligible means `REMOTE_ONLY` or `PARTIAL`, with `auto_queue_suppressed`
clear — so `STOPPED`, `FAILED`, `REMOVED_BOTH`, and anything already complete or in flight are
all excluded, `STOPPED`/`FAILED`/`REMOVED_BOTH` by the flag rather than by their state name
(§3.2 rules 3 and 7). `REMOVED_LOCAL` is excluded too by default — not by the flag, since a
plain `REMOVED_LOCAL` usually carries it clear, but because the state itself is left out of the
eligible set; `AutoQueueSettings.re_download_externally_removed` (site-level, default `False`,
§3.2 rule 3) is the opt-in that adds it back in, for anyone who wants an item something outside
lftpweb removed to be re-fetched. It additionally skips an item whose remote fingerprint has not
settled (§3.3), leaving it for a later pass. Before evaluating anything at all for a queue, the
queue's local root must pass the mount gate (§7.3); if it
does not, the whole pass is skipped for that queue and the reason is surfaced, rather than each
item being judged against a tree that isn't mounted.

#### Three pattern kinds, doing three different jobs

Conflating these is the classic mistake, so they are separate lists with separate semantics:

| Kind | Matches against | Effect | Enforced by |
|---|---|---|---|
| **select** | the item name (the release dir or file) | which items auto-queue picks up | us, in `core/autoqueue.py` |
| **skip** | the item name | items auto-queue never picks up | us, in `core/autoqueue.py` |
| **file_exclude** | paths *inside* an item | files never transferred at all | lftp, via `--exclude-glob` |

`*.nfo` is a **file_exclude** — you still want the release, minus the junk. `*SAMPLE*` is
usually a **skip** — you don't want the item at all. `*` as a select means "everything", which
is the same as having no select patterns with *patterns-only* off.

#### What counts as an item

An **item** is a top-level entry of a queue's `remote_path` — **either a directory or a loose
file**. Nothing deeper is an item; deeper paths are children of one.

| Item | Transferred with | `select` / `skip` match against |
|---|---|---|
| `Some.Release.S03E04/` | `mirror -c` | `Some.Release.S03E04` |
| `Movie.2024.1080p.mkv` | `pget -c` | `Movie.2024.1080p.mkv` |

So `*.mkv` as a **select** does exactly what you'd expect for a loose `.mkv` sitting in the
root — it matches, and the file is queued as a `pget`. What it does *not* do is match
`Movie.2024/` that happens to contain an mkv, because a directory is matched on its own name.
That's the only place the intuition breaks, and it breaks in one direction: item patterns see
item names, never contents.

If the intent is "out of each release, pull only the video", that's a `file_exclude` list
(exclude everything you don't want), not a select.

#### `file_exclude` also applies to loose top-level files

A queue whose root holds both `Some.Release/` and a stray `notes.nfo` would otherwise treat
`*.nfo` inconsistently: suppressed inside the release, downloaded at the root. So when the item
being evaluated is a **file**, both `skip` *and* `file_exclude` are tested against its name.

The pattern means "I don't want files like this." Where the file happens to sit shouldn't change
the answer, and requiring the user to enter `*.nfo` twice — once as a skip, once as a
file_exclude — is a trap rather than a feature.

#### Directories with nothing left in them

If every child of a directory is excluded, two things follow that the reconciler has to get
right:

- **It is vacuously `DOWNLOADED`**, not `PARTIAL`. There is nothing outstanding to fetch.
- **The local directory may not exist at all**, because lftp does not create a directory it has
  nothing to put in. So completeness must not require the local directory to be present when
  all of its expected children are `EXCLUDED`.

This is the same class of bug as §3.2 rule 8, one level up: an absence that is intended must not
be read as an absence that is missing.

#### Matching semantics

- **Case-insensitive**, always.
- **Glob (`fnmatch`) when the pattern contains `*`, `?`, or `[`; plain substring otherwise.**
  So `1080p` matches `Some.Show.1080p.WEB` without demanding `*1080p*`, while `*.nfo` behaves
  strictly as a glob. SeedSync tries *substring OR glob* on every pattern, which is friendlier
  still but ambiguous the moment a pattern contains a metacharacter — this rule is the same
  convenience without the ambiguity.
- **Skip beats select.** Evaluated after, and it wins.
- **No select patterns** ⇒ everything matches, unless *patterns-only* is on for that queue, in
  which case nothing does.
- **Retroactive.** Adding a pattern re-evaluates the whole known model, not just future scans —
  otherwise "add a pattern" silently means "add a pattern, for things I haven't seen yet."
- Patterns are per-queue, or global with `queue_id NULL` (§3.1).

#### File excludes must reach the reconciler, not just lftp

This is the one that bites, and it is not obvious.

`--exclude-glob '*.nfo'` means those files never arrive. But completeness (§3.2 rule 1) is
computed by comparing every remote child against local — so an excluded `.nfo` is remote,
locally absent, and counted as missing. The directory is then **permanently `PARTIAL`**: never
`DOWNLOADED`, never verified, never extracted, never deleted under `move`, and re-queued on
every single pass. One exclude pattern would quietly break the whole pipeline for every item it
touches.

So: **one pattern evaluator, used in two places.** The same compiled file_exclude set that
builds the lftp command line is applied by the reconciler when it decides what an item is
*supposed* to contain. Excluded children are marked `EXCLUDED` and do not count toward
completeness.

Two consequences worth knowing:

- **Changing file_excludes retroactively changes completeness.** Tightening them can make a
  `DOWNLOADED` item incomplete; loosening them can complete a `PARTIAL` one. That is correct
  behavior, but it should be surfaced in the pattern preview rather than discovered.
- **`EXCLUDED` is a real state, not an absence** (§3.2 rule 8), so the UI can show *why* a file
  isn't there — which is the difference between a filter working and a transfer being broken.

#### Preview

The Settings → Queues pattern editor runs the live evaluator against the current remote tree
and shows, for the queue being edited: which items would be **selected**, which **skipped**, and
within a sampled item, which files would be **excluded**. Patterns are the feature most likely
to be got subtly wrong, and the only cheap fix is showing the answer before it is saved.


---

## 5. Remote scanning

`core/remote.py`, over **asyncssh** with a pooled, reused connection.

**Primary path — one command, one round trip, nothing deployed:**

```bash
find <path> -mindepth 1 -printf '%y\t%s\t%T@\t%p\n'
```

Directory sizes are summed locally from children. Compare to SeedSync, which scp's a
`scan_fs.py` to the seedbox, md5-compares it on every run to decide whether to reinstall,
probes for a usable shell via sftp when the account has no login shell, resolves `~`, and
falls back to `~/scan_fs.py` on permission denied — all through pexpect-driven `ssh`/`scp`
with passwords typed at prompts. Most of that machinery exists to support the deployed
script; deleting the script deletes the machinery.

**Fallback** for non-GNU `find` (BSD/busybox seedboxes): upload a stdlib-only `scandir`
script over SFTP and run it, emitting the same records. Kept small and only used on demand.

**Cadence:** every 30 s by default, plus a forced rescan after any queue/stop/delete and on
job completion. Local full walk every 10 s; the 1 Hz active-set poll (§4.4) covers the hot set
in between. The scan interval is also the settle gate's unit of time (§3.3 counts *scans*, not
seconds), so changing it changes how long an arriving item is held — which is why §3.3's gate
also enforces its own wall-clock floor (`SETTLE_MIN_AGE_S`) independent of scan count, so a
faster queue (below) is held to the same real-time guarantee rather than a weaker one.

**Overridable per queue, as of `prompts/done/2026-08-12-per-queue-scan-interval.md`
(migration 009, `path_queue.scan_interval_s`).** `NULL` (every queue predating this feature)
means "use the site-wide default above"; `0` means on-demand only — never scanned on a timer,
reachable only via a forced pass (`request_rescan()`: "Rescan now," a config change, *Test
connection* succeeding); any positive number is a literal per-queue interval in seconds, with
10 / 30 / 60 / none offered as the Settings → Queues dropdown (values outside that set are
still accepted from a direct API call). The engine loop (`core/engine.py`) tracks one next-due
time per queue and wakes at the earliest of them, scanning only the queues that are actually
due — not every queue on the fastest one's cadence. A single serial `asyncio.Task` runs the
whole loop, so an overrunning scan (a real risk at 10 s against a slow shared seedbox: a scan
is an SSH round trip running `find` over the entire remote tree) can never stack a second,
concurrent scan of the same queue — its own next-due is scheduled from its completion, not its
scheduled start, so an overrun costs that queue a longer effective interval rather than a
pile-up.

Every pass — success or failure — announces its own completion on the WebSocket, rather than
leaving a client to infer it from the next tree update. A failed pass produces no fresh tree,
so a UI waiting on "the next update" after a scan that errored would wait forever; that is the
coverage gap the dedicated completion message exists to close.

**This component also owns remote deletion.** Every remote delete — user-initiated from the
Files view, or propagated by `move` / `sync` mode (§7) — goes out over this same asyncssh
connection, gated on verification, and is never delegated to lftp's
`mirror --Remove-source-files`. The Settings → *Test connection* button uses it too, so there
is exactly one code path for "can we reach the seedbox" and exactly one for "remove something
from it". §7.4 gives the reasoning for keeping deletion out of lftp.

---

## 6. Post-processing

`core/postprocess.py`, triggered on transition to `DOWNLOADED`, executed in a thread pool,
one item at a time by default (configurable). Each step is independently toggleable globally
and per path queue. As of 2026-08-13
(`prompts/2026-08-13-postprocess-inherit-or-override.md`) the per-queue half is
**inherit-or-override**, not an AND: a queue's own toggle is `NULL` by default, meaning
"whatever the site-wide setting says," and only an explicit `on`/`off` from that queue makes it
diverge. The AND it replaced could only ever narrow "on" toward "off" — flipping a queue's
checkbox on while the site-wide flag was off did nothing, silently, with no way for that one
queue to actually mean it. A fresh install still post-processes nothing before anyone has
visited a settings page, because every site-wide flag itself still defaults off and every
queue starts out inheriting it.

1. **Verify** — use `.sfv` / `.md5` sidecars when present; otherwise the optional hash-on-disk
   fallback. Result: `VERIFIED`, `CORRUPT`, or `SKIPPED` — "no evidence either way" is a third
   outcome, not a quiet success (§7.3 spells out what the fallback does and does not prove).
2. **Extract** — zip / 7z / tar / gz / bz2 / xz via `7zz`, rar / rar5 via `unrar` (§11 records
   why that is two binaries and not the one this document claimed for nine phases). Multi-part
   rar sets extract from the first volume only. Optional password list. Target: in place, or a
   configured directory. An item containing no archives at all is `SKIPPED`, and its state is
   left exactly as verification left it — claiming `EXTRACTED` (with an `extracted_at`) for
   work that never happened both lies and overwrites a real `VERIFIED`.
3. **Move** — staging → final destination. `os.rename` fast path, cross-device copy+fsync+
   unlink fallback. This is the "download to NVMe, settle on the array" workflow.

Failures are recorded on the item (`EXTRACT_FAILED`, `CORRUPT`) and never abort the pipeline
for other items.

**Two call sites, both narrow.** The job-success transition in `core/queue.py._reap_one` —
post-processing fires from the point where a transfer job exits 0 for a top-level item, not
from a scan that happens to compute `DOWNLOADED`. And `core/engine.py._persist`, but only for
an item its own settle-gate bookkeeping (§3.3) just released straight from
`REMOTE_ONLY`/`substate='settling'` to `DOWNLOADED` with no fresh job in between — the fix for
a real bug: a job can finish while its item is still unsettled, get held back, and with
auto-queue off and nobody re-clicking, never reach a job-success trigger at all. Both call
sites fire on the identical precondition (state about to become `DOWNLOADED`, no
post-processing outcome yet). This is still not a general scan-driven trigger — a pre-existing
local file reading `DOWNLOADED` on its first-ever scan, with no gate hold behind it, triggers
nothing, exactly as before this fix. **The remaining consequence is named rather than papered
over:** an item that
becomes complete with no job involved *and* no settle-gate hold behind it — files placed by
hand, or a restore — is never verified, extracted, or moved until something re-touches it. An
item the settle gate genuinely held (the second call site's whole reason to exist) no longer
has this problem.

**Extraction stages off to the side and merges into position on success.** Archives extract
into an `_UNPACK_<name>` directory created as a **sibling** of the final directory — never a
child, which would sit inside the tree the reconciler walks and inside anything a later move
relocates — and are merged into place only once every archive under the item has extracted
cleanly. A failure renames that directory to `_FAILED_<name>` and leaves it as evidence. Both
prefixes are filtered out of the local scan, at any depth, so lftpweb's own bookkeeping never
reconciles into a `LOCAL_ONLY` node. This is the same guarantee `xfer:use-temp-file` already
gives downloads (§4.4b), applied to the one step that lacked it: an importer watching the tree
must see nothing, then a complete release — never a growing, importable-looking file. A merge
that would overwrite existing content fails loudly rather than clobbering it.

`_FAILED_` directories are kept indefinitely by default; a sweep that removes them after a
configurable age exists and, like every other new capability, ships off. Unattended deletion is
the last place to grant an exception for "the containment check is solid."

**Deleting the archives after a successful extraction is a separate, off-by-default option**
(`PostprocessSettings.delete_archives_after_extract`, resolved against a queue's own
`auto_delete_archives` column via the inherit-or-override rule above — migration 012,
2026-08-13 — the same two-layer shape as verify/extract/move above; it shipped site-only in
migration 010 and was the odd one out until that fix). When the resolved value is on at both
layers, every file belonging to each extracted archive — including a multi-volume rar's
continuation volumes, not just the head — is removed once extraction reports `EXTRACTED`;
nothing is removed on `EXTRACT_FAILED` or a precondition failure, and non-archive files
(`.nfo`, `.sfv`/`.md5`, samples, subtitles) are never touched. It only ever acts on a
**directory** item — a loose top-level archive file is left alone with a withheld-cleanup event,
since removing its one file would be removing the whole item, which is the local-delete
primitive's job and not this one's. Settings → Queues shows, next to every per-queue
post-processing toggle (not only this one), whether it is currently inheriting the site-wide
value or has been explicitly overridden for that queue, and what either resolves to — archive
cleanup is the most destructive of the four to get wrong silently (§7 below: on a `move` queue
it can be the last copy of an archive's compressed bytes anywhere).

The naive version of this feature is an infinite loop, and avoiding it is the whole design.
Deleting the archives drops the item's local byte total below its remote total, which reads
`PARTIAL` on the next scan (rule 2) and outranks the `EXTRACTED` outcome (rule 9) — so
auto-queue re-fetches, re-extracts, and re-deletes it every scan interval, forever. The fix is
to reuse §4.7's existing completeness seam rather than add a second completeness rule: every
file removed this way is recorded, and the reconciler folds that record into the *same*
predicate a `file_exclude` pattern already feeds. A deleted archive therefore reads `EXCLUDED`
— a real state, deliberately absent — exactly like a pattern-excluded file, and rule 8's
vacuously-`DOWNLOADED` reading applies to it with no new branch anywhere. `auto_queue_suppressed`
was considered for this and rejected: suppression writes an item off *entirely* (§4.6), which
would also block a legitimate future re-fetch, whereas the exclusion only ever affects
completeness accounting for the specific bytes that were removed.

On a `move` queue the remote copy is already gone by the time cleanup runs, so the archive
volumes it removes are the last copy of those *compressed* bytes anywhere. That is accepted
rather than gated further: a successful extraction has already decoded the payload onto disk as
ordinary files, nothing re-extracts an item that already reads `EXTRACTED`, and so there is no
future read of those bytes to protect. It does sharpen the ordering risk named below, though —
the disk this frees is concentrated on exactly the items where getting extraction right the
first time already matters most.

**Extraction is gated on cheap, filesystem-only preconditions before any extractor is
invoked** — a zero-length head volume, and a multi-part rar set with a gap in it (both the
`.rNN` and `.partNN.rar` conventions, resolved through one shared volume map so the two cannot
disagree). The point is a named reason ("volume 3 of 15 missing") instead of the extractor's
own "cannot open the file as archive", and a gate that exists at all on a `copy`-mode queue
with verification off — the default, which previously had nothing checking completeness beyond
a stale size rollup. Two limits stated plainly: a wholly *absent* final volume cannot be
detected this way (there is no filename evidence of the true volume count without opening the
archive), and these checks are deliberately not a remote-vs-local byte comparison — that is
the settle gate's job (§3.3), one layer earlier. A precondition failure never creates an `_UNPACK_`/`_FAILED_` directory, because
nothing was attempted and a staging directory implying otherwise would be the same dishonesty
the `SKIPPED` outcome exists to remove.

**Ordering, including the one ordering that is not yet a decision.** The pipeline runs verify →
(`move` mode's remote delete) → extract → staging move. The delete sits where it does because
verification is its gate and verification has just run; nothing in this document has ever
argued for its position relative to *extraction*, and the consequence is real: a `move`-mode
item whose archive fails to extract has already lost its remote copy — `EXTRACT_FAILED` with
nothing left to re-fetch from. Recorded as a known, unreasoned ordering rather than presented
as a design.

**Verification stops being optional garnish whenever `sync_mode != copy`.** In `copy` mode a
`CORRUPT` result is an annoyance you can re-download from. In `move` and `sync` it is the gate
on an irreversible remote delete: verification is the only thing standing between a truncated
download and permanently losing the only good copy. Consequences:

- For a queue in `move` or `sync` mode, `auto_verify` is forced on and cannot be turned off in
  the UI. It is not a per-queue preference in those modes; it is part of the mode — stored as
  an explicit per-queue override (never left on "inherit"), so a later change to the site-wide
  verify flag can't silently turn it back off for that queue.
- A queue in `move` or `sync` whose items carry no usable verification evidence — no `.sfv` or
  `.md5` sidecar and hash-on-disk disabled — must say so loudly in Settings, because "verified"
  would otherwise silently mean "we compared the size and hoped".
- `CORRUPT`, or verification that never ran, means **no delete**. The item stays, the remote
  stays, and the event log records why the delete was withheld.

---

## 7. Sync modes

Every path queue (§3.1) carries a `sync_mode`. It is the single switch governing whether
lftpweb ever touches the remote side.

| Mode | Behavior | Ships |
|---|---|---|
| `copy` | Download; **never** touch the remote. Local deletes do not propagate. **Default.** | v1 |
| `move` | Download, verify, then delete the remote copy. | v1 |
| `sync` | Copy, plus propagate *local* deletes back to the remote. | **not scheduled** |

`copy` is the default and is the only mode that is safe under every deployment shape. The other
two perform an irreversible operation on a machine we don't own, so the rest of this section is
mostly about why that is acceptable here and what keeps it acceptable.

> **`sync` is deferred, not scheduled.** lftpweb ships `copy` and `move`. Backwards
> delete-propagation is a **possible later feature, to be built only if it turns out to be
> wanted** — it is not on the roadmap and no phase depends on it.
>
> This section stays anyway, for two reasons. First, the seam is v1 work regardless: the state
> model (§3.2), the `event` audit trail (§3.1), and the deletion mechanism (§7.4) all exist to
> serve `move`, and `sync` would plug into them rather than requiring a redesign. Second, the
> safety reasoning in §7.1–7.3 is the durable part — it is what a future session needs in order
> to decide whether to build this at all, and reconstructing it later from scratch is exactly
> how an irreversible feature ships with the wrong rails.
>
> Deferred with it: the mount sentinel, the grace period and `item.first_missing_at`, dry-run
> mode, and the rate-based backstop (§7.3). **Not deferred:** `move` deletes remote data too, so
> verification before deletion, deletes through our own asyncssh path (§7.4), and the full
> `event` audit trail are all v1.

### 7.1 Why remote deletion is safe here: the hardlink pickup directory

The deployment this is designed for looks like this:

```
torrent client  ──seeds──▶  /data/torrents/<release>/…      (the seeding data, untouched)
      │
      └──hardlinks on completion──▶  /data/pickup/<release>/…   ← lftpweb's remote_path
                                             │
                                    lftpweb downloads, verifies, deletes *this* copy
```

The torrent client hardlinks completed files into a separate pickup directory, and lftpweb's
`remote_path` points at the **pickup directory**, never at the torrent data directory. Unlinking
a file in the pickup dir drops one link; the seeding torrent keeps its own, so the data — and
the seed, and the ratio — survive untouched.

This dissolves the usual objection to deleting the source ("you'll break the seed and cost
yourself tracker ratio") **for this deployment shape**, and two things follow directly:

- **No torrent-client integration is needed for delete safety, and none is planned.** There is
  nothing to ask qBittorrent or rTorrent about: the hardlink already encodes the answer.
- **No minimum-file-age gate.** File age is a poor proxy for "safe to delete" — it neither
  proves seeding finished nor proves the download completed — and here it would gate an
  operation that is already safe. It would add friction and buy nothing. Don't add one.

> **⚠ Misconfiguration warning — this must appear in the doc *and* in the Settings UI next to
> the mode selector.** If you point a queue's `remote_path` at a live torrent **data**
> directory instead of a hardlink pickup directory, `move` and `sync` **will destroy your
> seeding torrents**. The safety argument above is a property of the *directory you point at*,
> not of lftpweb. Anyone running a setup without a hardlink pickup dir should stay on `copy`.

### 7.2 Why local deletes are routine: the move-on-import flow

The intuition you'd normally bring to `sync` mode is that a local file disappearing is
suspicious. Here it is the opposite. The user's Sonarr/Radarr import **moves** files out of the
local sync directory into the media library, and a cleanup job removes whatever is left behind.
So a local file vanishing is the **normal, expected end state of every successful import** — and
that disappearance is precisely the intended trigger for propagating a remote delete in `sync`
mode. The pipeline is: lftpweb pulls it down → *arr moves it into the library → lftpweb notices
it's gone → lftpweb removes the seedbox copy so the pickup dir doesn't grow forever.

Worth stating because a reader will expect an integration here: **this serves the *arr workflow
without talking to Sonarr or Radarr at all.** There is no *arr integration in v1 or on the near
roadmap; the filesystem observation is the whole interface, and it works with any importer that
moves files, or with a human doing it by hand.

This inverts the usual safety design, and the inversion has to be respected:

- **Deletes are routine, not anomalous.** A count-based circuit breaker — "more than N deletes
  in a cycle is suspicious, halt" — false-positives on *every* bulk import, which is the exact
  workload. Do not lean on one. If a breaker is kept at all it must be rate-based and generously
  sized (e.g. "more deletes in one cycle than we have ever downloaded in a day"), and it is a
  **backstop against absurdity, not a safeguard**. Nothing in this design may depend on it.
- Because anomaly detection is unavailable as a safety mechanism, the **health gate in §7.3
  carries the entire safety load**. That is a deliberate concentration of risk, recorded as
  such in §15.

### 7.3 Rails on delete propagation

*Deferred with `sync` (§7) — not built in v1. The verification gate and audit trail are the
exceptions: `move` needs them, so they ship.*

**The mount / sentinel gate.** This is the one that matters.

After its first successful scan of a queue's `local_path`, lftpweb writes a sentinel file
`.lftpweb-mount-ok` at the local root. Before **any** delete propagation, for every queue, it
requires all three:

1. the local root exists,
2. it is readable,
3. it contains that sentinel.

If any check fails, delete propagation for that queue is **disabled entirely** — not deferred
item-by-item — and the condition is raised in the UI and the event log.

The reason, said out loud because it is the whole point: **if a volume fails to mount, every
tracked item looks locally deleted at once.** Without this gate, the very next scan cycle would
read an unmounted (or empty, or freshly re-created) mount point as "the user deleted
everything", and `sync` mode would cheerfully wipe the corresponding tree off the seedbox. An
empty directory and a broken mount are indistinguishable by content; the sentinel is what makes
them distinguishable, because a bind mount that didn't come up has no sentinel in it.

**The remaining rails**, all of which apply on top of the gate:

- **Only propagate a delete for an item with a `DOWNLOADED` record.** The absence of something
  we never fetched means nothing at all. No history, no delete.
- **Grace period.** Absence must persist across several consecutive scans — default ~10 minutes,
  tracked via `item.first_missing_at` — before it counts. An import in progress, a move across
  filesystems, or a momentarily-unreadable directory must not be able to trigger a delete.
  **A `rel_path` can leave both trees at once, and that case runs through this same machinery.**
  The reconciler's node set is `remote_tree ∪ local_tree`, so a path in neither produces no node
  — and the persist pass only ever visits nodes. `move` mode manufactures exactly that shape
  routinely: the remote copy is deleted on verified completion and the staging move then
  relocates the local copy, leaving a tracked row for a path that no longer exists on either
  side. Left to the node loop alone such a row is simply never written again, freezing on
  whatever outcome it last held — `EXTRACTED` forever, never reaching `REMOVED_LOCAL`, defeating
  §3.2 rule 3 for precisely the items `move` produces the most of. So the persist pass makes a
  second, narrow sweep over every previously-tracked `rel_path` it did not write this pass and
  resolves each one through the same grace-period function, with a synthetic `REMOTE_ONLY`
  reading standing in for "there is nothing here to compare". It reuses that function's own
  eligibility gate rather than re-implementing it, so a previous state the function has no
  opinion about is handed to a second, even narrower fallback (`core/mount_sentinel.py.
  resolve_vanished`, 2026-08-13) before being left alone: `PARTIAL` and `LOCAL_ONLY` — states
  that assert *some* concrete content was actually here — rest at `REMOVED_BOTH` rather than
  freezing forever (this closed a real bug: a `move` queue's throttled per-child progress
  writer can leave a small file's row reading a fraction-of-a-second-true `PARTIAL` right as
  its job reaps, and post-processing can relocate the release out of both trees before any
  scan gets the chance to correct it — no rescan can fix it once that happens, because there is
  no fresh structural reading for a path in neither tree). `REMOTE_ONLY` and `EXCLUDED` — which
  assert the opposite, that no real content was ever here — are still left exactly as they were,
  and a suppressed or actively-claimed row never reaches either sweep at all.
- **Verification gate.** `move` deletes only after the item verifies (§6). `sync` propagates only
  for items that reached a verified-complete state. Never deleted on a stale size rollup alone,
  and never on a `SKIPPED` verification — "no evidence" is not evidence.
  **What each kind of evidence actually proves**, since the delete rides on it: a `.sfv`/`.md5`
  sidecar proves per-file content correctness and is the strong case. The hash-on-disk fallback
  (off by default) proves something weaker and now says so exactly — every file under the item
  reads end to end without error **and** the total bytes read match the item's known remote
  size. The size half is not decoration: reading a truncated file to EOF raises nothing, so
  readability alone once blessed an incomplete item and could authorize deleting the only good
  copy of it. With both halves the fallback offers precisely the guarantee the rest of the
  system already runs on — the bytes are all here — and no more: an in-place bit flip with no
  reference checksum to compare against is undetectable in principle. Demoting the fallback to
  `SKIPPED` for `move` queues was considered and rejected: it would silently mean a `move`
  queue can never complete a delete without a sidecar, which is its own surprise for a queue
  configured for that mode.
- **Dry-run mode.** Per queue, logs exactly what *would* be deleted and why, and acts on
  nothing. This is the expected way to turn `sync` on for the first time on a real tree.
- **Full audit trail.** Every delete — and every delete *withheld*, with the failing
  precondition — writes an `event` row (§3.1). The bar for an irreversible remote operation is
  that you can reconstruct afterwards exactly what was deleted and why.

### 7.4 Deletion mechanism

Remote deletes always go through our own asyncssh path (§5), issued by us, item by item, after
the gates above. **Never** lftp's `mirror --Remove-source-files`. Three reasons, all of them
about keeping control of an irreversible action:

- It keeps **verification** as the gate. `--Remove-source-files` deletes on lftp's notion of a
  successful transfer, which is not our notion of a verified item.
- It keeps every delete **auditable**. We know the path, the timestamp, the reason, and the
  outcome, because we performed it.
- It gives **one code path** for both `move` and `sync`, and for user-initiated deletes from the
  Files view. Deletion logic that exists in two places will diverge, and this is the worst
  possible place for it to diverge.

---

## 8. Auth and security

`AUTH_MODE = none | password | proxy`, default `none` (LAN-friendly):

- **`password`** — single user, argon2id hash in SQLite, HTTP-only `SameSite=Lax` session
  cookie, CSRF token required on mutating requests, rate-limited login.
- **API key** — `X-API-Key` header, accepted independently of the mode, for scripts.
- **`proxy`** — trust a configurable identity header (`Remote-User` by default), *only* when
  the request originates from a configured trusted CIDR. Without the CIDR check this mode is
  a bypass, so it is not optional.

Seedbox credentials are encrypted at rest with a key derived from a per-install secret in
`/config`. Log redaction as described in §4.2.

**The key is not included in database backups (§10.2), by design.** A `.db` backup therefore
contains no usable secret and is safe to download and store anywhere — the trade is that
restoring to a fresh install cannot recover the seedbox password (or a pasted key, below).

That has to be a designed behavior, not a documented caveat. On startup, if a credential blob
will not decrypt with the current install key, lftpweb must:

- mark the host **"credentials need re-entry"** rather than crashing or retrying,
- hold all transfers for that host instead of spawning jobs that fail,
- surface a banner that lands the user on Settings → Connection.

The failure mode this prevents is a restore that produces a pile of `AUTH_FAILED` jobs and no
explanation of why.

**Pasting a private key (migration 014, 2026-08-13).** Until this, `auth_method = 'key'` meant
`key_path` alone — the operator mounts a key file into the container themselves, unvalidated
beyond "the field is non-empty." That fails opaquely: OpenSSH refuses a private key with loose
permissions, and lftp shells out to `ssh` (which enforces that) while asyncssh scanning is more
lenient, so a wrongly-permissioned mounted key gave working scans and failing transfers with
nothing pointing at the cause.

Settings → Connection can now accept a **pasted** key instead, stored as `host.ssh_key_enc`,
encrypted with the exact same `core/crypto.py` mechanism as `password_enc` — not a second
crypto scheme, and not a ciphertext file kept outside the database: a config backup round-trips
a pasted key the same way it round-trips a password, where a file excluded from backups would
drop the user into "credentials need re-entry" on restore even though nothing about the key
itself changed (docs/decisions.md has the full reasoning, including why the security difference
between the two options is narrow).

This is **additive, never a replacement**: `key_path` keeps working untouched for anyone
already mounting a key. When both are set, **the pasted key wins** — decided once, server-side
(`api/settings.py`), and surfaced to the UI as `active_key_source` so it never has to re-derive
the rule. A pasted key is validated at save time (parses as a private key; a passphrase-
protected key is rejected outright, with a message pointing at stripping the passphrase or
using `key_path` instead — lftpweb cannot supply a passphrase non-interactively), and a stored
key that later fails to decrypt (the restore-to-fresh-install case above) sets the exact same
`credentials_need_reentry` flag a bad password does, not a parallel one.

Materialisation differs by consumer, because only one of the two actually needs a file:
**asyncssh scanning takes the decrypted key straight into memory** (`asyncssh.import_private_key`,
confirmed against the installed asyncssh rather than assumed — `client_keys` accepts parsed key
material, not only paths), so scanning never writes the plaintext to disk at all. **lftp shells
out to `ssh -i <path>`**, so it genuinely needs a file — written **per job**, alongside the
existing per-job rc file on the `/run` tmpfs (§11), mode 0600, unlinked the moment the job ends.
Per-job rather than a file held for the process's lifetime: the plaintext then exists on tmpfs
only while a transfer is actually in flight, and — because every spawn decrypts fresh from
whatever the DB row currently holds — there is no separate "re-materialise on startup" step to
remember; `/run` being emptied by a container restart simply doesn't matter, the same as it
doesn't matter for the per-job rc file today.

---

## 9. Frontend

React + TypeScript + Vite + Tailwind. **One WebSocket** delivering a full model snapshot on
connect and deltas thereafter; REST goes through a hand-rolled `fetch` client and a small poll
hook.

This document said "TanStack Query for REST" from its first draft, and **it has never been
true**. Phase 1 built `api/client.ts` (a thin `fetch` wrapper) and a `usePoll` hook instead;
phase 3b added `useJobs` in the same shape plus a `refresh()` escape hatch, and recorded the
divergence rather than deepening it silently. The library is not a dependency of this project
and never has been. Corrected here to describe what exists — **but adopting the library remains
an open choice nobody has made.** It would touch every data-fetching call site, so it is its own
scoped piece of work if it is ever wanted, never a side effect of whatever else happens to be
editing the frontend.

**Queues are the organizing axis.** Browsing, active transfers, and history are all filterable
and groupable by the named path queue an item belongs to (§3.1), so "what's happening in TV"
is one click everywhere rather than a mental filter.

**What the socket carries is the persisted state (§2.2)**, in both directions of that
sentence: the connect-time snapshot and every delta are projections of the `item` table, and a
row is sent when its *persisted* state changed — not when the reconciler's candidate reading
did. That distinction is what makes a grace period expiring, or a post-processing outcome
reasserting itself, visible at all; both are changes no writer pushes, because no module
"decides" them — a scan observes them.

### 9.1 Shell: left nav, top tabs, stats header

```
┌─ header ───────────────────────────────────────────────────────────────┐
│  ▲ 8.2 MB/s    ⣿ 9.0 / 10 allocated    ⏳ 12 queued (48 GB)    24h: 310 GB │
├──────────┬─────────────────────────────────────────────────────────────┤
│  Files   │  [ Connection · Queues · Transfer · Post-processing ·        │
│  Xfers   │    Logs · Backup · Auth ]        ← tabs, only where a        │
│  History │                                    section has >1 page       │
│ Dashboard│                                                              │
│  Settings│                                                              │
│ ──────── │                                                              │
│ v0.0.1 ↗ │  ← bottom-left, links to the GitHub release notes            │
└──────────┴─────────────────────────────────────────────────────────────┘
```

- **Left panel for section nav**; **tabs across the top** only where a section has more than one
  page. Settings is the one that needs them today.
- **Version in the bottom-left corner of the nav**, linking out to that release's notes on
  GitHub. (Repo not created yet — the base URL is a config constant so it can be filled in
  without touching the component.)
- **Theme: dark / light / system**, with **system as the default** so it follows the OS until
  told otherwise. Persisted per browser.

**Header stats.** Current aggregate speed · **allocated vs. ceiling** · pending queue depth
(count and bytes) · total transferred in the last 24 h.

The allocated-vs-ceiling readout is not decoration. Under admission control (§4.5) the answer
to "why hasn't the next item started?" is `9.0 / 10 allocated`, not current speed — a job
allocated 5 MB/s that is only pulling 2 still *holds* its 5. Without that number on screen the
scheduler looks broken at exactly the moments it is working correctly.

### 9.2 Pages

**Files** — virtualized tree (must stay smooth at 10k+ rows), per row: state chip, progress
bar, size, speed, ETA. Grouped by queue, collapsible per queue. Expand/collapse, multi-select
with shift-range, bulk *Queue / Stop / Delete local / Delete remote*, text search, state
filter, and a lifecycle facet filter (below) — composed together, never a second filtering
path.

Four further row-level readings, all of them projections of the same persisted `item` row the
state chip already reads (§2.2's one shared projection, never a second read of it):

- **Lifecycle icons — Remote / Local / Verified / Extracted.** `item.state` was carrying at
  least five orthogonal facts in one slot, and the state bugs this project actually hit were two
  of those facts fighting over it. The icons split them apart at the display layer. **Remote and
  Local are presence** — recomputed on every projection, and they may legitimately go dark: a
  `move`-mode item's remote copy goes dark the instant it is deleted on purpose, and that is the
  display being honest, so it renders dim, never red. **Verified and Extracted are milestones**
  — read from `verified_at`/`extracted_at`, timestamp columns nothing ever clears, never from
  `state` — so they stay correctly lit after a later rescan moves `state` on. That split is the
  load-bearing part and must not be collapsed back: it is why the whole bug class stops being
  visible here even where it is not yet fixed in the state layer.
  Local completeness reuses the leaf byte rule only for states that have not resolved yet;
  everything from `DOWNLOADED` onward reads complete from `state` instead, because two real
  cases have real remote bytes and no local ones **by design** — a directory whose children were
  all `EXCLUDED` (rule 8), and an `EXTRACTED` item whose spent archive volumes were deleted (§6).
  The one case that genuinely is broken — a `DOWNLOADED` row still claiming bytes an importer
  took — is told apart by `first_missing_at`, set only while §7.3's grace period is actually
  running. Originally surfaced as a **Missing only** checkbox; replaced (2026-08-13) by a
  **lifecycle facet filter** dropdown (has remote copy / has local copy / extracted / not
  extracted / "downloaded but missing locally") once the user could not tell what the checkbox
  meant from its name alone — the verdict on it. Composes with the text/state filters through
  the same mechanism; distinct from the state filter, which the facet filter does not make
  redundant — facets cannot tell `QUEUED` from `DOWNLOADING` from `STOPPED` from `FAILED`,
  which read identically on presence/milestone alone.
  **R also reads amber, client-side only, while `substate = 'settling'`** (2026-08-13):
  `core/itemview.py._remote_facet` itself never produces amber (only green/dim, by design), so
  this is a display override layered on top of the presence fact for exactly this one
  substate, never on L (the local side is legitimately empty during the wait, so amber there
  would imply activity that isn't happening).
- **The Status chip's own `substate = 'settling'` text is one of two sentences, not always the
  same one** (2026-08-13, §3.3). `matched_scans == 1` covers two cases the counter itself
  doesn't distinguish — a genuinely first-ever sighting, or a fingerprint that just changed from
  a previous one — and in both, the "Waiting N of 2 scans" countdown has nothing confirmed to
  count yet; on an actively-growing directory this read as pinned at "1 of 2" for the whole
  copy. While `matched_scans == 1`, the chip instead shows the byte count itself climbing
  (`item_settle.total_bytes`, already computed as part of the fingerprint) plus how long it has
  been watched and when it last moved (migration 013's `first_observed_at`/`last_changed_at`).
  The moment a confirming scan lands (`matched_scans >= 2`), the display switches back to the
  existing countdown, unchanged — `matched_scans`'s own arithmetic and `REQUIRED_SETTLE_SCANS`
  are untouched by this split (§3.3 has the full reasoning, including a same-shaped fix that
  was tried at the counter itself and reverted). Both are the same amber chip and the same
  `substate`; only the words differ, gated the same way the rest of the settle fields are —
  `substate == "settling"` only, never passed through otherwise (§2.2's projection invariant;
  see `core/itemview.py.item_view`'s own docstring for the WebSocket-delta regression this
  gate exists to prevent).
- **Inline progress**, drawn as the state chip's own background growing under the label, for
  `PARTIAL`/`DOWNLOADING` rows only — including a directory's rolled-up percentage, so a 40 GB
  release shows real progress rather than the word "partial". No new backend data and no
  per-row timer.
- **Sorting** by name, size, last state change, or percent complete, either direction, persisted
  across reloads. **The sortable column headers are themselves the control** (2026-08-13) —
  click to sort, click again to reverse, with a caret marking the active column and direction;
  a separate "Sort by" dropdown plus asc/desc button shipped first and was replaced the same
  day once used for real. A header that isn't sortable stays a plain label. Reorders
  **siblings within each parent**, never the flattened list the virtualizer walks, so a sorted
  tree can never tear a child away from its actual parent.
- **Columns are drag-resizable and remembered per browser** (2026-08-13). Name keeps flexing to
  absorb whatever space the fixed columns (Size / Status / R L V E / Changed / Actions) don't
  claim — resizing one of those five just changes how much Name gets, never a paired
  shrink-your-neighbor resize. A shared column definition drives both the header row and each
  data row, replacing two independently hardcoded, hand-synced sets of widths. **The drag never
  touches React state** — the live width is written straight to a CSS custom property on the
  tree's scroll container via a ref on every `pointermove`, so the virtualized list underneath
  never re-renders mid-drag; state (and `localStorage`, keyed by column id) is written once, on
  release. Keyboard-resizable too (arrow keys, Shift for a bigger step), since a drag-only
  affordance is unusable without a pointer, and a double-click on the handle resets that column
  to its default.
- **The Expand all / Collapse all choice persists** across reloads, stored as a default plus
  per-row exceptions rather than a saved set of collapsed paths. The tree updates continuously
  over the socket, and a directory that arrives *after* a saved set was written would not be in
  it — so it would render expanded against a stated "start collapsed" preference. A default with
  exceptions gives a newly-arrived directory the current default automatically.
- **A delete in progress reads honestly, not as whatever it happened to be beforehand**
  (2026-08-13). `item.substate = 'removing'`, the same vehicle the settle gate's `'settling'`
  uses, overlays the state chip (`REMOVING`, styled like a failure — it is destructive and
  irreversible) for the whole subtree of a `core/local_delete.py.delete_local()` call, written
  and published *before* the filesystem work starts so a large directory delete has visible
  feedback for the whole time it takes, not silence followed by a sudden final state. Protected
  from a racing scan by the same live-worker mechanism as `VERIFYING`/`EXTRACTING` (§3.2 rule 9)
  — a crashed or killed process cannot leave a row stuck reading "Removing" forever.
- **A row whose content came back reads "Re-Download", not "Queue"** (2026-08-13). A
  `REMOVED_LOCAL`/`REMOVED_BOTH` row this codebase deleted itself
  (`suppressed_reason = 'deleted_local'`) whose remote copy has since reappeared still won't be
  auto-fetched (rule 3), but the manual action button says so by name rather than reading like a
  brand-new item — derived from the suppression reason plus current remote presence, never from
  the state string alone, since an *unsuppressed* `REMOVED_LOCAL`/`REMOVED_BOTH`
  (`core/mount_sentinel.py.resolve_vanished`, rule 9) is a plain "Queue".
- **Deleting a `DOWNLOADING`/`QUEUED` item stops it first, rather than refusing** (2026-08-13,
  `prompts/2026-08-13-delete-during-transfer.md`). The Delete button was always offered on a
  mid-transfer row (`canDeleteLocal` never excluded those states), but the click used to just
  bounce off `core/local_delete.py.delete_local`'s "no active job" guard with a withheld 409 —
  the worst of both, a button that promises an action it can't perform. That guard is still
  correct and unchanged (`rmtree`-ing a directory lftp is still writing into races the writer);
  what changed is who satisfies it and when. `api/jobs.py.delete_item` now always calls
  `core/queue.py.TransferQueue.stop_item()` first — the identical SIGTERM → grace → SIGKILL path
  the Stop button drives (§4.6), a safe no-op when nothing is active — and only proceeds to
  `delete_local()` once that stop is confirmed complete: the process reaped, its job row
  terminal, never merely "signal sent." A stop that can't be confirmed within a bounded window
  withholds the delete with a 409 naming why, rather than deleting blind; the stop attempt itself
  is *not* cancelled on that timeout (it keeps running so `core/queue.py`'s own bookkeeping
  finishes cleanly instead of being abandoned half-updated) — the item can simply be deleted
  again once it settles. The confirmation dialog says so plainly before the click ("N of M is/are
  transferring now — deleting will cancel it/them first"), as its own line alongside — never
  replacing — the existing remote-copy line, since a selection can be both at once; this is a
  stronger sentence in the one existing dialog, not a second confirmation step stacked on it. The
  row that comes out the other end always reads `suppressed_reason = 'deleted_local'`, never the
  stop path's own `user_stopped` — `delete_local`'s unconditional write (rule 3) overwrites it a
  moment later, so a stopped-then-deleted item is indistinguishable from an idle one deleted
  directly, and (the user's own question) is never re-queued by auto-queue either way,
  regardless of `re_download_externally_removed`.
- **A loose top-level file's in-flight `.lftp` temp name is cleaned up too.** lftp writes to
  `<name>.lftp` while a transfer is in flight (`xfer:use-temp-file`, §4.4b) and renames it on
  completion; a **directory** item's delete (`shutil.rmtree`) sweeps that regardless of what's
  inside it, but a **loose file** item's delete targets the item's own final name — which, for
  an item stopped mid-transfer, may not exist yet at all. `core/local_delete.py._do_remove_
  from_disk` removes the `.lftp` temp file and its own `.lftp-pget-status` sidecar alongside
  the final name (whichever of the two, or both, are actually present), so a delete never
  leaves behind exactly the bytes it was asked to remove under a different name.

**Item detail.** A small info icon on each row — deliberately quieter than the lifecycle icons,
because it is a control and not a status — opens the item drawer described below. The icon exists
because the row's own click already drives multi-select, which feeds bulk *Delete*, so overloading
it would put an information affordance and an irreversible one on the same gesture.

Hovering a row's name (or giving it keyboard focus, for anyone not using a pointer) shows a
lightweight hover card with size and modified date, remote and local side by side — the row's
name as the header, two columns only when the item actually exists on both sides (a
`LOCAL_ONLY`/`REMOTE_ONLY`/deleted row degrades to one labelled column rather than showing a
permanently empty half), and no "Modified" row at all for a directory, since `local_mtime`/
`remote_mtime` are files-only. It fetches nothing — every field comes from what the row already
holds — and shares its label/value formatting with the item drawer's own both-sides panel below
(`lib/format.ts.bothSidesRows`) so the two surfaces can never disagree about what these numbers
mean. The card is portal-rendered, not a child of the row (the row can scroll out of the
virtualized list, or unmount, while it's showing), hides on any scroll, and never intercepts a
click meant for the row, a sort header, or a column resize handle.

**Transfers** — the job queue. Rows stay deliberately plain:

```
Some.Release.S03E04.2160p    [downloading]   18 files   62%   4.1 MB/s   ETA 12m
```

- Visible status vocabulary is **queued / downloading / downloaded**. The other internal states
  (§3.2) surface only on rows where they actually apply, rather than expanding everyone's
  mental model to twelve chips.
- **Move to top** on each row (§4.5); default order is oldest-first. **Each still-queued row
  shows its actual run position** (2026-08-13) — a small `#N` ordinal, 1/2/3… in the order
  `GET /api/jobs` already returns them (`rank DESC, queued_at ASC`), plus a one-line caption
  once there is more than one queued job that the list order *is* the queue order. The
  capability (ordering, Move to top, Start now) existed before this and was invisible; nothing
  new was added server-side, the list was already sorted. `rank` grows monotonically on every
  "Move to top" with no compaction — not a practical problem at 64-bit `INTEGER` width.
- **Start now at max bandwidth** as a per-row action, with its oversubscription behavior
  explained inline the first time it's used.
- Failures show the error class plus the captured lftp output tail.
- **Dismiss**, on a `failed`/`cancelled` row (2026-08-13) — a display-only action that stops
  the row showing on this page (`job.dismissed_at`) without deleting the `job` row or touching
  the item's own state/suppression. Added after a user hit a `REMOTE_GONE` failure — the
  remote files were genuinely gone, and Retry was the only action this page offered, which is
  exactly wrong for that case. History (below) keeps showing a dismissed job regardless — it
  reads the same table and dismissal was never meant to erase what happened, only to declutter
  this page. **Clear all failed** dismisses every currently-`failed` row in one action
  (`Promise.allSettled`, honest about partial failure the same way the Files page's bulk
  actions already are) — scoped to `failed` only, not `cancelled`, since a stopped job is a
  deliberate Stop click, not the kind of unattended pile-up a permanent-error class like
  `REMOTE_GONE` can become. No confirmation dialog on either: nothing is destroyed.

**Item drawer.** A **side drawer** — not a modal, because file lists get long and the queue
should stay visible — listing the files inside that item: name, size, transferred, per-file
progress, status. Virtualized; a release can carry hundreds of files.

This view is cheap for us specifically because of §1.3: per-file status is just the reconciler's
local-vs-remote size comparison over the whole tree. `jobs -v` only ever names the handful of
files lftp is actively touching, so a complete per-file breakdown isn't something the parsing
approach could have offered at all.

**It is one drawer, keyed on an item, opened from two places** — the Files row's info icon and a
Transfers row — rather than two overlapping detail surfaces. It was originally keyed on a *job*
and reachable only from Transfers, which meant it became unreachable the moment a transfer aged
out of that page's list. Alongside the per-file breakdown it shows size and modified date for
both sides where each exists (`local_mtime` is the local counterpart to `remote_mtime`; like
`remote_mtime` it is files-only, since neither a directory inode's own mtime nor a recursive
newest-child rollup answers a question the byte-comparison model asks), the lifecycle chronology
from `first_seen_at` through `state_changed_at` rendered in the order it actually happened, and a
bounded recent-history panel — the last handful of transfer attempts and audit events, including
the delete trail. History is fetched **once, when the drawer opens**, never per row and never
eagerly for a tree.

**History** — **its own page**, not a panel: the `job` and `event` tables, **grouped by queue**,
filterable by state, error class, date range, and event kind. This is where remote deletes are
reviewed, so it must render the delete audit trail (§7.3) legibly — what was deleted, from
which queue, under which mode, and what gated it. The only view that answers "what did it
remove last night". A row a Transfers-page Dismiss removed from that page still shows here
(same table, dismissal is display-only) with a quiet **dismissed** marker — worth surfacing,
since it's the only page that can answer "why is this no longer on Transfers."

**Dashboard** — throughput over time, from the sample store in §10.4: bytes moved per hour over
the last 24 h, broken down by queue with a site total, and speed over a selectable 1 h / 12 h /
24 h window for the site or one queue. Its own page rather than more chrome in the header —
the header answers "what is happening now" in a single row, and a chart is a different
question. **Downtime renders as a gap, never as a zero**, in both charts; that distinction is
the whole reason the store keeps a separate liveness heartbeat (§10.4). **The selected
timeframe is remembered per browser** (2026-08-13, `localStorage`), read synchronously on
first render so the chart never paints the default range and then jumps to the saved one.

**Settings** — tabbed:

| Tab | Contents |
|---|---|
| Connection | host address, port, protocol, credentials, *Test connection*, connection tuning |
| Queues | named path queues, remote → local mapping, `sync_mode`, patterns with a **live "what would this match" preview** against the current remote tree, post-processing toggles, staging path |
| Transfer | site-level bandwidth, concurrency, fast lane, parallelism — everything in §4.5 |
| Post-processing | verify / extract / move defaults |
| Logs | §10.1 |
| Backup | §10.2 |
| Auth | mode, password, API keys |

The `sync_mode` selector carries the §7.1 misconfiguration warning inline — pointing a `move`
or `sync` queue at a live torrent data directory rather than a hardlink pickup directory
destroys seeding torrents, and the warning belongs where the choice is made, not in a manual.
Switching a queue to `move` or `sync` requires explicit confirmation, and offers dry-run.

### 9.3 Advanced options exposed

**Host-level** — describes the connection to the seedbox: `net:connection-limit` ·
`net:socket-buffer` · `net:timeout` · `net:max-retries` · `net:reconnect-interval-base` /
`-multiplier` · `sftp:max-packets-in-flight` · `sftp:connect-program`

**Site-level transfer tuning** — one set for the whole instance (§4.5):
`max_bandwidth` · `max_concurrent_transfers` · `small_item_threshold` ·
`small_lane_concurrency` · `small_lane_reserve` · `min_share_floor` ·
`mirror:parallel-transfer-count` · `mirror:use-pget-n` · `pget:default-n` ·
`pget:min-chunk-size` · `mirror:parallel-directories` · `xfer:use-temp-file`

**Queue-level** — what varies per path, and nothing else: `sync_mode` · auto-queue enable ·
include/exclude globs · post-processing toggles · staging path

The split follows one rule: **a queue governs what and where, never how fast.** Parallelism and
bandwidth were originally sketched as per-queue overrides, but they multiply into a single
host-wide connection ceiling — letting each queue raise them independently makes that ceiling
unenforceable. `net:limit-rate` is deliberately **not** offered as a bandwidth control at any
level, because per-connection limits don't compose (§4.5).

**Live connection-count readout — required, not a nice-to-have.** Wherever max concurrent jobs,
`mirror:parallel-transfer-count`, and `mirror:use-pget-n` are editable, the UI computes and
displays the worst case as they change:

```
2 jobs × 4 parallel × 4 pget-n = 32 concurrent SFTP sessions      ⚠ over net:connection-limit (16)
```

These three numbers multiply silently (§4.5) and seedboxes refuse connections well below what
the sliders will happily let you ask for. Warn when the worst case exceeds
`net:connection-limit`, and show the resulting per-job bandwidth cap next to it.

…plus a free-text **"extra lftp settings"** box injected verbatim into every job's rc file, so
a power user is never blocked waiting for us to add a checkbox.

---

## 10. Operations

### 10.1 Logging

Three streams exist and they must stay separate. A log viewer that mixes them becomes a
dumping ground nobody reads.

| Stream | What it holds | Where it lives |
|---|---|---|
| **App log** | errors, warnings, scheduler admission decisions, scan failures | `/config/logs/lftpweb.log`, rotating, viewable in Settings → Logs |
| **Per-job lftp output** | the ~4 KB tail captured from a failed transfer | `job.output_tail` (§3.1), shown in History detail |
| **Event table** | structured, queryable lifecycle + audit records | SQLite `event` (§3.1), drives History |

The app log is **not** the lftp transcript. lftp's own chatter belongs to the job that produced
it; putting it in a global text log destroys the one property that makes it useful, which is
that it's attached to a specific failure.

- **Rotation by size with a max file count** — default 5 MB × 5 files, a 25 MB ceiling. Both
  configurable.
- **Viewer** in Settings → Logs: list the rotated files, tail the current one, filter by level,
  download.
- **Everything passes the credential redactor (§4.2) before it is written**, not before it is
  displayed. A secret that reaches disk has already leaked.

### 10.2 Database backup

Losing `/config/lftpweb.db` costs the queue configuration *and* the record of what has already
been downloaded — which would trigger a mass re-download of everything still on the seedbox.
That is worth a backup.

- **`VACUUM INTO`**, not a file copy. It is atomic and WAL-safe, so a backup taken during a
  transfer cannot capture a torn database. Copying the file while WAL is active can.
- Target `/config/backups/lftpweb-YYYYMMDD-HHMMSS.db`; **daily by default, keep 7**, both
  configurable.
- **Automatic backup immediately before any schema migration.** This is the one that actually
  saves you — migrations are the failure mode that loses everything, not a random Tuesday.
- **Backup now** button and a **download** button in Settings → Backup.
- **The encryption key is deliberately not included** (§8). The backup therefore contains no
  usable secret and is safe to store anywhere; the trade is that a restore to a fresh install
  requires re-entering the seedbox credentials, which the app must handle as a designed state
  rather than as a wave of `AUTH_FAILED` jobs.

### 10.3 Health and shutdown

- **`HEALTHCHECK`** on `/api/health` — reports DB reachability, host reachability, and whether
  the scheduler loop is live.
- **Graceful shutdown:** SIGTERM propagates to in-flight lftp children so their `-c` resume
  state is clean and the next start resumes rather than restarts.

### 10.4 Throughput metrics

The Dashboard (§9.2) needs history, and progress sampling (§4.4) is in-memory and lives only as
long as the job does. So a small store persists it — **derived from the same filesystem byte
accounting**, never from a second measurement and never from lftp's output (§1.3).

- **One sample every ~30 s**, driven by a counter on the transfer tick rather than a second
  timer, so the sample cadence cannot drift out of step with the engine's own notion of a tick.
- **Two tables, deliberately not one.** `metric_sample` gets a row for a queue only when that
  queue's running jobs moved a nonzero number of bytes in the window. `metric_heartbeat` gets
  exactly one row every tick, unconditionally. **Idle and down are then distinguishable without
  writing an explicit zero per queue per interval**: heartbeats with no samples is idle, no
  heartbeats at all is down, and the charts must render the second as a gap rather than a flat
  zero line. Padding every idle queue with a zero row would inflate the store for an instance
  that transfers nothing, to record something the heartbeat already recovers for free.
- **Deltas are per job and measured from that job's own start**, never from `job.bytes_done`
  alone. That column is the absolute local footprint of the item, so a retry — which resumes
  rather than restarting — begins life already holding whatever the previous attempt left on
  disk. Differencing it by job id would render a restart as a phantom spike. Subtracting the
  job's own `bytes_start` makes the tracked quantity zero at its first tick and monotonic
  thereafter, so a new attempt can never inherit a dead one's history.
- **Retention** in days, pruned on a fixed cadence — a cheap idempotent delete, never on a
  request path. Both charts read pre-bucketed rows from one endpoint, with the bucket width
  chosen per range so the bar chart and the 24 h line agree on what "one slice of time" means.

---

## 11. Container

**Base: `python:3.13-alpine`**, chosen for size and attack surface rather than for consistency
with sibling projects (which use Debian slim). Reasoning and the rejected alternatives are in
§11.1.

Multi-stage:

```
node:22-alpine        → builds the SPA (static output; base is irrelevant to the artifact)
python:3.13-alpine    → builder: build-base + any wheels lacking musllinux builds → venv
python:3.13-alpine    → runtime: venv + lftp, openssh-client, 7zip, su-exec, tini
```

No compiler in the final image — build tooling stays in the builder stage and only the venv is
copied forward.

**Archive tooling: two tools, chosen for what each is licensed and able to do.** `7zz` (the
`7zip` package — 7-Zip proper, not p7zip) covers zip / 7z / tar / gz / bz2 / xz. `unrar`, built
from RARLAB source in its own builder stage, covers rar / rar5 — Alpine's `7zz` build ships
with no RAR codec at all (7-Zip's RAR decoder derives from unRAR source, whose licence Alpine's
`main` repo won't carry), and no packaged alternative (`unrar`, `unrar-free`, `unar`, `p7zip`)
exists in Alpine's indexes either. Both tools only ever extract; neither builds nor
re-compresses an archive, which is what keeps unRAR's own "no RAR-compatible archiver" licence
restriction out of scope. See `NOTICE` and `docs/decisions.md` for the full licence reasoning
and the rejected `libarchive-tools` alternative.

This document said "`7zz` alone" for nine phases, on the strength of upstream 7-Zip supporting
RAR since 21.07 — true of 7-Zip, false of the build Alpine ships, and never checked against the
actual image. Rar extraction was broken the whole time, silently, on the format this app exists
to fetch. Kept here as the reason the tooling line now names both binaries and the tests assert
what the shipped decoder can actually decode, rather than what a changelog says it should.

- **Volumes:** `/config` (SQLite, logs, backups, install secret, keys), `/downloads`, optional
  `/staging`
- **`PUID` / `PGID` / `UMASK`** honored by an entrypoint that fixes ownership and then drops
  privileges with `su-exec` — the convention every seedbox-adjacent container user expects
- **`tini`** as PID 1 so lftp children are reaped
- Env vars bootstrap first-run config; everything is editable in the UI afterwards

### 11.1 Why Alpine, and the hardening that actually matters

**Alpine over Debian slim:** ~3× smaller and a much smaller installed package set, which is
most of the CVE surface. The historical arguments against it are largely spent — musl's DNS
resolver gained TCP fallback in 1.2.4 (Alpine 3.19+), and every dependency we need
(`cryptography`, `pydantic-core`, `argon2-cffi`) publishes `musllinux` wheels, so there is no
Rust toolchain in the runtime image.

**Rejected: Debian slim.** Larger, more packages, and its archive story is worse — `unrar` is
non-free and `unrar-free` historically can't read RAR5. Chosen by the sibling projects, but
they don't need lftp or RAR.

**Rejected: distroless / Chainguard.** Genuinely smaller CVE counts, but we need several system
binaries (`lftp`, `ssh`, `7zz`) and a shell for the PUID/PGID entrypoint. Getting arbitrary
packages into those images means fighting the toolchain for a marginal gain over Alpine.

The base image is the smaller half of "secure". The rest is runtime posture, and it belongs in
the compose file as much as the Dockerfile:

- **Pin the base by digest**, not just tag, so a silent retag can't change the archive tooling
  underneath you.
- **Run as non-root.** Privileges are dropped before the app starts; nothing but the entrypoint
  ever runs as root.
- **`cap_drop: ALL`, then add back exactly `CHOWN`, `SETUID`, `SETGID`.** The *running app*
  needs no capabilities — but the entrypoint does, briefly, before it drops privileges. `chown(2)`
  and `setuid(2)`/`setgid(2)` are capability-gated even for uid 0, so a literal `cap_drop: ALL`
  crash-loops the container before the app starts. Verified in build phase 1. Once `su-exec`
  drops to `PUID`/`PGID`, the app process holds none of the three.
- **`no-new-privileges: true`**.
- **Read-only root filesystem**, with `/config`, `/downloads`, `/staging` and a small `/run`
  tmpfs as the only writable paths.
- **The per-job rc file carrying seedbox credentials (§4.2) lives on the `/run` tmpfs at mode
  0600 and is unlinked when the job exits** — never on a persistent volume, never in argv.

### 11.2 Compose: production and development

Two committed files, because the two environments differ in more than a port.

| | `docker-compose.yml` (production) | `docker-compose.dev.yml` |
|---|---|---|
| Image | pulled by digest from the registry | built from the local tree |
| Source | baked in | bind-mounted, hot reload (uvicorn `--reload`, Vite dev server) |
| Identity | dedicated NFS uid/gid | the developer's own uid/gid |
| Storage | NFS-backed bind mounts | local scratch under `private_data/` |
| Hardening | `read_only`, `cap_drop: ALL`, `no-new-privileges`, tmpfs `/run` | relaxed enough to debug |
| Logging | INFO | DEBUG |

A third, `docker-compose.test.yml`, brings up the fake seedbox for integration tests (§14).

**Identity is the part that actually bites, and it's a production-only problem.** The data
volumes are NFS shares that require a specific uid/gid, so the container has to *be* that
identity — not merely have permission locally.

- **`PUID` / `PGID` / `UMASK`** select the runtime identity; the entrypoint applies them and
  drops privileges with `su-exec`. The alternative — compose's native `user: "3000:3000"` — is
  cleaner where nothing needs chowning, and both are supported. Production should prefer
  whichever matches the NFS export.
- **The entrypoint must chown `/config` only, never the data volumes.** Under the usual
  `root_squash` export, a root-owned entrypoint is squashed to `nobody` on the NFS mount and
  the chown fails. Attempting it anyway is how these containers end up crash-looping on
  startup against a perfectly healthy share.
- **A chown failure on a data volume must be a warning, not a fatal.** Ownership on an NFS
  share is the server's business; our job is to run as the right uid and get out of the way.
- **`UMASK` matters more than usual here**, because every file we create lands on a share that
  other services (the `*arr` stack, the media server) also read. Default `022`, configurable.
- **Never create a passwd/group entry for `PUID`/`PGID`.** `read_only: true` puts `/etc/passwd`
  and `/etc/group` on the read-only root, so an `addgroup`/`adduser` step fails outright.
  `su-exec` and `chown` both take a raw numeric `uid:gid`, so nothing needs an NSS entry — log
  lines just print numeric ids. Verified in build phase 1.
- Startup should **verify writability** of each configured local path as its identity and
  report a clear error naming the path and the effective uid/gid — the single most common
  deployment failure, and one that otherwise surfaces as inscrutable transfer errors much
  later.

---

## 12. Files to create

```
docker/Dockerfile · docker/entrypoint.sh
docker-compose.yml          production
docker-compose.dev.yml      development
docker-compose.test.yml     the fake-seedbox integration harness (§14)
backend/lftpweb/
  __init__.py                      # __version__ — the single source of truth, starts at 0.0.1
  main.py config.py db.py models.py auth.py logsetup.py middleware.py
  migrations/*.sql                 # hand-rolled, one file per step, run in order
  api/{files,jobs,settings,auth,health,logs,backup,history,metrics,stats}.py   api/ws.py
  core/engine.py      core/remote.py     core/local_scan.py   core/lftp.py
  core/queue.py       core/scheduler.py  core/progress.py     core/reconcile.py
  core/patterns.py    core/autoqueue.py  core/postprocess.py  core/events.py
  core/backup.py      core/crypto.py     core/auth.py         core/util.py
  core/verify.py      core/extract.py    core/local_delete.py # the three §6 steps, plus
                                                              #   deleting a local copy (§3.2)
  core/itemview.py    core/settle.py     core/mount_sentinel.py
  core/audit.py       core/metrics.py    core/logtail.py
  remote_agent/scan_fs.py          # stdlib-only fallback scanner
frontend/   Vite app — routes Files / Transfers / History / Dashboard / Settings
tests/      unit + integration
private_data/   gitignored — local scratch, test fixtures, sample trees, scratch compose (§12.1)
```

`core/sync.py` was sketched here as the home for the mount gate, the grace period, and the
delete policy. `sync` was deferred (§7), but the first two shipped in phase 4 regardless — they
are what auto-queue needs, not what delete propagation needs (§13 phase 4) — so they live in
`core/mount_sentinel.py` and there is no `core/sync.py`.

Two of these were separate modules from the first draft, and both for the same reason — the
interesting logic is a pure function that deserves to be tested without a subprocess or a
filesystem:

- **`core/scheduler.py`** vs `core/queue.py`. The queue owns job lifecycle and process
  supervision; the scheduler owns only the admission decision (§4.5) — `(N, B, running, queue)`
  in, an admit list out.
- **`core/patterns.py`** vs `core/autoqueue.py`. `autoqueue` decides *when* to evaluate;
  `patterns` decides *what matches*. It must be one module because §4.7 requires the identical
  compiled pattern set to be used in two places — building the lftp `--exclude-glob` arguments
  and telling the reconciler what an item is supposed to contain. Two copies of that logic
  drifting apart is precisely the bug that leaves every filtered release stuck in `PARTIAL`.

Everything added after phase 4 followed the same rule, and five are worth naming because their
boundaries are load-bearing rather than tidy:

- **`core/itemview.py`** is the one projection of an `item` row that everything publishes
  through — the WebSocket delta, the connect-time snapshot, and `GET /api/files` alike (§2.2).
  Four hand-built copies of that dict existed before it, and the engine's copy is the one that
  drifted. It is deliberately dependency-free so every other module can read back through it
  without a cycle.
- **`core/mount_sentinel.py`** holds the sentinel gate and the absence grace period as pure
  decision functions, so the state machine is testable without a filesystem or a database.
- **`core/settle.py`** holds the fingerprint arithmetic (§3.3) on the same terms.
- **`core/audit.py`** writes the `event` rows an irreversible action must leave behind (§3.1),
  and is distinct from `core/events.py`'s in-process `EventBus` despite the name overlap: one is
  a durable table, the other is a fan-out to WebSocket clients.
- **`core/logtail.py`** is the bounded backwards read behind Settings → Logs (§10.1), kept out
  of `api/logs.py` so "read the last N lines of a possibly-5 MB file" is unit-testable.

**Versioning.** `backend/lftpweb/__init__.py` holds `__version__` as a bare string (no `v`
prefix) and is the only place the version is written. First release is **`0.0.1`**. The API
exposes it at `/api/health` — along with `repo_url`, because `LFTPWEB_REPO_URL` is a *runtime*
container env var while the SPA is built into static files long before that env exists, so a
Vite build-time constant cannot carry it. The UI renders the version bottom-left (§9.1) and
builds the release-notes link from those two fields, degrading to plain text when `repo_url` is
empty — so the link starts working the moment the GitHub repo exists, without a rebuild.

### 12.1 `private_data/` and the gitignore

Everything local, generated, or machine-specific goes under a single gitignored
`private_data/` directory rather than being scattered: build scratch, generated test trees,
sample seedbox fixtures, throwaway compose overrides, captured lftp output kept for debugging.

One directory beats a growing list of ignore patterns for two reasons — a new kind of scratch
file is safe by default instead of one `git add -A` away from being committed, and "is this
safe to share?" has a single answer rather than requiring a scan of the tree.

The gitignore additionally protects the runtime paths that must never be committed: `config/`
(SQLite, logs, backups, install secret, keys), `downloads/`, `staging/`, `*.db`, `.env`, and
`docker-compose.override.yml`.

---

## 13. Build order

Each phase ends at something that can actually be looked at and judged.

> **Status (2026-08-12): all 9 phases below shipped.** ✅ marks each one done. Every phase was
> verified against the real fake seedbox and, for phases 1–3, against real hardware too (see
> `prompts/startnewsession.md`'s "What real hardware taught us" section); **no UI phase was
> ever click-tested in an actual browser** — none exists in the environments this project has
> been built in. Exact commits, test counts, and every non-obvious decision made along the way
> are in `docs/decisions.md`, newest first, and in `prompts/done/`. `sync` mode (below) remains
> genuinely unscheduled — nothing changed that.

**v1**

1. ✅ **Skeleton + container** — FastAPI, SQLite schema/migrations (`host` + `path_queue`),
   config, healthcheck, both compose files, SPA shell with nav / theme / version link.
   *Done when:* container starts, UI loads, `/api/health` is green.
2. ✅ **Scanning + model** — asyncssh connect/test, remote `find` scan, local walk, reconciler,
   read-only Files tree pushed over WS, grouped by queue. Also **credential encryption at rest**
   (§8): this is the phase where a seedbox password first exists, and storing it in plaintext
   until phase 8 is not acceptable even in a dev build. Phase 8 keeps the rest of §8.
   Also builds the §14 fake seedbox, since there is nothing to scan without it.
   *Done when:* the real seedbox tree renders with correct sizes and correct REMOTE_ONLY /
   LOCAL_ONLY / PARTIAL classification.
3. ✅ **Transfer engine + scheduler** — process supervision, job queue, the admission-control
   scheduler (§4.5) with fast lane and priority, FS-derived progress, queue/stop/retry,
   Transfers view with the item drawer. **The load-bearing phase.** *Done when:* you can queue
   a directory, watch it move, stop it, and resume it; and the worked examples in §4.5 hold
   against a real seedbox. Also where the live-retune experiment gets tested or discarded.
4. ✅ **Auto-queue + patterns** (§4.7) — the three pattern kinds and one shared evaluator, wired
   into both the lftp command line and the reconciler; retroactive re-evaluation on pattern
   change; per-queue enable; the live preview. *Done when:* a `file_exclude` of `*.nfo` leaves
   its release `DOWNLOADED`, not permanently `PARTIAL`.
   **Also required here: the mount sentinel and grace period from §7.3.** They are written up
   under `sync` mode because that is where they were first needed, but auto-queue is the point
   at which local absence starts *driving action*, and a network mount that drops makes every
   item look locally absent at once. Without the gate, one NFS hiccup re-downloads the entire
   library — every item reads `REMOTE_ONLY` the moment the mount is unreachable, right back to
   matching its own select pattern (§3.2 rule 3 excludes `REMOVED_LOCAL` from this by default;
   it is `REMOTE_ONLY` alone that does the damage here, and that exclusion offers no protection
   against it). Do not ship auto-queue without it.
5. ✅ **Post-processing + `move` mode** — verify, extract, staging move; and remote deletion on
   verified completion via §7.4, with its audit trail. `move` ships here because it is
   verification plus one delete call, and the delete path it establishes is what `sync` later
   reuses.
6. ✅ **History page** — the `job` / `event` views, grouped by queue, filterable, rendering the
   delete audit legibly.
7. ✅ **Operations** — rotating app log and its viewer, `VACUUM INTO` backup on schedule,
   pre-migration backup, manual backup + download (§10).
8. ✅ **Auth + hardening** — the three auth modes, sessions, API keys, log redaction, rate limits,
   the compose hardening in §11.1, and the full "hold transfers for this host" behavior behind
   the credentials-need-re-entry state. (Credential *encryption* itself moved to phase 2 —
   see above.)
9. ✅ **Polish** — bulk ops, filters, virtualization tuning, docs. Shipped: Files-page
   text/state filters, honest partial-failure reporting on bulk Queue/Stop, and a
   `host_reachable`/`scheduler_alive` header readout. Named rather than silently dropped at the
   time, and **both closed since** (see below): the Settings → Transfer tab, and "Delete local"
   on Files. Still not built: **"Delete remote" from Files** — the only remote deletion in this
   codebase remains `move` mode's verification-gated pipeline (§7.4), and a manual button is a
   materially larger safety conversation, deferred rather than forgotten.

That is the whole of v1. `0.0.1` is the first version (§12).

**Since phase 9 (2026-08-12), still pre-release.** Real use surfaced a set of correctness gaps
that the nine phases' green CI never touched, and the fixes are documented in the sections
above rather than as a tenth phase: the settle gate (§3.3, on by default), the publish
invariant and the single item-view projection (§2.2), state ownership between the three writers
of `item.state` (§3.2 rule 9), the empty-remote-directory reading (§3.2 rule 1), the
`REMOVED_LOCAL`/suppression correction (§3.2 rule 3), `_UNPACK_` extraction staging and the
extraction preconditions (§6), the hash-on-disk fallback's size check (§7.3), local delete and
retention (§3.2), throughput metrics and the Dashboard (§9.2, §10.4), and the Settings →
Transfer tab (§9.3).

**A second such run followed on 2026-08-13**, driven by the user running `move` mode end to end
against a real release for the first time: rar extraction turned out never to have worked at all
and now uses a real decoder (§11), the settle gate gained a wall-clock floor and defaults on
(§3.3), the scan interval became per-queue (§5), archive cleanup after extraction landed (§6), a
local delete now marks its whole subtree and picks each row's state per row (§3.2 rule 3), a
`move`-mode outcome survives the `LOCAL_ONLY` reading that follows its own remote delete and a
row that leaves both trees is resolved rather than frozen (§3.2 rule 9, §7.3), and the Files row
gained lifecycle icons, inline progress, sorting, and the item detail drawer (§9.2).

`docs/decisions.md` carries the reasoning for each, newest first.

**Not scheduled.** `sync` mode — local-delete propagation with the mount sentinel gate, grace
period, dry-run, and rate-based backstop (§7.3) — is a possible later feature, built only if it
proves wanted. Nothing in phases 1–9 depends on it. If it is ever picked up it depends on 3 and
5, and wants 6 to review what it did; *done when* a dry-run on a real tree lists exactly the
right deletes, and an unmounted local root propagates none.

---

## 14. Verification

**Integration: a real fake seedbox.** `docker-compose.test.yml` runs an
`linuxserver/openssh-server` (or plain `sshd`) container seeded with generated files of known
sizes. Because we have SSH+SFTP, this exercises the *actual* path — real ssh, real sftp, real
lftp, real resume — not mocks. This is the single highest-value piece of test infrastructure
in the project and it's cheap.

**Unit tests**, concentrated on the pure functions that are historically where bugs live:
- `.lftp-pget-status` effective-size math (§4.4a)
- `.lftp` temp-suffix matching (§4.4b)
- `find -printf` record parsing, including paths with tabs, newlines, and non-UTF-8 bytes
- lftp error classification
- EMA/ETA behavior
- **A reconciler state-matrix table test** — every combination of (remote present/size,
  local present/size, active job y/n, persisted history, `sync_mode`) → expected state and
  expected action. SeedSync's real bugs lived here; a table test pins all of §3.2 at once.
- **The admission scheduler (§4.5)** — a table test over the worked examples: two-at-half,
  one-at-full, the blocked third, refill-at-half on completion, the `min_share_floor` loop
  reducing `ready`, fast-lane items bypassing headroom, and "start now at max bandwidth"
  oversubscribing then freezing admissions. Pure function over (N, B, running, queue) → the
  admit list, so it tests without a single subprocess.
- Worst-case connection-count arithmetic and its warning threshold.
- **Pattern evaluator (§4.7)** — glob-vs-substring dispatch, case-insensitivity, skip-beats-
  select, empty-select behavior with and without *patterns-only*.
- **Excluded files do not count toward completeness** — a release with `*.nfo` excluded reaches
  `DOWNLOADED`, and does not sit `PARTIAL` forever. The single highest-value test in this
  group; it is the failure that would silently break the pipeline for every filtered item.
- **Loose top-level files (§4.7)** — a root-level `Movie.mkv` is an item, matched by a `*.mkv`
  select and transferred with `pget`; a root-level `notes.nfo` is suppressed by a `*.nfo`
  `file_exclude` rather than downloaded; and a `*.mkv` select does **not** match a directory
  containing an mkv.
- **A directory whose children are all excluded** is `DOWNLOADED` even though the local
  directory was never created — and, the other reading of the same arithmetic, **a directory
  with no remote children at all** is `REMOTE_ONLY` until it exists locally (§3.2 rule 1). The
  two must be asserted side by side; a fix for either that is keyed on local presence breaks
  the other.
- **The settle gate (§3.3)** — the fingerprint/counter arithmetic as a pure function, and the
  reproduction that motivated it against the fake seedbox: a release directory that gains a
  second file between two scans must not read `DOWNLOADED` in between, and a growing file must
  not be auto-queued while it grows.
- **Suppression (§4.6)** — a `STOPPED` item is not resurrected by an auto-queue pass whose
  patterns still match it; likewise `FAILED` after exhausted retries; and a manual re-queue
  does clear both.
- **Retry classification** — transient classes retry to `max_attempts` then stop; permanent
  classes never retry at all.

**Sync-mode tests — the highest-consequence logic in the project**, because the operation under
test is irreversible. Most are deferred with `sync` (§7); the verification gate and audit
assertions are v1, because `move` deletes too.

- **Mount gate.** Simulate an unmounted / missing / sentinel-less local root against a tree of
  `DOWNLOADED` items in `sync` mode and assert **zero** deletes are propagated — and that the
  condition is raised, not silently swallowed. Include the nastiest case: root exists, is
  readable, is empty, has no sentinel.
- **Grace period.** An item absent for less than the window propagates nothing; absent across
  the full window propagates once and only once. An item that reappears mid-window clears
  `first_missing_at`.
- **Mode behavior differences.** The same observation (`DOWNLOADED` item now locally absent)
  against all three modes: `copy` → no remote action ever; `sync` → propagate after the rails;
  `move` → already deleted at verified completion, nothing further.
- **An item lftpweb deleted itself is never re-queued, under either `AutoQueueSettings.
  re_download_externally_removed` value** — the regression that would otherwise re-download
  everything the retention sweep just removed, on a 30-second loop. Asserted alongside its
  default-off counterpart (§3.2 rule 3): a `REMOVED_LOCAL` item nothing in this codebase
  touched stays excluded at the default setting, becomes eligible once the setting is turned
  on, and one carrying `auto_queue_suppressed` from our own delete never is, at either setting
  value. A fourth, named-for-the-scenario test covers the regression directly: an `*arr`
  importer moving a completed release out of the local downloads directory must not cause a
  re-download on the next scan.
- **Verification gate.** A `CORRUPT` or unverified item in `move` / `sync` propagates no
  delete, and the withheld delete is recorded.
- **Dry-run** performs no remote mutation while producing the identical decision list to a
  live run over the same fixture.
- **Audit completeness.** Every propagated delete leaves an `event` row sufficient to
  reconstruct what was deleted and why.

**Resume test:** start a large transfer, `docker kill` the container mid-flight, restart, and
confirm it resumes from the partial rather than restarting, and that the item reads `PARTIAL`
in between.

**Manual acceptance:** queue a directory → live speed/ETA → stop mid-transfer → verify
`PARTIAL` → re-queue → completion → extraction → staging move. Then, on a `sync` queue against
the fake seedbox: delete locally → watch the grace period elapse → confirm the remote delete
and its History entry.

---

## 15. Risks

Ordered roughly by consequence.

All 15 rows were re-reviewed at phase 9 (2026-08-12) now that v1 is fully built, without
rewriting the original reasoning — each cell keeps its original mitigation text and gets a
**Status (phase 9):** sentence appended saying whether the risk is now closed, still live, or
superseded, and why.

| # | Risk | Mitigation / status |
|---|---|---|
| 15.1 | **Misconfiguration hazard: pointing a `move` queue at a live torrent data directory** rather than a hardlink pickup dir destroys seeding torrents (§7.1). The safety property belongs to the directory you point at, not to lftpweb. | The one delete-related risk that is *live in v1*, because `move` ships. Warning in the doc *and* inline at the mode selector; explicit confirmation to leave `copy`; `copy` is the default. **Status (phase 9): still live, as designed.** Phase 5 shipped exactly this — the doc warning, the inline mode-selector warning, and the confirmation checkbox all exist in code — but the checkbox is enforced client-side only (§7.1, `docs/decisions.md`'s phase 5 entry #13) and, like every other UI in this project, has never been click-tested in a browser. The risk this row names is about the *directory*, not the software, so it cannot be "closed" by any amount of testing — it is a standing warning to read before ever setting `move`. |
| 15.2 | **Bandwidth goes under-utilized** when a job keeps its half-share after its partner finishes (§4.5). Allocations are never re-shaped. | Accepted, and it is the price of not needing a control channel. The build-phase-3 live-retune experiment closes it without redesign if it pans out. **Status (phase 9): accepted, not closed.** The live-retune experiment (holding lftp's stdin open + `set net:limit-total-rate` mid-job) was confirmed *working* in phase 3 but deliberately never wired into production — admission control stands alone, as the design required. Under-utilization remains a known, accepted trade rather than something later phases revisited. |
| 15.3 | *(Deferred with `sync`, §7.)* **The mount sentinel would be a single point of failure for an irreversible operation** (§7.3). Because move-on-import makes local deletes routine (§7.2), there is no anomaly signal to fall back on — if the gate were wrong, `sync` would wipe the seedbox. | **Not a v1 risk: `sync` is not being built.** Recorded because it is the reason the feature is deferred, and the thing to re-read before anyone reconsiders. **Status (phase 9): unchanged.** `sync` shipped in none of the 9 phases and remains genuinely unscheduled (§13) — this row's concern was never exercised because the code it describes was never written. |
| 15.4 | *(Deferred with `sync`.)* **Routine deletes mean anomaly detection cannot be a safeguard.** A count-based circuit breaker false-positives on every bulk import, so it was rejected outright rather than tuned. | Same status as 15.3. If `sync` is ever picked up, the rate-based backstop is explicitly a backstop, not a safeguard — the gate would carry the load. **Status (phase 9): unchanged**, same reasoning as 15.3. |
| 15.5 | **Restore without the encryption key** leaves credentials unrecoverable (§8, §10.2). | Deliberate — it keeps backups free of secrets. Handled as a designed "credentials need re-entry" state rather than a wave of `AUTH_FAILED` jobs. **Status (phase 9): closed.** Phase 8 built exactly this: `HostConfig.credentials_need_reentry`, `core/queue.py._admit` holding every scheduler decision for the host, and `core/engine.py.scan_queue` failing that queue's scan cleanly before ever attempting a doomed connection — proven by `tests/test_credentials_reentry.py`, not just implemented. Phase 7 separately proved the encryption secret is byte-for-byte absent from a `VACUUM INTO` backup. The frontend's `CredentialsBanner` surfacing it has not been click-tested (see 15.1's caveat). |
| 15.6 | **NFS identity mismatch** — wrong uid/gid, or an entrypoint that insists on chowning a `root_squash` share (§11.2). | Chown `/config` only; treat data-volume chown failure as a warning; verify writability at startup and name the path and effective uid/gid in the error. **Status (phase 9): mitigated and exercised on real hardware**, not just designed. The first real deployment (phase 1–3 era, see `prompts/startnewsession.md`'s "What real hardware taught us") hit and fixed the closely-related identity problem this row anticipated: OpenSSH fataling with "No user exists for uid N" because asyncssh's own `getpass.getuser()` call crashes under this project's numeric-uid convention. Both fixes are load-bearing in the shipped entrypoint/`core/remote.py`, not just documented. |
| 15.7 | **`find -printf` is GNU-specific** | Stdlib script fallback over SFTP; needs a one-line check against the actual seedbox in build phase 2 (§13). **Status (phase 9): continuously exercised, beyond the one-line check this row originally asked for.** `docker-compose.test.yml` runs two fake-seedbox variants side by side — one GNU `findutils`, one busybox — specifically so both remote-scan code paths run on every single test suite invocation from phase 2 onward, not once and never again. |
| 15.8 | **Sparse-file progress depends on `.lftp-pget-status`** (§4.4) | Pinned by unit tests; degrades to raw size — monotonic, and never wrong about completion, because completion is the exit code. **Status (phase 9): pinned by tests and hardened on real hardware.** Phase 3's real-hardware run found `pget:save-status` defaults to a sampler-breaking 10s (a `.lftp-pget-status` sidecar didn't exist yet at the 1s/2s/3s marks a ~1 Hz sampler inspects); every job's rc file now sets `pget:save-status 1s`, closing the gap this row's own mitigation was written to tolerate rather than fix. |
| 15.9 | **Many concurrent small files** make per-file stat sampling expensive | The sampler only stats the active set, never the tree. If a mirror runs thousands of files at once, fall back to sampling the job's local subtree total. **Status (phase 9): unchanged — still an open, never-load-tested assumption.** The sampler's active-set-only design shipped as described in phase 3, but no phase load-tested thousands of concurrent small files against it to confirm the stated fallback actually engages correctly at that scale; nothing in the real-hardware findings (a 1.29 GB single file) exercised this path. |
| 15.10 | **Filenames with odd bytes** | `surrogateescape` end to end (scan → DB → JSON → UI), tested explicitly. **Status (phase 9): closed as designed, with one narrow, named, accepted edge case.** `core/remote.py`'s scan parser anchors on the `find -printf` record header rather than naively splitting on `\n`, which handles "paths can contain newlines" (§15.10) in practice — but a path containing the *exact* bytes of a record header immediately after a literal newline would still misparse. A property of the specified `find -printf` output format itself, not something deviating from it would fix; recorded in `prompts/startnewsession.md`'s traps list, not silently accepted as impossible. |
| 15.11 | **No `jobs -v` anywhere** | If per-connection chunk detail is ever wanted, add it as a strictly optional, failure-tolerant *enrichment* — never a source of truth (§1.3). **Status (phase 9): holding, by design, across all 9 phases.** No phase reintroduced `jobs -v` parsing as a source of truth. The Item drawer's per-file breakdown (§9.2) comes entirely from the reconciler's local-vs-remote comparison instead — exactly the alternative §1.3 argues `jobs -v` could never have offered in the first place. |

**Open questions:** none outstanding. The four carried by earlier drafts are settled — path
queues under a single host (§3.1, §9), `copy` and `move` shipping with backwards `sync`
deferred entirely (§7), no `*arr` integration, and History as its own page grouped by queue
(§9.2).
