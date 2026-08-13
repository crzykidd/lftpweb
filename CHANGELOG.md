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

Everything below reflects `DESIGN.md` §13 build phases 1–9 — **all nine are built**, matching
the state `README.md` describes. Nothing here has been released; `0.0.1` remains the
in-development version. Read `README.md`'s "Known gaps" alongside this list: several entries
below ship with deliberate, documented limitations, and **no UI screen in this project has
ever been opened in a browser**.

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

### Changed

- **`sync_mode = 'move'` went from stored-but-inert to fully live** when phase 5 shipped.
  An existing queue already configured for `move` begins deleting verified remote copies
  with no further action — review any stored `move` queue before pulling this.

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
