# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it starts
cutting releases (see [`standards.md`](standards.md) — `release-prep-and-cut`). `0.1.0` is the
first tagged release — a beta. Everything before it was in-development and never released, so
`0.1.0`'s section below carries the project's whole history to date; later sections will be far
smaller.

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

- **Pause the transfer queue for a fixed duration** — a dropdown (1 / 10 / 30 / 60 minutes,
  alongside the existing default "until I unpause") next to the Queue tab's Pause control,
  combinable with both entry modes (*pause after current* / *pause now*). The deadline is a
  stored absolute timestamp, not a running timer, so it survives a restart correctly: paused
  before the deadline stays paused, but an app that comes back *after* the deadline resumes
  unpaused rather than quietly re-honoring a stale pause. Expiry is enforced on the transfer
  queue's own ~1s tick, so the queue resumes itself on schedule with no page open, and records
  its own audit event distinct from a manual unpause. The paused banner and header badge both
  show the deadline ("resumes at HH:MM") whenever one is set.

### Changed

- **Active/pending now sorts running → queued → still-processing**, rather than placing
  pipeline-in-flight rows (verifying, extracting, awaiting import, deleting source) between
  running and queued. A processing row is lftpweb *waiting on someone else* — usually an *arr
  import — while `queued` is its own genuinely-next work, so the list reads now / next / parked.
  The tradeoff, recorded rather than left to be rediscovered: on a deep backlog at 20 rows a
  page, a processing row can now land pages below the fold, which is what the original placement
  avoided. Accepted because such rows are transient and few.

### Fixed
### Security
### Deprecated
### Removed

## [0.3.0] — 2026-08-21

### Added

- **The Queue row's own chip fills and ticks again, and Preflight's "Waiting" chip now fills as
  the remote client downloads**: the single-line Transfers row lost its state chip's percent when
  it collapsed to one line (2026-08-15) — it's back (`Downloading 45%`, alongside the figure
  column's own `45% · 40 MB/s · 25m left`, both shown deliberately). Preflight's *arr "Waiting"
  chip gets its own fillable amber bucket (`WAITING`, `components/StateChip.tsx`), fed the *arr's
  own `size`/`sizeleft` queue fields — visibility into a download happening entirely outside
  lftpweb. `Settling` deliberately keeps no fill (its detail is its tooltip, not a bar); a row
  with no size data from the *arr renders a plain chip, never `0%` or a fabricated bar.
