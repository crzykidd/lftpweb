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
- **The Files tree now shows when each row last changed state** *(2026-08-12)*: "Downloaded
  3 min ago" / "Remote 2 hr ago", relative time via the built-in `Intl.RelativeTimeFormat`
  (no new dependency), with the absolute local time on hover. Backed by a new
  `item.state_changed_at` column (migration 006), stamped by two triggers rather than writer
  discipline — `item.state` is written from three separate modules, and a timestamp every
  writer has to remember to also set is a timestamp that eventually goes silently wrong.
  Existing rows are backfilled from the closest thing already on hand
  (`extracted_at`/`verified_at`/`downloaded_at`/`first_seen_at`, an approximation); everything
  from this migration forward is exact. A single shared per-tree ticker drives the relative
  reading rather than a timer per row, since the Files tree can hold thousands of rows.
- **The settle gate** *(2026-08-12, defaults **off**)*: a top-level item (a release directory
  or a loose top-level file) is now fingerprinted every scan
  (`file_count, total_bytes, max_mtime` over its whole remote subtree), and — when this new
  toggle is on — must hold that fingerprint across 2 consecutive scans before auto-queue will
  pick it up or before it's allowed to reach `DOWNLOADED` and trigger post-processing. Fixes a
  real gap: a release still being uploaded, caught mid-upload, can look byte-complete for the
  files that *have* fully arrived while more are still coming — a growing single file
  self-heals (re-queued, resumes) but a growing *directory* previously did not, and could be
  moved/extracted/deleted-from-remote with files still missing. A manual Queue click still
  overrides the *queueing* half (explicit user action beats a heuristic); the *completion*
  half — never publishing `DOWNLOADED` for an unsettled item — always applies regardless, so
  the worst case is a wasted partial transfer that resumes, never a bad import or a bad
  delete. Held items surface as `REMOTE_ONLY` with a new `substate: "settling"` (a small,
  deliberately quiet dot next to the state chip on the Files page — most items pass through
  it on every first sighting). **Off by default because it delays every transfer by up to
  ~60 seconds** (two scan intervals at the current 30s default), including the atomic
  hardlink path where it buys nothing; turn it on via `PUT /api/settings/settle` (no
  Settings-page UI yet — a named gap, same as Settings → Transfer for several earlier
  phases). See `docs/decisions.md` for the full reasoning and the DESIGN.md wording drafted,
  not yet applied, alongside this entry.
