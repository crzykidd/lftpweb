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

### 1.2 Why not just run SeedSync

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
     ├── RemoteScanner   (core/remote.py)      asyncssh → remote tree
     ├── LocalScanner    (core/local_scan.py)  os.scandir → local tree
     ├── ProgressSampler (core/progress.py)    ~1 Hz stat of active files only
     ├── TransferQueue   (core/queue.py)       semaphore + one lftp subprocess per job
     ├── AutoQueue       (core/autoqueue.py)   pattern matching on newly-seen remote items
     ├── PostProcessor   (core/postprocess.py) verify → extract → move
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
| Queue & concurrency | delegated to lftp `cmd:queue-parallel` | owned in Python (asyncio semaphore) → reorder, priority, per-job settings |
| Remote scan | scp `scan_fs.py`, md5-compare, shell detection, pexpect `ssh` | one `find -printf` over asyncssh; script fallback |
| SSH layer | pexpect typing passwords at prompts | asyncssh (keys, agent, password, known_hosts) |
| Persistence | JSON persist files | SQLite — history, restart-safe, auditable |
| Failure blast radius | whole pair stalls | one job fails; others continue |

---

## 3. Data model

### 3.1 SQLite schema (`/config/lftpweb.db`)

```sql
-- Connection + global settings live as typed key/value so the UI can edit anything
setting(key TEXT PK, value JSON, updated_at)

path_pair(
  id, name, remote_path, local_path, staging_path NULL, enabled,
  auto_queue_enabled, auto_extract, auto_verify, auto_delete_remote,
  overrides JSON,          -- per-pair lftp tuning, nullable fields fall back to global
  created_at)

pattern(id, pair_id NULL, kind TEXT, -- 'include' | 'exclude'
        expr TEXT, enabled, created_at)

-- One row per item we have ever cared about; the durable lifecycle record
item(
  id, pair_id, rel_path, is_dir,
  remote_size, local_size, remote_mtime,
  state TEXT, substate TEXT NULL,
  first_seen_at, downloaded_at NULL, extracted_at NULL, verified_at NULL,
  error_class NULL, error_detail NULL,
  UNIQUE(pair_id, rel_path))

-- One row per transfer attempt — this is the audit trail SeedSync lacks
job(
  id, item_id, kind TEXT,           -- 'mirror' | 'pget'
  state TEXT,                        -- queued|running|succeeded|failed|cancelled
  priority INT, attempt INT,
  pid NULL, argv JSON, lftp_settings JSON,
  bytes_start, bytes_done, bytes_total,
  started_at, finished_at, exit_code NULL,
  error_class NULL, output_tail TEXT NULL)

event(id, ts, level, item_id NULL, job_id NULL, kind, message)
```

Rationale: SeedSync keeps five parallel name-sets in JSON (`downloaded`, `extracted`,
`extract_failed`, `validated`, `corrupt`). Collapsing those into one `item` row with a state
column plus timestamps removes a category of "sets disagree with each other" bugs and gives
the UI a real history view for free.

### 3.2 File states

```
REMOTE_ONLY   remote exists, nothing local
QUEUED        accepted into our queue, no process yet
DOWNLOADING   an lftp process is running for it
PARTIAL       local < remote (or incomplete children), no active job — re-queueable
DOWNLOADED    complete
VERIFYING / VERIFIED / CORRUPT
EXTRACTING / EXTRACTED / EXTRACT_FAILED
FAILED        last attempt failed; carries error_class + output_tail
LOCAL_ONLY    present locally, absent remotely, never tracked
DELETED_REMOTE was downloaded, still remote, deliberately removed locally
```

Four rules that are easy to get wrong and that want review:

1. A **directory** is `DOWNLOADED` only when every non-directory descendant that has a remote
   size is itself complete. Otherwise `PARTIAL`.
2. `local_size < remote_size` with **no active job** ⇒ `PARTIAL`, never `DOWNLOADED`. This is
   what makes a stopped transfer resumable rather than silently "done".
3. Previously downloaded, now absent locally, still present remotely ⇒ `DELETED_REMOTE`, and
   auto-queue must **not** re-fetch it. (You deleted it on purpose.)
4. **Remote size is a moving target** — a torrent may still be downloading on the seedbox.
   Never latch it; recompute completeness on every scan.

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
- Concurrency: `asyncio.Semaphore(max_concurrent_jobs)`; the queue is ours, so reordering,
  priority, and pause-all are trivial.

### 4.4 Progress without parsing

