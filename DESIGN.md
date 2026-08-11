# lftpweb — Design Document

*A containerized web interface for keeping a local directory in sync with a seedbox, using
lftp as the transfer engine.*

Sections are numbered so feedback can be given by reference (e.g. "§4.3 — no, do it this way").

**Status:** draft, pending review. Nothing implemented yet.

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
  connection_overrides JSON,         -- net:connection-limit, net:socket-buffer, timeouts, retry
  created_at)

-- A named path queue: one remote path → one local path, plus the policy for that mapping.
-- Shown in the UI simply as a "Queue" with the user's own name ("TV", "Movies", "Music").
path_queue(
  id, host_id, name, remote_path, local_path, staging_path NULL, enabled,
  sync_mode TEXT,                    -- 'copy' | 'move' | 'sync'   (default 'copy', §7)
  auto_queue_enabled, auto_extract, auto_verify,
  created_at)
-- Note: no bandwidth / concurrency / parallelism columns. Those are site-level (§4.5) —
-- a queue governs what and where, never how fast.

pattern(id, queue_id NULL, kind TEXT, -- 'include' | 'exclude'
        expr TEXT, enabled, created_at)

-- One row per item we have ever cared about; the durable lifecycle record
item(
  id, queue_id, rel_path, is_dir,
  remote_size, local_size, remote_mtime,
  state TEXT, substate TEXT NULL,
  first_seen_at, downloaded_at NULL, extracted_at NULL, verified_at NULL,
  first_missing_at NULL,             -- when local absence was first observed → grace period (§7)
  remote_deleted_at NULL,            -- when we deleted the remote copy, if we did
  error_class NULL, error_detail NULL,
  UNIQUE(queue_id, rel_path))

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
DOWNLOADED     complete
VERIFYING / VERIFIED / CORRUPT
EXTRACTING / EXTRACTED / EXTRACT_FAILED
FAILED         last attempt failed; carries error_class + output_tail
LOCAL_ONLY     present locally, absent remotely, never tracked
REMOVED_LOCAL  was downloaded, now absent locally, remote still present
REMOVED_BOTH   was downloaded, absent locally, remote deleted by us — terminal, kept as history
```

`DELETED_REMOTE` used to cover the first of those two. It was ambiguous the moment remote
deletion became real: "gone locally" and "gone from both sides" are now distinct states with
distinct consequences, and a name that could be read either way is a bug waiting to happen.

Six rules that are easy to get wrong and that want review:

1. A **directory** is `DOWNLOADED` only when every non-directory descendant that has a remote
   size is itself complete. Otherwise `PARTIAL`.
2. `local_size < remote_size` with **no active job** ⇒ `PARTIAL`, never `DOWNLOADED`. This is
   what makes a stopped transfer resumable rather than silently "done".
3. Previously downloaded, now absent locally, still present remotely ⇒ `REMOVED_LOCAL`, and
   auto-queue must **not** re-fetch it. (It left on purpose — either you deleted it or an
   importer moved it.)
4. **Remote size is a moving target** — a torrent may still be downloading on the seedbox.
   Never latch it; recompute completeness on every scan.
5. **The same detection drives different actions by mode** (§7). `REMOVED_LOCAL` is one
   observation — "we downloaded this and it is no longer here" — and the queue's `sync_mode`
   decides what it means. In `copy` it means *do not re-queue this*, and that is the end of it.
   In `sync` it is additionally the trigger to propagate the delete to the remote, subject to
   every rail in §7.3. `move` never reaches `REMOVED_LOCAL` for this purpose; it deletes on
   verified completion instead.
6. `REMOVED_BOTH` is terminal and is **never** re-queued or re-scanned into `REMOTE_ONLY`. If
   the same `rel_path` reappears remotely later it is a genuinely new item as far as the
   lifecycle is concerned; the old row stays as history.

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
- Retry with exponential backoff **only** on transient classes; `AUTH_FAILED` and
  `PERMISSION_DENIED` stop and surface immediately rather than hammering the seedbox.
- Concurrency: `asyncio.Semaphore(max_concurrent_jobs)`; the **job queue** is ours, so
  reordering, priority, and pause-all are trivial. ("Job queue" throughout this document means
  the pending-transfer queue owned by `core/queue.py`. A user-facing **queue** is a named path
  queue — see §3.1 and §9.)

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
| `small_lane_reserve` | bandwidth carved out for the fast lane | 10% of B, min 1 MB/s |
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
cost is that the slice sits idle when no small items are running. Letting the fast lane run
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
in between.

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
and per path queue.

1. **Verify** — use `.sfv` / `.md5` sidecars when present; otherwise optional hash-on-disk.
   Result: `VERIFIED` or `CORRUPT`.
2. **Extract** — rar (`unrar`), zip/7z (`7z`), tar/gz/bz2/xz (stdlib). Multi-part rar sets
   extract from the first volume only. Optional password list. Target: in place, or a
   configured directory.
3. **Move** — staging → final destination. `os.rename` fast path, cross-device copy+fsync+
   unlink fallback. This is the "download to NVMe, settle on the array" workflow.

Failures are recorded on the item (`EXTRACT_FAILED`, `CORRUPT`) and never abort the pipeline
for other items.

**Verification stops being optional garnish whenever `sync_mode != copy`.** In `copy` mode a
`CORRUPT` result is an annoyance you can re-download from. In `move` and `sync` it is the gate
on an irreversible remote delete: verification is the only thing standing between a truncated
download and permanently losing the only good copy. Consequences:

- For a queue in `move` or `sync` mode, `auto_verify` is forced on and cannot be turned off in
  the UI. It is not a per-queue preference in those modes; it is part of the mode.
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
| `sync` | Copy, plus propagate *local* deletes back to the remote. | **phase 2** |

`copy` is the default and is the only mode that is safe under every deployment shape. The other
two perform an irreversible operation on a machine we don't own, so the rest of this section is
mostly about why that is acceptable here and what keeps it acceptable.

> **`sync` is designed now and built in phase 2.** v1 implements `copy` and `move` only. The
> whole section stays because the seam has to exist before the feature does — the state model
> (§3.2), the `event` audit trail (§3.1), and the deletion mechanism (§7.4) are all v1 work that
> `sync` later plugs into, and the safety reasoning in §7.1–7.3 is the most valuable part of the
> design regardless of when it ships. What defers with it is everything that exists *solely* to
> make local-delete propagation safe: the mount sentinel, the grace period and
> `item.first_missing_at`, dry-run mode, and the rate-based backstop (§7.3).
>
> What does **not** defer: `move` also deletes remote data, so every rail that gates *any*
> remote delete is v1 — verification before deletion, deletes through our own asyncssh path
> (§7.4), and the full `event` audit trail.

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

*Ships with `sync` in phase 2, except the verification gate and audit trail, which are v1
because `move` needs them.*

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
- **Verification gate.** `move` deletes only after the item verifies (§6). `sync` propagates only
  for items that reached a verified-complete state. Never deleted on a size comparison alone.
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
restoring to a fresh install cannot recover the seedbox password.

That has to be a designed behavior, not a documented caveat. On startup, if a credential blob
will not decrypt with the current install key, lftpweb must:

- mark the host **"credentials need re-entry"** rather than crashing or retrying,
- hold all transfers for that host instead of spawning jobs that fail,
- surface a banner that lands the user on Settings → Connection.

The failure mode this prevents is a restore that produces a pile of `AUTH_FAILED` jobs and no
explanation of why.

---

## 9. Frontend

React + TypeScript + Vite + Tailwind. TanStack Query for REST; **one WebSocket** delivering a
full model snapshot on connect and deltas thereafter.

**Queues are the organizing axis.** Browsing, active transfers, and history are all filterable
and groupable by the named path queue an item belongs to (§3.1), so "what's happening in TV"
is one click everywhere rather than a mental filter.

### 9.1 Shell: left nav, top tabs, stats header

```
┌─ header ───────────────────────────────────────────────────────────────┐
│  ▲ 8.2 MB/s    ⣿ 9.0 / 10 allocated    ⏳ 12 queued (48 GB)    24h: 310 GB │
├──────────┬─────────────────────────────────────────────────────────────┤
│  Files   │  [ Connection · Queues · Transfer · Post-processing ·        │
│  Xfers   │    Logs · Backup · Auth ]        ← tabs, only where a        │
│  History │                                    section has >1 page       │
│  Settings│                                                              │
│          │                                                              │
│ ──────── │                                                              │
│ v0.1.0 ↗ │  ← bottom-left, links to the GitHub release notes            │
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
with shift-range, bulk *Queue / Stop / Delete local / Delete remote*, text search and state
filters.

