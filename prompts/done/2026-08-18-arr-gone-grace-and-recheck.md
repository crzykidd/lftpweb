---
name: 2026-08-18-arr-gone-grace-and-recheck
status: completed
created: 2026-08-18
model: sonnet
completed: 2026-08-18
result: >-
  Added the `dropped` amber grace state (no migration -- arr_status is free-text) with a
  6h DROPPED_GONE_GRACE_S constant, rechecked every poller pass; retroactive heal sweep
  for already-stranded `gone` rows; frontend amber variant/overlay/hover-label; docs and
  tests updated. All six gates green.
---

# Task: A queue-record disappearance gets an amber grace state, rechecks, and only goes red after a window

Production incident (2026-08-17/18, diagnosed from the user's support bundle
`lftpweb-support-0.2.3-20260818T013532Z`): SABnzbd sometimes returns a blank/empty
response to Sonarr's queue poll, so Sonarr's queue view momentarily empties and the
records return on the next refresh. During one such blip (~01:20Z), **8 items flipped
`gone` in a single lftpweb poller pass** — the two-pass quiescence guard didn't help
because the poller runs every minute and the blip covered both passes. Proof it was a
blip, not 8 real removals: lftpweb was *still downloading* those items at the verdict
(their verify/rename events run minutes later), so no import could have existed yet.
Sonarr then imported all of them normally an hour later — but `gone` is terminal,
re-matching a `gone` row on the *identical* downloadId is deliberately refused
(docs/decisions.md 2026-08-16), and the stranded-source-delete sweep gates on a
terminal-*import* status — so the 8 rows show a permanent red dot, their rung-4 source
deletes are parked forever, and *arr cleanup never runs for them.

**The design, settled with the user (their own proposal, quoted intent):** in-queue
stays as today; a record that drops from the queue turns **amber** ("removed from *arr
queue X minutes ago — rechecking") instead of red; it goes back to normal if the same
downloadId reappears, green-check if an import shows up in history, and **red only
after a grace window expires** with neither. A deliberate manual queue removal thus
just sits amber for the window and then goes red — intended, only delayed.

## Before you start

- Read `CLAUDE.md`, `DESIGN.md` §16 (*arr integration state machine) and §7.3/§7.4
  (move-delete ladder rung 4), `docs/arr-integration-spec.md`.
- Read the mechanism end to end:
  - `backend/lftpweb/core/arrsync.py` — `_check_import` (~line 1011: the
    queue-record-gone + history check + two-pass `_PendingVerdict` quiescence),
    `_commit_terminal` (~line 1067), the matching section (~line 776, incl. the
    `_REMATCHABLE_STATES` same-downloadId refusal ~lines 800–804),
    `_sweep_stranded_source_deletes` (keyed off `remote_delete_pending` +
    terminal-import `arr_status`), `_maybe_cleanup`, `_InstanceBackoff`.
  - `backend/lftpweb/core/arrclient.py` — `import_events` (paged, exact-downloadId).
  - How migrations are declared (find `db.py.migrate()` / the migrations list; last
    is 021) and whether `item.arr_status` carries a CHECK constraint — if it does, a
    migration 022 widens it; if it's free text, no migration for the new value (say
    which in your report).
  - Frontend: `frontend/src/lib/fileTree.ts` — `ARR_ICON_VARIANTS`/`arrIconVariant`,
    `arrChipOverlay` (~lines 470–501), `arrHoverLabel`;
    `frontend/src/components/LifecycleIcons.tsx` — `ArrRowChip`,
    `ArrChipOverlayBadge` (green check / red dot), and the drawer's `ArrIcon`.
- `tests/fake_arr.py` — you will extend it to model a transient blank queue.

## Working tree check

Run `git status --porcelain` before editing; cross-reference against the files this
plan touches; ask before touching any that are dirty. This prompt file is exempt.

## What to do