- **Delete local files — manually from the Files page, and on a retention schedule**
  *(2026-08-12)*. The Files tree now has a per-row and bulk "Delete" action (with a
  confirmation dialog showing the count and total bytes — this is irreversible, unlike
  Queue/Stop), and a new background `RetentionScheduler` can remove local copies older than a
  configurable number of days, keyed on `downloaded_at` (not `state_changed_at` — "when did it
  complete" and "when did it last move" are different questions). **Retention defaults off,
  non-negotiably** — this deletes the user's own data, and that is not where this project makes
  its one "ships on" exception (scheduled backups, which only ever add files). Both callers
  share one primitive (`core/local_delete.py.delete_local`), which enforces path containment
  (refusing to follow a symlink out of the queue's local root), no active job, no in-flight
  post-processing worker, and the same mount-sentinel gate auto-queue uses — with an audited
  `event` row for every delete *and* every withheld one. They differ in exactly one guard: a
  robot deleting unattended (retention) requires proof another copy exists via a hard link
  (`nlink > 1`, e.g. an `*arr`'s pickup directory) before it will act; a human deleting
  `LOCAL_ONLY` junk by hand (Files page) does not need that proof, since removing the one and
  only copy is the point. A dry-run preview endpoint (`POST /api/settings/retention/preview`)
  reports exactly what a real retention pass would delete, using the same guard chain rather
  than a second approximation of it. Also fixes a coupled bug: `REMOVED_LOCAL` items (a local
  copy moved away by a human or an `*arr` importer) were previously excluded from auto-queue
  forever with no way back; they're eligible again now that a deleted-by-this-app item is
  distinguishable from one that merely left (a new `auto_queue_suppressed` reason,
  `'deleted_local'`, migration 008). **"Delete remote" is explicitly out of scope** — the only
  remote deletion in this app remains `move` mode's verification-gated pipeline; a manual
  remote-delete button is a materially larger safety conversation, deliberately deferred, not
  forgotten. No Settings-page UI for the retention toggle yet — same named gap as the settle
  gate above and Settings → Transfer.

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
- **`DESIGN.md` caught up with the code** *(2026-08-12, documentation only)*. The backlog of
  replacement wordings that earlier sessions drafted into `docs/decisions.md` rather than
  editing the doc was applied: three new sections (§2.2 the publish invariant, §3.3 the settle
  gate, §10.4 throughput metrics), a new §3.2 rule 9 on which module wins when two of them
  write `item.state`, and corrections to §3.1, §3.2, §4.6, §4.7, §5, §6, §7.3, §9, §11, §13,
  and §14. No behavior changed; nothing was renumbered.
- **The settle gate now defaults on, gained a wall-clock floor alongside its scan count, a
  self-heal for a stuck item, and a Settings UI** *(2026-08-12)*, three follow-ups to the gate
  added above:
  1. **`SettleSettings.enabled` now defaults `True`.** The third reasoned exception to this
     project's "every new capability ships off" rule, after `move`-mode verification and the
     phase 7 scheduled backup. **Existing installs will see transfers complete up to about a
     minute later than before this upgrade** — deliberate, not a regression: it is the fix for
     a real, confirmed-live directory-corruption bug (a release caught mid-upload can read as
     complete off whichever files happened to arrive first). Switch it back off at
     Settings → Transfer if your seedbox's landing path is atomic end to end (e.g. hardlinked
     torrent pickup) and you'd rather not pay the delay.
  2. **Settling now requires *both* 2 consecutive matching scans *and* at least 60 seconds of
     wall-clock time** since the fingerprint was first observed — a scan count alone is only a
     reliable proxy for "quiet for a while" as long as every queue shares one scan interval,
     which stops being true the moment a per-queue interval lands.
  3. **An item stuck at `REMOTE_ONLY`/`settling` now self-heals.** Previously, if a job
     finished while its item was still unsettled, the item was held back (correctly — see the
     entry above) but then only ever reached `DOWNLOADED` by being re-queued; with auto-queue
     off and nobody clicking Queue again, it could sit there forever with its bytes already
     complete. The next scan that finds the remote genuinely quiet now reaches `DOWNLOADED` on
     its own and triggers post-processing, with no new transfer. This is a second, narrower
     entry point into post-processing (`core/engine.py._persist`, alongside the existing
     job-success trigger in `core/queue.py._reap_one`) — DESIGN.md §6's trigger paragraph is
     now stale and needs a follow-up correction; see `docs/decisions.md`.
  4. **Settings → Transfer gained a "Settle gate" section**: the enable toggle, plus a
     read-only readout of the required scan count and the wall-clock floor, with an
     explanation of what the gate does.

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
- **`REMOVED_LOCAL` items could never be re-queued, even deliberately** *(2026-08-12)*: a local
  copy moved away by a human or an `*arr` importer (the normal end state of a successful
  import) was excluded from auto-queue outright, forever, with no way back short of editing the
  database by hand. Fixed alongside the local-deletion feature above, which is what makes it
  safe: an item this app deleted on purpose is now distinguishable (a new
  `auto_queue_suppressed` reason) from one that merely left, so only the latter becomes eligible
  again. Present since phase 4.
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
- **Scheduled backups failed under an unrelated write** *(found and fixed 2026-08-12)*.
  `create_backup` ran `VACUUM INTO` on the shared application connection, and `VACUUM` cannot
  execute inside a transaction — any other writer with a commit pending at the same moment made
  the backup fail with `cannot VACUUM from within a transaction`. The race dates from phase 7
  but became routine once the metrics sampler began writing a heartbeat every 30 seconds, and
  scheduled backups default on, so an unattended instance was silently failing its nightly
  backup. `create_backup` now takes its `VACUUM INTO` on a dedicated connection that no other
  coroutine's transaction can reach. `:dev` images built before this fix (published from
  `fe80aaf`) still carry the bug.
- **The shared application connection had no `busy_timeout`** *(2026-08-12)*, so any lock
  contention between the engine's scan persist, the transfer queue's ~1 Hz tick, the metrics
  heartbeat, and post-processing failed instantly with `SQLITE_BUSY` instead of waiting.
  `db.py.connect()` now sets 30000ms, matching `core/backup.py`'s dedicated `VACUUM INTO`
  connection.
- **Files' Expand all / Collapse all gave no reason when disabled for having no directories**
  *(2026-08-12)* — the `title` only ever explained the filter-active case, so a queue with a
  flat tree rendered both buttons greyed out with no explanation and read as broken.
- **"Rescan now" reported completion it knew nothing about** *(2026-08-12)*: `POST
  /api/files/rescan` only wakes the engine and returns immediately, so the button faked
  completion with a bare 1-second timer regardless of how long the scan actually took, and
  stayed "Rescanning…" for exactly that second even when the scan failed outright. The engine
  now publishes a `scan_complete` WebSocket message at the end of every scan pass — success or
  failure — and the button clears on the first one after its own request. The Files page also
  now shows each queue's last-scanned time relative to now (absolute on hover), driven by the
  same message, with a partial-scan warning folded into the same readout instead of only a log
  line.
- **An item with nothing to extract was stamped `EXTRACTED`, with a real `extracted_at`**
  *(2026-08-12)*: `extract_item` returned a bare `ok=True` for "no archives found", and
  post-processing treated any `ok=True` as a genuine success — so a plain, non-archive download
  on an auto-extract queue got a false extraction record. `ExtractResult` now carries the same
  three-outcome shape as `core/verify.py`'s `VerifyResult` (`EXTRACTED` / `EXTRACT_FAILED` /
  `SKIPPED`); the pipeline checks `find_archives` itself before ever transitioning an item to
  `EXTRACTING`, so a non-archive item's state (including a real `VERIFIED` from earlier the same
  pass) is left untouched rather than overwritten or reverted to `DOWNLOADED`.
