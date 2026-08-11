---
name: 2026-08-11-phase2-scanning-and-model
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: Remote/local scanning, reconciler, engine loop, and read-only Files UI built and verified live against a fake seedbox over both the GNU find -printf and busybox-fallback scan paths, including through the real containerized image.
---

# Task: Phase 2 — scanning + model

Make lftpweb *see*: connect to a seedbox over SSH, scan the remote tree, walk the local tree,
reconcile the two into one model, and render it read-only in the Files page over a WebSocket.

**No transfers.** Queueing, the scheduler, and lftp itself are phase 3. Nothing in this phase
moves a byte.

**Done when:** you can configure a host and a path queue in the UI, hit *Test connection*, and
see the tree render with correct sizes and correct `REMOTE_ONLY` / `LOCAL_ONLY` / `PARTIAL` /
`DOWNLOADED` classification — verified against the fake seedbox you build in step 1.

## Before you start

- **Read `DESIGN.md`**: §3.1 (schema — already migrated in phase 1), §3.2 (file states and the
  eight rules), §4.4 (the two lftp on-disk conventions), §5 (remote scanning), §8 (credentials),
  §9.1–9.2 (frontend), §14 (verification), and §13 phase 2.
- Read `prompts/startnewsession.md`, especially the **traps** list.
- Read `docs/decisions.md` — phase 1's entries explain the container, ports, and migration
  rules you'll be building on.
- Phase 1 exists and is committed: FastAPI + migrated SQLite + React shell + container. Build
  *into* it; don't restructure it.

## Working tree check

Run `git status --porcelain` and cross-reference the files this plan touches. If any have
uncommitted changes, list them and ask before touching them. Surface unrelated dirty files once
as awareness; don't block. This file is exempt.

## Decisions already made — do not re-litigate

- **Credential encryption ships in this phase, not phase 8.** The build order in §13 puts it in
  phase 8, but this is the phase where a seedbox password first exists, and storing it in
  plaintext "until later" is not acceptable even in a dev build. Implement §8's scheme now:
  encrypt at rest with a key derived from a per-install secret file in `/config` (mode 0600,
  generated on first run). Phase 8 keeps the *rest* of §8 — auth modes, sessions, API keys,
  rate limiting. Record this deviation in `docs/decisions.md`.
- **`asyncssh`**, added to `pyproject.toml` this phase. Support key file, agent, and password
  auth. Respect `known_hosts` with a configurable policy, defaulting to accept-and-pin on first
  use — a seedbox's host key is not knowable in advance and hard-failing makes the app unusable.
- **The remote scan is one `find` command** (§5), not a deployed agent script:
  `find <path> -mindepth 1 -printf '%y\t%s\t%T@\t%p\n'`. Directory sizes are summed locally.
- **States in scope this phase:** `REMOTE_ONLY`, `LOCAL_ONLY`, `PARTIAL`, `DOWNLOADED`. There is
  no job engine yet, so `QUEUED`/`DOWNLOADING`/`STOPPED`/`FAILED` cannot occur. `EXCLUDED`
  belongs to phase 4 — but see "Leave the seam" below.

## What to do

### 1. The fake seedbox — build this first

You cannot reach the user's real seedbox, and you must not ask for its credentials. Build
`docker-compose.test.yml` per §14: an sshd container seeded with a generated tree of known
sizes, so every later assertion is checkable.

Two details that matter more than they look:

- **Seed a realistic tree**: nested release directories, a loose top-level file, a large-ish
  file, a zero-byte file, a file with spaces and one with a non-ASCII name, and a deep path.
- **Test both scan paths.** Alpine's busybox `find` does **not** support `-printf`. Make that
  work *for* you: verify the primary path against a container with GNU `findutils` installed,
  and the fallback path against one without it. That is the cheapest possible test of §5's
  fallback, and it is exactly the risk §15.7 records.

### 2. Remote scanning — `core/remote.py`

- Pooled, reused `asyncssh` connection; the same connection serves scanning, *Test connection*,
  and (later) remote deletes.
- Primary `find -printf` path; the stdlib-only fallback script (`remote_agent/scan_fs.py`)
  uploaded over SFTP and run when `-printf` isn't supported. Detect, don't guess.
- **Parse defensively.** Paths can contain tabs, newlines, and non-UTF-8 bytes. Use
  `surrogateescape` end to end (§15.10). A filename must never crash a scan.
- Scan on an interval (default 30 s, configurable) and on demand.

### 3. Local scanning — `core/local_scan.py`

`os.scandir` walk, plus the two lftp conventions from §4.4 — implement them **now**, because
they determine whether a partial file reads as partial:

- **`<file>.lftp-pget-status` sidecars.** Effective size = `size − Σ(limit − pos)`. Raw
  `st_size` lies for sparse files.
