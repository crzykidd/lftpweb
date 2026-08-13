# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it starts
cutting releases (see [`standards.md`](standards.md) — `release-prep-and-cut`). `0.0.1` is the
in-development version, not a release; nothing has been tagged yet.

<!--
Skeleton for the next roll:
## [Unreleased]

### Added
### Changed
### Fixed
### Security
### Deprecated
### Removed
-->

## [Unreleased]

### Added

Everything below reflects `DESIGN.md` §13 build phases 1–9 — **all nine are built** — plus a
post-phase-9 session on 2026-08-12 (its entries are marked *(2026-08-12)*). Nothing here has
been released; `0.0.1` remains the in-development version. Read `README.md`'s "Known gaps"
alongside this list: several entries below ship with deliberate, documented limitations.

- **Phase 1 — skeleton + container.** FastAPI + SQLite backend, `host` / `path_queue`
  schema, `/api/health`, both production and development `docker compose` files, and the
  React + Vite SPA shell (nav, theme, version link).
- **Phase 2 — scanning + reconciliation.** Connect to a seedbox over SSH/SFTP and browse
  the remote tree alongside the local one; named **path queues** (one remote → local
  mapping each, with their own settings); a read-only Files view, live over WebSocket,
  grouped by queue. Credentials encrypted at rest (moved up from phase 8 — this is the
  phase where a seedbox password first exists). The fake-seedbox integration harness
  (`docker-compose.test.yml`, GNU + busybox sshd containers over a known-size seeded
  tree) so the remote scan path is tested against real ssh/sftp, not mocks.
- **Phase 3 — transfer engine + scheduler.** Queue transfers manually, watch live
  progress, stop them, and resume from the partial; bandwidth ceiling and concurrency
  limits enforced by an admission-control scheduler. The Transfers page, the per-item
  drawer, and per-row/bulk Queue/Stop actions on a virtualized Files tree.
- **Phase 4 — auto-queue + patterns.** `select` / `skip` / `file_exclude` patterns with a
  live "what would this match" preview, evaluated by one shared evaluator so an excluded
  file is marked `EXCLUDED` rather than leaving its release permanently `PARTIAL`. A
  mount sentinel gates every auto-queue action for a queue whose local root isn't really
  mounted, and the `REMOVED_LOCAL` grace period lands with it. Auto-queue defaults **off**
  per queue.
- **Phase 5 — post-processing + `move` mode.** Verify (`.sfv`/`.md5` sidecars, with an
  opt-in whole-file-read fallback), extract (`7zz`, including multi-part rar and compound
  tar), and relocate a finished item to its final destination. `move` mode deletes the
  remote copy **only** after verification passes — verification is forced on for `move`
  regardless of any other toggle, because it is the sole gate on an irreversible delete.
  Every delete and every delete *withheld* writes an audit event. All post-processing
  defaults **off** at two independent layers.
- **Phase 6 — History page.** Every completed, failed, and cancelled transfer plus the
  full audit trail (including remote deletes), paginated with a server-enforced row cap,
  filterable by queue / state / error class / date range, and grouped by queue. A failed
  row expands to fetch its real `output_tail` on demand.
- **Phase 7 — operations.** A rotating log viewer with a bounded backwards-read tail and
  level filter; `VACUUM INTO` database backups (never a file copy — WAL safety), both
  scheduled and manual, with oldest-first retention; and a **pre-migration backup wired
  directly into the migration runner**, unconditional and not gated by any settings
  toggle. `/api/health` grew `host_reachable` and `scheduler_alive`.
- **Phase 8 — authentication + hardening.** Three `AUTH_MODE`s (`none` / `password` /
  `proxy`), argon2id password hashing, sessions, CSRF, API keys, and per-IP login rate
  limiting, enforced by a single default-*deny* ASGI middleware covering both HTTP and
  WebSocket scopes. Two tested lockout-recovery routes. Auth defaults **off**.
- **Phase 9 — polish.** Files-page text/state filters, honest partial-failure reporting on
  bulk actions ("7 of 10 queued, these 3 failed because …"), and a seedbox-reachability /
  scheduler-liveness readout in the stats header.
- **A Dashboard page with throughput charts** *(2026-08-12)*. Bytes transferred per hour over
  the last 24 hours, and transfer speed over a selectable 1 h / 12 h / 24 h window, both
  hand-rolled SVG with no charting dependency. Backed by a new per-queue sample store
  (30-second interval, 7-day retention, configurable to 30) that also distinguishes **idle
  from down** — an instance that was stopped renders as a gap, never a flat zero line.