- **Extraction had no completeness precondition of its own** *(2026-08-12, found against a real
  production failure — root cause of the specific file involved not confirmed, but the gating
  gap is real regardless)*: a `copy`-mode queue with verification off — the default — gated
  extraction on nothing but a size rollup computed at the last scan, so a truncated or
  short-by-one-volume rar set reached 7zz and failed with the opaque "Cannot open the file as
  archive" instead of a diagnosis. `core/extract.py.check_extract_preconditions` now rejects a
  zero-length head and an incomplete multi-volume rar set (both old-style `.r00`/`.r01`/... and
  new-style `.partNN.rar`, detecting gaps in the sequence, not just "some volumes exist") before
  any archive is handed to 7zz, with a named reason ("volume 3 of 4 missing") — and, since
  nothing was actually attempted, without creating a `_UNPACK_`/`_FAILED_` staging directory for
  the failed attempt.
- **`_FAILED_` extraction-evidence directories accumulated on disk forever, invisibly** — kept
  correctly as diagnostic evidence on a real extraction failure, but nothing ever removed them,
  and `core/local_scan.py` already filtered the prefix out of every scan, so they consumed disk
  with no UI trace at all *(2026-08-12)*. `core/extract.py.sweep_failed_dirs` can now bound their
  lifetime (default 14 days), gated by a new Settings → Post-processing toggle that defaults
  **off** — a new capability, and deletion isn't where this project makes an exception to that
  rule — with a re-verified containment check and an `event` row for every removal.
- **Files inside a mirroring directory sat visibly frozen, then flipped a whole batch to
  `DOWNLOADED` at once** *(2026-08-12)*: `_sample_and_publish_progress` samples one entry per
  running *job*, and a `mirror` job is one job for the whole release, so every child `.rar` only
  got a fresh `local_size`/`state` from the next full engine scan (`scan_interval_s`, default
  30s) — and `xfer:use-temp-file` meant even that scan saw files *appear* in clumps, since a
  child doesn't exist under its final name until it's done. `core/progress.py`'s per-tick
  subtree walk already computed every child's size and discarded it; it now surfaces that
  breakdown, and `core/queue.py` diffs it against the previous tick and publishes only the
  children that changed (throttled to every 3rd tick, capped, with a logged truncation) using
  the same `local >= remote_size -> DOWNLOADED : PARTIAL` rule `core/reconcile.py` uses for a
  leaf file. In the same pass, the parent item's WS row stopped hardcoding `"state":
  "DOWNLOADING"` and is now read back from `item` like everything else `core/itemview.py`
  projects.
- **The hash-on-disk verification fallback could bless a truncated file** *(2026-08-12)*: with
  no `.sfv`/`.md5` sidecar and the fallback enabled, `verify_item` proved a file was
  *readable* end to end, which a short/truncated file passes just as cleanly as a complete
  one — and `VERIFIED` is the sole gate on a `move`-mode queue's irreversible remote delete.
  The fallback now also compares total bytes read against the item's known remote size and
  returns `CORRUPT` on a mismatch.
- **Rar extraction has never worked** *(found against a real production failure, 2026-08-12)*.
  `core/extract.py` routed `.rar` through `7zz`, and Alpine's `7zip` package has never shipped
  a RAR codec at all — `7zz i` inside the built image lists no `Rar`/`Rar5` handler, distros
  strip it because 7-Zip's RAR decoder derives from unRAR source, whose licence they won't ship
  in `main`. Every `.rar` extraction attempt failed with the opaque "Cannot open the file as
  archive," present since phase 5 and undetected through nine phases of green CI because no test
  ever built a real rar — every fixture was fake bytes exercising naming logic only. `unrar`,
  built from RARLAB source in a new Docker builder stage (statically linked against
  libstdc++/libgcc so the runtime and dev images need nothing but musl libc), now handles rar
  and rar5; `7zz` keeps zip/7z/tar/gz/bz2/xz, which it genuinely does support. See `NOTICE` and
  `docs/decisions.md` for the licence position (UnRAR's own licence permits redistributing the
  binary; it forbids only using its source to build a RAR-compatible compressor, which this
  project never needed).

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