- **The Preflight box gains a second source (settle-gated releases) and a mount-gate banner**
  (`docs/transfers-redesign-spec.md` §4, `prompts/2026-08-20-preflight-waiting-sources.md`): an
  item that would be auto-queued right now if only its remote fingerprint had settled shows up
  as a row with its known remote size (`remote — 22 GB`), alongside the existing *arr-sourced
  rows — the settle gate held these releases back invisibly before, with no signal anywhere on
  the Queue tab. When a settle row and an *arr row would describe the same release, the settle
  row wins (it's confirmed bytes on the seedbox, not just a queue entry). A queue whose local
  root fails the mount sentinel now also gets one banner line on the box naming the queue and
  why — its entire auto-queue pass is skipped at once, so a row per affected item would bury the
  one fact that matters. A suppressed item or a pattern-unmatched `REMOTE_ONLY` item still never
  appears in Preflight from either source — nothing is coming for either, so showing them would
  turn the box into a second Files tree.
- **A name filter on the Transfers page**: start typing and only rows whose name contains that
  text stay visible (case-insensitive, matches a dotted release name literally). A **"Dismiss
  list"** button beside it bulk-dismisses exactly the finished rows the filter currently matches,
  in one request — greyed out until the filter matches at least one dismissable row. The filter
  itself doesn't persist across a reload, matching the Files page's own text filter and the Logs
  filter.
- **Per-row queue reordering on the Transfers page**: **▲ up one**, **▼ down one**, and **▲▲ to
  top** on each queued row, replacing the previous single "Move to top" button
  (`docs/transfers-redesign-spec.md` §3.4, phase 1 stage 2). One endpoint,
  `POST /api/jobs/{id}/move`, backs all three. Disabled at the front/back of the global queue
  order (the position number already says so); an out-of-turn request against the backend itself
  (a second tab, a stale render) is a silent no-op rather than an error, and a job that started
  running or finished between the page render and the click is rejected instead of silently
  reordering a job whose bandwidth allocation is already fixed. The move's scope is global — now
  that the Transfers page renders one flat, globally-ordered list (below), a move always swaps a
  row with the one shown directly above/below it on screen.
- **A short display name per queue** (Settings → Queues, `docs/transfers-redesign-spec.md` §3.6,
  phase 1 stage 3): an optional, per-queue label (e.g. "DC-Movies" → "MOV") for the compact
  per-row queue badge the Transfers page now renders (below). Trimmed at save time, capped at 10
  characters, and not required to be unique — it's a display hint, not an identifier. Leaving it
  blank falls back to the queue's full name.
- **The Transfers page drops per-queue grouping for one globally-ordered list**
  (`docs/transfers-redesign-spec.md` §3.1, phase 1 stage 4a) — `core/scheduler.py` has zero
  references to `queue_id`; admission is one global line, and grouping visually implied each
  queue had its own ordering, which was false. Each row now carries a compact queue badge (its
  short name if set, else its full name, always in the row's `title`) and, for a job admitted
  from the small-item fast lane, a **fast lane** marker explaining that it may start before a
  lower-numbered main-lane job. This also resolves the stage-2 chevron oddity above: with
  grouping gone, ▲/▼ always trade with the row directly on screen. The per-queue **Dismiss
  Queue** button (v0.2.3) is **superseded, not merely deleted** — the name filter plus
  **Dismiss list** does the same job: filter to a queue, dismiss the list. This reverses the
  2026-08-16 "group rows by queue" decision; see `docs/decisions.md` for why.
- **The Transfers page splits into two paginated boxes** (`docs/transfers-redesign-spec.md`
  §3.2, phase 1 stage 4b): **Active / pending** (queued/running, client-side — the set is bounded
  and already loaded) and **Complete** (finished, newest-finished first, **server-side** via the
  new `GET /api/jobs/complete`), numbered pages, SAB-style. Rows shifting between pages as work
  completes is expected, not a bug. The name filter now runs server-side for the Complete box (it
  can no longer see everything client-side once it's paginated), and **Dismiss list** carries
  that same filter text to the server (`dismiss_all_terminal`'s new `name_filter` scope) so it
  dismisses every matching row across every page, not just the one currently on screen — the
  id-list scope it used before this change could only ever name a single page's worth. An empty
  filter result still dismisses nothing, never everything, the same guarantee the id-list scope
  already gave. **Each box now also carries its own "Show 10/20/50" rows-per-page selector**
  (2026-08-20, a follow-up from the user's first real look at the finished page,
  `prompts/done/2026-08-20-transfers-page-size-selector.md`), independently remembered per
  browser (`localStorage`, invalid/stale stored values fall back to the default). Both boxes now
  default to **20** — the Complete box changes from a fixed 50, per the user's own call once they
  saw it on screen ("50 is too many rows at once in practice"). Changing a box's size always
  resets it to page 1 rather than trying to preserve scroll position or compute an equivalent
  page.
- **"Dismiss" moves into the Complete box header and becomes an outcome menu**
  (2026-08-20, a follow-up to phase 1 stage 4b from the user's browser review,
  `prompts/done/2026-08-20-transfers-dismiss-menu-and-counts.md`): the old page-top "Dismiss
  all" button sat far from the rows it acted on ("the dismissall button should move down the
  top of the completed section"); it's now a keyboard-navigable dropdown right there — **All**,
  **Downloaded**, **Failed**, **Stopped** — reusing the site bandwidth "Start now" menu's own
  popover pattern rather than a new one. **Folds in "Clear all failed"**, whose whole job is now
  exactly "Dismiss → Failed" done server-side and atomically instead of a client-side
  `Promise.allSettled` loop over each row. **The outcome filter composes with the name filter**
  (both narrow the same set; `job_ids`/`queue_id` stay mutually exclusive with everything, as
  before) — `DismissAllRequest` gains an `outcome` field and its mutual-exclusion validator is
  restructured to allow that composition (see `docs/decisions.md`). An outcome matching zero
  rows still dismisses nothing, never everything — the same guarantee `name_filter`'s own empty
  match already gave, now extended to the composed case. Per-outcome counts aren't fetched (the
  Complete box's total is server-paginated and doesn't have them cheaply available) — only
  "All" shows a count, reusing the box's own already-known total.
- **The Active/pending box gets the same "Page X of Y (Z total)" readout the Complete box
  already had** (2026-08-20, same follow-up) — a real inconsistency the user's browser review
  caught, not a design choice: the Complete box showed it unconditionally, the Active box showed
  nothing. Both boxes now read it from one shared `lib/pagination.ts.pageReadout` so the wording
  can't drift between them again. **The Active box's entire shell (header, empty state,
  page-size selector, pager) now always renders**, matching the Complete box's own always-on
  shell — previously an empty or fully-filtered Active box vanished outright, taking its
  page-size selector with it.
- **A directory row's expand panel on the Transfers page now shows per-file progress**
  (`docs/transfers-redesign-spec.md` §3.3, phase 1 stage 5) — the thing the Files page was
  previously the only way to see, moved to where the ordering lives. Expanding a row fetches its
  files once (`GET /api/items/{id}/children`, capped at 500) and keeps them live from the same
  WebSocket connection the page already has open, so expanding several rows at once never means
  several independent polls. A `pget` (single-file) job has no children and doesn't offer this
  group — its own progress is already the row's collapsed-line figure.
- **Transfers is now the main nav section, with Queue and Files as tabs beneath it**
  (`docs/transfers-redesign-spec.md` §2, phase 1 stage 6) — navigation only, nothing about
  either page's rendering, fetching, filters, pagination, expansion, or actions changed. Files is
  demoted, not removed or merged into Queue: it stays the only view of `REMOTE_ONLY` items that
  never entered the pipeline (no pattern matched, or auto-queue was off), the only home for
  Delete, and the only tree-shaped view of the remote. Queue (`/transfers/queue`) is the default
  tab — "the working surface now" — and Files moves to `/transfers/files`; the old standalone
  `/files` route now redirects there rather than 404ing. Each tab has its own URL, so it's
  linkable and survives a reload, the same pattern Settings' and Docs' tabs already use
  (`nav.ts.tabsForPath`).
- **History becomes Events** (`docs/transfers-redesign-spec.md` §2, phase 1 stage 7, the last
  stage of phase 1) — now the audit-event log only: every verify/extract/move outcome, every
  remote delete, and every delete withheld, with the reason. Its own `job` list is gone; the
  Queue tab's Complete box already covers "what finished, in what order" (stage 4b), so the two
  were answering the same question. `/history` still resolves to this page (a redirect,
  `App.tsx`) so nothing that links or bookmarks the old path breaks — the same pattern stage 6
  set for `/files` → `/transfers/files`.
- **A per-item Events deep link, in the item drawer's own header** — one click to the full,
  unbounded, filterable event log for exactly this item, pre-filtered via the URL (`?item_id=`)
  so the resulting view is linkable, reloadable, and back-button friendly. The Events page shows
  plainly when it's filtered this way, with one click back to the unfiltered log.
- **"Mark complete" / "Mark failed" on an in-flight Transfers row**, with **Undo** — the manual
  escape hatch for a release that has genuinely wedged somewhere in its pipeline. **It is a
  classification only**: it files the row under Complete and does nothing else. It never deletes
  the seedbox source, is never read as a confirmed Sonarr/Radarr import, and never triggers
  post-processing, notify, or cleanup — a source delete is irreversible and still waits on real,
  twice-confirmed evidence, never on a button click. Every resolution is written to the Events
  log, and the row carries a **Marked complete**/**Marked failed** chip so it never quietly
  reads as a normal completion.
- **A site-wide Pause control at the top of the Transfers → Queue tab** (`prompts/2026-08-20-
  queue-pause.md`): **Pause after current** leaves running transfers alone and admits nothing
  new; **Pause now** additionally stops every in-flight transfer and returns each one to
  `queued` at its same position, ready to resume — not restart — from the same bytes once
  unpaused. Deliberately **not** the same thing as Stop: a paused-now item never carries
  `auto_queue_suppressed` and never reads `STOPPED`/`FAILED`, so nothing needs a manual re-queue
  to come back. Auto-queue, manual Queue clicks, reaping, post-processing, and scanning all keep
  running while paused — only admission itself stops — and reordering (the ▲/▼/▲▲ chevrons)
  stays fully live, which is the point: pause, rearrange the queue, then unpause. Persisted
  across a restart. An unmistakable amber banner marks the paused state on the Queue tab, and
  the header bar's health readout gets a matching **● queue paused** badge. "Start now" is
  disabled (with a reason in the tooltip) and rejected server-side (409) while paused.
- **A "Preflight" box at the top of the Transfers → Queue tab** (`docs/transfers-redesign-spec.md`
  §4, prefigured) — things a bound *arr instance already knows about that haven't reached this
  seedbox's completed folder yet, so there is nothing here for lftpweb to do work on. **A pure
  projection of the *arr poller's own latest ~60s poll — no table, no migration, nothing
  persisted**: a release that drops out of the *arr's queue simply stops being projected, with a
  brief flap-tolerance hold (150s) so a single missed poll (the same SABnzbd blank-queue blip
  behind the amber `dropped` state) doesn't blink a row out and back. Attribution is
  `arr_visible_path` prefix-matching a record's `outputPath` against each bound queue; a record
  matching no queue is silently omitted (never a guess), and a record that already matches a
  real lftpweb item never appears here at all, so a release is never visible twice at once. Five
  rows by default, expandable and paged (reusing the existing pager) past that; zero rows reads
  as a single "Nothing in preflight." line rather than reserved empty space, and the whole box
  disappears when no source is configured. Rows are inert by construction — no queue position,
  no chevrons, no Dismiss/Start now/Stop — there is no `item` and no `job` behind one yet. The
  row/box shape (`core/preflight.py`) is deliberately source-agnostic: the *arr poller is the
  only source wired up so far, ahead of an already-planned settle-gate source as a follow-up.
- **Preflight rows now read as the top of the same table as Active/pending and Complete**, from
  the user's first browser look at the shipped box: *"we missed the remaining time on the
  preflight list. and we moved the columns around. it should still have the tag and the column
  for status on arr icon. arr icon is at the first of the line now."* Column order now mirrors
  every other Transfers row — queue tag, title, state chip, *arr chip, then a right-aligned
  figure — instead of leading with the *arr logo and carrying no queue tag at all (added:
  `PreflightRow` now carries the bound queue's own name/short-name, so it can show the identical
  tag every other row does). An *arr row now shows its remaining time too, parsed from the *arr's
  own `timeleft`, through the same "`<duration> left`" figure the Transfers row already uses for
  its own ETA — omitted whenever the *arr has no meaningful estimate (a paused/stalled download
  client item), never a fabricated or zero figure. Every row's chip is now rendered through the
  same `StateChip` component the rest of the app uses — the box previously hand-rolled a flat
  grey span, which is why "Settling" read as a different kind of thing from everywhere else it
  appears; it now gets `StateChip`'s existing amber, the same colour a Preflight row gets
  regardless of source, since every row here is "waiting," whatever it's waiting on. An *arr
  row's chip also now speaks lftpweb's own vocabulary instead of the *arr's raw wire word — the
  *arr's own `"downloading"` becomes **"Waiting"** (renamed again just below, from an interim
  "Waiting for download"; lftpweb is doing nothing here, just watching the *arr's own download
  client work) and `"importing"` becomes **"Importing"**; the *arr's own detail moves to the
  chip's tooltip instead — `Downloading from "<download client>" — reported by <instance>`.
- **The *arr chip's label shortens to "Waiting", the Settling chip gets a tooltip of its own,
  and the Preflight box gains a "Show 5/10/20" page-size selector** — three follow-ups from the
  user's live browser review. *"Waiting for download"* still read as ambiguous ("lftpweb is
  waiting to download it" vs. "waiting for the download client to finish"); after considering
  "Waiting for remote client" and asking to shorten it, the user picked **"Waiting"** — seven
  characters, matching sibling chip `Settling` in shape (one word, present tense), and saying
  nothing about *where* since the *arr brand logo, the box's own name, and the existing tooltip
  already carry that. The `Settling` chip was the one asymmetry left — no tooltip at all — so it
  now gets one too, reusing `lib/format.ts.settleWaitLabel` **verbatim** (the same "Waiting for
  changes — 1 of 2 scans, 35s of 60s" sentence the Files tree and the lifecycle R-icon tooltip
  already share) rather than a third copy of that wording; `PreflightRow` carries the settle
  gate's own `matched_scans`/`updated_at` pair (generic `wait_scans`/`wait_since` fields, unset
  for an *arr row) so the countdown stays live between polls instead of freezing at a pre-baked
  string. Finally, the box's own "Show all (N)" expand-then-page toggle is replaced outright by
  a persisted **5/10/20** selector (`preflight.pageSize`), matching the "we should have a drop
  down on preflight like the rest" request — smaller than the other two boxes' 10/20/50 since
  this box is smaller by intent, reusing the same `Pager`/`pageReadout` and a `PageSizeSelect`
  now shared with the Active/Complete boxes rather than a second independent control.

### Changed

- **The Transfers page's two boxes now split on *pipeline* completion, not on the transfer
  exiting** (`docs/transfers-redesign-spec.md` §3.2) — the user's own report: *"Shouldn't a job
  live in that state until the sonarr/radarr hook lands if they are enabled? Currently they move
  to complete but they technically aren't."* A release's work continues well past lftp:
  verifying, extracting, the move out of staging, waiting for the *arr to actually import it, and
  deleting the seedbox source. Those rows now stay under **Active / pending** and **say what
  they're waiting on** — *Verifying*, *Extracting*, *Processing*, *Awaiting import*, *Deleting
  source* — instead of sitting under Complete while the release plainly wasn't. Applied to every
  queue, whether or not it's bound to a Sonarr/Radarr instance: one definition of "done".
  Nothing can get stuck there — disabling an *arr instance immediately releases everything
  waiting on it, a post-processing step that died with the app doesn't hold a row, and every
  remaining wait is time-bounded. A row that's still in flight can no longer be dismissed
  (including by the bulk Dismiss menu), since dismissing something still being worked on would
  make it vanish from both boxes at once; use "Mark complete"/"Mark failed" if it really is
  stuck. In-flight rows sort next to the running ones rather than beneath the whole queued
  backlog.

- **The transfer queue's ordering internals moved from a `rank`/boost scheme to a dense
  `queue_position` model** (migration 023, `docs/transfers-redesign-spec.md` §3.4/§3.5,
  phase 1 stage 1) — the prerequisite for the upcoming per-row "move up one / down one"
  reordering. No user-visible change on its own: existing queues are backfilled in the same
  order they'd have run in before, "Move to top" behaves identically, and the v0.2.6
  startup-rescue ordering (re-queuing an interrupted item back to its original place in line)
  was re-derived and re-proven by test rather than merely ported.

### Fixed

- **A handed-over release now disappears from Preflight within a few seconds, not 20-30s and
  sometimes longer.** From the user's browser review: "it does take 20-30 seconds for items to
  be removed from preflight after it shows in active — sometimes it is fast and sometimes it is
  slow." The variance was the tell: the previous evict-on-handover fix (below) removed the 150s
  flap-tolerance hold, but retirement was still only *decided* once per *arr poll pass
  (`ArrSettings.poll_interval_s`, 60s default) — an item landing right after a poll waited nearly
  a full interval, one landing right before felt instant, plus the frontend's own 15s poll on
  top. The underlying question, "does a matching `item` exist now," is purely local state, so
  `ArrSyncScheduler.preflight_rows` (now `async`) re-asks it on every `GET /api/queue/preflight`
  call rather than only on the next poll — the same `_record_matches_any_item` predicate
  `_preflight_candidates` already used, extracted into one shared function so the two paths can
  never drift. The frontend's own poll also drops from 15s to 5s (`hooks/usePreflight.ts`), now
  the dominant remaining delay since the endpoint itself is no longer bounded by the *arr's own
  cadence. Flap tolerance (a merely-absent row still held for the full 150s) and the settle
  source (unaffected, still a wholesale per-scan replace) are both unchanged. *arr poll rate
  itself is untouched — this needed no more requests to the *arr, only a cheap local re-check.
- **A handed-over release no longer lingers in Preflight for up to 150s alongside its own new
  Active/pending row.** Found by reading the code, not observed in a browser: `core/arrsync.py`'s
  Preflight cache couldn't tell "this record just matched a real lftpweb item" (a known, terminal
  reason to stop showing it) apart from "the *arr's report simply didn't mention it this pass"
  (the SABnzbd blank-queue blip the cache's flap-tolerance hold exists to absorb) — both looked
  identical to the hold, so a just-handed-over release sat duplicated in both boxes until the
  150s hold expired. `PreflightHold.update` now takes a `retired` set alongside the rows it still
  sees, and evicts anything in it immediately; a record that's merely missing still gets the full
  150s tolerance, unchanged. The settle-gated source was never affected — it replaces its rows
  wholesale on every scan pass rather than using this hold at all, which this incident is the
  concrete evidence for having been the right call.
- **Auto-queue no longer re-downloads a release the *arr has just imported.** Found in
  production on a `move` queue bound to Sonarr: an item finished, post-processing renamed it and
  told Sonarr to scan, and Sonarr began moving the media file into the library — leaving the
  release directory reading `PARTIAL`, which auto-queue treated as "an interrupted transfer,
  pick it back up." The re-queued job sat in the queue until a slot freed, by which time the
  seedbox source had been deleted on the confirmed import, so it failed `REMOTE_GONE` on zero
  bytes; worse, while it waited it blocked the *arr cleanup for the whole time (97 minutes, in
  one of the two observed cases). Two independent fixes: the ~10-minute local-absence grace
  period now covers a *partial* loss of local content, not only a total one — keyed strictly on
  "was complete, and the remote total is unchanged, and local shrank", so a genuinely
  interrupted transfer still resumes on the very next pass and a release whose remote *grew* is
  still fetched immediately — and auto-queue now skips any item whose bound *arr has already
  been handed it (`arr_status` of `notified`, `imported`, or `cleaned`), which has no time bound
  and so also covers a slow import like a 38-episode season pack. A manual Queue click is
  unaffected by either. Not fully closed: on a queue with **no** *arr binding, an external
  removal that takes longer than the grace window can still trigger one re-queue — see README's
  "Known gaps."
- **Support bundle *arr log fetch now spends its per-instance budget on the newest files, not
  the biggest stale ones.** The fetch order was a filename/rotation-suffix sort, which orders
  correctly *within* one log series (`sonarr.*`, `sonarr.debug.*`, `sonarr.trace.*`) but
  interleaves *across* series purely by name — a dormant debug/trace series' own stale files
  could sort ahead of a live series' current file. Seen in production: a ~20 MB budget was
  spent entirely on three files from a switched-off debug/trace session, all nine days stale,
  while the files covering the actual incident window were dropped. Now sorted by the *arr's
  own reported `lastWriteTime`, newest first, across every series at once; a file with no
  usable timestamp sorts last, never first. `TRUNCATED.txt` now lists a last-modified timestamp
  for both the fetched and the skipped files.
- **A file actively being written now reads "Downloading," not "Partial," in both the Queue
  row's file-list expansion and the item drawer.** The user's browser review: *"the sidebar for
  active file ... I think it should show downloading and the chip should show progress. Not
  Partial."* A leaf file inside a mirroring directory never gets a persisted `state` of
  `DOWNLOADING` itself (only its parent job's own top-level item does) — its state caps at
  `PARTIAL`/`DOWNLOADED`, which is structurally correct but misleading to read while lftp is
  actively writing it. A new shared helper, `childDisplayState`
  (`frontend/src/lib/fileTree.ts`), maps a child to `DOWNLOADING` only when it is **both**
  currently `PARTIAL` **and** owned by a job that is currently `running` — not every child of a
  running job, since a `mirror` works through a release's files progressively and most children
  are complete or untouched at any given moment. Both surfaces (`TransfersPage.tsx`'s
  `FileListRow`, `ItemDrawer.tsx`'s `Row`) call the one function, so they can't drift; the
  drawer's chip also gains the progress-fill bar the Queue-row expansion already had. Display
  only — `item.state` and the backend state machine are unchanged.
- **The Active box no longer pads itself out to roughly five empty rows when nothing is
  transferring.** The user's browser review: *"Active box shrinks to one row when 1 active item.
  then expands to 5 rows when nothing is going on ... We should keep this at one row always and
  only expand when we have more rows."* The empty state was a fixed-height (`h-40`) dashed
  panel — the emptiest state took the most room, pushing the Complete box down. The Complete box
  shared the identical panel for its own "nothing finished yet"/filter-empty states, so it had
  the same defect. Both now render a single line, matching the rule already applied to the
  Preflight box ("Nothing in preflight.") rather than inventing a second empty-state idiom.

### Security
### Deprecated

### Removed

- **The old History page's job list** (`docs/transfers-redesign-spec.md` §2, phase 1 stage 7) —
  superseded by the Queue tab's Complete box (stage 4b). One consequence named rather than
  hidden (README's Known gaps): a dismissed job no longer appears on any list page. Its `job`
  row is untouched — dismissal was always display-only — and stays reachable one item at a time
  from that item's own drawer, but nothing lists every dismissed job across the whole install
  any longer. The underlying `GET`/`DELETE /api/history/jobs*` endpoints are unaffected —
  `docs/decisions.md` records why they're staying.

## [0.2.6] — 2026-08-18

### Added

- **A downloading transfer's row now shows how long until it completes**, next to its percent
  and speed on the same collapsed line.
- **"Start now" is a menu, not a single button**: 10% / 25% / 50% / 75% / Max of your configured
  site bandwidth limit, instead of always jumping straight to the full ceiling. The percent
  options are disabled with a hint if no site bandwidth limit is set — Max always works.

### Changed
### Fixed

- **A transfer interrupted by a restart now resumes at its original place in the queue**,
  instead of dropping to the back behind everything that hadn't started. The startup rescue
  used to re-queue an interrupted item with a fresh timestamp, which could put an item that was
  40 GB into a 66 GB download behind a long line of items that had never even started.

### Security
### Deprecated
### Removed

## [0.2.5] — 2026-08-18

### Added
### Changed
### Fixed

- **A transfer that finished during a crash/hang no longer strands as
  downloaded-but-never-processed.** A restart's startup sweep now re-queues every item it just
  marked interrupted, and separately rescues rows an earlier restart already left in that state:
  a completed transfer's re-queued `mirror -c` no-ops straight into post-processing, a partial
  one resumes from its bytes. Interrupted items re-queue themselves on restart instead of
  requiring a manual Queue click.
- Leftover `_FAILED_`/`_UNPACK_` extraction folders whose release is long gone are now cleaned
  up automatically instead of sitting invisible on disk forever. Previously, once an item's own
  row left tracking (a manual delete, or the item leaving both trees entirely), its extraction
  staging/evidence directory had no item row, no Files-page presence, and no delete affordance
  — nothing would ever clean it up.

### Security
### Deprecated
### Removed

## [0.2.4] — 2026-08-17

### Added

- **An amber "rechecking" state for the Sonarr/Radarr icon** — a release that drops out of the
  bound *arr instance's queue with no import evidence yet no longer jumps straight to the red
  "gone" warning. It now holds amber and is rechecked on every poll: if the same release
  reappears in the queue it goes back to "being watched," if the *arr's history shows an import
  it goes green, and only if neither happens within 6 hours does it turn red. Fixes a
  download-client blip (a blank/empty queue response) permanently mislabeling an item that the
  *arr actually imported normally minutes later.

### Changed
### Fixed

- **Already-"gone" items with a stranded delete now self-heal.** A release that hit the old,
  immediate "gone" verdict before this fix — and was later imported by the *arr anyway — used to
  sit stuck forever with a parked seedbox-side delete and no cleanup. lftpweb now rechecks a
  bounded number of times in the background and promotes it once an import shows up.

### Security
### Deprecated
### Removed

## [0.2.3] — 2026-08-17

### Added

- **Settings → Queues' queue list now shows the Sonarr/Radarr brand logo beside the name of
  any queue bound to an *arr instance** — previously the only way to tell was opening Edit and
  checking the dropdown. Muted (reduced opacity) when the bound instance is currently disabled;
  falls back to a small text chip naming the instance id if the binding points at an instance
  that's since been deleted. Reuses the same real brand logos already shown on Files/Transfers/
  History (`LifecycleIcons.tsx`'s new `ArrBrandMark`, factored out of `ArrRowChip`).
- **The Dashboard's bytes-transferred chart gains its own 24h / 7d / 30d range selector, plus
  a total for the selected range.** Previously it only ever showed the last 24 hours; the new
  7d range buckets at 6 hours and 30d at 1 day (same finer-when-short/coarser-when-long
  reasoning as the existing ranges), and the chart's title, bar labels, and hover tooltips
  scale with the bucket width. The header now reads "Total: 84.2 GB" for the whole selected
  range, and each queue's legend entry gets its own range total alongside it. The speed chart's
  1h/12h/24h selector is untouched and independent — the two charts remember their timeframes
  separately. Sample retention still defaults to 7 days (30 max, Settings → configurable): a
  7d or 30d selection past what's actually retained now says so in a one-line note instead of
  silently rendering empty gaps with no explanation.
- **The Transfers page's per-queue group header gains its own "Dismiss Queue" control**,
  scoped to just that queue's finished rows — previously the only bulk option was the
  page-wide "Dismiss all," with no way to clear one queue's terminal jobs without touching
  every other queue's. `POST /api/jobs/dismiss-all` gains an optional `queue_id` body field
  (omitted means the original every-queue behavior, unchanged); the control itself only shows
  once its queue actually has something dismissable, and clicking it never toggles the group's
  own collapse state.

### Changed

### Fixed

- **Bulk delete of a mixed Files-page selection no longer errors rows with no local copy.**
  With both Local and Source checked, each selected row now gets only the scopes that actually
  apply to it — a remote-only row gets a source-only delete instead of a doomed local delete
  that used to fail the whole row before its source delete was ever attempted. A row where the
  checked scopes leave nothing applicable (e.g. only Local checked on a remote-only row) is now
  reported as skipped rather than as an error.
- **Expanding a failed transfer with no captured output on the History page now explains
  itself instead of showing an empty panel.**
- **A transfer interrupted by an application restart now records why it failed** — the History
  popout says the transfer was interrupted and that the next attempt resumes from the partial
  bytes already on disk, instead of leaving the job's captured output blank.
- **Dashboard charts no longer grow unbounded with window width**, and the app now has a
  single scroll context with no white flash below the page. The bytes and speed charts'
  `<svg>` had no height ceiling — the browser derived height from width via the viewBox's
  aspect ratio, so a wider window made them arbitrarily taller; both now cap at 320px, paired
  with a max-width on the chart block so the cap doesn't pillarbox on very wide windows. The
  app shell root was `min-h-screen`, which let it grow past the viewport and engage the window
  scrollbar alongside `<main>`'s own inner scroll; it's now pinned to `h-dvh` with
  `overflow-hidden` so `<main>` is the only scroll context, and the document background is now
  themed so overscroll/rubber-band can no longer flash white in dark mode.

### Security
### Deprecated
### Removed

## [0.2.2] — 2026-08-17

### Added

- **A what's-new popup on the first page load after an upgrade**, plus a Docs → Release notes
  page. The popup reads the release notes for every version between what this browser last saw
  and the version now running (an upgrade that skips a release shows all of them, newest
  first) and shows nothing on a fresh browser, an unchanged version, or a downgrade. Docs →
  Release notes renders `CHANGELOG.md` itself, verbatim, with a "View on GitHub" link at the
  top; the nav's bottom-left version readout now opens that page in-app instead of linking
  straight out to GitHub (the GitHub link still exists, just one click further in). Per-browser
  only (`localStorage`) — a second browser, or a private window, tracks its own "last seen"
  independently.
- **Two advisory warnings catch a misconfigured *arr "Path as seen by the *arr" setting**,
  predictively and after the fact. If the *arr's own reported path for a matched release
  doesn't agree with what a notify push would translate to, one warning event fires the moment
  the match commits — before the first notify ever goes out — naming the *arr's own path and
  suggesting the setting value that would fix it. Separately, the notify push itself is no
  longer fire-and-forget: lftpweb now checks whether the *arr's scan command actually completed
  and writes a warning if it didn't, rather than only ever knowing whether the push was
  *accepted*. Both are advisory only — they change no behavior, only visibility, in History.
- **Settings → Logs gains a text filter and a deeper lookback.** A new search box filters the
  currently-shown lines by a case-insensitive substring, instantly, with no refetch, alongside
  a "showing N of M lines" readout while it's active. The `Lines` option tops out at 10,000
  (was 2,000) so the *arr integration's per-minute poller traffic no longer eats the whole
  window in under an hour on a busy install.
- **Settings → Logs gains a "Support bundle…" button** — a dialog of checkboxes (all default
  ON) producing one downloadable zip to attach to an issue or send manually: lftpweb's own logs
  (always included), a build/environment snapshot (version, migration level, health, `lftp`/
  Python versions, per-queue disk usage), a sanitized settings dump — host/queue/pattern/
  transfer/post-processing/backup settings, built from the same response models the Settings
  pages already return, so a secret can never leak into it — the most recent 1,000 audit events,
  the most recent 100 jobs with their error output, and — one checkbox per enabled Sonarr/Radarr
  instance — that instance's own log files, fetched newest-first up to a per-instance size
  budget. The database itself, the `known_hosts` pins, and the install secret are never
  included.

### Changed

- **Settings → Queues' "Path as seen by the *arr" field moved to sit directly below Local
  path** — the two paths describe the same files from two different containers' mount views,
  and are meant to be read (and set) as a pair. It also gained a help tooltip explaining the
  namespace split, how to find the right value from the *arr's own Queue/History path, and
  what silently degrades when it's wrong.

### Fixed

- **A fully cleaned-up release's spent archive volumes no longer orphan in the Files page
  forever.** A rar'd release that ran the whole pipeline — verified, extracted, had its spent
  volumes removed, imported, and was cleaned up locally — used to leave its volumes behind as
  permanent grey "Extracted" rows with no parent directory and no delete affordance, because the
  exemption that keeps a spent volume from showing a false "Missing" countdown never expired
  once the release itself was gone too. It now lapses the moment the release's own row leaves
  both trees, the same way an ordinary vanished item does; existing orphans from before this fix
  clean themselves up within a scan pass or two, with no manual reset needed.
- **A transient seedbox failure during an *arr-tracked `move` queue's deferred source delete no
  longer strands the remote copy permanently.** The delete used to fire exactly once, on the
  confirmed *arr import; a failed attempt (an SSH hiccup, say) was never retried, and cleanup
  removed the local copy anyway — leaving a row with only a remote copy and, until now, no
  Delete affordance in the UI to fall back on. It now retries every pass (with backoff, and a
  bounded pause rather than an error every ~60s while a seedbox stays down), cleanup withholds
  until the source delete actually clears, a row already stranded before this fix self-heals on
  the first pass after upgrade with no manual action, and the Files-page Delete action is now
  offered for a row whose only remaining copy is remote.
- **A support bundle's settings dump no longer carries archive extract passwords verbatim.**
  Found reviewing the first real bundle: `postprocess.extract_passwords` was exported as-is —
  they're user secrets like any other. The bundle's copy of that setting now carries only
  `extract_passwords_count`; the real `/api/settings/postprocess` response is unaffected.
- **A support bundle's per-*arr-instance log budget (~20 MB) is now enforced across the whole
  instance, not reset for every file in it.** One Sonarr with 53 debug files produced a 54 MB
  (uncompressed) folder in the first real bundle, because the budget was applied per file with
  no running total. Files are now fetched newest-first (current file, then rotations oldest
  last) against one running total per instance; once it's exhausted, fetching stops and a
  `TRUNCATED.txt` names how many files were skipped.
- **One *arr log file failing to fetch no longer reads as the whole instance failing.** The
  first real bundle hit a 404 on a custom-script log the *arr lists but serves from a different
  endpoint, and the resulting `FETCH-FAILED.txt` sat beside 50+ log files that fetched fine — an
  instance-level marker for a single-file problem. An individual file's own fetch failure now
  writes a narrower `<filename>.FETCH-ERROR.txt` beside the files that did fetch;
  `FETCH-FAILED.txt` is reserved for the instance itself being unreachable, its key failing to
  decrypt, or its listing request failing outright.

### Security
### Deprecated
### Removed

## [0.2.1] — 2026-08-16

### Added

- **Browse dialog for Settings → Queues' path fields** (GitHub issue #4). `Remote path`,
  `Local path`, and `Final destination` each gain a `Browse…` button that opens a directory
  picker over the relevant filesystem — the container's own local tree, or the seedbox over the
  already-pooled SSH connection — rather than requiring the path to be typed blind. Two new
  read-only endpoints, `GET /api/browse/local` and `GET /api/browse/remote`: a path that doesn't
  exist, isn't a directory, or can't be read walks up to the nearest listable ancestor instead
  of erroring, so a half-typed field still opens somewhere useful. Directories only; no create-
  directory affordance. Not offered on `Path as seen by the *arr` (describes the *arr's own
  host's view, which neither side here can list) or `Key path` (a file, not a directory).
- **Settings → Queues now validates a queue's paths at save time.** `Local path` and (when set)
  `Final destination` must be real, readable directories on the container's own filesystem, or
  the save is rejected with a specific reason — catching a typo immediately instead of it
  surfacing hours later as a WARNING log line the next time auto-queue's mount gate silently
  refused to act. `Remote path` gets the same check, best-effort: only a reachable seedbox that
  clearly reports the directory missing blocks the save — an unconfigured or unreachable host,
  or one whose credentials need re-entry, never does.
- **A gated or recovered auto-queue mount gate is now recorded in the audit trail**, not only
  logged. Once per gating episode (not once per scan pass), and once when it recovers — visible
  on the History page's Events section, the same place every other remote-delete gate/outcome
  already shows up.

### Changed
### Fixed

- **`gone`-commit `REMOVED_BOTH` resurrection fix (`4ecf5dc`, 2026-08-16) missed its changelog
  entry at the time.** The *arr poller's `gone` commit no longer republishes a WebSocket delta
  for a row that has already reached `REMOVED_BOTH` — it used to resurrect a dead, un-actionable
  row on every connected client's Files page whenever a hand-deleted item's *arr queue record
  was later removed. See `docs/decisions.md` (2026-08-16) for the full mechanism.

### Security
### Deprecated
### Removed

## [0.2.0] — 2026-08-16

### Added

- **Optional Sonarr/Radarr integration** (`docs/arr-integration-spec.md`), off at every level by
  default. Bind a queue to a Sonarr or Radarr instance (new Settings → Integrations tab: instance
  CRUD, write-only API key, a Test button) and lftpweb watches that instance's own download queue
  for a matching release, marks the Files row with an *arr icon once found, and — only after the
  *arr has *fully* confirmed import across two consecutive checks, never on an ambiguous signal —
  can optionally clean up the local copy (a new per-queue "Delete when imported" toggle). The icon
  is the real Sonarr/Radarr logo, multi-faceted: the bare logo while a release is being watched,
  a green ✓ once imported, a red mark if a release left the *arr's queue without ever importing
  (independently filterable, since that state usually needs a look), and the existing
  removal-grace countdown chip reads
  "Processed · Xm" instead of "Missing · Xm" for a row this feature cleaned up itself. A new
  "*arr-tracked" filter facet covers every tracked row at once. Built across three phases
  (backend foundation, notify + cleanup, this UI pass); see `DESIGN.md` §16 and
  `docs/arr-integration-spec.md` for the full design.

- **Transfers rows collapse to one line, with an expand panel for the rest of the story.**
  Real-use feedback: the row had grown a queue position, file count, percent, live rate, ETA,
  allocated rate, elapsed time, average speed, queued wait, and a post-processing note, all
  inline — a wall of numbers rather than a scannable list. Each row now shows just name / queue /
  state / one live number (progress + current speed while downloading, final size once
  terminal); a chevron expands a detail panel with three groups: **Transfer** (every figure the
  row used to show, plus the failed-job error/output block), **Processing** (verify/extract/
  remote-delete timestamps, enriched on expand by the pipeline's own recorded event messages —
  new bounded `GET /api/items/{id}/events`), and ***arr** (instance name, status, and timestamp,
  reusing the Files page's own icon/vocabulary — hidden entirely on a queue with no bound
  instance). A new "Dismiss all" control at the top of the page clears every dismissable
  (terminal, not-yet-dismissed) row in one server-side call (`POST /api/jobs/dismiss-all`),
  alongside the existing failed-only "Clear all failed".

- **Transfers rows show when a terminal job completed, and the list sorts by it.** Real-use
  follow-on to the single-line row pass above: a succeeded/failed/cancelled row now carries a
  compact "3m ago"-style reading next to its state chip (exact timestamp on hover, and as a new
  "Completed" field in the expand panel's Transfer group). Active rows (running, then queued)
  still sort first in scheduler order exactly as before; terminal rows below them now sort
  newest-completed-first, replacing the previous implicit order (the same `rank`/`queued_at`
  scheduler order active rows use, which said nothing about when a finished job actually
  finished).

- **Transfers rows are now grouped by queue, each group collapsible and remembered.** Real-use
  follow-on to the two passes above: with more than one active queue, a per-row queue tag on
  every line made the page busy. Rows now sit under one collapsible header per queue (ordered by
  queue name, click anywhere on the header to toggle), and the header carries what the row tag
  used to: the queue name, job counts by outcome (active / queued / succeeded / failed / stopped
  — zero counts omitted), the group's total size (summed `bytes_done`), and its combined current
  rate while anything in it is downloading. Collapse state persists per queue in `localStorage`
  (default expanded), including for a queue that temporarily has no visible jobs — its
  preference is still there when it returns. "Dismiss all" is unchanged, still global at the top
  of the page.

- **History's jobs section now groups by queue the same way, collapsible and remembered
  separately from Transfers.** Same single-click-anywhere header, same default-expanded
  `localStorage` persistence, but under its own storage key — collapsing a queue on one page
  never collapses it on the other. Because History's job list is paginated, the header's outcome
  counts (succeeded / failed / cancelled) and total size are computed **server-side** over the
  whole filtered set, not just the currently-loaded page, so they stay correct regardless of how
  many rows are actually loaded — a new `queue_summaries` block riding alongside `GET
  /api/history/jobs`'s existing response, honoring the same queue/state/error-class/date filters
  as the list itself.

- **A `:dev` image now identifies itself in the nav, so a test instance is never mistaken for a
  release.** `docker/Dockerfile`'s `runtime` stage bakes the commit SHA and a build channel
  (`dev` / `release`) at image-build time — the container has no git tree to ask at runtime —
  and `/api/health` now carries them (`build_sha`, `build_channel`, both `null` when unbaked).
  The bottom-left version readout renders `DEV: v0.1.1 · <short-sha>` in amber for a dev build,
  linking to the commit on GitHub instead of the release tag; a release build (or anything run
  without baked args — local `uv run`, the compose dev stack) renders exactly as before.

- **Transfers and History job rows now carry a Sonarr/Radarr brand-logo chip with a status
  overlay**, distinct from the Files page's own generic *arr mark. The collapsed Transfers row
  and each History job row show the real Sonarr (blue) or Radarr (gold) logo — recognition at a
  glance was the point, per the user's own decision — with a small green check once the *arr has
  processed the item (`imported`/`cleaned`), a small red dot once a release left the *arr's queue
  without importing (`gone`), the logo alone while still mid-flight (`detected`/`notified`), and
  no chip at all for an item that isn't *arr-tracked. An unrecognized/future instance `kind`
  falls back to a text chip of the instance name in the same status colors, so a tracked item
  never renders nothing just because a logo is missing. `GET /api/history/jobs` rows gained
  `arr_status`/`arr_status_at`/`arr_instance_name`/`arr_instance_kind` (the same
  `path_queue.arr_instance_id -> arr_instance` join `core/queue.py.list_jobs()` already used for
  the Transfers panel); `JobOut` gained `arr_instance_kind` alongside its existing
  `arr_instance_name`. Logo path data copied from the simple-icons dataset (CC0), itself sourced
  from Sonarr's/Radarr's own repositories — see `NOTICE`.

- **The Files page delete dialog gained an independent Source (seedbox) scope — the first
  manual remote-delete path in the app.** Two checkboxes, Delete local copy and Delete source,
  can be ticked independently (at least one is required); the Source checkbox only appears when
  a remote copy actually exists. Defaults follow the queue's sync mode: both checked for `move`
  (the queue is already configured to have lftpweb delete the source itself, so finishing that
  by hand for a stuck/deferred item is the expected action), source left unchecked for `copy`
  with a warning if checked anyway — a `copy` queue's remote path isn't required to be a
  hardlink pickup directory, so deleting the source there can destroy a seed (DESIGN.md §7.1).
  A combined request runs local's existing stop-then-delete first; a source-only request
  refuses (409) rather than stopping a live transfer itself. `POST /api/items/{id}/delete`
  reuses `RemoteConnectionPool.delete_path` (never a second SSH-delete implementation) and
  writes the same `remote_delete`/`remote_delete_failed` events the automatic `move`-mode ladder
  writes, tagged "manual" so History can tell the two apart; it's idempotent against a remote
  copy that's already gone (including one the ladder deleted itself, or a mid-ladder deferred
  item — a source-only delete on one of those simply completes the ladder early) and marks a
  source-only success `auto_queue_suppressed` (migration 020, `suppressed_reason =
  'deleted_source'`) so a release that later reappears under the same path isn't silently
  auto-queued right back. This closes the gap `sync` mode would otherwise have existed to cover
  — see `DESIGN.md` §7's own note, added alongside the move-mode delete ladder.

### Changed

- **`move`-mode remote delete is now the last gate on a ladder, not the second step.**
  Previously a `move` queue deleted the seedbox copy right after verification, *before*
  extraction ran — so an extraction failure (or an *arr that never actually imports) could
  discover a problem after the only other copy was already gone. The source is now deleted
  only once every applicable rung has passed, in order: completeness (unchanged), verification
  (`CORRUPT` still vetoes at every rung; `SKIPPED` still passes, unchanged), extraction (an
  archive release must have extracted successfully — a failure now *withholds* the delete
  instead of it having already happened), and, only for an item the optional Sonarr/Radarr
  integration is already tracking, confirmed *arr import. Every deferral writes a
  `remote_delete_deferred` event naming the rung it's waiting on. There is no timeout and no
  automatic fallback — a withheld or deferred item keeps its source on both sides until the
  user acts (fix the failing step and let it re-run, or the manual-delete dialog — a follow-on
  task), by design.
  This is strictly in the later/safer direction for every existing install; not a new setting.
  See `docs/decisions.md` and `prompts/done/2026-08-16-move-delete-gate-ladder.md`.
- **Job speed and per-file speed now sample on one shared 5-second cadence, instead of two that
  drifted apart.** Watching a live transfer showed a one-file directory reporting two different
  speeds at once (46 vs. 40 MB/s) because job-level speed sampled roughly every second while
  per-file speed sampled every third tick, each smoothed independently. Both now sample every 5th
  tick of the underlying 1-second loop, which itself is unchanged — admission, reaping, and
  Stop/Cancel still act within about a second. The one visible side effect: a freshly started
  job's speed reads 0 for a little longer (up to ~10s, was ~2s) before its first real sample.

- **The SPA fallback route's path guard was recast so CodeQL recognizes the containment barrier**
  (post-v0.1.1 follow-up to audit item S1). No behavior change — the same requests are admitted
  and refused as before; the guard is now expressed in the shape the analyzer models, so the
  fixed alert stays fixed instead of reopening on every scan.

### Fixed

- **Fixed *arr import detection against a real Sonarr run.** The Sonarr/Radarr v3 API returns
  history `eventType` in response bodies as a camelCase **string** (`"downloadFolderImported"`),
  not the numeric code the integration was built against — the numeric codes are only meaningful
  as query-parameter values. Two releases that were genuinely matched, transferred, and imported
  on the first live run were misclassified `gone` as a result. Import detection now matches the
  string form, with the legacy numeric code tolerated as a fallback so no *arr version regresses.
- **Auto-queue no longer grabs a SABnzbd `_UNPACK_`/`_FAILED_` staging directory on the remote.**
  The user's seedbox stages an in-progress unpack under `_UNPACK_<name>` before renaming it to
  the release's final name; these still show up as ordinary items in the Files tree (visible on
  purpose — "show it, don't grab it"), but are now excluded from auto-queue eligibility
  regardless of state or matching patterns. Manual queueing is unaffected.
- **Verification no longer reports `CORRUPT` for a release extracted upstream.** A release rar'd
  at origin but unpacked by the seedbox itself (rars deleted, `.sfv` kept) arrives locally as e.g.
  `movie.mkv` + `movie.sfv`, with the sidecar listing rar volumes that were never local to begin
  with. Every referenced entry read as "missing," and on a `move` queue this permanently withheld
  the remote delete. Verification now reads "every sidecar-referenced file absent, with other
  real content present" as `SKIPPED` — no evidence either way, the same trust level a
  sidecar-less release already gets — while any referenced file actually present (including a
  half-deleted archive set) still reports `CORRUPT` exactly as before, and a sidecar with no other
  content at all still reports `CORRUPT` too.
- **An *arr-cleaned item no longer vanishes from the Files page the instant cleanup runs.** It
  now rides the existing ~10-minute removal grace as "Processed · Xm", exactly as the spec always
  promised, before leaving through the normal `REMOVED_LOCAL`/`REMOVED_BOTH` transition. Two
  stacked bugs, both in `core/engine.py._persist`: the row's own `auto_queue_suppressed = 1`
  (set deliberately, to keep a copy-mode queue from re-grabbing the still-present remote copy)
  was also excluding it from the scan machinery that starts the grace clock at all; and a
  verify-skipped `move`-mode item's `LOCAL_ONLY` resting state fell straight to an instant,
  grace-free removal once unprotected. Fixed narrowly — `core/mount_sentinel.py` itself is
  untouched — so a genuinely externally-vanished item still behaves exactly as before.
- **The *arr success check no longer disappears the moment cleanup runs.** With "Delete when
  imported" on, `imported` is a seconds-long transient — cleanup runs on the very next poller
  beat — so the green ✓ flashed and was immediately replaced by the `cleaned` presentation
  (the *arr mark plus the "Processed · Xm" countdown, no check), which meant the success
  indicator effectively never got seen on a real run. `cleaned` now renders the same green-✓
  icon variant as `imported`, alongside the existing countdown chip; the hover text still
  reads "imported" vs. "imported and cleaned up locally" so the two states stay tellable apart.
- **The Transfers expand panel now shows total bytes transferred, not just elapsed time and
  average speed.** Real-use feedback: a terminal job's Transfer group showed "Elapsed" and
  "Average speed" as two separate figures, but never the reading a user actually wants — "14.8 GB
  in 6m 12s (40 MB/s avg)." A terminal job's `Elapsed` and `Average speed` fields now collapse
  into one `Transferred` field composing exactly that sentence; a still-running job's fields are
  unchanged.
- **The Files page's *arr indicator now renders the same real Sonarr/Radarr brand-logo chip as
  Transfers and History, instead of its own generic mark.** User feedback: the real logos shipped
  on Transfers/History rows the same day, but the Files tree still showed the older generic *arr
  mark — one visual language everywhere was the point. The Files tree's *arr column now renders
  `ArrRowChip` (`LifecycleIcons.tsx`), resolving each row's bound instance `kind` the same way it
  already resolved the instance name (`FilesPage.tsx`, from `path_queue.arr_instance_id` against
  `GET /api/settings/arr`), with the existing `ArrTextChip` fallback for an unrecognized/future
  `kind`. Status colors now unify on the chip's own mapping too — `gone` reads a **red** dot on
  Files, same as Transfers/History, replacing the old amber ⚠; the "Processed · Xm" countdown
  chip, filters, and the removal-grace machinery are unchanged. The older generic mark (`ArrIcon`)
  is unchanged and still renders in the Transfers/History job-detail drawer's own "*arr" section,
  the one remaining place it's used.

### Security

### Deprecated

### Removed

## [0.1.1] — 2026-08-15

A maintenance release driven entirely by a **full post-`v0.1.0` audit of the codebase performed
by Claude (Fable)** — a sweep for security flaws, a look at how the code is partitioned, and a
fresh pass over the settings and gating rules. The audit and its full findings are recorded in
[`docs/audit-v0.1.0.md`](docs/audit-v0.1.0.md); this release closes the fixable ones. Four were
security fixes (one of them a real unauthenticated file-read) and three were internal,
behaviour-preserving refactors that make the largest files far cheaper to work in. The audit
items that remain — a `move`-mode delete-ordering design question, a missing connection-limit
setting, and two deeper module splits — are named in `docs/audit-v0.1.0.md` and tracked in
`prompts/open-issues.md` for a later, reviewed session rather than rushed into this release.

### Security

- **Fixed an unauthenticated arbitrary-file read in the single-page-app fallback route** (audit
  S1). The catch-all route that serves the built UI joined a request-controlled, percent-decoded
  path onto the static directory with no containment check, and sat outside the API auth gate —
  so a crafted `..%2f…`-style request could read any file the container's user could, including
  the credential-encryption key and the database. It now resolves the path and confirms it stays
  under the static root before serving anything, falling back to the app shell on any escape.
  Verified exploitable before the fix and blocked after, with regression tests pinning both.
- **Archive extraction now refuses to publish a member that escapes its staging directory**
  (audit S2). A malicious archive whose symlink member points outside the extraction root is
  caught before the extracted release is merged into the directory an importer watches, rather
  than relying solely on the archiver's own traversal defences.
- **Capped the length of every credential and free-text API input, and bounded port numbers**
  (audit S3) — closing a request-body denial-of-service on the unauthenticated login path, which
  would otherwise argon2-hash an arbitrarily large password. The caps are generous enough that no
  legitimate value is ever rejected.
- **Added baseline security response headers** — `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: SAMEORIGIN`, and `Referrer-Policy: same-origin` — on every response (audit
  S4). A Content-Security-Policy and HSTS were deliberately left out for now: both need
  browser verification before they can be enabled without risking the UI or plain-HTTP LAN use.

### Changed

- **Internal code partitioning, with no change to behaviour** (audit P1–P3). Three of the
  largest files were split along the seams they already had, so a localized change no longer
  means reading thousands of lines: `api/settings.py` (1068 lines) became three per-resource
  routers; `core/local_delete.py` (1649 lines) split into focused `retention`, `archive_cleanup`,
  and `reset` modules with its import surface preserved; and `FileTree.tsx`'s pure logic moved to
  a standalone `lib/fileTree.ts` (the component dropped from 2267 to ~1765 lines). Each was
  verified behaviour-preserving — the `/api/settings` route list is byte-identical, and the full
  test suite (1063 backend, 266 frontend) passes unchanged.

## [0.1.0] — 2026-08-14

**The first tagged release, and the first beta.** Everything below is the whole of the
project's development history to date — `DESIGN.md` §13 build phases 1–9, all nine built, plus
four sessions of fixes driven by real use against a real seedbox. Subsequent releases will be
very much smaller than this one.

Read `README.md`'s "Known gaps" alongside this list: several entries below ship with
deliberate, documented limitations. There is **no upgrade path from any earlier state**, since
no earlier release exists; the database schema may still change between beta releases.

### Added

Entries from the post-phase-9 session on 2026-08-12 are marked *(2026-08-12)*; later sessions
are dated likewise.

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
  evidence. **Correction, same day:** the phrase "a scan keeps filtering whatever prefix is
  physically in use" describes what shipped first, not what shipped last — see the "Changed"
  entry below for the reversal that landed the same day.
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
- **"Folder prefix during transfer": the reconciler now *maps* a still-prefixed directory onto
  its real name instead of filtering it out of the local scan entirely** *(2026-08-14, same
  day)* — reversing the original mechanism while keeping its goal (importers still cannot see
  in-flight content; that was always the dot-prefixed on-disk name's job, never the scanner's).
  Filtering made lftpweb's own reconciler blind to its own in-flight working directory, which
  turned out to be the root cause behind three defects fixed individually that morning (a
  mirror's child rows flipping `PARTIAL`↔`REMOTE_ONLY`, a delete refusing a stopped transfer's
  leftovers, and `bytes_start` reading 0 on resume) plus one that stayed open:
  `.downloading-Release/a.mkv` on disk is now reported as `Release/a.mkv`, matching its remote
  counterpart directly — the same "physical detail mapped back to a logical one" `scan_local`
  already does for an in-flight `*.lftp` file, one level up. A stale prefixed directory sitting
  beside a since-completed release (the same coexistence the user hit live) is resolved rather
  than merged or dropped: the real, unprefixed directory always wins the shared name, and the
  stale one stays visible under its own literal, still-prefixed name — an ordinary leftover a
  user can find and delete through the normal Files path, not a silent merge of two unrelated
  subtrees. Closes `prompts/open-issues.md`'s "the folder prefix and the settle gate's stuck-item
  recovery don't compose" for real, rather than leaving it to auto-queue's own self-recovery. See
  `docs/decisions.md` for the full design, every consumer checked, and one narrower residual gap
  named rather than silently left: a `.downloading-<name>/` leftover with **no** recorded
  `item.pending_download_prefix` at all (predating this bookkeeping, say) is now visible as an
  ordinary local-only row but not yet deletable through the normal path.
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

- **The Files page still offered "Queue" on a row with no remote copy to fetch** *(2026-08-14)*.
  Reported live: after a `move`-mode release completed and its remote copy was deleted, the
  parent folder and every removed child still showed a **Queue** button — clicking it would
  spawn a job against a remote path that no longer exists. `rowAction` used to special-case only
  `state === 'LOCAL_ONLY'`; it now gates on `hasRemoteCopy(node)` (`remote_size != null`)
  generally, so a `REMOVED_BOTH` child and a move-mode parent whose remote this codebase deleted
  on purpose are treated the same way `LOCAL_ONLY` already was. The button is hidden, not
  disabled with an explanation — there is nothing a "Queue" click could ever mean for these rows,
  unlike the transient, user-changeable reasons `cd74f91` added tooltips for. "Re-Download" for a
  row whose remote copy has since come back is unaffected — its own branch already required a
  remote copy and now simply sits after the new, more general gate. Bulk "Queue selected" already
  filtered through the same `rowAction` rule and needed no separate fix.
- **A cleaned-up archive volume ran the ten-minute removal-grace countdown instead of resting
  as `Extracted`** *(2026-08-14)*. Live evidence: nine seconds after extraction succeeded,
  archive cleanup removed twelve rar volumes, and the very next scan started
  `first_missing_at`'s grace clock on all twelve — an alarming `Missing · 9m` countdown for
  files this codebase deleted on purpose, then resolving to `REMOVED_BOTH` and vanishing. The
  `deleted_archive` table (migration 010) already recorded exactly which paths were removed
  this way and was already folded into completeness accounting
  (`core/engine.py.build_scan_counts_predicate`), but nothing consulted it when deciding
  whether a row was *missing*. Also closes the open-issues entry "A cleaned-up archive rests in
  a different state depending on sync mode": a `copy` queue's surviving remote volume already
  read `EXCLUDED` correctly; a `move` queue's (whose remote copy is deleted before extraction
  even runs) fell into `core/engine.py._persist`'s "vanished from both trees" sweep, which had
  no way to tell "we deleted this" from "this just vanished." Both sync modes now resolve to
  the identical `EXCLUDED` reading, never through the grace clock — no new `state` value, and
  `EXCLUDED` is not overloaded with a new meaning, since "excluded from completeness accounting,
  for a real reason" is already what it means for a pattern-matched file. The Files page's state
  chip now shows a greyed-out **`Extracted`** for these rows (same word as the parent release's
  emerald `Extracted`, a duller weight, "consumed, and this is why" rather than an alarm) via a
  new `deleted_archive_at` field on the wire, the same display-projection pattern the R/L/V/E
  lifecycle icons already established — never a new enum value. A genuinely missing row (no
  `deleted_archive` entry) is unaffected and still runs the countdown as before.
- **The whole-queue Reset preview undercounted what Reset would actually do, then reset the
  larger set anyway** *(2026-08-14)*. Reported live: Pattern `*` showed 2 items; **All** showed
  *none*, then reset those same 2 items when confirmed. The All scope's preview read the
  published Files tree (`nodes`), which deliberately stops showing a row once it reaches a
  terminal removed state with nothing left in either tree — correct for the Files page, wrong for
  "everything this queue tracks." The execute path (`reset_queue`) always enumerated the `item`
  table directly, so an already-removed-but-still-tracked row was invisible in the preview and
  reset regardless. Fixed by giving the All scope a real `reset-all-preview` endpoint that reads
  the identical query `reset_queue` executes against (`core/local_delete.py.
  reset_queue_targets`), the same share-one-query invariant the Pattern scope already had. The
  preview now also states how many of its rows are already-removed items the Files page no
  longer shows. The Selected scope is unchanged — it can only ever offer rows the user can see,
  which is correct.
- **Archive cleanup no longer deletes a release's archives after a failed verification**
  *(2026-08-14)*. Found live: a release whose `.sfv` no longer matched its files reported
  `CORRUPT`, extraction still succeeded, and cleanup then removed all twelve rar volumes (2.2 GB)
  — destroying the only re-extractable source for an item the pipeline had just declared corrupt,
  on a `move` queue where the remote copy is the only other one. Cleanup was gated on extraction
  succeeding and never saw the verify result. It now withholds on `CORRUPT` and writes an
  `archive_cleanup_withheld` event saying so. Deliberately one notch looser than the remote
  delete's gate — `SKIPPED`/never-ran still cleans up, since requiring positive verification would
  silently stop cleanup working for the many releases that ship no sidecar.
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
- **A `move`-mode remote delete was withheld on `SKIPPED` verification, not only on `CORRUPT`**
  *(2026-08-14, confirmed live: two `ar-tv` releases with no `.sfv`/`.md5` sidecar downloaded
  correctly and had their remote copies withheld, while a sidecar-bearing release in the same
  log deleted normally)*. The rule was "verification must have run and passed"; the correct
  rule, and the one now implemented, is "verification must not have failed" — `SKIPPED` ("no
  `.sfv`/`.md5` sidecar found and hash-on-disk verification is disabled") is not a failure, only
  `CORRUPT` is. **Behaviour change an existing install will notice: a `move` queue's releases
  with no checksum sidecar will now have their remote copy deleted, where previously it was kept
  indefinitely.** This is safe now in a way it would not have been when the stricter rule was
  written (phase 5): by the time this gate runs, the item has already cleared lftp's own exit-0
  check, the settle gate, and (added earlier the same day) a filesystem completeness check — no
  leftover `.lftp`/temp files, local bytes at least matching the remote total — which closes the
  truncation risk the strict gate existed to catch. The residual risk is content that is wrong
  despite arriving complete, undetectable without a checksum to compare against; accepted, not
  hidden — a delete backed only by this completeness evidence records a distinct event message
  ("deleted remote copy ... on completeness evidence alone") rather than reading identically to
  a checksum-verified one, at `warning` level so it stands out in History. Also fixed in the same
  change: the download-prefix rename's own event message used to hardcode "downloaded, verified,
  and extracted" regardless of what verify/extract actually returned — found live, the same two
  `ar-tv` items got that exact sentence while their own `verify`/`extract` events recorded
  `SKIPPED`/"no archives found" in the same second. It now names the real `verify_state`/
  `extract_state`.

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