**Transfers** — the job queue. Rows stay deliberately plain:

```
Some.Release.S03E04.2160p    [downloading]   18 files   62%   4.1 MB/s   ETA 12m
```

- Visible status vocabulary is **queued / downloading / downloaded**. The other internal states
  (§3.2) surface only on rows where they actually apply, rather than expanding everyone's
  mental model to twelve chips.
- **Move to top** on each row (§4.5); default order is oldest-first.
- **Start now at max bandwidth** as a per-row action, with its oversubscription behavior
  explained inline the first time it's used.
- Failures show the error class plus the captured lftp output tail.

**Item drawer.** Clicking a row opens a **side drawer** — not a modal, because file lists get
long and the queue should stay visible — listing the files inside that item: name, size,
transferred, per-file progress, status. Virtualized; a release can carry hundreds of files.

This view is cheap for us specifically because of §1.3: per-file status is just the reconciler's
local-vs-remote size comparison over the whole tree. `jobs -v` only ever names the handful of
files lftp is actively touching, so a complete per-file breakdown isn't something the parsing
approach could have offered at all.

**History** — **its own page**, not a panel: the `job` and `event` tables, **grouped by queue**,
filterable by state, error class, date range, and event kind. This is where remote deletes are
reviewed, so it must render the delete audit trail (§7.3) legibly — what was deleted, from
which queue, under which mode, and what gated it. The only view that answers "what did it
remove last night".

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