- **Settings → Transfer** *(2026-08-12)*. Every site-level bandwidth, concurrency, fast-lane
  and retry knob, plus the free-text "extra lftp settings" box — previously reachable only by
  hand-crafting HTTP requests despite the API existing since phase 3a. Includes §9.3's live
  worst-case connection-count readout ("2 jobs × 4 parallel × 4 pget-n = 32 concurrent SFTP
  sessions"), since those three numbers multiply silently and seedboxes refuse connections
  well below what the inputs accept.
- **Expand all / Collapse all** on the Files tree, and the **queue name on each Transfers
  row** *(2026-08-12)*.

### Changed

- **`sync_mode = 'move'` went from stored-but-inert to fully live** when phase 5 shipped.
  An existing queue already configured for `move` begins deleting verified remote copies
  with no further action — review any stored `move` queue before pulling this.
- **Extraction now stages into `_UNPACK_<name>` and merges into place only on full success**
  *(2026-08-12)*; a failure leaves `_FAILED_<name>` as evidence. Extraction was the one step
  that wrote files under their *final* names while incomplete, which meant Sonarr/Radarr could
  import a half-extracted release — both prefixes are the convention those tools already skip.
  Downloads were never exposed this way (`xfer:use-temp-file`).
- **The `item` table is now the single authority for item state** *(2026-08-12)*. The
  WebSocket, its connect-time snapshot, and `GET /api/files` all publish a projection read
  back from the database rather than the reconciler's structural reading, so the REST view and
  the live view can no longer disagree about the same item.

### Fixed

- Ten defects found only against real hardware, none reachable from unit tests or the fake
  seedbox: OpenSSH fatally requiring a `/etc/passwd` entry for its own uid under the
  PUID/PGID identity model; lftp retrying forever with no `net:max-retries` / `net:timeout`
  written; `net:reconnect-interval-base` silently rejecting a `5s`-style value; the
  WebSocket omitting `item.id` so no Files row could ever be queued; a `VOLUME` declaration
  creating a phantom root-owned `/downloads`; the per-job `/run` directory never created
  before privileges dropped; `pget -n 4` fanning a 16-byte file across four connections;
  jobs left `running` by a restart becoming permanent phantom transfers; and a `sync_mode`
  the UI offered but nothing implemented behaving silently as `copy`.
- A scan aborting an entire queue's tree because one subdirectory was unreadable — now a
  partial scan with a surfaced warning.
- The `README.md` volume table described `/staging` and `/downloads` backwards relative to
  what post-processing actually does.
- **Post-processing outcomes were erased ~30 seconds after being set** *(2026-08-12)*. The
  periodic rescan overwrote every §6 state with a freshly computed structural one, so a
  verified, extracted release read as plain `DOWNLOADED` within half a minute — and, worse,
  **`CORRUPT` and `EXTRACT_FAILED` disappeared on their own** before anyone could see them.
  Outcomes now win over a fresh `DOWNLOADED` while the content is present, `PARTIAL` still
  beats them, and absence still reaches `REMOVED_LOCAL` through the grace period. Present
  since phase 5.
- **A `REMOVED_LOCAL` item was published to the UI as `REMOTE_ONLY`** — Queue button and all —
  because the WebSocket carried the structural reading rather than what was persisted
  *(2026-08-12)*. Present since phase 4.
- **An empty remote directory reported itself as `DOWNLOADED`** when nothing had been
  downloaded *(2026-08-12)*, because a directory with no files that count is vacuously
  complete. Now `REMOTE_ONLY` until mirrored — while a directory whose children are *all
  excluded* by a pattern still reads `DOWNLOADED`, which is what stops a filtered release
  being re-queued forever.
- **lftpweb's own mount sentinel (`.lftpweb-mount-ok`) appeared in the Files tree** as a
  local-only file the remote was missing *(2026-08-12)*.
- **The development container could not transfer anything** *(2026-08-12)*: the `dev` image
  shipped without `lftp`, `ssh` or `7zz`, had no `/etc/passwd` entry for the running uid (which
  OpenSSH fatally requires), and could not write `/run/lftpweb`. Scanning worked throughout, so
  the environment looked healthy until the first Queue click. Production was unaffected in all
  three cases. Also: the Vite dev proxy never forwarded the WebSocket upgrade, so the Files
  page connected to nothing while every REST call succeeded.
- **Application logs were 99.8% library noise** *(2026-08-12)*: `LFTPWEB_LOG_LEVEL=DEBUG` set
  the *root* logger, so `aiosqlite` logged every statement twice — measured 37,388 library
  lines against 1 from lftpweb itself, on a rotating handler whose fixed budget meant that
  chatter evicted anything an incident would need. Third-party loggers now have floors, lifted
  per-library with `LFTPWEB_DEBUG_LIBS`.

### Known issues

- **Scheduled backups are broken** *(found 2026-08-12, unfixed)*. `VACUUM INTO` runs on the
  shared application connection and cannot execute inside a transaction, so a backup taken
  while any other write sits between its statement and its commit fails with
  `cannot VACUUM from within a transaction`. The race dates from phase 7 but became routine
  when the metrics sampler began writing a heartbeat every 30 seconds. Backups default on, so
  an unattended instance fails its nightly backup silently. Reproduction and the agreed fix
  are in `prompts/startnewsession.md`.

### Security

- Seedbox credentials encrypted at rest (moved up from phase 8 to phase 2 — that is where
  a password first exists). A restored database whose key is absent marks the host as
  needing credential re-entry, and both the scheduler and the scanner refuse to act on it
  rather than spawning doomed processes.
- Authentication, CSRF protection, API keys, and login rate limiting (phase 8, above).
  Known deliberate trade-offs — SHA-256 rather than argon2id for high-entropy tokens,
  un-normalized login response timing, and a fail-open `password` mode when no user row
  exists — are named in `README.md`'s "Known gaps" rather than left to be discovered.

### Deprecated

### Removed

## Archived releases

_(none yet — the first minor/major release will create `docs/CHANGELOG-<minor>.x.md`
archive files per the `release-prep-and-cut` standard's summarize-on-archive rule; this
index will list them here.)_
