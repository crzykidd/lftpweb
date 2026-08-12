# Start-new-session brief — lftpweb

Point a fresh session at this file. It is a **standing onboarding brief, not a task** — it
never moves to `done/`. It restates what the project is, where the build has got to, and the
rules to honor, so a new session is productive even with no conversation memory.

**Keep the "Where we are" section current.** Update it at the end of any phase or whenever a
significant decision lands, in the same commit as the work.

---

## What this project is

**lftpweb** is a containerized web interface that keeps a local directory in sync with a
seedbox, using **lftp** as the transfer engine over SSH/SFTP. It browses the remote and local
trees as one view, queues and supervises downloads with live progress, auto-queues on patterns,
and optionally verifies / extracts / relocates finished items.

- **Stack:** Python 3.13 / FastAPI / SQLite / asyncssh backend; React + TypeScript + Vite +
  Tailwind frontend; one Alpine container; lftp for transfers.
- **First version:** `0.0.1`. Version lives in `backend/lftpweb/__init__.py`, bare (no `v`).
- **Licence: AGPL-3.0** (`LICENSE`). Bundled third-party programs in the image — lftp, OpenSSH,
  7-Zip, su-exec, tini — are aggregated, not linked, and are recorded in `NOTICE`.
- **Repo: https://github.com/crzykidd/lftpweb** — public, created 2026-08-11.

### The one idea everything hangs off

> **lftp is a transfer engine, not a status API.** Progress is derived from the filesystem
> (local bytes vs. known remote size); each transfer is its own short-lived lftp process.

Do **not** reintroduce `jobs -v` parsing as a source of truth. `DESIGN.md` §1.2 explains why at
length — it is the single most important thing to read before touching the transfer engine.

---

## Read first, in this order

1. **`DESIGN.md`** — the architectural source of truth, 15 numbered sections. Cite sections as
   `§4.5` when discussing it. **Required reading before writing any code.**
2. **`CLAUDE.md`** — per-session operating rules (the handoff-prompt workflow, in full).
3. **`docs/decisions.md`** — the "why" log, newest first. Check it before re-deriving anything;
   several decisions have non-obvious rejected alternatives.
4. **`standards.md`** — which homelab standards this repo implements, pinned.

---

## Repo, branches, and what's on GitHub

**Bootstrap is done — this is no longer a pending to-do.** `docs/repo-setup.md` carries the
one-time runbook that got the repo from "prepared" to "actually on GitHub, with
`code-checkin-and-pr` fully enforced"; it's a historical record now, not a checklist to
re-run. As of phase 9 (verified live via `gh api repos/crzykidd/lftpweb/branches/main/
protection`, not assumed from an old note): **`main` is branch-protected** — 8 required status
checks (Backend lint, Frontend lint + typecheck, Config validation, Compose validation, Image
build, Test suite, and CodeQL for both languages), PR required, force-push and deletion both
blocked. `dev` and `main` are both fully pushed and in sync with `origin` (`git rev-list
--left-right --count origin/<branch>...<branch>` reads `0 0` for both). `dev` sits ahead of
`main` by design — protection means `main` only advances via a green PR, so `dev` naturally
runs ahead between release-prep passes; check the actual commit count
(`git rev-list --left-right --count main...dev`) rather than trusting a specific number here,
since it moves.

