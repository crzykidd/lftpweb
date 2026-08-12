---
name: 2026-08-11-phase3a-transfer-engine
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: |
  Built core/lftp.py (process spawn/rc-file/classification), core/scheduler.py (pure admission
  control, table-tested against every §4.5 worked example), core/queue.py (lifecycle/
  supervision/retry/stop), core/progress.py (EMA speed/ETA over the active set), and api/jobs.py
  (queue/stop/retry/move-to-top/start-now, live jobs list, transfer settings). Verified end to
  end against the real fake seedbox, through the real HTTP API: a real transfer with checksum
  match, a deterministic mid-transfer SIGTERM stop (state STOPPED, partial kept, process gone,
  auto_queue_suppressed set), a real resume from partial (byte count never dropped, final
  checksum matched), concurrency (2 at half, 3rd waits, refill on completion), and AUTH_FAILED
  classification with no retry. Live-retune experiment (§4.5) confirmed working with real
  before/after throughput measurements; not wired into production. uv run pytest: 127 passed,
  including the scheduler table test and 4 real-seedbox integration tests. Found and fixed two
  real state-ownership bugs (GET /api/files serving scan-only state; a periodic rescan able to
  clobber STOPPED/FAILED) and three undocumented real lftp behaviors (mirror's parent-dir
  target, the open -u password quirk, pget:save-status's 10s default). All docker containers
  torn down; docker ps -a and docker compose config --quiet both clean.
---

# Task: Phase 3a — transfer engine, scheduler, progress (backend)

Make lftpweb *move bytes*. Spawn lftp processes, admit them under the §4.5 scheduler, derive
progress from the filesystem, and expose queue/stop/retry over the API.

**This is the load-bearing phase of the whole project** — it is the first point where the
central thesis (§1.3: lftp is a transfer engine, not a status API) either works against a real
SFTP server or doesn't.

**Backend only.** The Transfers UI and the item drawer are phase 3b. Build the API they will
consume, and verify through it.

**Done when:** you can queue a directory through the API against the fake seedbox, watch bytes
land with live progress, stop it mid-transfer, see it resume from the partial rather than
restart — and the §4.5 worked examples hold as tests.

## Before you start

- **Read `DESIGN.md`** — §4 in full (§4.1 process model, §4.2 credentials, §4.3 failure/retry,
  §4.4 progress without parsing, §4.5 the scheduler, §4.6 stop semantics), plus §3.1 (`job`
  table — already migrated), §3.2 (states), §15 (risks).
- Read `prompts/startnewsession.md` — the **traps** list is directly relevant to this phase.
- Read `docs/decisions.md` — phases 1 and 2 hit several non-obvious things you'll build on.
- Phases 1–2 are committed. `core/local_scan.py` already implements the `.lftp-pget-status`
  sidecar math and `.lftp` suffix handling (§4.4) — **reuse it, don't reimplement it.**

## Working tree check

Run `git status --porcelain` and cross-reference. If files this plan touches have uncommitted
changes, list them and ask first. Surface unrelated dirty files once. This file is exempt.

## The thing most likely to go wrong

`jobs -v` parsing must not reappear. Progress comes from **local file sizes vs. known remote
sizes**, sampled ~1 Hz over the active set only. If you find yourself reading lftp's stdout to
learn how far along a transfer is, stop — that is the exact design the project exists to avoid,
and §1.2 documents why at length. lftp's output is captured **only** for error classification
on a non-zero exit.

## What to do

### 1. `core/lftp.py` — one process per job

Per §4.1 and §4.2:

- Build the command; single files use `pget -c`, directories `mirror -c`. `-c` on both is what
  makes every restart resumable.
- **stdin/stdout/stderr are pipes, never a PTY.** With stdin not a tty lftp disables readline,
  so none of the escape/wrapping problems in §1.2 can occur.
- `set cmd:fail-exit true` so **success is exit code 0** — no inference.
- Credentials go in a **per-job rc file, mode 0600, on the `/run` tmpfs, unlinked when the job
  exits** — never in argv (§4.2). Everything logged passes the phase 1 redactor.
- Classify non-zero exits into `AUTH_FAILED`, `HOST_UNREACHABLE`, `TLS_ERROR`,
  `PERMISSION_DENIED`, `DISK_FULL`, `REMOTE_GONE`, `UNKNOWN`; keep the last ~4 KB of output on
  the `job` row.

### 2. `core/scheduler.py` — admission control, as a pure function

§4.5. Keep it free of subprocesses and I/O: `(N, B, running, queue, settings)` in, an admit
list out. That is what makes the worked examples testable.

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

Also implement, all from §4.5:

- **The invariant: a running job's allocation is never re-shaped.** This is what makes the
  absent lftp control channel a non-issue rather than a workaround.
- **Fast lane** — items under `small_item_threshold` run in their own lane with its own
  concurrency cap, sharing `small_lane_reserve`, consuming no main-lane slot and never entering
  the headroom calculation. It exists to prevent head-of-line blocking, not because small files
  are special.
- **Ordering** `rank DESC, queued_at ASC` — default oldest-first — plus **Move to top**.
- **Start now at max bandwidth** — admits immediately at full `B`, deliberately oversubscribing;
  no new main-lane admissions while `Σ allocations > B − reserve`.
- Site-level settings per §4.5's table, persisted in `setting`, exposed via the settings API.

### 3. `core/queue.py` — job lifecycle and supervision

Owns spawning, watching, and reaping; the scheduler owns only the decision. Persist `job` rows
per §3.1 (`lane`, `rank`, `rate_limit_bps`, `forced_full_rate`, `attempt`, `exit_code`,
`output_tail`).

**Stop semantics (§4.6) — get these exactly right:**

- Running job → **SIGTERM to that one PID**, not SIGKILL, so lftp flushes its
  `.lftp-pget-status` sidecar and the partial stays resumable. SIGKILL only after a ~10 s grace.
- Queued-not-started → just removed from the queue.
- Either way → `STOPPED`, partial data kept, and **`auto_queue_suppressed` set**.
- Retries exhausted or a permanent error class → `FAILED`, also suppressed.
- Manual re-queue clears suppression and resets `attempt`.

Auto-queue is phase 4, so nothing reads the suppression flag yet — **set it anyway**. Without
it, phase 4 would resurrect stopped jobs 30 s later, forever (§4.6, and it's in the traps list).

**Retry (§4.3):** transient classes back off to `max_attempts` (default 3); `AUTH_FAILED`,
`PERMISSION_DENIED`, `REMOTE_GONE`, `DISK_FULL` never retry.

### 4. `core/progress.py` — progress without parsing

§4.4. Sample **only the active set** at ~1 Hz — never a full tree walk. Reuse
`core/local_scan.py`'s sidecar and temp-suffix logic. Compute transferred bytes, speed, and ETA
yourself, EMA-smoothed (α ≈ 0.3) so the UI doesn't jitter. Feed `/api/stats`'s real values
(current speed, **allocated vs. ceiling**, queued count/bytes, 24 h transferred) — phase 1
stubbed that endpoint with zeros precisely so this phase fills it in.

### 5. `api/jobs.py`

Queue an item, stop, retry, move-to-top, start-now-at-max, and list active + pending with live
progress. Plus the site-level transfer settings from §4.5.

### 6. The live-retune experiment — test it or discard it

§4.5 records an **unverified** idea: hold the lftp process's stdin open as a pipe, start the
transfer with `&` so the command loop stays live, and write `set net:limit-total-rate <n>`
mid-flight to retune a running transfer.

**Actually test this against a running transfer** and report a clear yes/no with evidence. If it
works it closes §15.2 later; if it doesn't, we stop carrying it as a maybe. Do **not** make the
design depend on it either way — admission control stands on its own.

## Verify before reporting — actually run these

The fake seedbox (`docker-compose.test.yml`, phase 2) is your test target. Bring it up with
`docker/test-seedbox/gen_key.sh` first if you need key auth; password auth is `seeduser` /
`testpass123` on ports 2222 (GNU) and 2223 (busybox). **Tear everything down when done.**

1. `uv run pytest` passes, including a **scheduler table test** covering every §4.5 worked
   example: two-at-half, one-at-full, the blocked third, refill-at-half on completion, the
   floor loop reducing `ready`, fast-lane items bypassing headroom, and start-now
   oversubscribing then freezing admissions.
2. **A real transfer**: queue the 20 MB file and a directory against the fake seedbox; bytes on
   disk match the source exactly (compare checksums, not sizes).
3. **Stop mid-transfer**, then verify: state is `STOPPED`, the partial file is still on disk,
   `auto_queue_suppressed` is set, and no lftp process survives. Set a low bandwidth cap to make
   this deterministic rather than racing a fast local transfer.
4. **Resume**: re-queue the stopped item and confirm it *continues* from the partial rather than
   restarting — the byte count must not drop back to zero.
5. **Concurrency**: queue several items with N=2 and a bandwidth cap; confirm two run at half
   each, a third waits, and on completion the next is admitted at the freed share.
6. **Failure classification**: point at a bad password and confirm `AUTH_FAILED` with no retry.
7. `docker compose config --quiet` clean; `docker ps -a` shows nothing left behind.

Report exact commands and output, **separating verified from unverified**. An admitted gap is
worth more than an unverified claim.

## Surfacing design decisions

Report prominently anything where `DESIGN.md` is wrong, ambiguous, or silent on a hard-to-reverse
choice. Make the smallest reasonable call to keep moving. **Do not edit `DESIGN.md`** — it gets
corrected deliberately, in conversation with the user. Phases 1 and 2 each found real doc errors
this way; that is the process working.

## Conventions to honor

- Module layout per §12. Type-annotated, small testable functions; the scheduler especially must
  stay pure.
- Comment the non-obvious (the rc-file lifecycle, the SIGTERM-not-SIGKILL reason); not routine code.
- No credentials in logs, argv, or tracked files.

## When done

1. Record decisions in `docs/decisions.md` (newest at top), including the live-retune result.
2. Update **`prompts/startnewsession.md`** — "Where we are", the phase table, traps if any are new.
3. Update this file's frontmatter: `status`, `completed`, `result`.
4. `git mv` to `prompts/done/` (success) or `prompts/failed/` (failure).
5. **You are a spawned agent: do NOT commit.** Prepare the tree and report the file list plus a
   proposed one-line commit message (`feat:` prefix, no `Co-authored-by:`; branch `dev`).
   Never `git add -A`, never push.
