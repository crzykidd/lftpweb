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
blocked. `dev` sits ahead of
`main` by design — protection means `main` only advances via a green PR, so `dev` naturally
runs ahead between release-prep passes; check the actual commit count
(`git rev-list --left-right --count main...dev`) rather than trusting a specific number here,
since it moves. Check `git rev-list --left-right --count origin/dev...dev` too rather than
assuming everything local is pushed.

Day-to-day work happens on `dev`, pushed freely. `main` only ever moves via a PR from `dev`
with every required check green — never a direct push, never `--force`.

**The first release was cut on 2026-08-14: `v0.1.0`, a beta** (PR #3 → `main`, tag `v0.1.0`,
release notes = the `[0.1.0]` CHANGELOG section verbatim). `release-prep-and-cut`'s two-phase
flow has now been exercised end to end, so the next release is a repeat of a known path rather
than a first. Two things that bit the first time and will bit again:

- **A PR body caps at 65,536 characters; a release body at 125,000.** The `[0.1.0]` section is
  99,959 — it fit the release verbatim but *not* the PR, whose body is a generated list of every
  entry's headline plus a link. Later releases will be far smaller, so this is likely a one-off.
- **`/release-prep` is forbidden from touching `DESIGN.md`**, which left four now-false
  pre-release statements in it. Corrected afterwards on `dev` (`b06cafe`). If a future release
  changes something `DESIGN.md` asserts, that is a separate follow-up commit by design.

## Where we are

### 🚀 v0.2.0 released 2026-08-16 — the Sonarr/Radarr release

PR #6 (`dev` → `main`, merged `013bf7a`), tag `v0.2.0`, release notes = the `[0.2.0]`
CHANGELOG section verbatim; `:latest`/`:0.2.0`/`:0` images published on the release event.
The headline is the **optional *arr integration** plus the **move-delete gate ladder**
(issue #2/G1 closed: source deletes only after the last enabled check — completeness →
verify → extract → *arr import when tracked) and the **manual Local/Source delete dialog**
(first manual remote-delete in the API). The whole pipeline was verified live before the
cut — a 46-file Sonarr season pack and a single-file Radarr remux each ran
match → notify → two-pass import confirm → source delete → cleanup → grace countdown, in
order, on the first attempt. The build-run table below (rows A–R) is the full item log.
Tests at release: **1186 backend / 378 frontend, 0 skipped.** The per-minor changelog
archive was deliberately NOT performed (user call: leave 0.1.x in `CHANGELOG.md`).

### *arr integration build run (2026-08-15, unattended) — LIVE PROGRESS LOG

> `docs/arr-integration-spec.md` (approved 2026-08-15) specs a Sonarr/Radarr integration in
> three handoff prompts: `prompts/2026-08-15-arr-integration-{backend,notify-cleanup,ui-and-docs}.md`.
> The icon reads a bound instance's queue to prove a Files-page item is *arr-driven, watches it
> through import, and optionally cleans up the local copy once the *arr is done with it. This
> log tracks each phase as it lands, same convention as the 2026-08-14 overnight audit run below.