`ProgressSampler` ticks at ~1 Hz and stats **only the active file set** — the files under
currently-running jobs — never a full tree walk. From that it computes transferred bytes,
instantaneous speed, and ETA, with EMA smoothing (α ≈ 0.3) so the UI doesn't jitter.

Two lftp on-disk conventions must be honored, because raw `st_size` lies:

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

### 4.5 Bandwidth limiting — a known trade-off

`net:limit-total-rate` is per-process. With N concurrent processes, a global cap has to be
applied as `global_limit / max_concurrent_jobs` per job, which under-utilizes when fewer than
N jobs are running.

Exact global shaping would require going back to one shared lftp process — which is precisely
the design we're rejecting. **Recommendation: accept the approximation**; it's a soft cap
whose purpose is "don't saturate my uplink", not precise QoS. Per-pair overrides are
available. Flagged explicitly because it's the one place this design is measurably worse
than SeedSync's.

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

The same asyncssh connection serves remote deletes and the Settings → *Test connection*
button, so there's exactly one code path for "can we reach the seedbox".

---

## 6. Post-processing

`core/postprocess.py`, triggered on transition to `DOWNLOADED`, executed in a thread pool,
one item at a time by default (configurable). Each step is independently toggleable globally
and per path-pair.

1. **Verify** — use `.sfv` / `.md5` sidecars when present; otherwise optional hash-on-disk.
   Result: `VERIFIED` or `CORRUPT`.
2. **Extract** — rar (`unrar`), zip/7z (`7z`), tar/gz/bz2/xz (stdlib). Multi-part rar sets
   extract from the first volume only. Optional password list. Target: in place, or a
   configured directory.
3. **Move** — staging → final destination. `os.rename` fast path, cross-device copy+fsync+
   unlink fallback. This is the "download to NVMe, settle on the array" workflow.

Failures are recorded on the item (`EXTRACT_FAILED`, `CORRUPT`) and never abort the pipeline
for other items.

---

## 7. Auth and security

`AUTH_MODE = none | password | proxy`, default `none` (LAN-friendly):

- **`password`** — single user, argon2id hash in SQLite, HTTP-only `SameSite=Lax` session
  cookie, CSRF token required on mutating requests, rate-limited login.
- **API key** — `X-API-Key` header, accepted independently of the mode, for scripts.
- **`proxy`** — trust a configurable identity header (`Remote-User` by default), *only* when
  the request originates from a configured trusted CIDR. Without the CIDR check this mode is
  a bypass, so it is not optional.

Seedbox credentials are encrypted at rest with a key derived from a per-install secret in
`/config`. Log redaction as described in §4.2.

---

## 8. Frontend

React + TypeScript + Vite + Tailwind. TanStack Query for REST; **one WebSocket** delivering a
full model snapshot on connect and deltas thereafter.

**Files** — virtualized tree (must stay smooth at 10k+ rows), per row: state chip, progress
bar, size, speed, ETA. Expand/collapse, multi-select with shift-range, bulk *Queue / Stop /
Delete local / Delete remote*, text search and state filters.

**Transfers** — active jobs with live throughput, aggregate bandwidth, queue reorder
(drag), and for failures the error class plus the captured lftp output.

**Settings** — connection (with *Test*), path pairs, patterns with a **live "what would this
match" preview** against the current remote tree, transfer tuning, post-processing, auth.

**History** — the `job` and `event` tables, filterable. Free from the schema; genuinely useful
when something failed overnight.

Dark mode default.

### 8.1 Advanced options exposed

Global, with per-pair override, matching SeedSync's surface and then some:

`mirror:parallel-transfer-count` · `mirror:use-pget-n` · `pget:default-n` ·
`pget:min-chunk-size` · `net:connection-limit` · `net:limit-total-rate` · `net:timeout` ·
`net:max-retries` · `net:reconnect-interval-base` / `-multiplier` · `net:socket-buffer` ·
`mirror:parallel-directories` · `xfer:use-temp-file` · `sftp:max-packets-in-flight` ·
`sftp:connect-program` · include/exclude globs

…plus a free-text **"extra lftp settings"** box injected verbatim into every job's rc file, so
a power user is never blocked waiting for us to add a checkbox.

---

## 9. Container

Multi-stage build: `node:22` builds the SPA → `python:3.13-slim` runtime carrying `lftp`,
`openssh-client`, `unrar`, `p7zip-full`, `tini`.

