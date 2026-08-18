---
name: 2026-08-18-sweep-orphaned-extract-debris
status: completed
created: 2026-08-18
model: sonnet
completed: 2026-08-18
result: >
  Fixed. New `extract.list_top_level_debris_dirs` (pure enumeration, both prefixes, no age
  filter) feeds a new `RetentionScheduler._sweep_orphan_extract_debris` pass (core/retention.py)
  that widens the existing age-gated `extract.sweep_failed_dirs` mechanism to the case it can't
  reach: the owning item gone from tracking, or its row already REMOVED_BOTH. Derives the
  owning item's name via `_derive_debris_owner_candidates` (strips `_FAILED_`/`_UNPACK_`, then
  a leading `.downloading-` if present), checks it against `item`, and only removes when no
  candidate resolves to a live or in-flight row. Mount-sentinel gated per queue; deliberately
  unconditional (no settings toggle), reasoned in docs/decisions.md. 17 new tests (11 in
  tests/test_local_delete.py, 6 in tests/test_postprocess.py). Full backend suite (1317),
  frontend suite (477), ruff, oxlint, and build all green.
---

# Task: Orphaned `_FAILED_`/`_UNPACK_` extraction debris is swept once its item is gone

Production find (2026-08-18): the user's queue root held
`_FAILED_.downloading-Hard.Knocks.in.Season.S02E09.1080p.WEB.h264-KOGi` — lftpweb's
own failed-extraction evidence directory (`_FAILED_<name>` where `<name>` was the
item's then-physical `.downloading-`-prefixed dir; the wrapped download prefix proves
local origin — SAB's remote `_FAILED_` dirs are auto-queue-excluded and no job ever
transferred one, verified in the support bundle
`lftpweb-support-0.2.4-20260818T192004Z`). The item itself was later manually deleted
(both scopes), but the evidence dir is a *sibling* with its own top-level name —
hidden from `scan_local` by design, so it has no item row, no UI presence, no delete
affordance, and nothing that will ever clean it. The user found it with `ls` and asked
why the interface showed nothing. Same defect shape as the v0.2.2 orphaned
spent-archive fix (`prompts/done/2026-08-17-orphaned-spent-archive-rows.md`): a
bookkeeping artifact whose parent left both trees, resting invisible forever.

## Before you start

- Read `CLAUDE.md`; `DESIGN.md` §6 (extraction staging: `_UNPACK_` sibling merges on
  success, `_FAILED_` left as evidence on failure); the trap list in
  `prompts/startnewsession.md` ("Extraction must never write a final-named file where
  an *arr can see it" — both prefixes are filtered from `core/local_scan.py`).
- Read before editing:
  - `backend/lftpweb/core/extract.py` and wherever the **existing bounded
    `_FAILED_` lifetime mechanism** lives (2026-08-13, commit `819b82c` "bounds
    `_FAILED_` lifetime") — this task extends that mechanism's reach; do NOT build a
    second, parallel one. Understand exactly what it bounds today and why it missed
    this case (the item was deleted, so whatever keyed the bound presumably lost its
    anchor).
  - `backend/lftpweb/core/local_delete.py` / `core/archive_cleanup.py` — the
    orphaned-spent-archive precedent (2026-08-17) whose "is the parent still in this
    pass's written/publishable set" reasoning this should mirror.
  - `backend/lftpweb/core/local_scan.py` — where the prefixes are filtered; the
    sweep needs its own listing of the queue root's `_FAILED_*`/`_UNPACK_*` entries
    (or a tagged return from the scanner — your call, but don't unhide them from the
    reconciler).
  - `backend/lftpweb/core/postprocess.py` — `in_flight_item_ids()`; a live
    extraction's `_UNPACK_` staging must never be swept.

## Working tree check

Run `git status --porcelain` before editing; cross-reference; ask before touching
dirty files. This prompt file is exempt. **Coordination note:** the sibling task
`2026-08-18-startup-rescue-complete-unwitnessed-items` may have just landed —
`CHANGELOG.md`/`docs/decisions.md` will carry its entries; append alongside, don't
disturb.

## What to do

1. **A per-scan-pass (or per-postprocess-pass — pick the layer that already owns
   similar cleanup and say why) sweep of the queue local root** for top-level
   `_FAILED_*` and `_UNPACK_*` directories. For each, derive the owning item's
   logical name: strip the `_FAILED_`/`_UNPACK_` prefix, then strip a leading
   `.downloading-` if present (the incident's exact shape). Then:
   - **Owning item still live** (row exists in a non-terminal state, or its
     extraction is in flight per `in_flight_item_ids()`) → leave it alone. Today's
     bounded-lifetime behavior, whatever it is, continues to apply; this sweep only
     widens coverage to the orphan case.
   - **No owning item, or the item has left both trees** (`REMOVED_BOTH`, or no row
     matches the derived name at all — the manual-delete case) → delete the debris
     directory (through the same guarded local-deletion machinery the codebase
     already uses — path containment checks included, never a bare `shutil.rmtree`
     on a joined path) and write one info event (kind e.g.
     `extract_debris_removed`) naming the directory, the derived item name, and why
     it was judged orphaned.
2. **Safety rails:** never touch anything that isn't a directory matching exactly
   the two prefixes at the queue root's top level; never sweep while the owning
   item's delete/extract worker is in flight; the mount sentinel failing for the
   queue skips the sweep entirely (a vanished mount must not read as "everything is
   orphaned"). That last one is load-bearing — state it in a comment.
3. **Tests** (extend the existing extraction/local-delete suites where they live):
   orphaned `_FAILED_` dir with no item row → swept + event; `_FAILED_` whose item
   still exists non-terminally → untouched; `_UNPACK_` for an in-flight extraction →
   untouched; the incident's exact name shape (`_FAILED_.downloading-X`) resolves to
   item name `X`; mount-gate-failed queue → sweep skipped.
4. **Docs, same commit:** `CHANGELOG.md` Unreleased `### Fixed` (user-voiced: leftover
   `_FAILED_`/`_UNPACK_` extraction folders whose release is long gone are now
   cleaned up automatically instead of sitting invisible on disk forever).
   `docs/decisions.md`: where the sweep lives and why, the relationship to the
   existing bounded-lifetime mechanism, the mount-sentinel rail.

## Conventions to honor

- Gates, each run separately IN THE FOREGROUND with adequate timeouts (backend
  `uv run pytest` from the repo root, ~3.5 min — timeout 400000ms; never background
  a gate or wait on a Monitor notification), exit codes read: `uv run --project
  backend ruff check`, `uv run --project backend ruff format --check`,
  `uv run pytest`; frontend untouched — re-verify anyway (`npm run lint`,
  `npm test`, `npm run build`).
- Comment style: dated, incident-citing.
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Frontmatter: `status: completed` (or `failed`), `completed` date, one-line
   `result`.
2. Move this file into `prompts/done/` (or `prompts/failed/`).
3. Hand off ONE commit (prompt file + changes + move). Present file list + one-line
   message. **You are a spawned agent: do not commit, never `git add -A`, never
   push.** Branch is `dev`.