**Archive tooling: `7zz` alone** (the `7zip` package — 7-Zip proper, not p7zip). 7-Zip 21.07+
extracts RAR and RAR5 natively, so one binary covers rar / rar5 / zip / 7z / tar / gz / bz2 /
xz. This removes the usual mess of pulling `unrar` from a non-free repo, or shipping
`unrar-free` and discovering it can't read the RAR5 archives that scene releases actually use.
(7-Zip's RAR decoder derives from the unRAR source, whose licence forbids using it to build a
RAR-compatible *compressor*. We only extract, so this is fine — worth knowing, not worth
worrying about.)

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
- **`cap_drop: ALL`** — the app needs no capabilities at all.
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
  main.py config.py db.py models.py auth.py logsetup.py
  api/{files,jobs,settings,auth,health,logs,backup}.py   api/ws.py
  core/engine.py      core/remote.py     core/local_scan.py   core/lftp.py
  core/queue.py       core/scheduler.py  core/progress.py     core/reconcile.py
  core/autoqueue.py   core/postprocess.py core/events.py      core/backup.py
  core/sync.py                     # phase 2 — mount gate, grace period, delete decisions
  remote_agent/scan_fs.py          # stdlib-only fallback scanner
frontend/   Vite app — routes Files / Transfers / History / Settings
tests/      unit + integration
private_data/   gitignored — local scratch, test fixtures, sample trees, scratch compose (§12.1)
```

`core/scheduler.py` is deliberately separate from `core/queue.py`: the queue owns job lifecycle
and process supervision, the scheduler owns the admission decision (§4.5). The admission rule
is the piece most likely to change and the piece most worth unit-testing in isolation, so it
does not belong tangled up with subprocess handling.

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

**v1**

1. **Skeleton + container** — FastAPI, SQLite schema/migrations (`host` + `path_queue`),
   config, healthcheck, both compose files, SPA shell with nav / theme / version link.
   *Done when:* container starts, UI loads, `/api/health` is green.
2. **Scanning + model** — asyncssh connect/test, remote `find` scan, local walk, reconciler,
   read-only Files tree pushed over WS, grouped by queue. *Done when:* the real seedbox tree
   renders with correct sizes and correct REMOTE_ONLY / LOCAL_ONLY / PARTIAL classification.
3. **Transfer engine + scheduler** — process supervision, job queue, the admission-control
   scheduler (§4.5) with fast lane and priority, FS-derived progress, queue/stop/retry,
   Transfers view with the item drawer. **The load-bearing phase.** *Done when:* you can queue
   a directory, watch it move, stop it, and resume it; and the worked examples in §4.5 hold
   against a real seedbox. Also where the live-retune experiment gets tested or discarded.
4. **Auto-queue** — patterns (case-insensitive substring OR glob), applied retroactively when a
   pattern is added, exclude globs, per-queue enable.
5. **Post-processing + `move` mode** — verify, extract, staging move; and remote deletion on
   verified completion via §7.4, with its audit trail. `move` ships here because it is
   verification plus one delete call, and the delete path it establishes is what `sync` later
   reuses.
6. **History page** — the `job` / `event` views, grouped by queue, filterable, rendering the
   delete audit legibly.
7. **Operations** — rotating app log and its viewer, `VACUUM INTO` backup on schedule,
   pre-migration backup, manual backup + download (§10).
8. **Auth + hardening** — the three modes, credential encryption, the credentials-need-re-entry
   state (§8), log redaction, rate limits, the compose hardening in §11.1.
9. **Polish** — bulk ops, filters, virtualization tuning, docs.

**phase 2**

10. **`sync` mode** — local-delete propagation, the mount sentinel gate, grace period, dry-run,
    and the rate-based backstop (§7.3). Depends on 3, 5, and wants 6 to review what it did.
    *Done when:* a dry-run on a real tree lists exactly the right deletes, and an unmounted
    local root propagates none.

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

**Sync-mode tests — the highest-consequence logic in the project**, because the operation under
test is irreversible. Most of these land with `sync` in phase 2; the verification gate and audit
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
- **`REMOVED_LOCAL` is not re-queued in `copy` mode** — the auto-queue regression that would
  otherwise re-download everything the user just deleted.
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

| # | Risk | Mitigation / status |
|---|---|---|
| 15.1 | **The mount sentinel is a single point of failure for an irreversible operation** (§7.3). Because move-on-import makes local deletes routine (§7.2), there is no anomaly signal left to fall back on — if the gate is wrong, `sync` wipes the seedbox. | The highest-value tests in the project (§14) target exactly this. Ships in phase 2, not v1, so it gets built after the surrounding machinery is proven rather than alongside it. `copy` remains the default. |
| 15.2 | **Routine deletes mean anomaly detection cannot be a safeguard.** A count-based circuit breaker false-positives on every bulk import, so it was rejected outright rather than tuned. | The rate-based backstop is explicitly a backstop, not a safeguard. The gate carries the load, and the design says so instead of implying defence in depth that isn't there. |
| 15.3 | **Misconfiguration hazard: pointing a `move` / `sync` queue at a live torrent data directory** rather than a hardlink pickup dir destroys seeding torrents (§7.1). The safety property belongs to the directory, not to lftpweb. | Warning in the doc *and* inline at the mode selector; explicit confirmation to leave `copy`; dry-run offered on the way in. |
| 15.4 | **Bandwidth goes under-utilized** when a job keeps its half-share after its partner finishes (§4.5). Allocations are never re-shaped. | Accepted, and it is the price of not needing a control channel. The Phase 3 live-retune experiment closes it without redesign if it pans out. |
| 15.5 | **Restore without the encryption key** leaves credentials unrecoverable (§8, §10.2). | Deliberate — it keeps backups free of secrets. Handled as a designed "credentials need re-entry" state rather than a wave of `AUTH_FAILED` jobs. |
| 15.6 | **NFS identity mismatch** — wrong uid/gid, or an entrypoint that insists on chowning a `root_squash` share (§11.2). | Chown `/config` only; treat data-volume chown failure as a warning; verify writability at startup and name the path and effective uid/gid in the error. |
| 15.7 | **`find -printf` is GNU-specific** | Stdlib script fallback over SFTP; needs a one-line check against the actual seedbox in build phase 2. |
| 15.8 | **Sparse-file progress depends on `.lftp-pget-status`** (§4.4) | Pinned by unit tests; degrades to raw size — monotonic, and never wrong about completion, because completion is the exit code. |
| 15.9 | **Many concurrent small files** make per-file stat sampling expensive | The sampler only stats the active set, never the tree. If a mirror runs thousands of files at once, fall back to sampling the job's local subtree total. |
| 15.10 | **Filenames with odd bytes** | `surrogateescape` end to end (scan → DB → JSON → UI), tested explicitly. |
| 15.11 | **No `jobs -v` anywhere** | If per-connection chunk detail is ever wanted, add it as a strictly optional, failure-tolerant *enrichment* — never a source of truth (§1.3). |

**Open questions:** none outstanding. The four carried by earlier drafts are settled — path
queues under a single host (§3.1, §9), all three sync modes designed with `sync` deferred to
phase 2 (§7), no `*arr` integration, and History as its own page grouped by queue (§9.2).