1. **New intermediate status `dropped`** (this exact string — between
   `detected|notified` and `gone` in the lifecycle). When the existing two-pass
   quiescence would today commit `gone` (queue record disappeared, no import event),
   commit `dropped` instead: `arr_status='dropped'`, `arr_status_at=now`, one new
   `arr_queue_dropped` info-level event ("*arr queue record disappeared with no
   import history event — holding amber for <window> before calling it gone;
   rechecking each pass"). Add migration 022 only if `arr_status` is
   CHECK-constrained.
2. **While `dropped`, the poller keeps working the row, every pass:**
   - **Reappearance, same downloadId** → back to `detected` (then the normal flow).
     `dropped` joins the rematchable set *without* the different-downloadId
     restriction that `gone`/`cleaned` keep — the identical id reappearing is
     direct evidence the disappearance was a blip. The `gone`/`cleaned` rule itself
     stays exactly as it is (that decision stands for truly-settled rows; note this
     distinction in `docs/decisions.md`).
   - **Import history event found** (same `import_events` lookup) → commit
     `imported` via the existing `_commit_terminal` path, so the rung-4 deferred
     source delete and cleanup fire exactly as a normal import does. Event message
     should note it confirmed after a queue drop.
   - **Window expires** (`arr_status_at` older than the grace) → commit `gone`,
     today's terminal semantics unchanged, event message noting "unconfirmed for
     <window> after leaving the queue". Grace: a module constant
     `DROPPED_GONE_GRACE_S` defaulting to **6 hours** — name it in
     `docs/concepts.md` as a deliberate constant (a settings knob is a named
     future option, not built now).
   - Make sure the per-pass iteration actually visits `dropped` rows — they are by
     definition absent from the *arr queue snapshot, so confirm the code path that
     today visits `detected`/`notified` rows with vanished records covers them (or
     extend it).
3. **Ladder/cleanup gates:** `dropped` must behave exactly like a
   non-imported tracked status — rung-4 delete stays deferred, `_maybe_cleanup`
   stays withheld. This should already fall out of the existing
   `arr_status='imported'` gates; verify and pin with a test rather than assume.
4. **Retroactive healing for the already-stranded `gone` rows** (the 8 in
   production, and any like them): a bounded per-pass recheck for rows with
   `arr_status='gone'` AND `remote_delete_pending` non-null AND
   `remote_deleted_at` null — query `import_events` by the stored downloadId; an
   import event promotes the row to `imported` through `_commit_terminal` (rung-4
   delete + cleanup then proceed normally). Bound the attempts (reuse the
   `_InstanceBackoff` shape or a per-row counter, hard cap, one final
   "giving up" event) so a genuinely-gone row doesn't get queried forever. This
   sweep needs the downloadId — confirm it's persisted on the item row (the
   matching writes it; find the column) and handle its absence as "skip, count as
   an attempt".
5. **Frontend — the amber state:**
   - `ARR_ICON_VARIANTS` gains `dropped` → a new `'dropped'` variant;
     `arrChipOverlay` returns a new `'pending'` (amber/yellow dot) overlay for it;
     `ArrChipOverlayBadge` renders it (amber dot, same size/positioning as the red
     one); the drawer's `ArrIcon` gets an equivalent amber treatment.
   - `arrHoverLabel` for `dropped`: "removed from the *arr's queue <relative time>
     ago — rechecking" (reuse the existing relative-time helper the label already
     uses for other statuses).
   - Files facet filters: leave the existing "gone" filter meaning `gone` only;
     `dropped` already counts under "*arr-tracked". No new filter.
6. **Tests:**
   - `tests/fake_arr.py` gains a way to serve an **empty queue for N requests, then
     restore the same records** (the SAB-blank blip).
   - Scenarios: blip spanning both quiescence passes → row goes `dropped` (never
     `gone`), record returns same-id → back to `detected`; `dropped` + import
     events appear → `imported`, rung-4 delete fires; `dropped` + window expiry
     (inject/mock the clock or make the grace injectable) → `gone`; the retro-heal
     sweep promotes a seeded `gone`+pending row when history has an import, and
     gives up after the cap when it doesn't; ladder/cleanup stay withheld while
     `dropped`.
   - Frontend: variant/overlay/hover-label cases for `dropped` in the existing
     `fileTree` test file.
7. **Docs, same commit:** DESIGN.md §16 state-machine update (`dropped` inserted,
   grace named); `docs/concepts.md` status table + the grace constant;
   `docs/arr-integration-spec.md` if it tables the statuses; `CHANGELOG.md` under
   Unreleased (Added or Fixed — user-voiced: a download-client blip no longer
   permanently marks an imported release "gone"; amber "rechecking" state; red only
   after 6h unconfirmed; already-stranded gone rows self-heal); `docs/decisions.md`
   entry revisiting the 2026-08-16 same-downloadId refusal with this production
   evidence and recording why `gone`/`cleaned` keep the old rule while `dropped`
   doesn't.

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from the repo root takes ~3+ minutes — timeout 400000ms; never
  background a gate or wait on a Monitor notification), exit codes read:
  `uv run --project backend ruff check`, `uv run --project backend ruff format
  --check`, `uv run pytest`; frontend `npm run lint`, `npm test`, `npm run build`.
- Comment style: dated, incident-naming — this incident has a support bundle;
  cite it.
- No browser here — the amber chip ships unviewed; say so.
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter (`status` value `completed` or `failed`,
   `completed` date, one-line `result`).
2. Move this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. Hand off ONE commit covering this prompt file, the files modified, and the
   prompt move. Present the file list and a one-line message.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree and
     report the file list + proposed message back to the orchestrating session.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
