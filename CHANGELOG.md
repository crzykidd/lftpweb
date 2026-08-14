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

- **A read-only "What lftpweb already sets" readout on Settings → Transfer** *(2026-08-14)*,
  collapsed by default directly above the **Extra lftp settings** box — so far this has been a
  free-text field with no indication of what lftpweb already writes into every job's rc file,
  leaving a user typing into it unable to tell whether they're adding a setting, duplicating
  one, or fighting one. Shows, per job kind (mirror/pget), the transfer command's real argv
  (`pget -c -n N`, `mirror -c --parallel=N --use-pget-n=N`) and every `set` line lftpweb writes,
  each with a short *why* — all generated from `core/lftp.py.effective_tuning_settings` /
  `build_transfer_command` (the same functions that build a real job's rc and argv), never
  hand-typed, so this can't drift the way the Dockerfile's old rar-support claim and the
  Settings page's old `7zz` claim both did. **Credential-free by construction**: the two
  credential-bearing rc lines (`sftp:connect-program`, `open -u ...`) are built in a separate
  code path this feature's endpoint never touches, not filtered out of rendered text — proven
  absent by a byte-search test (`tests/test_effective_lftp_settings.py`), not assumed. **Flags a
  collision** when a line in the Extra lftp settings box names a key lftpweb already sets, and
  says the user's line wins — verified against a real lftp binary first
  (`tests/test_lftp_settings_accepted.py`'s new
  `test_extra_lftp_settings_override_a_colliding_lftpweb_default`: lftp's own `set` command is
  last-write-wins within one sourced script, and the box's contents are always appended after
  every built-in line) rather than assumed. Not yet click-tested — no browser exists in the
  environment this was built in, so density and placement on an already-dense tab need a human
  look.
- **A "How it works" page** *(2026-08-14)*, in the app under **Docs → How it works** and in the
  repo as [`docs/how-it-works.md`](docs/how-it-works.md) — the same single source, rendered both
  places. Two minutes on the one decision the rest of the design follows from (lftp is a transfer
  engine, not a status API), how an item actually gets queued, where progress comes from, and why
  polling `jobs -v` was rejected. `README.md` gained a short section summarising it and linking
  on, rather than repeating it.
- **A demo-tree generator for screenshots and manual UI work** *(2026-08-14)*,
  `docker/test-seedbox/make_demo_tree.py` — writes obviously-fake, generically-named releases
  into the dev seedbox's hand-testing dropbox covering the four shapes worth photographing: a
  loose file, a single-file directory, a real multi-volume rar set, and a multi-file pack.
- **A removal-grace countdown on the Files page** *(2026-08-14)* — a previously-complete item
  (`DOWNLOADED` or a post-processing outcome) whose local copy just vanished used to keep
  showing its last-known-good state, unchanged, for the whole ~10-minute §7.3 grace period
  before landing on `REMOVED_LOCAL`, with nothing indicating a decision was pending; a row like
  that read as broken rather than as §3.2 rule 3 working correctly. The state chip now
  substitutes a synthetic `Missing · 1m` reading (capping to a bare `Missing`, never a stuck
  `0s` or a negative number, whenever the exact remaining time can't be trusted — including
  while the mount gate has the grace clock deliberately frozen, DESIGN.md §7.3), the same
  substitution shape the settle gate's own `SETTLING` chip already established
  (`components/StateChip.tsx`, `FileTree.tsx`). The item drawer gets the full sentence plus the
  absolute time the local copy was first noticed missing. **The lifecycle icons are
  unchanged** — the Local icon going dim while Verified/Extracted stay green is correct, not
  the bug this closes (`core/itemview.py`'s presence-vs-milestone split). A new `GET
  /api/settings/removal-grace` endpoint exposes `core/mount_sentinel.py.DEFAULT_GRACE_S`
  read-only, the same "real constant, not a hand-copied number" pattern
  `SettleSettingsOut.required_scans`/`min_age_s` already uses. Not click-tested — no browser in
  this environment; see docs/decisions.md for what a human should confirm.

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
  opt-in whole-file-read fallback), extract (zip / 7z / tar / gz / bz2 / xz and rar / rar5,
  including multi-part sets and compound tar — see the rar entry under **Fixed** for which
  binary handles what, and why that took nine phases to get right), and relocate a finished
  item to its final destination. `move` mode deletes the
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
- **The settle gate** *(2026-08-12, defaults **on**)*: a top-level item (a release directory
  or a loose top-level file) is now fingerprinted every scan
  (`file_count, total_bytes, max_mtime` over its whole remote subtree), and must hold that
  fingerprint across **2 consecutive scans *and* at least 60 seconds of wall-clock time**
  before auto-queue will pick it up or before it's allowed to reach `DOWNLOADED` and trigger
  post-processing. Both conditions are load-bearing: a scan count alone is only a proxy for
  "quiet for a while" as long as every queue shares one scan interval, which stopped being true
  the moment the per-queue interval below landed. Fixes a real gap: a release still being
  uploaded, caught mid-upload, can look byte-complete for the files that *have* fully arrived
  while more are still coming — a growing single file self-heals (re-queued, resumes) but a
  growing *directory* previously did not, and could be moved/extracted/deleted-from-remote with
  files still missing. A manual Queue click still overrides the *queueing* half (explicit user
  action beats a heuristic); the *completion* half — never publishing `DOWNLOADED` for an
  unsettled item — always applies regardless, so the worst case is a wasted partial transfer
  that resumes, never a bad import or a bad delete. An item held after its own job already
  succeeded self-heals: the next scan that finds the remote genuinely quiet reaches
  `DOWNLOADED` and triggers post-processing on its own, with no new transfer and without
  needing auto-queue or another click. Held items surface as `REMOTE_ONLY` with a new
  `substate: "settling"` — originally a 6px dot next to the state chip, effectively invisible
  in practice; replaced *(2026-08-13)* with a readable countdown ("Waiting for changes — 1 of
  2 scans, 35s of 60s") on the Status chip itself, and the R lifecycle icon reads amber for
  the same duration (never L — the local side is legitimately empty during the wait, so amber
  there would imply activity that isn't happening). **On by default, which costs up
  to about a minute per transfer**, including on an atomic hardlink-pickup path where it buys
  nothing — the third reasoned exception to this project's "every new capability ships off"
  rule, made because it is the fix for a confirmed-live directory-corruption bug rather than a
  latency preference. Switch it off at Settings → Transfer (or `PUT /api/settings/settle`) if
  your seedbox's landing path is atomic end to end; that section also shows the required scan
  count and the wall-clock floor, both read-only, since they are constants rather than
  tunables.
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
  than a second approximation of it. **Deleting a directory marks its whole subtree** — the
  target and every descendant `item` row in the same queue, in the same transaction as the
  files' removal — rather than only the row that was clicked, so deleted files don't keep
  reading `DOWNLOADED` and then drift through the ten-minute absence grace period that exists
  for *unexplained* absence, not for a deletion this codebase performed and has a record of.
  **Each row's resulting state is chosen per row** from whether a remote copy actually survives:
  `REMOVED_LOCAL` when one does, `REMOVED_BOTH` only when both copies are genuinely gone. Every
  row is suppressed from auto-queue individually either way — suppression, not the state name,
  is what stops the re-fetch, so a deleted item can still be queued again manually and
  downloads normally. Also adds a new `auto_queue_suppressed` reason,
  `'deleted_local'` (migration 008), that distinguishes an item lftpweb deleted on purpose from
  one that merely left (moved out by an `*arr` importer, a human, or a script) — the mechanism
  the re-download setting below needs to stay safe. The Files-page delete confirmation also
  says, factually, what happens next: an item with a remote copy stays there untouched and is
  never re-fetched by lftpweb; a `LOCAL_ONLY` item with no remote copy is gone entirely.
  **"Delete remote" is explicitly out of scope** — the only remote deletion in this app remains
  `move` mode's verification-gated pipeline; a manual remote-delete button is a materially
  larger safety conversation, deliberately deferred, not forgotten. No Settings-page UI for the
  retention toggle yet — the manual Files-page delete has its UI, but turning the scheduled
  sweep on still needs `PUT /api/settings/retention`. The last remaining instance of this
  project's "backend first, settings screen catches up" gap; the settle gate's and Settings →
  Transfer's both closed since.
- **A setting to re-download items removed outside lftpweb** *(2026-08-12)*. There are two ways
  an item's local copy can go away: lftpweb deleted it itself (never re-queued, no matter what —
  see above), or something outside lftpweb removed it (an `*arr` importer picking up a finished
  release, a human, a script). `AutoQueueSettings.re_download_externally_removed`
  (`GET`/`PUT /api/settings/autoqueue`, Settings → Queues), **default off**, governs only the
  second case: on, a `REMOVED_LOCAL` item whose pattern still matches is eligible for auto-queue
  again; off (the default), it stays excluded, exactly as before this session. Off is the
  *correct* default, not merely the cautious one — on a `copy`-mode queue (remote copy never
  touched) with auto-queue on, an importer moving a release out would otherwise be re-fetched on
  the very next scan, re-imported, and repeat forever. It is meant to matter only for
  `copy`-mode queues — `move` deletes the remote copy on verified completion, so there is
  nothing left to re-fetch. In practice, turning it on affects `move` queues too, and not
  usefully: a completed `move` item currently lands on `REMOVED_LOCAL` rather than the
  `REMOVED_BOTH` the design describes, so it becomes eligible and produces a job that fails
  against a remote that is already gone. Leaving the setting off (the default) avoids this
  entirely; see `README.md`'s "Known gaps" and `DESIGN.md` §3.2 rule 3.
- **The scan interval is now per-queue, not one global 30s for every queue** *(2026-08-12)*.
  `scan_interval_s` (`path_queue`, migration 009; `GET`/`PUT /api/settings/queues`, Settings →
  Queues) offers 10s / 30s / 60s / **None** — **default unset** (every existing queue keeps
  using the site-wide `LFTPWEB_SCAN_INTERVAL_S` default, currently 30s, exactly as before this
  release). *None* means on-demand only: the queue is never scanned on a timer, only via
  "Rescan now" or a settings change that forces a rescan — the UI says so next to the option,
  because auto-queue only runs at the end of a scan pass, so a "none" queue with auto-queue on
  will not pick up new remote items until something forces one. 10s carries a warning in the UI
  (a scan is an SSH round trip running `find` over the entire remote tree — real, continuous
  load on a shared seedbox). The engine loop now wakes at the earliest next-due time across all
  queues and scans only the ones actually due, not every queue on the fastest one's cadence; an
  overrunning scan (a real risk at 10s against a slow seedbox) reschedules its own queue from
  its own completion time and can never stack a second, concurrent scan of itself — the loop
  remains one serial task, which is what actually guarantees this. The settle gate's wall-clock
  floor (`SETTLE_MIN_AGE_S`, shipped the same day specifically anticipating this feature) is
  unaffected: a fast per-queue interval cannot shrink the ~60s settle window below what a 30s
  queue already gets.
- **An option to delete a release's archive volumes once they've extracted successfully**
  *(2026-08-13)*. `PostprocessSettings.delete_archives_after_extract`
  (`GET`/`PUT /api/settings/postprocess`, Settings → Post-processing → Extract), **default
  off**, non-negotiably, like every other capability in this project that deletes something.
  On, once an item's archives (every volume of a multi-part `.rar` set — the `.r00`/`.r01`/...
  or `.partNN.rar` continuation volumes too, not just the head `find_archives` returns — or a
  single-file `.zip`/`.7z`/`.tar`/etc.) have extracted in full, they are removed from disk;
  `.nfo`/`.sfv`/`.md5` sidecars, samples, and subtitles are never touched, and nothing is
  removed on `EXTRACT_FAILED` or a precondition failure. Only ever acts on a directory item —
  a loose top-level archive file is left alone, since removing its own single file would be
  removing the whole item, `core/local_delete.py.delete_local`'s job, not this one's.
  **The naive version of this feature is an infinite re-download loop**: deleting the archives
  drops local bytes below remote, which reads `PARTIAL` on the next scan and would outrank the
  `EXTRACTED` outcome, so the item is re-fetched, re-extracted and re-deleted every scan
  interval, forever. Avoided by reusing the exact mechanism `file_exclude`
  patterns already use for the identical problem — a new `deleted_archive` table (migration
  010) records every file this codebase removed after extraction, and the reconciler
  (`core/engine.py.build_scan_counts_predicate`) folds it into the same completeness seam
  `core/patterns.py.build_counts_predicate` already feeds, so a deleted archive reads
  `EXCLUDED` — a real state, not an absence — exactly like a pattern-excluded file, rather
  than a second completeness rule. Composes with `move` mode (cleanup runs regardless of the
  remote copy already being gone by the time extraction runs — see `docs/decisions.md` for why
  that's the right call, not a gap) and with the relocate step (`_do_move` always runs after
  cleanup, per the pipeline's fixed step order, so there is nothing to reconcile between the
  two). No Settings-page UI gap this time — the toggle lives in Settings → Post-processing
  alongside the other extraction options.
- **The Files tree now shows an item's whole lifecycle, not just its current state word**
  *(2026-08-13)*. Four small colour-coded icons per row — **R**emote / **L**ocal /
  **V**erified / **E**xtracted — read from `core/itemview.py`'s new `facets` projection
  (`GET /api/files`, `queue_delta`, `item_delta`, and connect-time `snapshot()` all agree,
  since it's the one shared code path). R/L are *presence* facets and may legitimately go
  dark (a `move`-mode item's remote copy going dark once deleted on purpose is the success
  path, rendered **dim, never red**); V/E are *milestones*, read from `verified_at`/
  `extracted_at` rather than `state`, so they stay lit even after a later rescan moves
  `state` on. Makes visible, for the first time, the exact case a `DOWNLOADED` row can
  claim bytes that are not on disk (an `*arr` import mid-§7.3-grace-period) — a dark **L**
  distinguishes it from a directory whose children were all `EXCLUDED` by design, which
  reads complete/green despite also having zero local bytes. `item.state` itself, its
  transitions, and the grace period are unchanged — this is a display projection, not a
  state-machine change. Icons are inline SVG copied from Lucide (ISC), not a new npm
  dependency (see `NOTICE`). A **lifecycle facet filter** (has remote copy / has local copy /
  extracted / not extracted / "downloaded but missing locally") surfaces exactly the
  `*arr`-import case (a checkbox literally named **Missing only**, replaced *(2026-08-13)*
  once the user could not tell what it meant — the verdict on it), composing with the
  existing text/state filters through the same mechanism.
- **Inline progress bars on the Files tree's state chip** *(2026-08-13)*, for `PARTIAL`/
  `DOWNLOADING` rows only (including a top-level directory's own rolled-up percentage, so a
  40 GB multi-file release shows real progress, not just "partial"). The fill is the chip's
  own background, growing with a CSS width transition — no per-row timer, no JS animation
  loop. No new backend data: `local_size`/`remote_size` were already in the projection and
  already rolled up for directories.
- **Sortable Files tree** *(2026-08-13)*: name, size, last state change, or percent
  complete, ascending or descending, persisted across reloads. **The column headers
  themselves are the control** — click to sort, click again to reverse, with a caret marking
  the active column and direction — a separate "Sort by" dropdown plus asc/desc button
  shipped first and was replaced the same day once used for real; a header that isn't
  sortable stays a plain label, never looking clickable. Sorting reorders **siblings
  within each parent**, never the flattened list the virtualizer walks, so a sorted tree
  never tears a child away from its actual parent. Composes with the existing text/state
  filters and with collapse state.
- **Files columns are now drag-resizable, and remembered per browser** *(2026-08-13)*. A drag
  handle at the right edge of Size / Status / R L V E / Changed / Actions (Name keeps flexing to
  absorb whatever space the rest don't claim, as it always has) lets each column be resized by
  pointer or touch; a double-click on the handle resets that column to its default, and arrow
  keys (Shift for a bigger step) resize it from the keyboard, since a drag-only affordance isn't
  usable without a pointer. Widths persist in `localStorage`, keyed by column id (an unrecognised
  id is dropped, not misapplied to whatever now occupies that slot) so they survive a reload and
  degrade safely if a future column is added or renamed. **The header row and each data row now
  read one shared column definition** instead of two independently hardcoded, hand-synced sets
  of Tailwind widths (the drift risk this replaces: header and rows could quietly disagree, and
  had no defense if they did). During a drag, the live width is written straight to a CSS custom
  property on the tree's scroll container — a DOM write via a ref, never a React state update —
  so dragging a column costs a reflow, not a re-render of the virtualized list underneath it;
  the one `setState` (and `localStorage` write) happens once, on release. **The settle
  countdown's in-cell text is shorter** as part of the same pass — the full sentence ("Waiting
  for changes — 1 of 2 scans, 35s of 60s") was overflowing its column outright; the chip now
  shows `Waiting 1/2 · 35s` with the complete sentence still available on hover. Not verified
  against a real browser (no UI access in this environment) — the widths, minimums, and drag
  feel are reasoned choices, not observed ones.
- **Expand all / Collapse all now remembers your last choice** *(2026-08-13)*, in
  `localStorage`, surviving a reload. Stored as a default-plus-exceptions preference, not a
  saved set of collapsed paths — a directory that arrives later over the WebSocket inherits
  the current default automatically rather than defaulting to expanded regardless of what
  was last chosen.
- **The Files tree now has an item detail drawer, reachable from every row** *(2026-08-13)*.
  A small, deliberately quieter info icon per row opens a side drawer with both sides' size
  and modified date (a local file short of its remote size is called out explicitly as
  mid-transfer or truncated), the full lifecycle chronology (`first_seen_at` through
  `state_changed_at`, rendered in the order it actually happened, not an unordered field
  dump), and a bounded "recent history" panel (last 10 transfer attempts, last 10 audit
  events — including the delete-audit trail — fetched once when the drawer opens, never per
  row). This is the same drawer the Transfers page has used since phase 3b, generalised
  rather than duplicated: it previously took a job and was unreachable once a transfer aged
  out of that page's list; it now takes a plain item id and path, so Files can open it too.
  New: `local_mtime` (migration 011) — the local-side counterpart to `remote_mtime` that
  never existed before this task, so "modified date" could not be answered for the local
  side at all — and `first_seen_at` (already persisted since phase 2) reaching the wire for
  the first time. `GET /api/history/jobs` gained an `item_id` filter to match
  `GET /api/history/events`'s existing one, so the drawer's history fetch doesn't have to
  pull a whole queue's jobs client-side.
- **Archive cleanup after extraction is now a per-queue toggle too, and every post-processing
  toggle now shows what its site-wide half currently resolves to** *(2026-08-13)*. It shipped
  site-only (the entry above, `delete_archives_after_extract`) and was the one post-processing
  step that didn't follow verify/extract/move's own "toggleable globally *and* per path queue"
  shape — the user noticed after cleanup silently did nothing because the site-wide setting had
  been switched off without them realising. `path_queue.auto_delete_archives` (migration 012,
  **default off**, every existing queue unaffected) is ANDed with the site-wide flag exactly
  like the other three. Settings → Queues now shows, next to every one of the four toggles —
  not only the new one — whether the matching site-wide setting is on or off and therefore
  whether the queue's own toggle is currently doing anything, with a link to Settings →
  Post-processing; a `move`-mode queue's Verify readout says it always runs regardless of
  either toggle, never "system setting: off," since it is the sole gate on the irreversible
  remote delete.
- **The Transfers page now shows a queued job's actual run position** *(2026-08-13)* — the
  capability (`rank DESC, queued_at ASC`, "Move to top", "Start now") already existed and was
  invisible, so a user asking "what is the proper way to see the priority of the download
  queue" had to infer it from row order. Each still-`queued` row now carries a small `#N`
  ordinal (1, 2, 3… in the order jobs will actually run), and a one-line caption states that
  the list order *is* the queue order once there is more than one queued job to order. No new
  endpoint — `GET /api/jobs` already returns jobs pre-sorted; the frontend just counts.
- **The Dashboard page now remembers your last-selected timeframe** *(2026-08-13)*, in
  `localStorage`, per browser — read synchronously on first render so the chart doesn't paint
  the default range and then jump to the saved one.
- **Deleting a file or folder mid-transfer now works, instead of refusing** *(2026-08-13)*. The
  Delete button was already offered on a `DOWNLOADING`/`QUEUED` row, but clicking it just
  bounced off a 409 — the guard that refuses a delete with an active job is correct (deleting a
  directory an lftp process is still writing into races the writer) and is unchanged; what
  changed is that `POST /api/items/{id}/delete` now satisfies it itself first, stopping the
  item's active job through the exact same SIGTERM → grace → SIGKILL path the Stop button
  already uses and confirming the process is actually dead and reaped — not just signalled —
  before deleting. A stop that can't be confirmed within 25s withholds the delete with a 409
  and an audit event, rather than deleting blind; the stop attempt itself keeps running in the
  background rather than being abandoned. The Files-page confirmation dialog now says so
  plainly ("N of M is/are transferring now — deleting will cancel it/them first") as an added
  line alongside — never replacing — the existing remote-copy line, not a second dialog. A
  loose top-level file stopped mid-transfer can exist on disk only as lftp's own `<name>.lftp`
  temp name (§4.4b); the delete now removes that (and its `.lftp-pget-status` sidecar) too, so
  it never leaves the very bytes it was asked to remove sitting there under a different name.
  The resulting row always reads `suppressed_reason = 'deleted_local'`, never the stop path's
  own `user_stopped`, and — like every delete this codebase performs — is never re-queued by
  auto-queue, regardless of the `re_download_externally_removed` setting.
- **The Files-tree row hover now shows size and modified date side by side, remote and local,
  instead of a plain-text tooltip** *(2026-08-13)*. The previous hover was a native `title`
  attribute — one line of text, no columns, no styling. It is now a small portal-rendered card
  anchored to the row's name, positioned in the viewport (flipping above/below and clamped
  horizontally so it can never run off-screen), shown after a brief hover delay or immediately
  on keyboard focus of the row's name, and hidden immediately on any scroll or the instant the
  underlying row scrolls out of the virtualized list. Two columns only when the item exists on
  both sides; a `LOCAL_ONLY`/`REMOTE_ONLY`/deleted row degrades to one labelled column rather
  than showing a permanently empty half. A directory shows no "Modified" row at all —
  `local_mtime`/`remote_mtime` are files-only by existing convention (`de85753`), not something
  to invent for a directory. The card is `pointer-events: none` and never intercepts a click
  meant for the row, a sort header, or a column resize handle. The native `title` is removed
  outright rather than kept alongside the card, so the two can never fire on the same hover. New
  shared helper, `lib/format.ts.bothSidesRows`, now backs both this card and `ItemDrawer.tsx`'s
  own both-sides panel, so the two surfaces can never independently drift on what these numbers
  mean.
- **The settle countdown now says something true while an item is still actively arriving, not
  just once it's holding still** *(2026-08-13)*. User report: copying a large directory onto
  the seedbox, the Status chip's "Waiting N of 2 scans" countdown sat pinned at "1 of 2" for the
  whole copy — every scan found the fingerprint still growing, which reset the counter right
  back to the value that also means "confirmed unchanged once." Fixed with a second, independent
  signal rather than by touching the counter itself: migration 013 adds
  `item_settle.first_observed_at`/`last_changed_at`, and `last_changed_at` now moves to "now" on
  the same scan that resets the counter and holds on every scan that merely confirms it, so the
  two previously-identical cases (a fresh sighting and a just-changed fingerprint, both reading
  "1 of 2") are distinguishable without changing what the counter itself does or how long real
  settlement takes. The Files page reads that split as two different sentences on the same amber
  chip: **"Arriving · 3.4 GB"** (short) / **"Still arriving — 3.4 GB, changed 12s ago — watching
  for 3m"** (on hover) while nothing has been confirmed unchanged yet — the byte count itself
  (`item_settle.total_bytes`, already computed as part of the settle fingerprint) is the progress
  signal while that's true — and the existing **"Waiting 1/2 · 35s"** countdown, completely
  unchanged, from the first confirming scan onward. **The denominator was deliberately not made
  to grow** ("2/3", "3/4"…, the user's own first suggestion) — the requirement genuinely is
  always 2 consecutive unchanged scans, and a climbing denominator would say something false
  about it; `docs/decisions.md` has the full reasoning, including a same-shaped fix at the
  counter itself that was tried, would have silently required 3 observations instead of 2 for
  any item that had ever changed once, was caught by `tests/test_settle_gate_e2e.py`'s real
  fake-seedbox reproductions, and was reverted before shipping. Both new timestamp columns are
  `NULL`, rendered as "unknown" rather than a fabricated time, on any row that predates this
  migration and hasn't changed again since. The three new wire fields
  (`settle_total_bytes`/`settle_first_observed_at`/`settle_last_changed_at`) are gated on
  `substate == "settling"` exactly like the two that already existed — the same WebSocket-delta
  regression `tests/test_ws_deltas.py` already guards against for those two now covers all five.
- **Settings → Connection can now accept a pasted SSH private key** *(2026-08-13)*, alongside
  the existing `key_path` (a file mounted into the container). Until this, key auth meant
  mounting a file yourself, with nothing checking it was parseable or sanely permissioned before
  a transfer failed on it — confusingly, because lftp's `ssh` enforces OpenSSH's strict
  permission rules while the asyncssh scanning path is more lenient, so a bad mount gave working
  scans and failing transfers with nothing pointing at the cause. Migration 014 adds
  `host.ssh_key_enc`, encrypted at rest with the exact same mechanism as `password_enc` — the
  ciphertext round-trips through a config backup the same way a password already does, where a
  file kept outside the database would not (`docs/decisions.md` has the full reasoning). A
  pasted key is validated at save time (must parse as a private key; a passphrase-protected key
  is rejected outright with a clear message, since neither the scanning path nor lftp can supply
  a passphrase non-interactively) and never round-trips back to the browser, mirroring exactly
  how the password field already behaves. It is purely additive: `key_path` keeps working
  unchanged for anyone already mounting a key, and a pasted key wins when both are set — decided
  once, server-side, and surfaced to the UI so it always shows which one is actually in use.
  Materialisation differs by consumer: the asyncssh scanning path decrypts the key straight into
  memory and never writes it to disk at all (confirmed directly against the installed asyncssh
  that `client_keys` accepts parsed key material, not only a path); lftp, which has no way to
  hand `ssh -i` anything but a real file, gets one written **per job** — alongside the existing
  per-job rc file, same `/run` tmpfs, same mode 0600, same unlink-on-exit — rather than a file
  held for the whole process's lifetime, so the plaintext exists on disk only while a transfer is
  actually in flight, and a container restart (which empties `/run`) needs no separate
  re-materialisation step, because every job spawn decrypts fresh from the database row. A key
  that fails to decrypt (a restore onto a fresh install, same as a password) rides the exact same
  `credentials_need_reentry` state a bad password already triggers, holding transfers instead of
  spawning doomed jobs. `logsetup.CredentialRedactor` now also scrubs a private key's multi-line
  PEM block wherever one appears in a log line, not only the single-line `user:pass@` form it
  already handled.
- **"Reset item tracking"** *(2026-08-13)* — a real way to forget a path so it can be reused,
  after the user hit the lack of one three times (a reused directory name, a cross-queue test,
  and clearing History only to find the item still suppressed). Deliberately **not** "Clear
  History" (`48ad72c`, a few pixels away on the History page) — that clears `job`/`event`
  records and never touches `item`; this forgets the `item` row itself, plus its `item_settle`
  and `deleted_archive` bookkeeping, so a suppressed, stopped, or permanently-failed path reads
  as genuinely new on the next scan. Three scopes, one primitive: **selected items** (Files
  page multi-select, the everyday case — a violet "Reset item tracking" bulk button, distinct
  from Delete's red), a **whole queue** (the clean-slate case, requiring a typed queue-name
  confirmation — the most destructive action in the app), and **purge by filename pattern**
  (single-queue only, with a live "what would this match" preview as its own confirmation —
  reuses `core/patterns.py`'s single evaluator, never a second matcher). Every scope states the
  real consequence rather than a generic warning: "12 of these 14 items still exist on the
  seedbox, and auto-queue is on for this queue, so they will start downloading again within
  about 30s" — computed from the queue's actual `sync_mode`/`auto_queue_enabled`/
  `scan_interval_s`, not a hedge. Also states plainly that local files are never touched, and
  that transfer history for reset items is gone too (`job.item_id` cascades on delete) — an
  unavoidable consequence, not a silent one. Refused, not raced, for a busy item (active job,
  in-flight post-processing, or an in-progress delete) — per-target, so one busy item in a
  whole-queue or pattern purge is skipped and reported while the rest still resets; no
  stop-then-act ordering the way Delete uses, since forgetting a path has no urgency Delete's
  bytes-must-go-now case has. New `Engine.forget_rel_paths()` evicts the reset rows from the
  engine's own in-memory model and republishes over the existing `queue_delta` wire shape (no
  new WebSocket message type needed) — without it, a fully-forgotten item with nothing left on
  either side would be a permanent ghost row no future scan would ever revisit.
- **"Reset item tracking" unified into one control** *(2026-08-14)* — the three scopes above
  used to be three near-identical panels (whole-queue and purge-by-pattern in
  `QueueResetControls.tsx`, plus a third panel for selected items that lived entirely inside
  `FileTree.tsx`'s own multi-select toolbar) with different ceremony per scope, which is exactly
  why a live user could not tell them apart. Now one control: a scope selector
  (**All / Pattern / Selected**), a **Cancel that is always present** once the box is open (the
  old panels' dismiss controls both lived inside `preview &&` branches, so a panel opened by
  mistake could not be closed without running a preview first), and the identical
  **choose scope → preview → confirm** flow for every scope. The preview now reports a real
  breakdown ("3 directories and 12 files — 15 items") instead of a bare count, with its own
  explicit zero case rather than the previous "— 0 items" / "None of these 0 items still exist"
  nonsense at an empty match. The whole-queue scope's typed-name confirmation stays for now (the
  server still requires it) but moved to *after* the preview, as one cleanly removable stage —
  see `docs/decisions.md` for why it's considered borrowed time. Selection state moved up to
  `FilesPage.tsx` so `FileTree.tsx`'s own multi-select and the unified control's Selected scope
  read the identical `Set`, rather than each tracking its own copy.
- **A frontend test runner** *(2026-08-13)* — Vitest + happy-dom, `npm test`, wired into CI's
  "Frontend lint + typecheck" job. Until now the backend had 887 tests and the frontend had
  none; unit coverage now pins `lib/format.ts`, `lib/storage.ts`, and `lib/resetWarning.ts` in
  full, plus `components/FileTree.tsx`'s tree-sorting (the sibling-preserving invariant
  asserted on tree structure, not just flat order), the default-plus-exceptions collapse
  preference (including that a newly-arrived directory inherits the current default), the facet
  filter, and column-width clamping. Deliberately unit-only — no component is actually rendered
  (`README.md`'s "Known gaps" still names that). See `docs/decisions.md` for the stack choice
  and its trade-offs.
- **A Docs section in the app** *(2026-08-13)* — `Docs` in the left nav, with **Quick start** and
  **Concepts** tabs. Until now nothing served the person whose instance is *running*: `README.md`
  targets someone who hasn't deployed, `DESIGN.md` targets someone changing the code, and neither
  answers "why is nothing downloading." Quick start walks the real first-run sequence in order —
  deploy and what each volume is actually for (including that `/downloads` is where downloads
  land and `/staging` is only where a finished item is relocated *to*, which has been written
  backwards before), connect, create a queue, first scan, queue something by hand, then the
  optional layers — with every step a live link to the settings page it describes, which is the
  one thing a README structurally cannot do. Concepts covers only what demonstrably confused real
  users during the 2026-08-12/13 live-testing rounds: the settle gate and how to read
  `Arriving · 3.4 GB` versus `Waiting 1/2 · 35s`; auto-queue suppression, its four reasons, and
  why **Re-Download** appears instead of Queue; a blast-radius table for **Dismiss** vs **Clear
  history** vs **Reset item tracking** (three similarly-named actions that respectively tidy a
  list, delete records, and forget a path — only the last changes future behaviour); the
  lifecycle icons and the presence-versus-milestone distinction that makes a completed `move`
  item's dim remote icon read as success rather than failure; `copy` vs `move` including move's
  forced verification; and inherit-vs-override on the four post-processing toggles. Originally
  written as React components with no markdown-renderer dependency; **the prose moved to
  `docs/*.md` on 2026-08-14** (below) — see that entry for where it lives now. Every claim on
  both pages was verified against the code before being written rather than recalled; where
  something could not be confirmed it was left out.
- **`FieldHelp` — per-field help popups on the settings pages** *(2026-08-13)*. A small info-icon
  button beside a field label that reveals a short explanation of what that field actually does.
  **Not a third popup mechanism**: it reuses the Files-row hover card's portal-and-placement
  machinery (`f4a4205`) through a newly shared `lib/popoverPosition.ts`, which both now call, so
  the two can't drift apart on flip/clamp edge behaviour. Click or tap toggles it and Enter/Space
  opens it from the keyboard — a hover-only affordance is unusable on a phone — with hover layered
  on for mouse users only, Escape/outside-click/scroll to dismiss, and `aria-describedby` wiring
  so the text is announced. Demonstrated on three fields here (**Sync mode**, **Patterns-only**,
  and **Known-hosts policy**); a companion task applies it across the rest of the settings
  surface.
- **`FieldHelp` applied across the rest of Settings** *(2026-08-14)* — the companion task above
  promised. Added to the fields where a wrong answer costs data or silently does nothing
  (retention/failed-directory cleanup being API-only, said so rather than pretending there's a
  UI), where a number's real effect isn't obvious (**Max concurrent jobs** is main-lane only —
  the fast lane has its own independent budget and consumes none of these slots, so the two add
  together for the real ceiling, and **Start now** bypasses the cap entirely), and around
  **Extra lftp settings** (a rejected line can fail silently or with a misleading downstream
  error — `net:reconnect-interval-base` refusing `5s` is the concrete story). Also fixed a wrong
  label found in the process: **Extract archives** claimed `7zz` handles rar/rar5; it never has
  (Alpine's `7zz` ships with no RAR codec) — the image's separately-built `unrar` does, and the
  label/help now says so in both Settings → Post-processing and Settings → Queues. Found, but
  deliberately *not* fixed as part of this sweep: **Settings → Transfer's "Retry backoff base"
  field is inert** — the real retry delay is computed from a hardcoded constant, never from this
  saved value; its `FieldHelp` says so plainly rather than describing behaviour that isn't real
  (`docs/decisions.md`).
- **Adaptive scan cadence: a queue refreshes every ~5 seconds while something is actually
  happening in it** *(2026-08-14)*. Previously every queue polled at one fixed interval
  (default 30s, overridable per queue) regardless of activity, so the Files page could lag
  reality by most of that interval while a transfer was running, an item was settling, or
  post-processing was working. Now, while a queue has a running job, an item mid
  download/verify/extract, an item held at the settle gate ("arriving"), or post-processing in
  flight, an additional local-only pass runs every `min(configured interval, 5s)` between full
  scans — filesystem only, no SSH round trip, reconciled against the remote tree from the
  queue's last full scan. **The remote side keeps its own configured cadence unchanged** — this
  restores, rather than invents, the two-cadence shape `DESIGN.md` §5 originally specified
  before phase 2 collapsed it into one interval (`docs/decisions.md`). A queue configured with
  no timer (on-demand only) or already faster than 5s is unaffected. See `docs/decisions.md`
  for the settle-gate interaction this required getting right: a local-only pass never advances
  or resets `item_settle`, but still enforces whatever verdict the gate last recorded, so an
  item the real gate hasn't cleared cannot be released early just because local bytes caught up
  to a stale cached remote total.
- **"Folder prefix during transfer", on by default** *(2026-08-14)*. A directory item now
  download into a hidden-by-convention folder (`.downloading-<name>` by default, configurable
  site-wide and per-queue, both nullable-for-inherit) and is renamed to its real name only once
  the transfer is fully complete — a `mirror` job renames each file to its final name as that
  file finishes, so an importer polling the download directory could previously see (and act on)
  a partial multi-file release. Live incident this fixes: Sonarr imported the episodes that had
  finished, then its own post-import cleanup deleted the release folder while lftp was still
  writing the last two, and lftp died mid-rename for both. Directory items only — a single-file
  download is already complete the instant it's renamed off its own in-flight name, so there is
  no partial window to protect against. The rename happens once the transfer's own filesystem
  completeness check passes (DESIGN.md §4.3's exit-zero-is-not-completion fix, *2026-08-14*
  below) **and** post-processing (verify, then extract) has finished with nothing flagging the
  release bad — see the same-day reversal entry below for why this moved later than originally
  shipped. A stale prefix (the setting changed, or turned off, mid-transfer or while an
  item sits `STOPPED`) is handled: a resume always reuses whatever prefix is already recorded on
  the item rather than recomputing from current settings, and a scan keeps filtering whatever
  prefix is physically in use, not merely today's configured one. See `docs/decisions.md` for
  the full design, including why this reverses part of phase 5's `staging_path` reasoning on new
  evidence.
- **The Docs section's prose moved to Markdown** *(2026-08-14)* — `docs/quick-start.md` and
  `docs/concepts.md` are now the only copy of the Quick start/Concepts text; the app reads those
  same two files (`?raw` import) instead of carrying a parallel copy as hand-written JSX, and
  they're readable straight from the repo without deploying anything (indexed in
  `docs/README.md`, linked from `README.md`). Reverses part of the 2026-08-13 Docs section's own
  "no markdown-renderer dependency" choice: `react-markdown` + `remark-gfm` are now runtime
  dependencies, justified in `docs/decisions.md` against the rejected alternative of a
  hand-rolled parser. Content was re-verified against the code while migrating, not just
  copy-pasted: Quick start gained a step-6 bullet for "Folder prefix during transfer" and a
  paragraph on the ~5-second active-queue local-only scan pass, both new since the prose was
  first written; everything else carried over unchanged, already current.
- **The Files tree's Speed column now shows a live rate on each file inside a mirroring
  directory, not just the directory's own row** *(2026-08-14)*. `f728373`'s Speed column only
  ever lit up a `mirror` job's top-level row — its children, the individual files actually being
  transferred, showed nothing, because the byte delta already being diffed every throttled tick
  was computed and then discarded, never divided by an elapsed time. A new `child_progress`
  WebSocket message (item-keyed, EMA-smoothed the same way the job-level rate already is) closes
  that gap; a child's own live rate is gated on **freshness of the sample**, not `state`, since
  an actively-transferring child sits at `PARTIAL` under `core/reconcile.py`'s leaf rule and
  never reaches `DOWNLOADING`. A row's Speed cell prefers its own job-level rate and only falls
  back to the child-level one when the former has nothing to show, so the two granularities are
  never displayed or summed as peers — `mirror_parallel_transfer_count` files in flight sum to
  roughly the parent's own rate, the same bytes counted at two granularities, not extra
  throughput. See `docs/decisions.md` for the gating options considered.
- **The Files tree's Speed cell now shows an ETA alongside the rate, "34 MB/s · 3m", on both the
  top-level row and each file inside a mirroring directory** *(2026-08-14)*. The top-level ETA
  needed no new backend work — `core/progress.py.JobProgress.eta_s` was already computed and
  already on the wire (`progress` message), just not displayed on the Files page; this only
  threads it through a new `etaByItemId` map, the same shape `speedByItemId` already established.
  A **child** file's ETA has no server-computed counterpart (`_publish_child_progress` only ever
  emits a rate), so it's derived client-side: `remote_size - local_size`, divided by that child's
  own freshness-gated smoothed rate from the per-file-speed task above. Shows nothing rather than
  a wrong number in every degenerate case — unknown `remote_size`, a zero or stale rate, or
  remaining bytes at or below zero (already done) — and is deliberately uncapped on the high end
  rather than showing a fabricated "> 1h" ceiling. Appended into the existing Speed cell rather
  than a new column or a hover-only reading; the column still sorts by rate alone. See
  `docs/decisions.md` for the layout options weighed and why appending won.

### Changed

- **"Folder prefix during transfer" defaults ON** *(2026-08-14)* — the fourth deliberate
  exception to this project's "every new capability ships off" rule, after `move`-mode forced
  verification, the phase 7 scheduled backup, and the settle gate. It shipped off the same
  morning it was built and was flipped the same day by the same reasoning that flipped the settle
  gate: this is the fix for a **reproduced** defect, not a preference, and an existing install
  silently keeps running with that defect live unless the fix defaults on. **An existing install
  will notice**: a directory item now downloads into `<local_path>/.downloading-<name>/` and is
  renamed onto its real name only once complete, so anything watching that directory sees a
  release appear all at once instead of file by file. A transfer already in flight when you
  upgrade is unaffected — the prefix is resolved at spawn and recorded per item, so an
  in-progress job keeps whatever it started with. Single-file downloads are unaffected either
  way. Turn it off at Settings → Transfer, or per queue at Settings → Queues, if nothing watches
  your download directory.
- **"Folder prefix during transfer"'s rename moved to the *end* of post-processing, not the
  start** *(2026-08-14, same day)* — reversing that morning's own "rename before verify/
  extract/move" decision on new evidence: measured on the live instance, a 1.7 GB item takes
  7.7s to verify (the hash-on-disk fallback reads every byte), so a 21 GB release was
  previously visible under its real, unprefixed name for roughly a minute and a half while
  still unverified. If verify then returned `CORRUPT`, an importer watching the directory had
  that whole window to grab it — the exact scenario this feature exists to prevent. The rename
  is now the pipeline's own last step in `core/postprocess.py`, run only once nothing along the
  way (verify, extract) has flagged the release bad; a `CORRUPT` or `EXTRACT_FAILED` item is
  never renamed at all and stays hidden under its prefixed name until a retry succeeds. A queue
  with a staging move configured skips a separate rename entirely — the move's own destination
  is already built from the item's real name, so relocating the still-prefixed source straight
  there does both jobs in one operation. Two related defects, found auditing every place that
  builds a path from `local_path + rel_path` during the now-longer prefixed window and fixed in
  the same pass: `core/local_delete.py.delete_extracted_archives` was recording a deleted
  archive's path relative to the *physical* (possibly still-prefixed) root instead of the
  item's *logical* one, which would have silently broken the archive-cleanup completeness
  accounting the first time cleanup ran on a still-prefixed item; and a scan landing mid-verify/
  extract could flicker a mirrored release's child files between `PARTIAL`/`REMOTE_ONLY` for
  the same reason "folder prefix during transfer" already had to fix that flicker for the
  download window itself. See `docs/decisions.md` for the full reasoning, including why this
  doesn't reopen phase 5's original "the reconciler must never compare against a different
  root" worry.
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
- **`DESIGN.md` caught up with the code** *(2026-08-12 and 2026-08-13, documentation only)*.
  Earlier sessions drafted replacement wordings into `docs/decisions.md` rather than editing the
  design doc; the whole backlog has now been applied. The first pass added three sections (§2.2
  the publish invariant, §3.3 the settle gate, §10.4 throughput metrics) and a new §3.2 rule 9
  on which module wins when two of them write `item.state`. The second added the `LOCAL_ONLY`
  half of that rule, §7.3's "a path can leave both trees at once", §6's archive-cleanup
  paragraph, and §9.2's Files-row revamp, and corrected three long-standing untruths: §9 never
  used TanStack Query, §12's module list stopped at phase 4, and §3.2 rule 3 claimed a
  `move`-mode item reaches `REMOVED_BOTH` when the code writes `REMOVED_LOCAL` (now documented
  as the known gap it is, rather than silently fixed in the doc). No behavior changed; nothing
  was renumbered, and every `§N.M` citation in the repo still resolves.
- **Post-processing now has two entry points, not one** *(2026-08-12)*. It still fires when a
  transfer job this app spawned exits successfully; it *also* fires when the settle gate
  releases its own hold on an item, so an item whose job finished while the item was still
  unsettled no longer needs auto-queue or a manual click to ever get verified and extracted.
  Deliberately not a general scan-driven trigger — a file that appears under a queue's
  `local_path` some other way, with no gate hold behind it, still triggers nothing (see
  `README.md`'s "Known gaps").
- **A queue's four post-processing toggles now inherit the site-wide default instead of being
  ANDed with it** *(2026-08-13)*. `auto_verify`/`auto_extract`/`auto_move`/`auto_delete_archives`
  were `NOT NULL DEFAULT 0`, so a queue's own checkbox could only ever narrow the site-wide flag
  toward "off" — turning a queue's toggle on while the matching Settings → Post-processing flag
  was off did nothing, silently. Migration 015 makes all four columns nullable: `NULL` means
  "inherit," and only an explicit per-queue override diverges from the site-wide value, in
  either direction. Settings → Queues shows each toggle as locked to the resolved site-wide
  value until "Override for this queue" is clicked; the old "System setting: off — this toggle
  has no effect" readout is gone, since it described the AND this removes. The migration does
  **not** preserve any queue's pre-upgrade *effective* value — every existing queue's four
  toggles are simply set to inherit — since nothing has shipped yet and there is exactly one
  install to consider (see `docs/decisions.md`).
- **A failed or stopped job on the Transfers page can now be dismissed instead of only
  retried** *(2026-08-13)*. User report: they deleted files on the seedbox mid-transfer, the
  job failed `REMOTE_GONE`, and Retry was the only button — exactly the wrong action once the
  remote files are genuinely gone. Migration 016 adds `job.dismissed_at`; `list_jobs()` (the
  Transfers-page row set) excludes a terminal job once it's set, while `GET /api/history/jobs`
  keeps showing it, with the timestamp, since dismissal never touches the row itself — deleting
  it would have erased the record of what happened, the opposite of what History exists for.
  Deliberately does **not** touch the item's own state or `auto_queue_suppressed`/
  `suppressed_reason` — a `REMOTE_GONE` item's permanent-error suppression is correct and must
  survive a dismiss untouched; the "actually, try again" path is still Retry, which already
  clears suppression on its own. A `queued`/`running` job can't be dismissed (409, not a
  silent no-op). **Clear all failed** dismisses every currently-failed row in one action,
  reporting partial failure honestly the same way the Files page's bulk actions do.
- **The History page can now be cleared — one row, everything matching the current filter, or
  everything** *(2026-08-13)*. User request, modelled on SABnzbd: a seedbox user doesn't
  necessarily want a database that keeps two years of every transfer they've ever run. Clearing
  is the different, irreversible sibling of the Dismiss action above — Dismiss only hides a row
  from Transfers and leaves History alone; Clear deletes the `job`/`event` row from History
  outright, always behind a confirmation that says how many records will go. **No category is
  protected** — the delete-audit events (`remote_delete`/`remote_delete_withheld`/
  `local_delete`/`archive_cleanup`) clear the same as anything else; this was discussed and the
  "protect the audit trail" alternative was deliberately rejected (see `docs/decisions.md`).
  Bulk clears run as one server-side `DELETE ... WHERE`, built from the exact same filter the
  matching `GET /api/history/jobs`/`GET /api/history/events` already accepts (queue, state,
  error class, kind, level, date range) — "clear what I'm currently looking at" is the natural
  shape, not a second filtering vocabulary. **Never touches `item`,
  `item.auto_queue_suppressed`, or `item.suppressed_reason`**, and has no effect on the
  Dashboard, which reads its own `metric_sample`/`metric_heartbeat` tables that carry no `job`/
  `event` reference at all — both stated plainly in the UI next to the clear controls, alongside
  logs and backups being explicitly out of scope. An active (`queued`/`running`) job is not
  history and is rejected server-side (409), not just hidden from the button. No migration
  needed — this is a pure `DELETE`, not a schema change.

### Fixed

- **Shutdown while transfers are running is now survivable in practice, not just in design.**
  `TransferQueue.stop()` SIGTERMs each in-flight lftp child so its `-c` resume state is written
  out cleanly, but it did so *sequentially* with a 10s grace each — up to ~40s with both lanes
  full, before the other schedulers even begin stopping. `docker-compose.yml` set no
  `stop_grace_period`, so Docker's 10s default cut the container off mid-shutdown and the
  graceful path effectively never ran. Children are now terminated concurrently (bounded at
  roughly one grace period however many transfers are in flight) and the compose file sets
  `stop_grace_period: 60s`. Not a correctness fix — `pget -c`/`mirror -c` resume from whatever
  partial is on disk however the process died, and `_reconcile_orphaned_jobs` already marks an
  orphaned job `INTERRUPTED` at startup and leaves its item eligible to be picked up again —
  but a clean resume state rather than a merely recoverable one, which matters most on an image
  pull mid-release.
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
- **A `move`-mode item lost its verify/extract outcome within one scan of the remote delete**
  *(found by the user 2026-08-13, the first time `move` mode ran end to end against a real
  release)*: it downloaded, verified, deleted the remote, unrarred — and every item read
  `LOCAL_ONLY` again moments later. `core/reconcile.py` reads "remote absent, local present" as
  `LOCAL_ONLY` regardless of *why* the remote is absent, and `outcome_survives_rescan` (fixed for
  the `DOWNLOADED` case the day before, in the entry above) only ever protected a structural
  `DOWNLOADED`, never `LOCAL_ONLY`. It now also wins over `LOCAL_ONLY`, but only when
  `item.remote_deleted_at` is set — the signal that *this codebase* deleted the remote copy on
  purpose, as opposed to a genuinely untracked local file. Fixing that alone would have traded
  one bug for a worse one: once `auto_move` relocates the local copy too, the item's `rel_path`
  leaves both trees entirely and `core/reconcile.py` produces no node for it at all, so nothing
  would ever revisit the row again — `EXTRACTED` forever instead of reaching `REMOVED_LOCAL`
  through §7.3's grace period. `core/engine.py._persist` now also resolves every previously
  tracked `rel_path` that vanished from both trees this pass through the same grace-period
  machinery, so a relocated (or externally moved) `move`-mode item still reaches `REMOVED_LOCAL`
  rather than freezing on its outcome.
- **Four defects found by the user within hours of the local-deletion feature shipping**
  *(2026-08-13)*, all variations of one theme: a row that nothing will ever revisit, so it
  stays wrong forever.
  - A large delete gave no feedback while it ran, and the actual removal blocked the whole
    process (not just the request that started it) for its whole duration. `item.substate =
    'removing'` is now written and published *before* the filesystem work starts, and the
    work itself now runs off the event loop so that message — and everything else — can
    actually get through while a large directory delete is in progress. Protected from a
    racing scan (and a second concurrent delete of the same item) by a new in-memory
    `DeleteInFlight` tracker, the same shape and crash-safety guarantee as
    `PostprocessPipeline.in_flight_item_ids()` — a killed process cannot leave a row stuck
    reading "Removing" forever.
  - A row this codebase deleted itself (`REMOVED_LOCAL`/`REMOVED_BOTH`, suppressed) never
    noticed content coming back on either side — a re-uploaded release still read "Removed
    Both" with no indication the remote copy was back, and a child file a fresh extraction
    recreated locally stayed frozen at "Removed Both" even though the bytes were on disk
    again. Both now correct (`REMOVED_LOCAL` if only remote returned, `LOCAL_ONLY` if only
    local did) while staying exactly as ineligible for auto-queue as before — suppression and
    state text are separate questions, and only the text was ever wrong. The Files page also
    now labels the action "Re-Download" rather than "Queue" for exactly this row shape, named
    by the user directly.
  - **The most serious of the four**: a small file's row could get stuck at `PARTIAL` forever
    on a `move` queue, with no rescan able to fix it — reported by the user as "the last file
    downloaded was a Sample file and it ended at Partial but the file is there and there are
    no active transfers." Root cause: the throttled per-child progress writer can leave a
    stale mid-transfer reading behind right as a job finishes, and post-processing can relocate
    the whole release out of both trees before any scan gets the chance to correct it — once
    that happens, there is no fresh structural reading left to fix the row with. Fixed at the
    source (`core/queue.py._reap_one` now flushes one final, accurate, unthrottled reading of
    every child the instant its job reaps) and with a safety net for whenever a stale reading
    forms anyway (`core/mount_sentinel.resolve_vanished`, a narrow fallback for a `PARTIAL`/
    `LOCAL_ONLY` row that leaves both trees with no other opinion available).
  - A completed directory showed no size at all on a `move` queue, while every file inside it
    still did — files already fell back from a cleared `remote_size` to `local_size`;
    directories now do too.
- **`PUT /api/settings/postprocess` and `PUT /api/settings/retention` could silently reset a
  field a request genuinely omitted, rather than leaving it as previously saved** *(2026-08-13,
  found while hardening archive cleanup's own settings)*. Every field on `PostprocessSettingsIn`
  defaults except three (`failed_retention_enabled`/`_days`, `delete_archives_after_extract`),
  and both fields on `RetentionSettingsIn` default — a request missing any of those got the
  model's hardcoded default silently written over whatever was actually stored, no error, no
  event. Concretely reachable today for `failed_retention_enabled`/`_days`: Settings →
  Post-processing has no field for either (a pre-existing "backend first, UI catches up later"
  gap), so every save from that page has always omitted both, discarding any value set directly
  against the endpoint. Both endpoints now merge: a field present in the request is applied, a
  field genuinely absent keeps its previously-stored value, using pydantic's own
  `model_fields_set` to tell "omitted" apart from "sent." The literal race this was found
  investigating — a save fired before the initial `GET` populates the form — turned out not to
  be reachable in `PostProcessingTab.tsx` today (the Save button isn't in the DOM until loading
  finishes either way), but a related gap was: a *failed* initial load left the form at empty
  defaults with nothing telling the user, and Save fully clickable. The page now tracks a
  successful load separately from "not loading," disables Save until one lands, and surfaces the
  load error if it doesn't.
- **The one branch in archive cleanup that left no trace at all** *(2026-08-13)*: every withheld
  cleanup wrote an `event` row except "this item has no archives," the most common case by far
  and, for that reason, deliberately still not an `event` (the volume would be almost pure
  noise) — but it did not even log at debug level, so a user diagnosing "why didn't cleanup run"
  had nothing to find. Now logs at debug.
- **A row that left both trees for good never left the Files tree** *(2026-08-13, regression
  found by the user within hours of the fix directly above it — a real `move` queue: "in move
  mode it deleted the upstream, shows local only. but then when I delete local via CLI the files
  list shows them in the tree still as Extracted for the directory and removed_local on the
  mkv")*. The fix above it correctly stopped a vanished-from-both-trees row from freezing on its
  outcome forever, by writing a fresh resolved state for it every scan pass — but the same change
  also made every one of those rows *published* forever, since the set it wrote to is the same
  one the WebSocket projection filters on. A row is now published while it holds a
  content-asserting outcome during §7.3's grace period (the content could still come back), and
  drops out of the tree — reported once in that scan's delta — the moment it lands on a terminal
  `REMOVED_LOCAL`/`REMOVED_BOTH` with nothing left in either tree; it keeps being written to the
  database on every later pass regardless, so the History page is unaffected. The opposite case —
  delete locally while the remote survives — was never at risk and is now guarded by an explicit
  test: that row stays in the tree, `REMOVED_LOCAL`, "Re-Download" available, exactly as before.
  In the same fix: a fully-vanished `move`-mode item was landing on a bare `REMOVED_LOCAL`
  ("remote still present") rather than `REMOVED_BOTH`, a known, documented gap
  (`prompts/open-issues.md`) that also made `AutoQueueSettings.re_download_externally_removed`
  capable of queuing a doomed transfer against a remote that no longer exists; closed in the same
  pass rather than left open, since it was the same underlying question asked twice.
- **An item could be queued twice and run two concurrent lftp processes against the same paths**
  *(2026-08-13, user report: 4 lftp processes where there should have been 2, and
  `foo.mkv.lftp~20260813154311~` temp files on disk)*. `enqueue_item` had no guard against an
  already-active job, so a double-click (or Queue on an item auto-queue had just picked up)
  inserted a second job row and spawned a second process. Now idempotent (returns the existing
  job) plus a hard guard at the scheduler's admission layer that refuses to run two processes for
  one item regardless of how many job rows exist for it; `core/autoqueue.py`'s "no active job"
  eligibility rule is now enforced by its query, not merely by its docstring. The `~timestamp~`
  temp name (lftp avoiding a collision with the first process's own `.lftp` file) is now
  recognised everywhere `.lftp` already is, so an orphaned one from before this fix no longer
  shows as its own phantom row and — the dangerous part — can no longer make a directory read
  `DOWNLOADED` while genuinely incomplete, which on a `move` queue was the path to deleting the
  remote copy of a release that never finished. An optional, off-by-default cleanup pass
  (`Settings` API only for now, no UI yet) reaps stale orphaned temp files past a configurable
  age. Resume itself was verified working throughout (measured in bytes against the fake seedbox,
  not inferred from filenames) — the bug was duplicate processes, not broken resume.
- **The header's "24h" figure read `0 B` after Clear History even though the Dashboard showed
  real usage** *(2026-08-13, user report)*. The two read different tables: the header summed
  `job.bytes_done` for jobs that finished successfully in the last 24h, while the Dashboard reads
  `metric_sample`, which Clear History deliberately never touches — both behaved as designed, but
  the design let a *history* clear zero out a *usage* statistic. The header now reads
  `metric_sample` too, via the same `core/metrics.py.queue_breakdown` call the Dashboard's own
  bytes-per-hour chart uses, so the two numbers can no longer structurally disagree for the same
  window; this also means the figure now counts bytes from attempts that later failed, not only
  fully completed transfers, which is the more honest answer to "how much did this actually move
  in the last day." The "24h" item is now a link to the Dashboard.
- **`lftp` exiting 0 was treated as proof a transfer completed** *(2026-08-14, live incident: a
  job exited 0 having left one file 500 MB short as a `.lftp` temp file, and the item was marked
  `DOWNLOADED` and handed to post-processing anyway)*. `set cmd:fail-exit true`'s exit 0 means
  lftp reported no error, not that every byte arrived — before an item can now reach
  `DOWNLOADED`, `core/queue.py._reap_one` confirms completeness from the filesystem: no leftover
  `.lftp`/`.lftp~<timestamp>~` temp file or orphaned `.lftp-pget-status` sidecar anywhere under
  the item, and local bytes meeting the relevant remote total (excluding anything `EXCLUDED` by
  a `file_exclude` pattern, so this can't reintroduce the archive-cleanup infinite-loop failure
  mode §6 already solved for). **Behaviour change an existing install will notice:** an item that
  used to reach `DOWNLOADED` off a short transfer now goes `PARTIAL` and re-queues instead —
  auto-queue's existing eligibility picks it back up and `lftp -c` resumes from what's already on
  disk, rather than a bad import or (on a `move` queue) a bad remote delete going out on
  incomplete evidence. A new `incomplete_on_exit_zero` event names the expected-vs-actual byte
  counts and the leftover file(s) — the row that would have explained the incident at a glance.
  In the same fix: a successful job's `output_tail` is retained now instead of being nulled (the
  one job whose success was in doubt had its own explanatory output captured and then thrown
  away by the same code path), and the Transfers page now surfaces the item's most recent
  succeeded job (dismissible, same as a failed/cancelled one) instead of a completed transfer
  vanishing from the page the instant it's reaped — the gap that made the live incident look, for
  seven real minutes, like nothing was running and the header read 0 B/s.
- **A job's `bytes_total` could exceed its own `bytes_done`'s denominator** *(2026-08-14, same
  live incident: the API returned `bytes_total: 31812118603` alongside `bytes_done:
  38841560420` for one job)*. `job.bytes_total` was never persisted at spawn, so every API
  response fell back to the *live* `item.remote_size` — a value that can drift after the job
  spawned (a later scan, a pattern edit) while `bytes_done` stayed fixed at whatever
  `remote_size` was when the job actually finished. `core/queue.py._spawn_decision` now freezes
  `job.bytes_total` at spawn, the same "fixed at admission, never re-shaped" invariant §4.5
  already uses for bandwidth; `api/jobs.py`/`api/history.py` prefer that frozen value over the
  live column.
- **A local rename failure was misclassified `REMOTE_GONE` and permanently failed the item**
  *(2026-08-14, live incident: fired three times in one evening on `pget: rename(<file>.lftp,
  <file>): No such file or directory` — another process writing into the same directory once,
  Sonarr importing and removing the download folder mid-transfer twice)*. `REMOTE_GONE`'s
  pattern matched the bare substring "no such file" with no regard for whether the path
  involved was remote or local, and `REMOTE_GONE` never retries — so a transient local failure
  permanently failed the job and suppressed the item, every time reported to the user as "the
  remote file is gone." A local rename failure now classifies as a new `LOCAL_FS_ERROR`, matched
  by lftp's distinct `rename(<src>, <dst>): No such file or directory` message shape (both
  operands always local — lftp's sftp backend never shells a remote-side rename as part of a
  plain download), and joins the transient set: it retries with the same backoff as
  `HOST_UNREACHABLE`/`TLS_ERROR` instead of suppressing the item. A genuinely missing remote
  file — a different message shape, no `rename(...)` wrapper — still classifies `REMOTE_GONE`
  and still never retries.

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