| Phase | What | Status |
|---|---|---|
| A — backend foundation | Migration 018 (`arr_instance` + 3 `path_queue` cols + 3 `item` cols); `core/arrclient.py` (httpx, one class, `kind` switch); `core/arrsync.py` poller (matching + import/gone detection, two-pass quiescence guard, per-instance backoff); `ArrSettings`; `api/settings_arr.py` CRUD + Test; `api/settings_queues.py` extended; `arr_status`/`arr_status_at` joined into `core/itemview.py`'s one projection. No notify, no cleanup, no frontend — those are phases B/C. | ✅ done, this commit |
| B — notify + cleanup | `core/arrnotify.py` (new, shared notify implementation); `PostprocessPipeline._maybe_notify_arr` (primary push, tail of a fully-successful pipeline run); `ArrSyncScheduler._maybe_retry_notify` (bounded retry) + `._maybe_cleanup` (withheld gates, suppression-first, bytes removed without touching `item.state`) | ✅ done, this commit |
| C — UI | Integrations tab (instance CRUD + Test), Queues additions (*arr instance dropdown, delete-when-imported, visible-path), Files icon (own resizable column, multi-faceted) + "*arr-tracked"/"gone" filters, DESIGN.md §16 + README + CHANGELOG + Concepts doc section | ✅ done, this commit — **unviewed, no browser in this environment** |
| D — live-testing fixes | First real Sonarr run (`v0.1.1`+arr build) against the user's seedbox: `eventType` fixed to match the *arr v3 wire format (string in response bodies, `core/arrclient.py`/`core/arrsync.py`, `docs/decisions.md` 2026-08-15); auto-queue now excludes remote `_UNPACK_`/`_FAILED_` SAB staging dirs by eligibility, not visibility ("show it, don't grab it," same date) | ✅ done, `prompts/done/2026-08-15-arr-eventtype-and-unpack-autoqueue.md` |
| E — live-testing fix, verify | Same live-test session: an upstream-extracted release (rar'd at origin, unpacked+deleted by SABnzbd before reaching local disk) verified `CORRUPT` and wedged its `move` queue's remote delete forever. `core/verify.py` now reads "every sidecar-referenced file absent + other content present" as `SKIPPED`, narrowly — any referenced file present (including a half-deleted set) still stays `CORRUPT`; the broader pipeline-ordering question stays open (`prompts/open-issues.md` #2 / G1) | ✅ done, `prompts/done/2026-08-15-verify-skip-when-sidecar-targets-all-absent.md` |
| F — live-testing fix, Transfers UX | Session follow-on: real-use feedback that a Transfers row had grown too many inline figures (queue position, file count, percent, rate, ETA, allocated, elapsed, average speed, queued wait, post-processing note). Rows now collapse to name/queue/state/one live number, with a chevron expanding a Transfer/Processing/*arr detail panel — the *arr group is the first place `arr_status`/`arr_instance_name` surface outside the Files page, reusing `lib/fileTree.ts`'s existing variant helpers. New bounded `GET /api/items/{id}/events` (item-scoped, capped, newest-first) feeds the Processing group with the pipeline's own recorded event messages on expand. New `POST /api/jobs/dismiss-all` + a "Dismiss all" control at the top of the page (user addition mid-task) | ✅ done, `prompts/done/2026-08-15-transfers-single-line-rows-with-detail.md` |
| G — live-testing fix, cleaned-item grace visibility | First real *arr delete-completed run (`move` queue): a cleaned item vanished from the Files page instantly instead of riding the ~10-minute removal grace as "Processed · Xm". Two stacked bugs, both in `core/engine.py._persist`: `_protected_rel_paths` treated *any* `auto_queue_suppressed = 1` row (cleanup sets this first, before touching disk) as frozen, excluding it from the "vanished from both trees" sweep entirely — `first_missing_at` never started; and even once unprotected, a verify-skipped move-mode item rests at `state == "LOCAL_ONLY"`, which `resolve_absence` has no opinion about, so it fell straight to the instant-`REMOVED_BOTH` fallback with no grace at all. Fixed narrowly: `_protected_rel_paths` now exempts `arr_status = 'cleaned'` rows, and the vanished sweep remaps that one combination (`LOCAL_ONLY` + `arr_status == 'cleaned'`) to `"DOWNLOADED"` before consulting `resolve_absence`, reusing its existing grace machinery unmodified. `core/mount_sentinel.py` itself untouched. | ✅ done, `prompts/done/2026-08-15-cleaned-item-grace-visibility.md` |
| H — live-testing fix, Transfers completed time + sort | Same live-use session, follow-on to phase F's single-line rows: user report that a terminal row didn't show when it finished, and the list didn't sort by it. `lib/transferPanel.ts` gained `completedTimeLabel` (relative value + exact-timestamp title, terminal-only, null for active rows — also threaded into the expand panel's Transfer group as a new "Completed" field) and `sortTransferRows` (active rows keep the scheduler's own order, terminal rows now sort newest-completed-first, replacing the previous implicit `rank`/`queued_at` order for terminal rows, which said nothing about actual completion time). Frontend-only — `JobOut.finished_at` was already on the wire. | ✅ done, `prompts/done/2026-08-15-transfers-completed-time-and-sort.md` |
| I — live-use, Transfers grouped by queue | Same live-use thread, follow-on to phases F/H: per-row queue tags made the page busy with more than one active queue. `lib/transferPanel.ts` gained `groupJobsByQueue` (one collapsible group per queue present, ordered by queue name, within-group order untouched from `sortTransferRows`), `queueGroupSummary`/`formatQueueGroupCounts` (header line: outcome counts — active/queued/succeeded/failed, plus a `stopped` bucket for `cancelled` added beyond the prompt's literal four so counts don't silently undercount a group, `docs/decisions.md` — total `bytes_done`, combined current rate while anything's running), and a `transfers.collapsedQueues` `localStorage` map (`lib/storage.ts`'s existing wrapper, default-expanded exception set, never pruned so a queue that disappears keeps its preference). `TransfersPage.tsx`'s per-row queue tag is gone; a new `GroupHeader` component renders the collapsible line instead. Frontend-only. | ✅ done, `prompts/done/2026-08-16-transfers-group-by-queue.md` |
| J — live-use, History jobs grouped by queue | Same live-use thread, same treatment on the History page's jobs section — but History's `jobs` list is `LIMIT`/`OFFSET` paginated (unlike Transfers' unbounded set), so a client-side sum over the loaded page would misreport a queue's true counts/size the instant more rows match the filter than are loaded. `GET /api/history/jobs` gained a `queue_summaries` block (`HistoryQueueSummaryOut`, `api/history.py._queue_summaries`) — one bounded `GROUP BY item.queue_id, job.state` query honoring the exact same `_jobs_where_clause` filter as the `jobs` list beside it, inlined onto the existing response rather than a second endpoint (`docs/decisions.md`). `lib/transferPanel.ts` gained History-specific variants alongside the Transfers ones: `readHistoryCollapsedQueues`/`writeHistoryCollapsedQueues` (own `history.collapsedQueues` storage key — a queue collapsed on one page never collapses on the other), `historyQueueGroupCounts` (reshapes the server summary onto `QueueGroupCounts` so the existing `formatQueueGroupCounts` renders it unmodified), `groupHistoryJobsByQueue` (flattens jobs into the header+job virtual-row array `HistoryJobsSection.tsx`'s virtualizer already walked, now filtering a collapsed queue's job rows out while keeping its header), and `decrementHistoryQueueSummary` (keeps a cleared row's queue summary in sync locally, mirroring the existing local `jobs`/`total` trim, without a full reload). `HistoryJobsSection.tsx`'s bare queue-name header row became a clickable `QueueGroupHeader` (name + counts + total size). Events section untouched, per the task's own scope. | ✅ done, `prompts/done/2026-08-16-history-jobs-group-collapse.md` |
| K — live-use, unified progress cadence | Same live-use thread: watching a live transfer showed a one-file directory reporting two disagreeing speeds (46 vs. 40 MB/s) at once, because job-level speed sampled ~1 Hz while per-file (child) speed was throttled to every 3rd tick, each independently EMA-smoothed. `core/queue.py.PROGRESS_SAMPLE_TICKS = 5` (replacing `CHILD_PROGRESS_THROTTLE_TICKS = 3`) now gates job-level `ProgressSampler.sample`, the per-tick `item_delta` publish, and `_publish_child_progress` on one shared counter, so all three sample the same ~5s instant. The underlying 1s tick loop (`transfer_tick_s`) is untouched — admission, reaping, and Stop/Cancel still act within ~1s; only the progress-sampling work inside `_sample_and_publish_progress` moved to every 5th call. `DESIGN.md` §4.4 corrected (was ~1 Hz); `docs/decisions.md` has the full mechanism and rejected alternatives. | ✅ done, `prompts/done/2026-08-16-unify-progress-cadence-5s.md` |
| L — live-use, cleaned icon keeps the green check | First live Radarr run with "Delete when imported" on: `imported` is a seconds-long transient (cleanup runs on the next poller beat), so the green ✓ flashed and was immediately replaced by the `cleaned` presentation (mark + "Processed · Xm" countdown, no check) — the success indicator effectively never got seen. `lib/fileTree.ts.arrIconVariant` now maps `cleaned` to the same `'imported'` (green-check) variant; `LifecycleIcons.tsx.ArrIcon` and `TransfersPage.tsx`'s *arr expand-panel group inherit it unmodified since both already switch on the shared helper. Hover text (`arrHoverLabel`) still distinguishes "imported" from "imported and cleaned up locally". | ✅ done, `prompts/done/2026-08-16-cleaned-icon-keeps-green-check.md` |
| M — live-use, Transfers panel shows total transferred | Same live-use thread: the expand panel's Transfer group showed "Elapsed" and "Average speed" as two separate figures but never the reading the user actually wants — "14.8 GB in 6m 12s (40 MB/s avg)". `lib/transferPanel.ts` gained `transferredSummary` (composes `bytes_done` + `elapsedSeconds` + `averageSpeedBps` through the existing `formatBytes`/`formatEta`/`formatRate`, omitting the avg clause under `averageSpeedBps`'s own zero-elapsed guard rather than dividing by zero); `transferGroupFields` now collapses a terminal job's `Elapsed`/`Average speed` fields into one `Transferred` field, while a still-running job's fields are unchanged. Frontend-only. | ✅ done, `prompts/done/2026-08-16-transfer-panel-total-transferred.md` |
| N — user request, dev-build version badge | User request: a `:dev` image should identify itself in the UI so it's never mistaken for a release. `docker/Dockerfile`'s `runtime` stage (only) now accepts `BUILD_SHA`/`BUILD_CHANNEL` build args, baked to `LFTPWEB_BUILD_SHA`/`LFTPWEB_BUILD_CHANNEL` env vars; `.github/workflows/publish.yml` computes a short SHA and `dev`/`release` channel per push and passes both via `build-args:`. `config.Settings` gained the two fields plus a validator folding a baked-but-blank env var back to `None` (Docker always sets the ENV, blank or not). `/api/health` carries them (beyond §12's shape, same precedent as `repo_url` — `docs/decisions.md`). Frontend: new pure `lib/versionBadge.ts` (unit-tested) computes the nav's bottom-left readout — `DEV: v0.1.1 · <sha>` in amber, linking to the commit, for a dev build; exactly today's plain `v0.1.1` (release link or plain text) for every other case, including health not yet loaded. `VersionLink.tsx` only renders what it returns. No CI job renamed; no new dependency. | ✅ done, `prompts/done/2026-08-16-dev-build-version-badge.md` |
| O — user request, real brand-logo chip on Transfers/History row lines | User decision (refined same day): the collapsed Transfers row and each History job row show the **real** Sonarr/Radarr logo (not the Files page's generic *arr mark), in its own brand colour, with the outcome as a small status overlay — green check once processed (`imported`/`cleaned`), red dot once `gone`, logo alone while `detected`/`notified`, no chip at all when `arr_status` is null. `lib/fileTree.ts` gained `arrChipOverlay` (thin wrapper over the existing `arrIconVariant` — "one mapping, consumed everywhere"); `LifecycleIcons.tsx` gained `SonarrLogo`/`RadarrLogo` (real brand SVG path data, sourced from the simple-icons dataset — itself citing Sonarr's/Radarr's own repos — CC0, recorded in `NOTICE`) plus `ArrRowChip`, shared by both pages, with an `ArrTextChip` fallback for an unrecognized/future instance `kind`. Backend: `core/queue.py.list_jobs()` and `api/history.py.list_history_jobs()` both now also join `arr_instance.kind` (`JobOut`/`HistoryJobOut` gained `arr_instance_kind`; `HistoryJobOut` also gained `arr_status`/`arr_status_at`/`arr_instance_name`, which it lacked before this task) — two scalar columns on an already-paginated list, not the phase-6 blob trap. Deliberately red (not the Files icon's amber) for `gone` here — two different specs for two different affordances, noted in-code, not a drift. | ✅ done, `prompts/done/2026-08-16-arr-chip-on-row-lines.md` |
| P — move-delete gate ladder (resolves open issue #2 / audit G1) | User-approved design: a `move` queue's remote delete is now the *last* gate on a four-rung ladder (completeness, verify, extract, and — new — *arr import for a tracked item), not the second step after verify. `core/postprocess.py._maybe_delete_remote` moved to the tail of `_process_item`, gained an `extract_state` parameter, and now defers rather than deletes when the item is already *arr-tracked (`item.arr_status` non-null), recording the handoff in a new `item.remote_delete_pending` column (migration 019 — carries the verify evidence forward). The actual asyncssh delete was factored out into a module-level `perform_remote_delete`, reused by a new `core/arrsync.py.ArrSyncScheduler._maybe_delete_remote_on_import`, which performs the deferred delete the moment `_commit_terminal` confirms `imported` — before that same pass's `arr_delete_completed` cleanup sweep, so "import green → delete source → delete local" holds within one poll. `ArrSyncScheduler` gained `remote_pool`/`host_provider` constructor seams, wired in `main.py` from the same `app.state.engine.pool`/`_host_provider` postprocess already uses. `CORRUPT` still vetoes at every rung, including this new one. DESIGN.md §7/§7.3 updated to describe the ladder and to note `sync` mode's primary use case is now served without building it; `docs/arr-integration-spec.md`'s Cleanup section updated for the new ordering. | ✅ done, `prompts/done/2026-08-16-move-delete-gate-ladder.md` |
| Q — user feedback, Files unifies onto the brand-logo chip | User feedback: the real Sonarr/Radarr logos (phase O) show on Transfers/History rows, but the Files tree still rendered its own older generic *arr mark (`ArrIcon`) — one visual language everywhere was the point. `FileTree.tsx`'s *arr column now renders `ArrRowChip` (same component as Transfers/History); `FilesPage.tsx` resolves each row's bound instance `kind` the same way it already resolved the instance name (`listArrInstances()` keyed by `path_queue.arr_instance_id`), threaded down through `FileTree`/`Row` as a new `arrInstanceKind` prop. Status colors unify on the chip's own mapping — `gone` now reads **red** on Files too, replacing the old amber ⚠; the "Processed · Xm" countdown chip, filters, and removal-grace machinery are untouched. `ArrIcon`/`ArrMarkIcon` were *not* deleted — `TransfersPage.tsx`'s job-detail-drawer "*arr" section still consumes `ArrIcon` directly, the one place the generic mark and its amber `gone` reading remain. `docs/arr-integration-spec.md`'s "UI" section collapsed into one chip-based table covering all three surfaces. | ✅ done, `prompts/done/2026-08-16-files-brand-logo-icons.md` |
| R — the move-delete ladder's follow-on, manual delete gains an independent Source scope | User-approved design, resolving §7's forward note that `sync` mode's primary use case is now fully served without building it. The Files-page delete dialog's confirmation gained a second, independent checkbox (Delete source, alongside the pre-existing Delete local copy) — the first manual remote-delete path in the API. Defaults follow `sync_mode`: both checked for `move`, source unchecked for `copy` (with §7.1's misconfiguration warning shown if checked anyway); the checkbox itself only renders when a remote copy exists. Backend: `POST /api/items/{id}/delete` takes an optional `{local, source}` body (`DeleteItemRequest`, omitted = today's local-only default); a source-only request refuses (409) rather than stopping an active transfer itself, while a combined request lets local's own stop-then-delete satisfy that guard first. `core/postprocess.py.perform_remote_delete` gained a `caller` parameter (`"pipeline"` unchanged, `"manual"` new) so the manual path reuses it rather than a second SSH-delete implementation, and `PostprocessPipeline` gained a public `resolve_host()` around its existing `_host_provider` closure. A source-only success is idempotent against an already-gone remote copy (clears a stale `remote_delete_pending` too — "mid-ladder" simply completes early) and marks the item `auto_queue_suppressed`/`suppressed_reason = 'deleted_source'` (migration 020) so a later reappearance under the same path isn't auto-queued right back; a combined request leaves `delete_local`'s own `'deleted_local'` reason alone. Partial failure (local succeeds, source then fails) is a 200 with `source_deleted: false`/`source_reason` set, not a 409 — the local side effect already happened and can't be undone — and `FileTree.tsx`'s bulk-delete reporting reads those fields back out so a partial failure can't hide inside an otherwise-`fulfilled` promise. DESIGN.md §9.2/§7 updated; `docs/concepts.md`'s suppression table gained the new reason. | ✅ done, `prompts/done/2026-08-16-manual-delete-local-and-remote.md` |

**Phase A verification:** backend lint/format clean, full backend `pytest` green (new tests in
`tests/test_arrclient.py`, `tests/test_arrsync.py`, `tests/test_settings_arr_api.py`,
`tests/test_settings_queues_arr.py`, plus additions to `tests/test_itemview.py`), frontend
untouched and re-verified anyway. The fake-*arr fixture (`tests/fake_arr.py`) runs a real
`uvicorn` server on its own thread — see that file's docstring for why (a `TestClient`-driven
test's synchronous call blocks the event loop a same-loop fake server would need to respond).
Everything defaults off (`arr_instance.enabled = 0`, migration inserts no rows); the eventType/
trackedDownloadState vocabulary in `core/arrclient.py` is flagged unverified against a live
instance, per the spec's own warning.

**Phase B verification:** backend lint/format clean, full backend `pytest` green (new
`tests/test_arr_notify.py` for the primary push and `tests/test_arr_cleanup.py` for the poller's
bounded retry + cleanup, both against the fake-*arr fixture; `tests/fake_arr.py` gained
`FakeArrState.fail_command` so a test can fail only `POST /api/v3/command` without also failing
`/queue` — `fail_all` fails everything, which never reaches a queue's own notify/cleanup pass).
Frontend untouched, re-verified anyway. `core/arrnotify.py` is new: one `notify_arr()` shared by
both callers (postprocess's primary attempt, arrsync's retry) so there is exactly one place that
builds the *arr POST, translates the path, and writes the `arr_notified`/`arr_notify_failed`
event. **Cleanup deliberately never writes `item.state`** — it removes the bytes and leaves the
row exactly as it was, so the existing scan + `core/mount_sentinel.py` absence-grace machinery
discovers the disappearance and carries it to `REMOVED_LOCAL` on its own ~10-minute clock, the
same as `core/postprocess.py._do_move` already does for a staging relocation. This is a
deliberate, spec-driven departure from "just call `core/local_delete.py.delete_local()`" — see
`docs/decisions.md` (2026-08-15) for the full reasoning.

**Phase C verification:** frontend lint/test/build all green (new `arrIconVariant`/`arrHoverLabel`
tests in `components/FileTree.test.ts`, `removalGraceShortLabel`/`removalGraceLabel` "cleaned"
tests in `lib/format.test.ts`, and a new `pages/settings/QueuesTab.test.ts` for the
disabled-with-hint pure predicates `arrDeleteCompletedDisabled`/`nextArrDeleteCompleted`).
Backend untouched, re-verified anyway (ruff check, ruff format --check, full `pytest`). **No
browser exists in this environment** — every screen this phase shipped (Settings → Integrations,
the three new Queues fields, the Files-row icon and its hover card, the two new filter options)
is unviewed until a human opens the app; nothing here should be read as visually confirmed. The
instance name resolution (`FilesPage.tsx` cross-referencing `listQueues()`'s `arr_instance_id`
against a new `listArrInstances()` fetch, since the item wire itself only carries
`arr_status`/`arr_status_at`, never the instance's identity) and the *arr icon's own resizable
column (kept separate from the R/L/V/E cluster) are both recorded in `docs/decisions.md`
(2026-08-15).

**Run complete (2026-08-15, phases A-C).** All three phases landed on `dev`, nothing pushed,
every gate green throughout: backend foundation (phase A), notify + cleanup (phase B), UI + docs
(phase C, this entry). The feature is off at every level on every existing install (no instance
rows, no queue bound, nothing polls) and entirely unviewed in a browser — a human should open
Settings → Integrations, bind a queue, and watch a Files row before trusting the rendered result.

**Phase D verification (live-testing fixes, 2026-08-15):** the two fixes above were diagnosed
read-only against the user's live instance's audit trail (a real Sonarr run), then built and
verified against the fake-*arr fixture and `core/autoqueue.py`'s own test suite —
`tests/test_arrclient.py`/`tests/test_arrsync.py`/`tests/test_arr_cleanup.py` (eventType, string
+ legacy-numeric-tolerance coverage) and `tests/test_autoqueue.py` (`_UNPACK_`/`_FAILED_`
exclusion, plus the renamed-item-becomes-eligible-again case). Backend lint/format clean, full
backend `pytest` green, frontend untouched and re-verified anyway. Already-`gone` associations on
the live instance are terminal by design and stay `gone` — this fix only changes classification
for associations checked from now on; a human still needs to re-bind/re-watch anything that was
misclassified before the fix landed if they want it corrected.

**Phase G verification (live-testing fix, cleaned-item grace visibility, 2026-08-15):**
diagnosed read-only against `GET /api/files`'s own disagreement with the live WS-driven Files
page (REST kept showing the row, `state: LOCAL_ONLY`/`arr_status: "cleaned"`/`first_missing_at:
null`, minutes and several scan passes after cleanup; the WS view had already dropped it) —
exactly the split the publish invariant exists to prevent. Three new tests in
`tests/test_state_persistence.py` pin the mechanism: a direct regression on the
`_protected_rel_paths` SQL exemption, and a full engine-level reproduction (grace starts, ticks,
survives a second pass, then expires into `REMOVED_BOTH` and leaves `engine.models`, mirroring
the existing `test_move_mode_item_that_leaves_both_trees_reaches_removed_both`/`test_a_vanished_
local_only_row_rests_at_removed_both_not_left_alone` tests this fix sits alongside — both still
pass unmodified, proving a *generic* move-mode `LOCAL_ONLY` vanish (no arr cleanup) still lands
on `REMOVED_BOTH` instantly, as designed). Backend lint/format clean, full backend `pytest`
green, frontend untouched and re-verified anyway — the frontend's own `REMOVAL_GRACE_ELIGIBLE_
STATES`/`removalGraceShortLabel` "Processed" wording already shipped in phase C and needed no
change, since the backend now feeds it a state (`DOWNLOADED`) it was already built to expect.

### 🌙 Overnight audit run (started 2026-08-14, unattended) — LIVE PROGRESS LOG

> A post-`v0.1.0` audit landed in `docs/audit-v0.1.0.md` (findings S1–S4, G1–G3, P1–P5). The user
> authorised working the fixable ones unattended overnight, each as its own commit on `dev`
> (nothing pushed), gates green before moving on. **This log is updated in the same commit as each
> item** so that if the session crashes mid-run, the next session knows exactly what shipped and
> what didn't. Deliberately **not** touched unattended: **G1** (move-delete ordering = the design
> call in issue #2), **G2** (connection-limit needs a migration + UI feature), **P4/P5**
> (`queue.py`/`engine.py` splits — deepest stateful code, wants review).

| Item | What | Commit | Status |
|---|---|---|---|
| S1 | SPA catch-all path traversal (unauthenticated file read) fixed | `01efac4` | ✅ done (pre-run) |
| S3+S4 | Input length caps + port bounds; safe security response headers (no CSP/HSTS) | this commit | ✅ done |
| S2 | Post-extraction path-containment check | this commit | ✅ done |
| P2 | Split `api/settings.py` into sub-routers (host/queues/postprocess) | this commit | ✅ done |
| P3 | Split `core/local_delete.py` (retention/archive_cleanup/reset) | this commit | ✅ done |
| P1 | Split `FileTree.tsx`: pure logic → `lib/fileTree.ts` (2267→1765). Component extractions (Row/HoverCard) deferred to a browser-verified session | `0cb294f` | ⏳ partial |

**Run complete (2026-08-14 overnight).** Six commits landed on `dev`, nothing pushed, every gate
green throughout: `01efac4` (S1), `0a4593a` (S3+S4), `65b0618` (S2), `90df1ea` (P2), `d480885`
(P3), `0cb294f` (P1 partial). Backend **1063 tests**, frontend **266 tests**, both lint gates,
`vite build`, and the `/api/settings` route-parity check all pass. **Still open for the user:**
**G1** (move-delete ordering = issue #2 — a design call), **G2** (connection-limit write path —
migration + UI feature), the **rest of P1** (Row/HoverCard component extraction — wants a browser),
and **P4/P5** (`queue.py`/`engine.py` splits — deepest stateful code, wants review). See
`docs/audit-v0.1.0.md` for all of them.

> **Read `prompts/open-issues.md` first.** It carries the reasoning behind three sessions of
> live-testing fixes — including one fix shipped and deliberately reversed the same night, one
> thing the orchestrating session asserted that turned out to be false, and (2026-08-14) three
> confident diagnoses that were all wrong because a second application was writing into the same
> directory. Much of this file's older material predates it.

### State at the end of 2026-08-14 — **`v0.1.0` is released**

**The headline: this project has shipped.** `v0.1.0`, a beta, tagged from `main` on 2026-08-14
and published with `:latest` / `:0.1.0` / `:0` images on `ghcr.io/crzykidd/lftpweb`. `main` is
no longer ~100 commits behind — PR #3 brought everything across. Everything is pushed to
`origin/dev`; the handoff-prompt queue is **empty**.

Tests **1055 backend / 266 frontend**. Migrations run to **017**. Both lint gates, `npm test`,
`npm run build`, the image build, and all three compose files clean.

**Three things from the release session worth carrying forward:**

1. **CodeQL had never analyzed this codebase until PR #3.** Every prior run was against `main`,
   which was stuck at roughly the pre-phase-4 state. It raised 5 high-severity Python alerts —
   4 × `py/path-injection` on the log/backup download endpoints, 1 × weak-hash on
   `core/auth.py`. **All five were verified false positives and dismissed with written
   justifications**: the two download endpoints are guarded by fully anchored filename regexes
   that admit no path separator, and `_hash_token`'s SHA-256 only ever sees 256-bit
   `secrets.token_urlsafe` values (account passwords use argon2id, pinned by
   `test_password_hash_is_argon2id`). Verified by reading all 7 call sites and testing the
   patterns against traversal payloads — not by trusting the docstrings.
2. **That check found a real, if unexploitable, weakness.** Python's `$` also matches just
   before a trailing newline, so `"lftpweb.log\n"` passed a pattern documented as "anchored at
   both ends." Fixed to `\Z` in `b06cafe` with 18 parametrized tests pinning the invariant *at
   the regex*, because the pre-existing HTTP-level traversal tests could pass for the wrong
   reason (a router miss 404s identically to the guard working). **Those two patterns are
   security controls now, not tidiness — five dismissed alerts rest on them.**
3. **The `move`-mode delete gate was wrong, and the user caught it by using the app.** It
   withheld the remote delete unless verification returned `VERIFIED`, so any release without a
   `.sfv`/`.md5` sidecar downloaded fine and never got cleaned up. The rule is **"verification
   must not have *failed*", not "verification must have *run*"** — `SKIPPED` is not a failure.
   Fixed in `6883db3`. The clincher: `core/postprocess.py`'s *rename* gate already used that
   exact rule (`release_ok = verify_state != "CORRUPT" and ...`), so the same item was judged by
   two different standards a few lines apart — and the strict one guarded the *reversible*
   action while the permissive one guarded the irreversible one (publishing to an importer).

**Thirty commits on 2026-08-14** (`a75dc38`…`61f1f1a`) before the release session, which added
`6883db3` (the delete gate), `3281e48` (stale-tracker corrections + issue #2), `7ee11cb` (the
release prep) and `b06cafe` (the `\Z` anchoring). The handoff-prompt queue is **empty** — every
prompt written that day was executed and is in `prompts/done/`.

**A diagnostic method that worked, and should be reached for first.** The live instance's audit
trail is readable over HTTP: `GET /api/history/events` at `https://lftpweb.crzynet.com` answered
"why wasn't the remote deleted?" in one call, because `core/postprocess.py` writes an `event`
row on *every* branch — delete, withheld, failed — with the gating condition in the message.
That beat reading code and it beat theorising. When the user reports "X didn't happen", check
whether the pipeline already recorded why before reasoning about the source.

The day split into two halves. The first (overnight, unattended) worked a queue of prompts written
the night before. The second was the user click-testing a real seedbox and reporting what looked
wrong — **eight more defects, none of which CI could have caught**, every one found by using the
app. Three were in code that had full green tests at the time.

**The single most useful thing to know: five of those eight traced to one assumption** — that an
item's logical path (`local_path + rel_path`) and its physical path
(`local_path + prefix + rel_path`) are the same thing. See the boxed section further down; the
root cause was fixed in `0e93fab` by making `scan_local` **map** a `.downloading-` directory to its
logical name instead of filtering it out, the same thing it already did for `*.lftp` files.

Afternoon commits, on top of the table below:

| What | Commit |
|---|---|
| **Archive cleanup no longer deletes archives after a failed verification** — it destroyed the only re-extractable source for an item just declared corrupt | `f0ef53c` |
| **The All-scope reset preview under-reported** — it read the published tree while the execute path read the `item` table, so it previewed 0 and reset 2 | `95b38e4` |
| **A cleaned-up archive volume rests at `EXCLUDED` with a grey `Extracted` chip**, never a missing-file countdown | `9975223` |
| **`scan_local` maps the download prefix instead of filtering it** — the architectural fix behind five defects; closed the settle-gate stuck-item gap and the false 100% progress | `0e93fab` |
| **Queue is hidden on a row with no remote copy** — a `move`-mode release offered Queue after its remote was deleted | `61f1f1a` |
| **A loose file's verification picked up a sibling release's `.sfv`** — a 4.3 GB mkv verified against rar checksums | `c7fa6e8` |
| **Shutdown mid-transfer** — children terminated concurrently, `stop_grace_period: 60s` | `ab7764b` |
| **A removal-grace countdown** (`Missing · 1m`) so a vanished item says a decision is pending | `3ae2873` |
| **Per-file speed and ETA** inside a mirroring directory, and on the Transfers page | `25bc33c`, `740acc9` |
| **The first six screenshots**, a How-it-works doc page, and the effective-lftp-settings readout | `c4c8572`, `a171510`, `de3d97e` |

| What | Commit |
|---|---|
| **Exit 0 stopped meaning "complete"** — a filesystem completeness check now gates `DOWNLOADED`; `output_tail` is retained on success; a succeeded job stays visible on Transfers instead of vanishing on reap | `0460111` |
| **A local rename failure was classified `REMOTE_GONE`** and never retried — now `LOCAL_FS_ERROR`, and transient | `fe97fd1` |
| **Files-page Speed column**, and the column-resize handles moved to the edge that actually moves | `f728373` |
| **The three reset panels became one** All/Pattern/Selected control with a uniform preview → confirm flow | `4b15fcc` |
| **A ~5s local-only scan while a queue is active**, restoring §5's original two-cadence design | `33db032` |
| **Folder prefix during transfer** (**on** by default as of `d73e221`) — a directory downloads into `.downloading-<name>` and is renamed onto its real name only after post-processing succeeds (`a6b50ae`), so an importer can't grab a partial *or unverified* release | `342f96c` |
| **FieldHelp swept across Settings**; the false "7zz handles rar" label corrected | `8dc3c15` |
| **The retry-backoff-base setting was inert since phase 3a** and now actually applies | `94e2377` |
| **Transfers show elapsed / average speed / queued wait / post-processing state** | `6e6b217` |
| **The Docs section's prose moved to `docs/*.md`** — one source, readable on GitHub and rendered in-app | `b4de50a` |
| The image build needed the repo-root `docs/` copied into the frontend stage | `d1fe8ca` |

**The one assumption that has now caused five separate defects — check this before writing code
that touches a local path.** "Folder prefix during transfer" made an item's **logical** path
(`local_path + rel_path`, what the reconciler matches, what `item_settle` is keyed by, what
patterns evaluate) differ from its **physical** path (`local_path + prefix + rel_path`, where the
bytes actually are). Five things assumed those were the same:

1. Child rows inside a mirroring directory flipped `PARTIAL`↔`REMOTE_ONLY` every scan —
   `_protected_rel_paths` only protected items with a job of their *own*, and a `mirror` job
   belongs to the top-level item.
2. Delete refused to clean up a stopped transfer — it built the logical path, found nothing, and
   said "does not exist".
3. The settle gate's stuck-item recovery can't fire, because the in-flight tree is hidden from
   `scan_local` so the item never computes `DOWNLOADED` (open, self-recovering, see
   `prompts/open-issues.md`).
4. `delete_extracted_archives` recorded an archive's path relative to the physical root instead
   of `rel_path`, silently breaking completeness accounting.
5. `_find_item_id_for_failed_dir` had the same bug for `_FAILED_` staging directories.

`core/local_delete.py._physical_local_root` is the one resolver for "where are this item's bytes
actually" — it handles a nested child by resolving through the top-level ancestor, and falls back
to the logical path when the prefixed one isn't on disk. **Use it; don't write a second one.**
`a6b50ae` widened the window in which the two differ from "during transfer" to "until
post-processing finishes", so the surface is larger now, not smaller.

**The 2026-08-14 lesson, which outranks any individual fix here:** an old `seedsync` container
autostarted after an unrelated update and wrote into the same download directory. It produced
symptoms diagnosed — confidently, in detail, and wrongly — as the settle gate releasing early,
lftp lying about exit 0, and `item.local_size` latching a stale value. All three writeups were
retracted. The tell missed for hours: files were actively being written while
`ps aux | grep [l]ftp` showed **no lftp process in the container**. `prompts/open-issues.md` has
the full account under the `bytes_start` heading. Two real bugs did come out of it (the
`REMOTE_GONE` misclassification, and Sonarr importing a partial release mid-`mirror`), so the
accident was worth having — but read that section before trusting any live-evidence conclusion.

### State at the end of 2026-08-13

**Everything is pushed to `origin/dev` and CI-green.** Tests **489 → 887 backend**, plus a new
**105→118 frontend** suite. Migrations run to **016**. Both lint gates, `npm test`,
`npm run build`, and all three compose files clean.

The day was driven entirely by the user running the app against a real seedbox and reporting
what looked wrong. **CI was green before every single one of these was found** — that pattern
has now held for two consecutive sessions and is the single most useful thing to know about
this project.

The most consequential finds, in rough order:

| What | Commit |
|---|---|
| **An item could be queued twice and run two concurrent lftp processes** — `enqueue_item` had no active-job guard. The user saw 4 processes where 2 were configured and assumed his transfer settings were broken. Guards now at enqueue, admission, and spawn | `6740c84` |
| **rar extraction had never worked, for any release** — Alpine's `7zip` has no RAR codec; `unrar` is now built from source | `855e7a3` |
| **Reset item tracking** — forget a path so its name is reusable, by selection / whole queue / filename pattern | `244ce2a` |
| **Clear History** (one row, by outcome, or all) and **Dismiss** for terminal jobs | `48ad72c`, `b1eb8a4` |
| Post-processing toggles became **inherit-or-override** instead of an AND; also fixed a table-rebuild cascade-delete in `db.py.migrate()` | `3500b3f` |
| **SSH key can be pasted**, encrypted at rest, in memory for asyncssh, tmpfs per-job for lftp | `6359569` |
| Delete now **stops an active transfer** rather than refusing | `21c41b0` |
| **Frontend test runner** (Vitest + happy-dom) and an in-app **Docs** section | `129cfcf`, `dfff677` |

**Three things a fresh session must not undo, beyond the list further down:**

- **`enqueue_item` is idempotent and the spawn path re-checks `_running`.** Both layers are
  deliberate; removing either reopens duplicate transfers.
- **The CI job name `"Frontend lint + typecheck"` is a live required status check** on `main`
  (verified via `gh api`). It now also runs tests. Renaming the job without updating branch
  protection in the same motion will block every PR.
- **`docker-compose.yml` uses `ghcr.io/crzykidd/lftpweb:latest`** (changed from `:0.0.1` on
  2026-08-13) — the tag the publish matrix pushes on every merge to `main` and every published
  release. Don't put a semver tag back until one has actually been released.

**Three things a fresh session must not undo:**

1. **The real RAR fixtures in `tests/test_postprocess.py` are hand-built valid archives**,
   cross-validated against a desktop 7-Zip. Fake fixtures are exactly why rar extraction was
   broken for nine phases. Do not "simplify" them.
2. **Presence icons read the world; milestone icons read timestamps.** `core/itemview.py`'s
   facets depend on that split. Collapsing it back into one notion reinstates the bug class.
3. **`REMOVED_LOCAL` is excluded from `ELIGIBLE_STATES` on purpose.** It looks like an
   oversight. It is the safety mechanism. See `prompts/open-issues.md`.

### 2026-08-12/13 overnight session — sixteen issues, all pushed and CI-green through `a6741ba`

The user drove this one from a running instance: they used the app, reported what looked
wrong, and each report became a handoff prompt executed by a spawned agent. **Tests went
489 → 596.** Every commit is on `origin/dev` with all CI jobs green.

| Commit | What |
|---|---|
| `209928d` | `VACUUM INTO` gets its own connection — scheduled backups were failing whenever any writer held a transaction |
| `cd74f91` | `busy_timeout` on the shared connection; disabled-button reasons; a real `scan_complete` WS message replacing a fake 1s spinner |
| `819b82c` | Extraction stops claiming `EXTRACTED` for no-ops, gains volume-set preconditions, bounds `_FAILED_` lifetime; live per-file progress inside a mirroring directory |
| `9b11df6` | The settle gate; `verify_hash_on_disk` can no longer bless a truncated file |
| `57f7ce9` | `item.state_changed_at`, trigger-enforced, shown on Files rows |
| `dfb74c2` | Manual + retention local deletion |
| `855e7a3` | **rar extraction fixed** — it had never worked; settle-gate follow-ups; the whole pending `DESIGN.md` wording backlog applied |
| `6d3bd95` | Reverses `dfb74c2`'s `REMOVED_LOCAL` change into an opt-in setting, default off |
| `c8d3e8b` | Per-queue scan interval (10/30/60/none), multi-cadence overrun-safe loop |

**The two findings worth carrying forward:**

1. **rar extraction had never worked, for any release.** Alpine's `7zip` ships without the RAR
   codec. The Dockerfile comment claimed rar support, `DESIGN.md` §6 specified it, and
   `core/extract.py`'s multi-volume machinery was dead code. It survived nine phases because
   **no test ever built a real rar** — every fixture was fake bytes. Now fixed with `unrar`
   built from source, and guarded by two hand-built real RAR4 fixtures cross-validated against
   a desktop 7-Zip. Do not replace those with fake bytes.

2. **A "bug" that was not one.** `REMOVED_LOCAL` being excluded from `ELIGIBLE_STATES` was
   reported as a bug by the orchestrating session, fixed faithfully by an agent, and then
   reversed hours later when it turned out to cause an infinite re-download loop on any
   `copy` queue with auto-queue on. `prompts/open-issues.md` § "4" has the full account. The
   lesson for a future session: an exclusion that looks like an oversight may be the entire
   safety mechanism.

**Behaviour changes an existing install will notice:** the settle gate is **on by default**
(items hold at `REMOTE_ONLY`/`substate='settling'` until their remote fingerprint is unchanged
for 2 scans *and* 60s wall-clock), and migrations 006–009 run at startup, with 008 rebuilding
the `item` table to widen a `CHECK` constraint.

**Still true and still the biggest risk: no browser has seen most of this.** New and unviewed:
the delete confirmation panel and bulk delete, the `state_changed_at` column, the settling
badge, the "scanned Xs ago" readout, Settings → Transfer's settle section, and Settings →
Queues' scan-interval dropdown and re-download toggle.

---

**Status: all 9 phases done — v1 is complete — plus a post-phase-9 session on 2026-08-12 that
first ran the app for real (its own section follows this one; read it, several of this
section's phase-9-era statements are superseded there).** `DESIGN.md` is settled and reviewed (§13's
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
doesn't duplicate it). Two headline items from phase 9 are now **closed** by the 2026-08-12
post-phase-9 session below — Settings → Transfer has a UI, and the app has been run in a real
browser — but Files still has no bulk "Delete local"/"Delete remote" (Queue/Stop only, per
phase 9's own scope), and no manual delete endpoint exists anywhere in the API.

## 2026-08-12, post-phase-9 session — the dev environment became real, and the UI was opened

**This is the first session in which the application was actually run and looked at.** That
single fact is why this session found more real bugs than any phase since the first live
deployment, and it is the thing to remember when planning work: build/lint/type-check and
endpoint-level HTTP checks had all been green for nine phases *while five distinct bugs sat in
plain sight on the first screen a human opened*.

**A coding agent still cannot see the UI** — no browser exists in the environment agents run
in. Every UI claim in this file and in `docs/decisions.md` means "builds, type-checks, lints,
and the endpoints it calls were verified over real HTTP", never "renders correctly". The user
is the only one who has seen a screen. Say so plainly rather than implying otherwise.

### The dev environment now runs — and four things had to be fixed before it could

`docker-compose.dev.yml` + `docker-compose.test.yml` bring up a working stack:
`http://localhost:5187` (Vite, hot reload, proxies `/api`), `http://localhost:8087` (API), and
two fake seedboxes on `localhost:2222` (GNU) / `2223` (busybox). From *inside* the backend
container the seedboxes are reachable as `seedbox-gnu` / `seedbox-busybox` on port 22 —
**use the service name, not `localhost:2222`**, which is a host-side port. Credentials
`seeduser` / `testpass123`, remote path `/data/pickup`. `private_data/seedbox-dropbox/` on the
host is bind-mounted at **`/data/dropbox`** on both seedboxes for hand-testing — deliberately
*not* over `/data/pickup`, since a mount there would shadow the seeded fixture tree several
integration tests assert on. `private_data/dev-config/` holds the dev database and survives
every rebuild (it is a bind mount; `down`, `up --build`, even `down -v` all leave it).

The four dev-only breakages fixed (all in `1235293`; production was already correct in each
case, which is exactly why they had gone unnoticed):

- **The dev image had no `lftp`, no `ssh`, no `7zz`.** Only the runtime stage installed them.
  Scanning worked (asyncssh is pure Python), so everything looked healthy until the first
  Queue click died `SPAWN_FAILED: FileNotFoundError`.
- **The dev image had no `/etc/passwd` entry for the running uid**, so OpenSSH would have
  fatalled `No user exists for uid N` on every transfer even once lftp existed — asyncssh has
  an env fallback (`core/remote.py` sets `LOGNAME`), OpenSSH has none. The dev stage now creates
  the user at build time from `DEV_UID`/`DEV_GID`; the runtime stage solves it differently
  (symlink `/etc/passwd` into the `/run` tmpfs, write it in the entrypoint) because there the
  uid is a runtime PUID/PGID and the root filesystem is read-only.
- **`/run/lftpweb` was root-owned and unwritable**, since the dev stage has no entrypoint to
  create and chown it before dropping privileges. Now a uid-owned `tmpfs` mount at the same
  path — *not* an `LFTPWEB_RUN_DIR` override onto the writable layer, which would put per-job
  rc files (seedbox password, known_hosts pin, mode 0600) on real disk and quietly drop the
  property §4.2/§11.1 exists to guarantee.
- **Vite refused the host and never proxied the WebSocket.** `allowedHosts` now defaults open
  (dev server only — production serves the built SPA same-origin from FastAPI, so this reaches
  no deployed user), narrowable via `LFTPWEB_DEV_ALLOWED_HOSTS`. And the `/api` proxy gained
  **`ws: true`**, which was load-bearing: `useLiveModel.ts` opens the one WebSocket at
  `window.location.host`, which in dev is *Vite*, not the backend. Without it the Files page
  connects to nothing and renders empty while every REST call works — verified by removing the
  line and watching the upgrade hang with zero bytes.

**Logs are readable now.** `LFTPWEB_LOG_LEVEL=DEBUG` set the *root* logger, so `aiosqlite`
logged every statement twice: measured 37,388 library lines against **1** line from lftpweb
itself, on a rotating handler with a fixed 25 MB budget — library chatter was actively evicting
anything an incident would need. `logsetup.py` now applies per-logger floors (`aiosqlite`,
`asyncssh`, `websockets` → WARNING), with `LFTPWEB_DEBUG_LIBS=asyncssh` as the escape hatch.
That hatch matters: asyncssh's own output is how connection behaviour gets diagnosed. Result:
~2,500 lines/minute → ~10.

### What shipped this session (committed `16a1a2f`, one `feat:` commit)

Seven agent-executed tasks, each with a `docs/decisions.md` entry and a prompt in
`prompts/done/2026-08-12-*.md`. Tests went **367 → 489**; both lint gates and `npm run build`
clean throughout.

- **`_UNPACK_` extraction staging** — extraction was the one step writing files under their
  *final* names, incomplete, where Sonarr/Radarr could import them. (Downloads were already
  safe: `xfer:use-temp-file yes` with `*.lftp`.) Now extraction stages into a `_UNPACK_<name>`
  **sibling** and merges into position only on full success via `move_tree(merge=True)`;
  failure leaves `_FAILED_<name>` as evidence. Both prefixes are hidden from `scan_local`.
- **Settings → Transfer built** (the phase-3a backend had no UI since phase 3), with §9.3's
  required live connection-count readout, and **`queue_name` added to `JobOut`** so the
  Transfers page shows which queue a row belongs to.
- **Post-processing states survive the rescan.** They never had: every outcome was overwritten
  within ~30s, so `CORRUPT` and `EXTRACT_FAILED` erased themselves before a human could see
  them. Fixed as a precedence rule with a bounded domain (outcomes beat a fresh `DOWNLOADED`,
  `PARTIAL` beats them, absence goes to §7.3's grace period), with transient states protected
  by the **live worker's existence**, never by the state string — so a crashed worker cannot
  wedge an item.
- **An empty remote directory reads `REMOTE_ONLY`, not vacuously `DOWNLOADED`.**
- **Throughput metrics + a Dashboard page** — `metric_sample` + `metric_heartbeat`
  (migration 005), 30s sampling, per-queue, 7-day retention (configurable to 30), two
  hand-rolled SVG charts, no new dependency.
- **Files tree Expand all / Collapse all.**
- **The WebSocket now publishes the persisted state, not the structural one** (below).

### The one architectural change worth reading before touching `core/engine.py`

**The `item` table is the single authority for item state; everything published is a projection
of it.** `scan_queue`'s order is now the invariant: **reconcile → persist → read back → diff →
publish**. `core/itemview.py` owns the one projection (`ITEM_VIEW_COLUMNS` + `item_view()`),
and `GET /api/files`, the `queue_delta`, the connect-time `snapshot()`, `TransferQueue.
_publish_item_state` and `PostprocessPipeline._publish` all go through it — **four hand-written
copies of the same dict collapsed into one**. `ReconciledNode.state` was renamed
**`structural_state`** so a *candidate* reading can no longer be mistaken for the real one at a
call site. `snapshot()` re-reads the database (and became `async`) because writers change
`item.state` between scans, and the reload path is how the old bug was actually visible.

Before this, a `REMOVED_LOCAL` item was published as `REMOTE_ONLY` — Queue button and all —
since phase 4, and REST and the socket could disagree about the same item.

### ✅ Backup/VACUUM race — fixed 2026-08-12

The race where `core/backup.py.create_backup` ran `VACUUM INTO` on the shared application
connection (raising `sqlite3.OperationalError: cannot VACUUM from within a transaction`
whenever another coroutine held an open transaction — routine once the metrics heartbeat
started writing every 30s) was fixed via `prompts/done/2026-08-12-fix-backup-vacuum-race.md`.
`create_backup` now opens a dedicated `aiosqlite` connection (with a 30s `busy_timeout`) just
for the `VACUUM INTO`, so it can never inherit another coroutine's transaction state. See
`docs/decisions.md` for the full writeup, including why commit-then-vacuum on the shared
connection was rejected.

**`:dev` images built before this fix (published from `fe80aaf`) still carry the bug** —
pull a fresh image to pick up the fix.

### ⚠ Open items awaiting the user — updated 2026-08-12 (post-phase-9 session)

They stay in this file until the user resolves them, not until the phase that raised them
shipped. **Do not action any of them unilaterally.**

1. **The user's live queue still has `sync_mode = 'move'` stored, and `move` works.** The row
   has been in the database since before phase 4's guard existed, inert the whole time because
   nothing implemented `move` or read `sync_mode` to act on it. As of phase 5 it **deletes the
   verified remote copy after every download that queue completes.** Deliberately **not**
   touched or reset — see `docs/decisions.md`'s phase 5 entry, point 0. The user needs to either
   switch it to `copy` or confirm that `/home/crzykidd/downloads/complete/testlftp` is a genuine
   hardlink pickup directory rather than live torrent data (§7.1 — deleting from a live torrent
   data directory destroys the seed). Nothing has been pulled on that queue since, so nothing
   has been deleted yet. **Note (2026-08-12):** the user intends to purge and rebuild the
   dev *and* production-test databases, which would delete this row — resolving it by deletion
   rather than by decision. If they rebuild the queue, the mode they pick is a fresh choice.
2. **Scheduled DB backups default ON** (daily, keep 7) — the one deliberate exception to the
   overnight run's "every new capability defaults off" rule, reasoned in `docs/decisions.md`'s
   phase 7 entry. The user's call whether to reverse it.
3. ~~No UI screen has ever been opened in a browser.~~ **Resolved 2026-08-12** — the app now
   runs and the user has used it (see the session section above). **As of 2026-08-14 every
   screen shipped to date has been looked at by a human at least once** — the "never viewed"
   list in `prompts/open-issues.md` is empty for the first time. Still true and still worth
   repeating: **a coding agent cannot see it**, so anything shipped from here starts unviewed
   again, and "viewed once, no obvious defect" is weaker than tested.
4. ~~**`DESIGN.md` has three proposed wordings pending the user's approval.**~~ — **all three
   were applied in `cad5891`** ("DESIGN.md backlog applied"). Verified 2026-08-15 against the
   doc itself: **§3.2 rule 9** (who wins between the three modules that write `item.state`) is
   at §3.2; the **empty-remote-directory clause** is in rule 1/8's neighbourhood; and the
   **publish invariant** got its own section, **§2.2 "What is published is the persisted state,
   never the structural one"**. Nothing is pending.

   **Kept as a caution, not as a task.** This entry, and a matching one in
   `prompts/open-issues.md` about two §4.3 wordings (also long since applied), both sat here
   claiming work was outstanding after it had shipped — and were repeated as "this must land
   before the release" advice in a later session before anyone opened `DESIGN.md` to check.
   **A drafted-wording entry in a tracker is not evidence the wording is still pending.** The
   authoritative record is `docs/decisions.md`, which marks each draft `APPLIED <date>` at the
   draft itself, and `DESIGN.md` itself. Read one of those before repeating any claim from a
   tracker about what the design doc does or doesn't say.
5. **`net:connection-limit` is not settable from any UI.** §4.5 calls it "a first-class setting,
   host-level, not an advanced afterthought", but it lives only inside the `host`.
   `connection_overrides` JSON blob, with no write path anywhere. Settings → Transfer's live
   connection-count readout therefore computes the worst case correctly but **can never fire its
   "⚠ over the limit" warning** on any current install. Surfaced read-only rather than promoted
   to a real column (that needs a migration). Recorded in `README.md`'s "Known gaps".
6. **Nothing deletes `item` rows, ever.** Found live: `GET /api/files` returned 27 rows while
   the WebSocket published 6, the difference being paths that had left both trees during
   testing. Row *lifetime* is an unanswered design question distinct from state ownership —
   when, if ever, may a row be deleted, and what does History/audit need to keep?
7. **Per-queue rescan interval** was requested and is not built. `scan_interval_s` is a single
   global (30s, env-overridable); phase 2 collapsed §5's separate 30s remote / 10s local
   cadences into it. Needs a migration, a per-queue next-due in the engine loop, and a field in
   Settings → Queues.

Also open, but not a decision so much as a next step: **`dev` is ahead of `main` by every
phase 4–9 commit plus the two 2026-08-12 commits, and no PR has been opened.** `main` is
protected; merging is a `dev` → `main` PR with all 8 required checks green, whenever the user
is ready. No release has been cut.

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
| 9 — Polish + docs reconciliation | **done, committed** (2026-08-12, `9272f36`) |
| post-phase-9 session | **done, committed** (2026-08-12, `1235293` + `16a1a2f`) — see above |
| `sync` mode | **not scheduled** — designed in §7, built only if it proves wanted |

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
guard existed. As of phase 5 this is **no longer inert** — see item 1 of the decisions-awaited
list above. Not silently rewritten; it is the user's call, they have been told, and as of the
last session they had not yet answered.

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
complete backend since phase 3 — **since built, 2026-08-12**; Files has no bulk Delete
local/remote — **still true**) — named rather than built, per this phase's own explicit
instruction not to close gaps silently. One factual error
was also caught and fixed: the README's volume table had `/staging` backwards relative to what
phase 5 actually built (`local_path` is the download target; `staging_path` is where a
`move`-mode item is *relocated to* afterward — the opposite of "download here, move to
`/downloads` when complete"). `uv run pytest`: 367 passed, 0 skipped (fake seedbox up), 357
passed / 10 skipped without it — no regressions, no backend code changed. Both lint gates
clean. `npm run build`/`npm run lint` clean. All three compose files validate. Fake-seedbox
containers torn down and confirmed removed via `docker ps -a` afterward. Committed as
`9272f36` and pushed to `dev`; every decision is in `docs/decisions.md`'s phase 9 entry.

**Commits so far:** repo init + standard adoption, the design revisions, phase 1 (`b0109ae`),
phase 2 (`de6d74b`), phase 3a (`36b9123`), phase 3b (`c814aa0`), phase 4 (`db89b63`), phase 5
(`b0c9cb3`), phase 6 (`d76a662`), phase 7 (`c6dcc03`), phase 8 (`b936576`), phase 9
(`9272f36`), then the 2026-08-12 post-phase-9 session as two commits: `1235293`
(`chore:` dev environment + logging) and `16a1a2f` (`feat:` the seven application changes,
51 files). All on `dev`. **Everything through `9272f36` is pushed; the two 2026-08-12 commits
are NOT** — the user asked to work locally without pushing, so `dev` is 2 ahead of
`origin/dev`. CI on `9272f36` was green across all six jobs (Backend lint, Frontend lint +
typecheck, Config validation, Compose validation, Image build, Test suite); **CI has not yet
seen the two newer commits.** **489 tests pass** with the fake seedbox up (367 at phase 9),
both lint gates clean, `npm run build` clean.

**Still uncommitted, and not ours:** `CHANGELOG.md`, `standards.md`,
`.claude/commands/release-prep.md`, and this file carry a pre-2026-08-12-session documentation
sweep that no agent touched. Its `docs/decisions.md` entry ("Post-phase-9 documentation
currency sweep") rode along in `16a1a2f` because it sat in the same contiguous block as the
session's own entries — so that entry is committed while the files it describes are not. Commit
them and the ordering resolves itself.

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

**Added 2026-08-15 (first real Sonarr live-testing run) — read this one first, it's the newest:**

- ***arr enums are strings in bodies, ints in query params — the fixture must model the wire,
  not the assumption.*** The Sonarr/Radarr v3 API serializes `eventType` as a camelCase
  **string** (`"downloadFolderImported"`) in every response body; the numeric codes this
  integration was originally built against exist only as query-parameter values. Two genuine
  live imports were misclassified `gone` because `IMPORT_EVENT_TYPES = {3}` could never match a
  real record — and every test stayed green because the fake-*arr test data encoded the same
  wrong numeric assumption instead of the real wire shape. `docs/decisions.md` (2026-08-15) has
  the full account; `core/arrclient.py.HistoryEvent.is_import_event()` is the one place the
  comparison now happens, with a numeric fallback kept for tolerance only. **The general lesson:
  when a spec flags a vocabulary "unverified against a live instance," the test fixture that
  data drives must not itself be trusted as ground truth for that vocabulary — it can encode the
  identical wrong guess the production code does, and then prove nothing.**
- ***Missing-vs-the-sidecar at verify time always means the remote lacked it too, never "still
  arriving."*** `core/verify.py` only runs from `core/postprocess.py`, which only fires after
  `core/queue.py`'s local-vs-remote completeness gate has already passed — so a file a
  `.sfv`/`.md5` references but can't find locally was *also* absent on the remote by the time
  completeness was measured. That's what makes "every referenced file absent" a safe signal for
  an upstream anomaly (a release rar'd at origin, extracted upstream, rars deleted before this
  ever reached local disk) rather than a partial transfer — see the fix below and
  `docs/decisions.md` (2026-08-15).
- ***An upstream-extracted release verifies `SKIPPED`, not `CORRUPT` — but only when nothing
  is provably missing.*** Live case: `National.Lampoons.Animal.House.1978.iNTERNAL.1080p.BluRay
  .x264-EwDp` on the ar-movies queue arrived `movie.mkv` + `.sfv`, the `.sfv` still listing the
  rar volumes SABnzbd had already extracted and deleted upstream. Treating every sidecar entry
  as "missing" reported `CORRUPT` and permanently withheld the `move`-mode delete. The fix
  (`core/verify.py`) narrows on purpose: every referenced entry absent **and** other content
  present → `SKIPPED`; *any* referenced entry present (including a half-deleted archive set) →
  unchanged, stays `CORRUPT`; sidecar and nothing else → stays `CORRUPT` (nothing to have been
  vouching for). Don't widen the relaxation to mixed presence — that's the still-open pipeline-
  ordering question, `prompts/open-issues.md` #2 / G1.

**Added 2026-08-12 (post-phase-9 session):**

- **`relevant == 0` on a directory means two different things**, and only one of them is
  `DOWNLOADED`. Every child excluded by a `file_exclude` pattern → vacuously `DOWNLOADED`, and
  that is load-bearing (§4.7/§3.2 rule 8: it is what stops a filtered release sitting `PARTIAL`
  and being auto-queued forever). A genuinely empty remote directory → `REMOTE_ONLY` until
  mirrored. `core/reconcile.py` tells them apart with `remote_file_totals`, a rollup counting
  remote files *before* the predicate runs. **Do not "simplify" this by keying on local
  presence** — an all-excluded directory legitimately has no local presence either (lftp never
  creates a directory with nothing to put in), so that reintroduces the infinite re-queue loop.
- **A state that is merely protected is a state that can never be un-stuck.** Post-processing
  outcomes beat a freshly computed `DOWNLOADED` and *only* that: `PARTIAL` beats them (rule 2 —
  the bytes are not all there) and absence goes to §7.3's grace period, or an item an importer
  moved out would never reach `REMOVED_LOCAL` and auto-queue would re-download the whole
  release. Transient `VERIFYING`/`EXTRACTING` are protected by the **live worker's existence**
  (`PostprocessPipeline.in_flight_item_ids()`), never by the state string, so a crashed worker
  cannot wedge an item — an in-memory registry empties on death, exception, and shutdown alike.
- **Nothing may publish a state it did not read back from the `item` table.** `scan_queue`'s
  order is the invariant: reconcile → persist → read back → diff → publish, with
  `core/itemview.py` the single projection shared by the socket, the snapshot, and
  `GET /api/files`. The reconciler's field is `structural_state` — a *candidate* — and if you
  find yourself publishing it directly, that is the bug this rename exists to make visible.
- **`_project`'s `rel_paths` filter is load-bearing, not tidiness.** Nothing deletes `item`
  rows, so projecting unfiltered would resurrect rows that left both trees *and* leave
  `diff_nodes`'s `removed` permanently empty.
- **Extraction must never write a final-named file where an `*arr` can see it.** It stages into
  a `_UNPACK_<name>` **sibling** (not a child — a child sits inside the tree the reconciler
  walks and inside anything a later move relocates) and merges into place only on full success;
  failure leaves `_FAILED_<name>` as evidence. Both prefixes, and `.lftpweb-mount-ok`, are
  filtered out in `core/local_scan.py` — lftpweb's own bookkeeping must never reconcile to a
  `LOCAL_ONLY` node. Downloads were already safe via `xfer:use-temp-file`/`*.lftp`.
- **`job.bytes_done` is not monotonic** — a retry or resume resets it, which is why
  `bytes_start` exists. Any code differencing it (the metrics sampler, anything new) must
  compute deltas per job and clamp, or a restart renders as a phantom throughput spike.
- **The dev image is not the runtime image.** It has no entrypoint, so anything
  `docker/entrypoint.sh` does for production (creating/chowning `/run/lftpweb`, writing
  `/etc/passwd` for the running uid) must be arranged another way in
  `docker-compose.dev.yml`/the `dev` stage — see the session notes above. A dev-only breakage
  that production does not share is easy to misdiagnose as an application bug.
- **Vite's `/api` proxy needs `ws: true`.** The one WebSocket is opened at
  `window.location.host`, which in dev is the Vite server, not the backend. Without it the
  Files page connects to nothing and renders empty while every REST call succeeds.
- **`root.setLevel()` sets the level for every library in the process.** `logsetup.py` applies
  per-logger floors for exactly this reason; `LFTPWEB_DEBUG_LIBS` lifts them when you actually
  need transport-level output.
- **`VACUUM` cannot run on a connection anyone else might have a transaction open on.**
  `core/backup.py.create_backup` learned this the hard way (fixed 2026-08-12, above) — it now
  opens its own connection just for the `VACUUM INTO` rather than reusing the caller's.

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
  5 shipped, and was deliberately left untouched** — see item 1 of the decisions-awaited list
  near the top of "Where we are", and docs/decisions.md's phase 5 entry, point 0. The user has
  been told; they had not answered as of the last session. Don't change the row for them.
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