- **Volumes:** `/config` (SQLite, keys, install secret), `/downloads`, optional `/staging`
- **`PUID` / `PGID` / `UMASK`** honored by an entrypoint that drops privileges — the
  convention every seedbox-adjacent container user already expects
- **`HEALTHCHECK`** on `/api/health`
- **Graceful shutdown:** SIGTERM propagates to in-flight lftp children so their `-c` state is
  clean and the next start resumes rather than restarts
- Env vars bootstrap first-run config; everything is editable in the UI afterwards

---

## 10. Files to create

```
docker/Dockerfile · docker/entrypoint.sh · docker-compose.yml · docker-compose.test.yml
backend/lftpweb/
  main.py config.py db.py models.py auth.py
  api/{files,jobs,settings,auth,health}.py   api/ws.py
  core/engine.py      core/remote.py     core/local_scan.py   core/lftp.py
  core/queue.py       core/progress.py   core/reconcile.py    core/autoqueue.py
  core/postprocess.py core/events.py
  remote_agent/scan_fs.py          # stdlib-only fallback scanner
frontend/   Vite app — routes Files / Transfers / History / Settings
tests/      unit + integration
```

---

## 11. Build order

Each phase ends at something that can actually be looked at and judged.

1. **Skeleton + container** — FastAPI, SQLite schema/migrations, config, healthcheck,
   compose, SPA shell. *Done when:* container starts, UI loads, `/api/health` is green.
2. **Scanning + model** — asyncssh connect/test, remote `find` scan, local walk, reconciler,
   read-only Files tree pushed over WS. *Done when:* the real seedbox tree renders with
   correct sizes and correct REMOTE_ONLY / LOCAL_ONLY / PARTIAL classification.
3. **Transfer engine** — process supervision, queue + semaphore, FS-derived progress,
   queue/stop/retry, Transfers view. **The load-bearing phase.** *Done when:* you can queue a
   directory, watch it move, stop it, and resume it.
4. **Auto-queue** — patterns (case-insensitive substring OR glob, matching SeedSync's
   semantics), applied retroactively when a pattern is added, exclude globs, per-pair enable.
5. **Post-processing** — verify, extract, staging move.
6. **Auth + hardening** — the three modes, credential encryption, log redaction, rate limits.
7. **Polish** — bulk ops, filters, history view, virtualization tuning, docs.

---

## 12. Verification

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
  local present/size, active job y/n, persisted history) → expected state. SeedSync's real
  bugs lived here; a table test pins all of §3.2 at once.

**Resume test:** start a large transfer, `docker kill` the container mid-flight, restart, and
confirm it resumes from the partial rather than restarting, and that the item reads `PARTIAL`
in between.

**Manual acceptance:** queue a directory → live speed/ETA → stop mid-transfer → verify
`PARTIAL` → re-queue → completion → extraction → staging move.

---

## 13. Risks and open questions

| # | Risk | Mitigation / status |
|---|---|---|
| 13.1 | **Global bandwidth cap is approximate** under per-job processes (§4.5) | Recommend accepting; per-pair overrides available. Revisit only if it proves annoying in practice. |
| 13.2 | **`find -printf` is GNU-specific** | Stdlib script fallback; needs a one-line check against the actual seedbox in Phase 2. |
| 13.3 | **Sparse-file progress depends on `.lftp-pget-status`** | Pinned by unit tests; degrades to raw-size (monotonic, never wrong about completion). |
| 13.4 | **Many small files** — per-file stat sampling could get expensive | Sampler only stats the active set, not the tree. If a mirror has thousands of concurrent files, fall back to sampling the job's local subtree total. |
| 13.5 | **Filenames with odd bytes** | Handle `surrogateescape` end to end (scan → DB → JSON → UI), tested explicitly. |
| 13.6 | **No `jobs -v` at all in v1** | If per-connection chunk detail is wanted later, add it as a strictly optional, failure-tolerant *enrichment* — never a source of truth. |

**Open questions:**

- **A.** Multiple path-pairs in v1, or one pair to start? The schema supports many; the UI is
  simpler with one. Assumed many, since a seedbox usually has `complete/` split by category.
- **B.** Should *delete remote after download* be in v1? SeedSync has it; it's a small feature
  with a large "oops" radius. Suggest defaulting it off and putting it behind a confirmation.
- **C.** Notifications and *arr integration are deferred — confirm that's still right, or say
  which one should be pulled into v1.
- **D.** History view in v1 (§8), or polish? Nearly free given the schema, so it's currently
  in phase 7.