Day-to-day work happens on `dev`, pushed freely. `main` only ever moves via a PR from `dev`
with every required check green — never a direct push, never `--force`. This project has not
yet cut a `v0.0.1` release (`release-prep-and-cut`'s two-phase prep/cut flow) as of phase 9;
that's a separate, explicit action for the user to request, not something any phase did as a
side effect of shipping.

## Where we are

**Status: all 9 phases done — v1 is complete.** `DESIGN.md` is settled and reviewed (§13's
build order is annotated with what shipped; §15's risk table was re-reviewed at phase 9). The
skeleton (phase 1), scanning + reconciliation + read-only Files view (phase 2), the transfer
engine + scheduler (phase 3a), the Transfers page / item drawer / Files actions / WebSocket
delta fix (phase 3b), auto-queue + patterns + the mount sentinel (phase 4), post-processing +
`move` mode (phase 5), the History page (phase 6), operations — log viewer + `VACUUM INTO`
backups + extended health (phase 7), auth + hardening — the three `AUTH_MODE`s, API keys,
CSRF, rate limiting, and the finished credentials-need-re-entry behaviour (phase 8), and polish
— Files-page filters, honest bulk partial-failure reporting, and the `host_reachable`/
`scheduler_alive` header readout (phase 9) — all exist and are verified — see
`prompts/done/2026-08-11-phase1-skeleton-and-container.md`,
`prompts/done/2026-08-11-phase2-scanning-and-model.md`,
`prompts/done/2026-08-11-phase3a-transfer-engine.md`,
`prompts/done/2026-08-11-phase3b-transfers-ui.md`,
`prompts/done/2026-08-11-phase4-autoqueue-and-patterns.md`,
`prompts/done/2026-08-11-phase5-postprocessing-and-move.md`,
`prompts/done/2026-08-11-phase6-history-page.md`,
`prompts/done/2026-08-11-phase7-operations.md`,
`prompts/done/2026-08-11-phase8-auth-and-hardening.md`, and
`prompts/done/2026-08-12-phase9-polish.md` for the exact commands run.

**Real, permanent gaps remain even though all 9 phases shipped — see `README.md`'s "What
doesn't yet" and "Known gaps" sections for the consolidated, canonical list** (this file
doesn't duplicate it). The headline items: **no UI screen in this project has ever been opened
in a browser** (none exists in any environment this project has been built in — every page is
confirmed to build/type-check/lint cleanly and every endpoint it calls is verified over real
HTTP, but actual rendering and click-through behavior have never been visually confirmed);
Settings → Transfer has no UI despite a complete backend API since phase 3; and Files has no
bulk "Delete local"/"Delete remote" (Queue/Stop only, per phase 9's own scope). Click-test the
UI before relying on any of it.

> **⚠ Phase 5 makes the user's live queue's `sync_mode = 'move'` row live.** It has been
> stored that way in the database since before phase 4's guard existed, inert until now
> because nothing implemented `move` or read `sync_mode` to act on it. As of phase 5, `move`
> **deletes the verified remote copy after every download that queue completes.** The row was
> deliberately **not** touched or reset — see `docs/decisions.md`'s phase 5 entry, point 0, and
> the phase 5 report. **Tell the user this first, before anything else, when they're back** —
> this note stays here until they've confirmed they've seen it, not just until the phase that
> introduced it shipped.

| Phase (`DESIGN.md` §13) | State |
|---|---|
| 1 — Skeleton + container | **done** (2026-08-11) |
| 2 — Scanning + model | **done** (2026-08-11) |
| 3a — Transfer engine + scheduler (backend) | **done** (2026-08-11) |
| 3b — Transfers UI, item drawer, WebSocket delta fix | **done** (2026-08-11) |
| 4 — Auto-queue + patterns | **done** (2026-08-11) |
| 5 — Post-processing + `move` | **done** (2026-08-12) |
| 6 — History page | **done** (2026-08-12) |
| 7 — Operations (logs, backup, health) | **done** (2026-08-11, committed `c6dcc03`) |
| 8 — Auth + hardening | **done, committed** (2026-08-12, `b936576`) |
| 9 — Polish + docs reconciliation | **done, not yet committed** (2026-08-12) — see below |
| `sync` mode | **not scheduled** — designed in §7, built only if it proves wanted |

> **⚠ Phase 9's work is prepared on the working tree but NOT YET COMMITTED**, per that task's
> explicit "do NOT commit — prepare the tree and report back" instruction (the same
> instruction phase 8 was given; phase 8 has since been committed — see the table above and
> "Commits so far" below). See the phase 9 report (linked from its `prompts/done/` file, once
> moved there) for the exact file list and the proposed commit message.

**Current instruction (2026-08-11, overnight run) — closed out as of phase 9.** Phases 1–3
were proven against **the user's real seedbox** — a 1.29 GB mkv transferred byte-exact, nested
directories, resume from partial, live progress — before the user authorised running **phases
4–9 in order, unattended**: for each phase write the handoff prompt, execute it via a spawned
agent, verify, commit, push to `dev`, then start the next, documenting every decision made
without them. That instruction is now fulfilled — phase 9 was the last phase in the list, and
this file's job going forward is accurate onboarding, not tracking an in-flight overnight run.

**SAFETY RULE that governed the unattended run — every new capability shipped defaulting to
OFF.** The user's live instance could pull `:dev` at any point during the run. Nothing landing
overnight was allowed to change how their running deployment behaved: auto-queue defaults
disabled, remote deletion defaults off, auth defaults to `none`. A capability that turns itself
on while the user sleeps was treated as a bug, not a feature — this held for every phase.
**Phase 5 was the one deliberate, flagged exception to "nothing changes behavior":** `move`
mode itself was already stored as the user's live setting before any guard existed, so
implementing it changed what their existing configuration did even though every *new* toggle
that phase added (global and per-queue post-processing switches, `auto_move`) still defaulted
off exactly like every other phase. Phase 7's scheduled backup was a second, smaller, explicitly
reasoned exception (see its own `docs/decisions.md` entry) — everything else held the rule.

**Their live config:** one queue, `sync_mode` stored as `move` in the database from before the
guard existed. As of phase 5 this is **no longer inert** — see the warning banner above. Not
silently rewritten; it is the user's call and they need to see it first.

## What real hardware taught us that the fake seedbox could not

Ten fixes came out of the first real deployment. They are the reason to keep testing against
real infrastructure rather than only the fixture:

- **OpenSSH fatals with "No user exists for uid N"** when the running uid has no `/etc/passwd`
  entry — which is exactly §11.2's identity model. lftp shells out to ssh, so *every* transfer
  died while scanning worked (asyncssh has an env fallback; OpenSSH has none). Fixed by
  symlinking `/etc/passwd` into the `/run` tmpfs and writing it in the entrypoint.
- **lftp retries forever by default.** `net:max-retries`/`net:timeout` were in §9.3's knob list
  but never written to the rc, so a failing connection hung as "DOWNLOADING, 0 bytes" instead
  of failing. Always set now.
- **`net:reconnect-interval-base` takes a bare number, not `5s`** — lftp rejected the line,
  carried on, and produced a misleading `HOST_UNREACHABLE`. `tests/test_lftp_settings_accepted.py`
  now feeds every generated setting to a real lftp binary; asserting the rc *contains* a string
  only proves we wrote what we meant.
- **The WebSocket omitted `item.id`**, so every Files row rendered with no action button — a
  remote file could be seen but never queued. The page renders purely from the WS stream.
- **`VOLUME` created a phantom root-owned `/downloads`**; the per-job `/run` dir was never
  created before privileges dropped; `pget -n 4` fanned a 16-byte file across four connections.
- **Jobs left `running` by a restart** became phantom transfers forever, and their items stayed
  stuck `DOWNLOADING` because scans deliberately don't overwrite lifecycle states.
- **A `sync_mode` the UI offered but nothing implemented** silently behaved as `copy`.

The pattern: none were reachable from unit tests or the fake seedbox. Job lifecycle logging and
`output_tail` are what turned each one from a guess into a diagnosis — keep them.

App ports are **8087** (API/SPA) and **5187** (Vite dev server) — not the more obvious
8080/5173 — chosen to avoid collisions with other stacks on the shared build host. See
`docs/decisions.md`.

Two design gaps phase 1 found in `DESIGN.md` and worked around (see `docs/decisions.md` for
the full reasoning): §11.1's `cap_drop: ALL` doesn't actually boot the §11.2 PUID/PGID
entrypoint without `CHOWN`/`SETUID`/`SETGID` added back, and `/api/health` had to grow a
`repo_url` field beyond §12's literal 4-field shape so the nav's version link can get a
runtime (not build-time) value.

Phase 2 found four more, all worked around and recorded in `docs/decisions.md` rather than
folded back into `DESIGN.md` (a deliberate corrected-in-conversation call per the workflow):
`asyncssh.connect()` crashes outright under §11.2's own numeric-uid convention on Python 3.13
(`getpass.getuser()` raises `OSError`, worked around in `core/remote.py`); `known_hosts=None`
silently disables asyncssh's host-key callback entirely, so the working accept-and-pin
implementation passes an empty `SSHKnownHosts()` instead; §3.2 rule 1 doesn't say what a
directory with *zero* local presence should read as, resolved as `REMOTE_ONLY` rather than
`PARTIAL`; and §4.7's narrow "item" (top-level entries only) and the `item` table's evident
full-tree scope disagree, resolved toward persisting one row per node.

**Every credential encryption gap is closed as of phase 2**, moved up from build phase 8
because phase 2 is where a seedbox password first exists (`core/crypto.py`; see
`docs/decisions.md`). **Phase 8 (now done) built the rest of §8**: the three `AUTH_MODE`s,
sessions, CSRF, API keys, rate limiting, and finished the credentials-need-re-entry behaviour
for the restore-to-fresh-install case (`core/queue.py._admit` holds transfers,
`core/engine.py.scan_queue` fails scanning cleanly, both keyed off a new
`HostConfig.credentials_need_reentry` flag). Phase 3 had already landed the "hold transfers
for a host with *no* host configured at all" half by construction —
`TransferQueue._admit` just doesn't spawn anything when `core/engine.load_host_config`
returns `None` — phase 8's addition is the narrower "a host *is* configured but its password
won't decrypt" case, which is different code path (host is not `None`, `password` is).

**Phase 3, in one paragraph:** `core/lftp.py` builds and spawns one lftp process per job
(pipes, never a PTY; credentials + tuning in a per-job `/run` tmpfs rc file, never argv) and
classifies non-zero exits. `core/scheduler.py` is a pure `(settings, running, queue) -> admit
list` function pinned by a table test covering every §4.5 worked example, the floor loop, the
fast lane, and start-now. `core/queue.py` ties it together — spawn/watch/reap, retry with
backoff on transient classes only, SIGTERM-then-grace-then-SIGKILL stop semantics, and
`auto_queue_suppressed` on every STOPPED/FAILED item even though phase 4's auto-queue doesn't
exist yet to read it. `core/progress.py` samples the active set at ~1 Hz via
`core/local_scan.py`'s sidecar math (reused, not reimplemented) and EMA-smooths speed/ETA.
`api/jobs.py` exposes all of it, plus the site-level transfer settings. The **live-retune
experiment is confirmed working** (holding lftp's stdin open + `set net:limit-total-rate` while
a job runs) but is **not** wired into production — admission control stands alone, as required.
Six non-obvious things were found running real lftp against the real fake seedbox — see
`docs/decisions.md`'s phase 3 entries, especially: `mirror`'s target must be the item's *parent*
directory (not the item's own directory, unlike `pget`); a bare `open sftp://user@host` makes
lftp prompt for a password itself even under key auth; `pget:save-status` defaults to a
sampler-breaking 10s; and `GET /api/files` was serving a state that a stop/queue action could
never actually reach until it was pointed at the database instead of the scan-only in-memory
model.

**Phase 3b, in one paragraph:** the WebSocket delta fix landed first, because it constrains
everything else — `core/engine.py.diff_nodes` turns `scan_queue`'s full-tree publish into a
`changed`/`removed` delta, and `core/queue.py._publish_item_state` pushes single-item deltas on
every lifecycle transition plus a per-tick batch for the active set, so the Files page updates
live without the WebSocket ever resending a whole queue's tree (proven by test across a 20-item
and a 5,000-item tree, and measured live: ~152–189 bytes/message vs. a 2,754-byte full snapshot
for the fake seedbox's 18-node tree). The Transfers page (`TransfersPage.tsx`) shows the
three-word visible vocabulary from DESIGN.md §9.2 with `STOPPED`/`FAILED` surfacing where they
apply, both allocated and current rate, and a one-time inline explanation for "Start now."
`ItemDrawer.tsx` is the side drawer (not a modal), `FileTree.tsx` gained virtualization
(`@tanstack/react-virtual`), multi-select with shift-range, and per-row/bulk Queue/Stop actions.
Two backend gaps found wiring the UI to the phase 3a API: `list_jobs()` excluded every
failed/cancelled job, which made DESIGN.md §9.2's own "failed rows show the error class" and the
phase 3b prompt's "stop it and see it go STOPPED" both impossible — fixed by including an item's
*most recent* terminal job; and the Files page had no way to stop an item at all (only
job-scoped `POST /api/jobs/{id}/stop` existed) — fixed by adding `POST /api/items/{id}/stop`.
Also fixed, out-of-scope-turned-in-scope per the phase 3b prompt: the phase-2 scan-abort bug
(one permission-denied subdirectory used to discard an entire queue's tree) — now a partial
scan plus a surfaced warning. Full detail, including two deliberately-flagged design deviations
(TanStack Query never adopted; a new `@tanstack/react-virtual` dependency), in
`docs/decisions.md`'s phase 3b entries.

**Phase 4, in one paragraph:** `core/patterns.py` is the one evaluator DESIGN.md §12 requires —
`select`/`skip` (item-name, case-insensitive, glob-when-metacharacters-else-substring, skip
beats select, empty-select matches everything unless *patterns-only*) and `file_exclude`
(file basename, any depth, also applied to loose top-level file items). It feeds two
consumers: `core/reconcile.py`'s `counts_predicate` seam (phase 2 left it; a matched file is
now marked `EXCLUDED` — a real state, not an absence — and doesn't count toward its parent
directory's completeness, so a `*.nfo` file_exclude leaves a release `DOWNLOADED` instead of
permanently `PARTIAL`), and `core/queue.py._spawn_decision`'s `exclude_globs` for lftp's own
`--exclude-glob`. `core/autoqueue.py` evaluates every eligible (`REMOTE_ONLY`/`PARTIAL`,
unsuppressed) top-level item against the compiled patterns at the end of every scan pass —
retroactive by construction, since it re-queries the whole known model rather than tracking
"newly seen" itself — and skips anything `auto_queue_suppressed` or in `STOPPED`/`FAILED`/
`REMOVED_LOCAL`/`REMOVED_BOTH`. **The mount sentinel and grace period landed here, not with
`sync`**, per this file's own 2026-08-11 entry: `core/mount_sentinel.py` writes/checks
`.lftpweb-mount-ok` at a queue's local root, `AutoQueue.on_scan()` refuses to act on
*anything* for a queue whose gate fails, and `resolve_absence()` — a pure function wired into
`core/engine.py._persist` — implements DESIGN.md §3.2 rule 3's `REMOVED_LOCAL` transition
with the ~10 minute grace period, which this phase had to build from scratch since phases 2-3
explicitly left it undone. Auto-queue and *patterns-only* both default off per queue
(migration 002 adds the one new column, `DEFAULT 0`, changing nothing for any existing row).
API: pattern CRUD, a live "what would this match" preview endpoint, and a queue-level
mount-gate status read. UI: Settings → Queues gained the two toggles and a patterns editor
with that live preview. Verified against the real fake seedbox
(`tests/test_autoqueue_e2e.py`): a `file_exclude` of `*.nfo` drove `AutoQueue` to queue a real
release, the `.mkv`/`.srt` arrived byte-exact, the `.nfo` never did, and the item reached
`DOWNLOADED`. Every decision made unattended is in `docs/decisions.md`'s phase 4 entry,
including two rejected alternatives worth a second look: whether `file_exclude` should support
path-aware (not just basename) matching, and whether the grace period belongs in the Settings
UI now rather than later.

**Phase 5, in one paragraph — the first phase that deletes data on a machine we don't own.**
`core/verify.py` checks `.sfv`/`.md5` sidecars, falling back (opt-in, off by default) to a
whole-file read as a weaker "readable end to end" guarantee when no sidecar exists.
`core/extract.py` extracts every archive found under an item via `7zz` — the image's only
archive tool, no `unrar` — including multi-part rar (first volume only) and compound tar
formats (two passes: strip compression, then unpack). `core/postprocess.py` is the pipeline
`core/queue.py._reap_one` triggers for a top-level item's job success: verify → (for a `move`
queue) the delete gate → extract → move-to-final, each step off by default at **two**
independent layers (a site-wide `PostprocessSettings` flag AND the queue's own `auto_verify`/
`auto_extract`/`auto_move` column must both be on), except verification for `move`, which is
forced on regardless of either toggle because it's the sole gate on an irreversible delete.
`move_tree` is the cross-device-safe staging→final relocator: `os.rename` fast path,
copy-to-a-same-filesystem-sibling-then-atomic-rename on `EXDEV` (the expected case — the
user's downloads are on NFS), verified to leave no partial file at the destination when the
copy itself fails partway. Deletion (`RemoteConnectionPool.delete_path`, `core/remote.py`)
goes out as `rm -rf --` over the same pooled asyncssh connection scanning already uses, never
lftp's `--Remove-source-files`; every delete and every delete withheld writes an `event` row
(`core/audit.py`) naming the item, queue, mode, and gating condition. `api/settings.py` now
accepts `move` in `IMPLEMENTED_SYNC_MODES` (`sync` still rejected) and force-sets `auto_verify`
server-side whenever `sync_mode == 'move'`. UI: the Settings → Queues mode selector's `move`
option is enabled with an inline misconfiguration warning and a required confirmation
checkbox (DESIGN.md §7.1), per-queue verify/extract/move toggles, and a filled-in Settings →
Post-processing page for the site-wide defaults. Verified end to end against the real fake
seedbox (`tests/test_postprocess_e2e.py`): a `move` queue transferred a freshly-uploaded file,
verified it, deleted the remote copy, and a **second, independent** remote scan confirmed it
gone. Every decision made unattended is in `docs/decisions.md`'s phase 5 entry — read point 0
first, it's about the live queue row above.

**Phase 6, in one paragraph:** `api/history.py` adds `GET /api/history/jobs` (completed/
failed/cancelled jobs — this is where a `succeeded` job's own record lives, since phase 3b's
`list_jobs()` deliberately excludes it from the Transfers page), `GET
/api/history/jobs/{id}/output` (the on-demand fetch for a job's ~4KB captured output —
deliberately *not* inlined in the list payload, since History's row set is unbounded unlike
Transfers'), and `GET /api/history/events` (the full `event` audit trail, including every
`remote_delete`/`remote_delete_withheld`/`remote_delete_failed` row phase 5's postprocessing
pipeline writes). Both list endpoints are `LIMIT`/`OFFSET` paginated with a server-enforced
cap (`MAX_LIMIT = 500`) and a `total` count, filterable by queue/state/error class/date range
(jobs) or queue/kind/level/date range (events). No schema change — every column this phase
reads already existed. The frontend (`pages/HistoryPage.tsx`,
`components/HistoryJobsSection.tsx`, `components/HistoryEventsSection.tsx`) renders two
independently filtered sections, each grouped by queue and virtualized
(`@tanstack/react-virtual`, already a dependency since phase 3b) by flattening queue headers
and rows into one array a single virtualizer walks. A failed job's row can expand to fetch and
show its error class plus the real `output_tail`; delete-audit events get a distinct amber
treatment and a "Deletes only" quick filter, but the legibility DESIGN.md §7.3 asks for comes
from rendering `core/postprocess.py`'s own carefully-worded event messages verbatim, not from
new structured columns. Verified end to end against the real fake seedbox: a real 512-byte
transfer landed in history with `bytes_total`/`bytes_done` both `512`, and a forced
bad-password failure carried `error_class: "AUTH_FAILED"` and a real, non-empty
`output_tail`. **Not verified: the actual browser rendering** — no browser is available in
this environment; only build/lint/type-check and the backend-level e2e were exercised. Every
decision made unattended, including the no-live-updates call and the UTC-only date-filter
limitation, is in `docs/decisions.md`'s phase 6 entry.

**Phase 7, in one paragraph:** `core/backup.py` adds `VACUUM INTO`-based backups — never a
file copy, per DESIGN.md §10.2's own WAL-safety reasoning — with settings (daily by default,
keep 7, both configurable, stored in `setting` the same way `TransferSettings`/
`PostprocessSettings` are), a `BackupScheduler` background loop (same `_task`/`start()`/
`stop()` shape as `Engine`/`TransferQueue`), and retention that prunes oldest-first. **The
pre-migration backup — the one DESIGN.md calls "the one that actually saves you" — is wired
directly into `db.py.migrate()`**, unconditional and not gated by any settings toggle,
firing exactly once before the first pending migration runs; a failed backup logs and lets
the migration proceed rather than blocking startup, since the migration's own
transaction-with-rollback (phase 1's finding) is still standing either way. The encryption
secret (`core/crypto.py`) is proven absent from a backup byte-for-byte, not just assumed
absent because `VACUUM INTO` "shouldn't" reach it. `core/logtail.py` bounds log tailing to a
fixed byte budget read backwards from the end of the file, proven by an instrumented test
against a 10+ MB fixture that the byte cap is actually honored, not merely correct on a small
file; `api/logs.py` lists rotated files, tails only the live one with an optional level
filter, and downloads any of them, with the credential redactor's existing coverage (it
already runs on the way *in*, `logsetup.py`) verified end to end rather than duplicated as a
second layer. `/api/health` (DESIGN.md §10.3) grows `host_reachable` (a tri-state: `null` =
no host configured, `false` = configured but the pooled connection last failed, read from the
engine's already-pooled connection rather than a fresh SSH call on every poll) and
`scheduler_alive` (`TransferQueue`'s own admission-loop task) without touching the container
`HEALTHCHECK`'s behavior, since it only checks the HTTP status code, never the body. Settings
→ Logs and Settings → Backup (previously placeholders) are filled in. **The scheduled backup
is the one deliberate exception to this run's "every new capability defaults off" rule** —
shipped at DESIGN.md's own literal default (daily, keep 7) because it changes nothing about
transfer behavior, only adds small bounded files, and an unattended install gets zero benefit
from phase 7 if it's off until someone finds the settings page. Verified: 304 tests pass with
the fake seedbox up (0 skipped), including the pre-migration backup exercised for real
(database built at migration N, migration N+1 added, `migrate()` run again, backup opened
with an independent connection and confirmed to hold the *prior* schema). Both lint gates
clean, `npm run build`/`npm run lint` clean, all three compose files validate, fake-seedbox
containers torn down afterward. **Not verified: the actual browser rendering** of the two new
Settings pages — no browser is available in this environment. Every decision made unattended
is in `docs/decisions.md`'s phase 7 entry.

**Phase 8, in one paragraph:** `core/auth.py` holds the three `AUTH_MODE`s
(`none`/`password`/`proxy`, stored in `setting` like every other `*Settings` dataclass,
defaulting to `none` when absent), argon2id password hashing, session create/validate/purge
(SHA-256-hashed token, the raw value only ever a cookie), API key create/validate/delete
(SHA-256-hashed, same reasoning as sessions — high-entropy tokens don't need argon2's
memory-hard slowness), trusted-CIDR matching off the ASGI socket's own peer address (never a
spoofable header), and an in-memory per-IP login rate limiter.
`middleware.py.AuthMiddleware` is one raw ASGI middleware (covers both HTTP and WebSocket
scopes, unlike `BaseHTTPMiddleware`) gating everything under `/api/` except a four-entry
public allowlist (`/api/health`, `/api/auth/login`, `/api/auth/session`, `/api/auth/logout`)
— a default-*deny* shape chosen specifically because the alternative (`Depends()` per route)
is default-*allow*, and "a route accidentally left open" is this phase's named failure mode.
`api/auth.py` exposes `/api/auth/{login,logout,session}` and
`/api/settings/auth/{,password,api-keys}`, refusing server-side to ever store `mode:
"password"` with nobody able to log in, or `mode: "proxy"` without a trusted CIDR — both
enforced regardless of what the frontend does. Migration `004_phase8_auth.sql` adds
`auth_user`/`session`/`api_key`, inserting no rows (mode stays `none` for every existing
install). The credentials-need-re-entry finish (§8, held over from phase 2): `HostConfig`
gained `credentials_need_reentry`, and `core/queue.py._admit` / `core/engine.py.scan_queue`
both check it — holding every scheduler decision and failing that queue's scan with one
clean message, respectively, instead of spawning doomed lftp processes or retrying a
connection that can only ever fail. Frontend: `hooks/useAuth.tsx` fetches `GET
/api/auth/session` once on mount; `App.tsx` gates the *entire* routed app behind one
`authenticated` check (mirroring the backend's one-gate philosophy) rather than a per-route
guard; `LoginPage.tsx`, `CredentialsBanner.tsx` (polls host status, links to Settings →
Connection), and a filled-in `AuthTab.tsx` (mode selector, user setup, password change, API
key management) round it out. Two lockout-recovery routes, both *exercised* by tests, not
just documented: `LFTPWEB_AUTH_MODE` (an env var override that wins over whatever is stored)
and deleting the `auth_user` row (treated as open access rather than a permanent lock).
Verified: 366 tests pass with the fake seedbox up (0 skipped; 357 passed / 10 skipped
without it) — no regressions in any earlier phase's tests, including a 42-route enumeration
proving every protected endpoint returns 401 unauthenticated in `password` mode, and a
drift-check comparing that enumeration against the app's own registered routes. Both lint
gates clean (`format --check` again caught files `check` alone missed — the third time this
exact failure mode has bitten this project). `npm run build`/`npm run lint` clean, all three
compose files validate, fake-seedbox containers torn down afterward. **Not verified: the
actual browser rendering** of the login page, Settings → Auth, and the credentials banner —
no browser is available in this environment. Every decision made unattended is in
`docs/decisions.md`'s phase 8 entry — read points 1–2 first, they're the lockout-recovery
design. This phase's work was prepared and reported without committing, per that task's
explicit instruction — **it was committed afterward as `b936576`**, so unlike phase 9 below,
there is nothing left prepared-but-uncommitted from phase 8.

**Phase 9, in one paragraph:** the UI half (§9.2) added Files-page text/state filters
(client-side — the page is WS-driven with the whole queue's tree already in the browser, so
there's no endpoint to add) and honest partial-failure reporting on bulk Queue/Stop
(`Promise.allSettled`, not `Promise.all` — "7 of 10 queued, these 3 failed because …" rather
than the first rejection hiding the other nine outcomes), plus a `host_reachable`/
`scheduler_alive` readout in the stats header (`StatsHeader.tsx`, polling `/api/health` — the
fields phase 7 added to the response and explicitly deferred the UI for). Virtualization
(`@tanstack/react-virtual` in `FileTree.tsx`, `ItemDrawer.tsx`, `HistoryJobsSection.tsx`,
`HistoryEventsSection.tsx`) was reviewed, not changed — all four already use sensible fixed or
dynamic sizing with 10–16-row overscan; no browser exists to measure actual scroll smoothness,
so this is a code-review finding, not a measurement. The documentation half — the larger half
of this phase's actual work — reconciled `README.md`, `DESIGN.md` §13/§15, and this file
against reality after eight phases of incremental docs, several written while later phases
were still hypothetical: `DESIGN.md` §13 now marks every phase shipped and names phase 9's own
two unbuilt items rather than pretending they don't exist; §15's risk table got a "Status
(phase 9)" line per row saying closed/live/superseded, keeping the original reasoning; and this
file lost a stale phase-8-not-committed banner (phase 8 was committed as `b936576` since that
report was written) along with a stale "phases 1–3 of 9" status line that had never been
updated in `CLAUDE.md`. `README.md` gained a "Known gaps" section consolidating seven
deliberate scope reductions collected from `docs/decisions.md` across all eight prior phases,
plus two more found while reconciling this phase (Settings → Transfer has no UI despite a
complete backend since phase 3; Files has no bulk Delete local/remote) — named rather than
built, per this phase's own explicit instruction not to close gaps silently. One factual error
was also caught and fixed: the README's volume table had `/staging` backwards relative to what
phase 5 actually built (`local_path` is the download target; `staging_path` is where a
`move`-mode item is *relocated to* afterward — the opposite of "download here, move to
`/downloads` when complete"). `uv run pytest`: 367 passed, 0 skipped (fake seedbox up), 357
passed / 10 skipped without it — no regressions, no backend code changed. Both lint gates
clean. `npm run build`/`npm run lint` clean. All three compose files validate. Fake-seedbox
containers torn down and confirmed removed via `docker ps -a` afterward. **This phase's work
is prepared but deliberately not committed**, per that task's explicit instruction — see
`docs/decisions.md`'s phase 9 entry and the phase 9 report for the exact file list and the
proposed commit message.

**Commits so far:** repo init + standard adoption, the design revisions, phase 1 (`b0109ae`),
phase 2 (`de6d74b`), phase 3a (`36b9123`), phase 3b (`c814aa0`), phase 4 (`db89b63`), phase 5
(`b0c9cb3`), phase 6 (`d76a662`), phase 7 (`c6dcc03`), phase 8 (`b936576`). All on `dev`. Phase
9's work is prepared on the working tree but **not yet committed** — see the phase 9 prompt's
final report for the proposed commit message.

---

## Operating rules

**Scope**
- Work only the phase or task the user names. Don't fan out into later phases or add
  "while I'm here" changes. Offer them as a one-liner, then wait.
- **Surface major design decisions discovered during a build** rather than silently resolving
  them. If the build reveals that `DESIGN.md` is wrong or underspecified, say so — the doc gets
  corrected, it isn't quietly diverged from.

**Handoff prompts** (`handoff-prompt-workflow` @ v2.0.0 — full rules in `CLAUDE.md`)
- Anything beyond ~1–2 files goes into a `prompts/` file executed by a **spawned subagent**.
  Opus for research/planning, **Sonnet for coding**.
- The prompt self-updates its frontmatter and `git mv`s to `prompts/done/` (or `failed/`).
- **One commit at the end**, prompt bundled in. Ask `y/n`. Never `git add -A`, never
  auto-commit, never push.

**Git**
- Day-to-day work is on `dev`.
- **Never a `Co-authored-by:` trailer** — explicitly reaffirmed by the user 2026-08-11, and true
  of every commit in the history so far. Conventional-Commit prefixes required:
  `feat:` / `fix:` / `chore:` / `docs:`.
- `code-checkin-and-pr` and `release-prep-and-cut` were adopted 2026-08-11 alongside repo
  creation; `standards.md` is the in-repo source of truth for what is actually wired.
  **Branch protection on `main` is live** (see "Repo, branches, and what's on GitHub" above,
  confirmed via `gh api` — not a pending step). Treat `main` as fully protected: PR + all
  required checks green, no direct push, no force-push, no exceptions.
- `repo-sandbox-permissions` is **deliberately not adopted** — dedicated dev host, same call
  the user made when de-adopting it from AmmoLedger. Don't "helpfully" add it.

**Docs**
- Non-obvious decisions go in `docs/decisions.md`, newest at top, with rejected alternatives.
- Doc updates ship in the same commit as the code they describe.
- Local scratch, fixtures, and generated files go under `private_data/` (gitignored).

---

## Traps worth knowing before you touch the code

These are the places where the obvious implementation is wrong. Each is written up in
`DESIGN.md`; this list exists so a fresh session knows to go read it.

- **Excluded files break completeness** (§4.7, §3.2 rule 8). A `file_exclude` of `*.nfo` means
  those files never arrive — so if the reconciler counts them as missing, every filtered
  release is permanently `PARTIAL` and re-queued forever. One evaluator (`core/patterns.py`),
  used by both the lftp command builder and the reconciler.
- **Stop must suppress auto-queue** (§4.6). A stopped item still matches its pattern; without
  `auto_queue_suppressed`, auto-queue restarts it 30 s later, forever.
- **Sparse files lie** (§4.4). `pget` writes sparse files, so `st_size` is wrong — read the
  `.lftp-pget-status` sidecar, and account for the `.lftp` temp suffix.
- **Allocations are never re-shaped** (§4.5). Bandwidth is assigned at admission and fixed for
  the job's lifetime. That's what makes the missing lftp control channel a non-issue.
- **NFS + `root_squash`** (§11.2). Chown `/config` only; a chown failure on a data volume is a
  warning, not a fatal.
- **`cap_drop: ALL` breaks the PUID/PGID entrypoint unless you add capabilities back**
  (found in phase 1, see `docs/decisions.md`). `chown`/`setuid`/`setgid` are capability-gated
  even for uid 0; `docker-compose.yml` adds back `CHOWN`, `SETUID`, `SETGID` on top of
  `cap_drop: ALL`. Also: `read_only: true` means the entrypoint can never write
  `/etc/passwd`/`/etc/group` (no `addgroup`/`adduser` — use numeric `uid:gid` everywhere).
- **A venv's shebangs bake in an absolute path.** Build and copy it forward at the *same*
  path in every Docker stage, or `COPY --from=` carries a venv whose scripts point at a
  directory that no longer exists (phase 1 hit this: `docker/Dockerfile` uses `WORKDIR /app`
  everywhere for exactly this reason).
- **`asyncssh.connect()` crashes under lftpweb's own numeric-uid convention** (found in phase
  2, see `docs/decisions.md`). It unconditionally calls `getpass.getuser()` for SSH-config `%u`
  templating; on Python 3.13, an unregistered uid (exactly §11.2's PUID/PGID and native `user:`
  identity model) makes that raise `OSError`, which asyncssh's own `except KeyError:` doesn't
  catch — every connection fails, for every auth method. `core/remote.py` sets a fallback
  `LOGNAME` at import time (only if nothing already identifies the user) as the fix.
- **`asyncssh.connect(known_hosts=None)` doesn't just skip verification — it skips your own
  callback too** (found in phase 2). `validate_host_public_key` is only invoked when
  `known_hosts` is a real (even empty) `SSHKnownHosts` object; passing `None` sets an internal
  flag that trusts any server key *and* never asks the client factory anything. Pass
  `asyncssh.SSHKnownHosts()` (empty, non-`None`) to actually enforce your own policy.
- **`find`'s `\n`-terminated wire format and "paths can contain newlines" are in tension**
  (§5 vs §15.10, phase 2). `core/remote.py`'s parser anchors on the record header rather than
  splitting lines, which handles it in practice, but a path containing the *exact* bytes of a
  header immediately after a literal newline would still misparse — a property of the
  specified `find -printf` command, not fixed by deviating from it. See the phase 2 report.
- **`mirror`'s local target is the item's *parent* directory, not the item's own directory**
  (found in phase 3). `mirror -c 'REMOTE/item' 'LOCAL/'` creates `LOCAL/item/...` itself —
  passing `LOCAL/item/`, the "obviously" symmetric choice with `pget`'s exact-file-path target,
  produces a doubly-nested `LOCAL/item/item/...` tree. `core/lftp.py.build_transfer_command`'s
  docstring has the full explanation; `core/queue.py` computes the two differently on purpose.
- **A bare `open sftp://user@host` makes lftp prompt for a password itself, even under key
  auth** (found in phase 3) — `GetPass() failed -- assume anonymous login` /
  `Login failed: Password required`, despite the connect-program's ssh having already
  authenticated successfully via the key. Always use `open -u user,password`, with an *empty*
  password field for `key`/`agent` auth.
- **`pget:save-status` defaults to 10s** (found in phase 3) — far too coarse for a ~1 Hz
  progress sampler; a transfer inspected at the 1s/2s/3s marks under the default has no
  `.lftp-pget-status` sidecar yet at all. Every job's rc file sets `pget:save-status 1s`.
- **`GET /api/files` must read `item.state` from the database, not `core/engine.py`'s
  in-memory scan model** (found live in phase 3, through the running API). The in-memory model
  is `core/reconcile.py`'s pure structural output — it has no notion of QUEUED/DOWNLOADING/
  STOPPED/FAILED, so serving it from an API a stop/queue action is supposed to affect silently
  reverts the visible state on the very next read. `api/files.py` queries `item` directly.
- **A periodic rescan can silently overwrite a job-lifecycle state back to a structural one**
  (found in phase 3) — a `STOPPED` item with a still-partial file reads as `PARTIAL` again on
  the next scan unless something stops it. `core/engine.py._persist` leaves `state` alone for
  any item with a `queued`/`running` job or `auto_queue_suppressed` set; everything else still
  gets recomputed every pass.
- **`pget -o <path>` does not create its target's parent directory** (found in phase 3, unlike
  `mirror`, which creates its own subtree). `core/queue.py._spawn_decision` `mkdir -p`s it
  first — a no-op for a genuinely top-level item, load-bearing for anything nested.
- **A leading blank line in an lftp `-c`/`source`d script corrupts quote-stripping on the next
  `set key "value with spaces"` line** (found in phase 3, real lftp 4.9.2). Reproducible on
  demand; `core/lftp.py.build_rc_text` never emits one.
- **Never publish a full node list except on WebSocket connect** (found/fixed in phase 3b, see
  `docs/decisions.md`). Every update after the initial `snapshot` must be a `queue_delta`
  (`core/engine.py.diff_nodes`, scan-driven) or an `item_delta` (`core/queue.py`, lifecycle- or
  progress-tick-driven) — both proportional to what changed, never to tree size. A future change
  that starts putting a full `nodes` array on anything but the connect-time `snapshot` message
  is this same regression coming back.
- **GNU `find -printf` exits nonzero the instant it can't read *one* subdirectory, but keeps
  scanning everything else and still prints what it found** (named in phase 3, fixed in phase
  3b — see `docs/decisions.md`). Any nonzero exit with usable stdout is a *partial* success
  (`core/remote.py.interpret_primary_scan_result`), not a hard failure — only an exit with *no*
  stdout at all means the scan genuinely failed.
- **`core/queue.py.list_jobs()` is not "queued + running jobs" — it also includes an item's most
  recent `failed`/`cancelled` job** (found in phase 3b). DESIGN.md §9.2 requires the Transfers
  page to show failed rows' error class/output tail and a stopped row going `STOPPED`; the
  phase 3a query structurally couldn't produce either, since a job vanishes from that query the
  instant it stops being active. A manual retry's fresh `queued` row naturally supersedes the
  old terminal one — no separate cleanup needed.
- **The Files page needs `POST /api/items/{id}/stop`, not `POST /api/jobs/{id}/stop`** (added in
  phase 3b). Unlike the Transfers page, the Files page only ever has an item id, never the job
  id currently servicing it — `GET /api/files` deliberately doesn't expose one, since an item
  can outlive several job attempts. `TransferQueue.stop_item` resolves item → active job.
- **A `file_exclude` pattern must reach the reconciler, not just lftp's `--exclude-glob`**
  (phase 4). `core/patterns.py.build_counts_predicate` marks the matched file `EXCLUDED` and
  removes it from its parent directory's completeness accounting; skip this and every
  filtered release sits `PARTIAL` forever. `core/reconcile.py` and `core/queue.py` both
  consume the identical compiled pattern set for exactly this reason — see docs/decisions.md.
- **`CompiledPatterns.compile()` iterating its input three times silently breaks on a
  generator** (found building the pattern-preview endpoint, phase 4, before it ever shipped).
  Fixed by materializing the iterable first. A reminder that "one evaluator, two consumers"
  doesn't protect against a bug *inside* the evaluator itself.
- **The mount gate blocks all auto-queue action for a queue, not just the `REMOVED_LOCAL`
  transition** (phase 4). A blanket per-queue check (`AutoQueue.on_scan` returns immediately
  if `core/mount_sentinel.py.check()` fails) is what also protects a **brand-new** queue
  whose local root never mounted — every item would read `REMOTE_ONLY` from the very first
  scan, with no history to compare against, so only a blanket gate stops auto-queue from
  queueing transfers into a directory that isn't really there.
- **DESIGN.md §9's "TanStack Query for REST" was never actually adopted** (found in phase 3b,
  flagged rather than silently followed or silently fixed). Phases 1–3a built a hand-rolled
  `fetch` client + poll hook instead, with no record of the substitution. Phase 3b's `useJobs.ts`
  continues that convention on purpose rather than introducing the library mid-project — a
  future session should either correct DESIGN.md §9 or do the migration as its own scoped phase,
  not as a side effect of whichever phase next touches data-fetching.
- **`path_queue.local_path` is still where lftp downloads to and what the reconciler scans —
  `staging_path` is the phase 5 post-processing Move step's *destination*, not a download
  target** (phase 5, resolved ambiguity — see docs/decisions.md). DESIGN.md names the field
  `staging_path` and describes Move as "staging → final destination," which reads naturally as
  "downloads land in staging first," but making that true would mean the reconciler comparing
  remote vs. local at a *different* root during a transfer than after one completes — reaching
  back into phase 2/3's already-verified scan/reconcile code for a phase whose brief is
  post-processing. The chosen reading needs zero changes there; the frontend labels the field
  "Final destination" to match, without renaming the column.
- **A `move`-mode item's verification always runs, bypassing the "global setting AND per-queue
  toggle" rule every other post-processing step follows** (phase 5). It is the sole gate on an
  irreversible remote delete (DESIGN.md §7.3); muting it via an unrelated site-wide default
  (`PostprocessSettings.verify_enabled`) would silently turn `move` into "downloads, never
  deletes, never explains why." `core/postprocess.py.process_item`'s `verify_effective`
  computation is the one step that ORs in `sync_mode == "move"` rather than ANDing two toggles.
- **A `move`-mode delete sets `item.remote_deleted_at` but never changes `item.state`** (phase
  5). DESIGN.md's `REMOVED_BOTH` is the wrong state for this — its own definition implies local
  absence too, and `move` never removes the local copy. The item's state stays whatever verify/
  extract last set (`VERIFIED`/`EXTRACTED`); if the item is *also* relocated by the Move step,
  the resulting local absence is picked up by phase 4's existing `REMOVED_LOCAL` grace-period
  machinery on the next scan, exactly as if a human or an `*arr` importer had moved it — no new
  state, no new code path.
- **The user's live queue's `sync_mode = 'move'` row went from inert to live the moment phase
  5 shipped, and was deliberately left untouched** — see the warning near the top of this
  file's "Where we are" and docs/decisions.md's phase 5 entry, point 0. The first thing to tell
  the user when they're back.
- **A list endpoint over an unbounded table must not inline a per-row blob just because a
  bounded sibling endpoint does** (phase 6). `api/jobs.py`'s `JobOut` inlines `output_tail`
  (~4KB) because that endpoint's row set is bounded by construction (`list_jobs()`'s own
  docstring). `api/history.py` reads the same `job` table with no such bound, so it carries
  only `has_output_tail` in the list and adds `GET /api/history/jobs/{id}/output` to fetch the
  blob on demand — copying `JobOut`'s shape onto an unbounded endpoint would have silently
  reintroduced the "thousands of rows × 4KB" cost the row cap exists to prevent.
- **`Settings → Transfer` (`TransferTab.tsx`) still renders `PagePlaceholder`, despite
  `core/queue.py`'s `TransferSettings` and `api/settings.py`'s `/api/settings/transfer` being
  complete and tested since phase 3a** (found reconciling docs at phase 9 — phase 5's own
  `docs/decisions.md` entry had already flagged this as "likely phase 9" territory, but phase
  9's actual prompt scoped its UI work narrowly and never named this tab). Don't assume every
  Settings tab has a form behind it just because the others do — check `nav.ts` against the
  page component before relying on one. Site bandwidth/concurrency/fast-lane tuning, the §9.3
  live connection-count warning, and the free-text "extra lftp settings" box are all reachable
  today only via direct API calls. See `README.md`'s "Known gaps."
- **The Files page's bulk actions cover Queue/Stop only, not the "Delete local"/"Delete
  remote" DESIGN.md §9.2 also lists** (phase 9's own explicit scope — see its prompt and
  `docs/decisions.md`). There is no manual per-item or bulk delete endpoint anywhere in the
  API; the only deletion in this codebase is `move` mode's automatic, verification-gated
  pipeline (`core/postprocess.py`). Don't assume a "Delete" button exists on the Files page
  just because DESIGN.md's mockup shows one.
- **`FileTree.tsx`'s text/state filters ignore `collapsed` entirely while a filter is active**
  (phase 9) — a match inside a collapsed directory must still surface, so a filtered view is
  computed by flattening the *whole* tree fully expanded, then keeping only matches and their
  ancestor directories, rather than trying to reconcile filtering with whatever the user had
  manually collapsed. Collapse state is restored the instant both filters clear.