- **`xfer:use-temp-file` / `*.lftp` suffix.** Strip it when matching local against remote; when
  the final name is absent, look for `<name>.lftp`.

Nothing writes these files yet — phase 3 does — so **unit-test them against fixtures you
construct**, not against a live transfer.

### 4. Reconciler — `core/reconcile.py`

Merge remote tree + local tree into the unified model, implementing §3.2's rules that apply
without a job engine. Rules 1, 2, and 4 are in scope and are the ones with teeth:

- a directory is `DOWNLOADED` only when every non-directory descendant with a remote size is
  complete, else `PARTIAL`;
- `local_size < remote_size` with no active job ⇒ `PARTIAL`, never `DOWNLOADED`;
- remote size is a moving target — never latch it, recompute on every scan.

**Leave the seam** for what comes later: rule 1's real form excludes `EXCLUDED` children (§4.7,
phase 4), and rules 3/5/6/7 need job and lifecycle state (phases 3–4). Structure the
completeness check so a "should this child count?" predicate drops in — do **not** hardcode
"every remote child counts" in a way that phase 4 has to unpick.

### 5. Persistence and the engine loop

- `core/engine.py` — the asyncio loop owning the scanners and holding the current model,
  publishing changes to `core/events.py`.
- Persist `item` rows per §3.1 so state survives restart. `first_seen_at` matters; the rest of
  the lifecycle columns stay unused this phase.

### 6. API and WebSocket

- `api/settings.py` — CRUD for `host` and `path_queue`, plus `POST /api/settings/host/test`
  returning a real connection result with a **useful** error (auth vs. DNS vs. refused vs.
  timeout), not a boolean.
- `api/files.py` — the reconciled tree, grouped by queue.
- `api/ws.py` — full model snapshot on connect, deltas thereafter (§2).

### 7. Frontend

- **Settings → Connection**: host form (address, port, username, auth method, key path or
  password), *Test connection* with a real result. The password field must never round-trip the
  stored secret back to the browser.
- **Settings → Queues**: add/edit/remove named path queues with their remote → local mapping.
- **Files**: the tree from §9.2 — virtualized, grouped by queue, collapsible, per-row state chip
  and size. Read-only: no queue/stop/delete buttons yet, since there's nothing behind them.
- Live updates over the WebSocket, with a visible reconnect state.

## Verify before reporting — actually run these

1. `uv run pytest` passes, including new unit tests for: the `.lftp-pget-status` size math, the
   `.lftp` suffix matching, `find -printf` record parsing (tabs, newlines, non-UTF-8), and a
   **reconciler state table** over (remote present/size × local present/size) → expected state.
2. `npm run build` clean, no TypeScript errors.
3. `docker compose -f docker-compose.test.yml up` brings up the fake seedbox; lftpweb scans it
   over real SSH and the tree renders. State this explicitly: **primary `-printf` path verified**
   and **fallback path verified**.
4. Sizes match what you seeded — check a nested directory's summed size against the known total.
5. Delete a local file and confirm the item flips to `PARTIAL`/`REMOTE_ONLY` on the next scan;
   restore it and confirm it flips back.
6. `docker compose config --quiet` clean for all three compose files.
7. **Tear down every container you started** and confirm with `docker ps -a` that nothing
   `lftpweb*` or test-related remains. Ports 8087/5187 free.

Report the exact commands and their output. **Separate verified from unverified explicitly** —
an unverified claim costs more than an admitted gap.

## Surfacing design decisions

Report prominently, in your final summary, anything where `DESIGN.md` is wrong, ambiguous, or
silent on a hard-to-reverse choice. Make the smallest reasonable call to keep moving. **Do not
edit `DESIGN.md`** — it gets corrected deliberately, in conversation with the user.

Phase 1 found three real doc errors this way; that was the process working, not a problem.

## Conventions to honor

- Module layout per §12. Type-annotated Python, small testable functions.
- Comment the non-obvious (the sidecar math, the surrogateescape handling); not the routine.
- No secrets in logs — everything through the redactor added in phase 1.
- Nothing under `private_data/` committed; no credentials, real or example, in tracked files.

## When done

1. Record decisions in `docs/decisions.md` (newest at top), including the credential-encryption
   move and anything else non-obvious.
2. Update **`prompts/startnewsession.md`** — "Where we are", the phase table, and the traps list
   if this phase found new ones.
3. Update this file's frontmatter: `status`, `completed`, `result` (one line).
4. `git mv` this file to `prompts/done/` (success) or `prompts/failed/` (failure).
5. **You are a spawned agent: do NOT commit.** Prepare the tree and report back the file list
   plus a proposed one-line commit message (`feat:` prefix, no `Co-authored-by:`; branch `dev`).
   Never `git add -A`, never push.
