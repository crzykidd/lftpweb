---
name: 2026-08-15-arr-integration-backend
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Migration 018 (arr_instance + 3 path_queue cols + 3 item cols, no rows inserted),
  core/arrclient.py (httpx, one class, kind switch, pagination-walk), core/arrsync.py
  (ArrSettings + ArrSyncScheduler: matching, two-pass import/gone quiescence guard,
  per-instance backoff isolation -- notify/cleanup deliberately deferred to phase B per the
  spec's own build-plan phasing), api/settings_arr.py (instance CRUD + Test), settings_queues.py
  extended (arr_instance_id/arr_delete_completed/arr_visible_path + validation), arr_status/
  arr_status_at joined into core/itemview.py's projection and models.FileNode. 34 new backend
  tests (fake-*arr fixture over a real threaded uvicorn server) + additions to test_itemview.py;
  2 pre-existing WS-delta payload-size thresholds bumped for the 2 new wire fields. All 4
  verification gates green: ruff check, ruff format --check, pytest (1109 passed, 0 skipped),
  frontend lint/test/build (untouched, re-verified).
---

# Task: Sonarr/Radarr integration — backend foundation (phase A of 3)

Build the data model, API client, poller, and settings API for the *arr integration, per
the approved spec. **No notify push, no cleanup/deletion, no frontend in this phase** —
those are phases B and C.

## Before you start

- Read **`docs/arr-integration-spec.md`** end to end — it is the spec this phase
  implements and every design decision in it is settled. Where this prompt and the spec
  disagree, the spec wins.
- Read `DESIGN.md` §1.3, §3.2, and the publish invariant (§2.2). `CLAUDE.md` for
  conventions. Skim `docs/decisions.md` recent entries.
- Study these files as your conventions to copy, before writing anything:
  - `backend/lftpweb/core/backup.py` (`BackupScheduler`) — the background-loop shape
    (`_task`/`start()`/`stop()`) `core/arrsync.py` must follow, and how its settings
    dataclass lives in the `setting` table.
  - `backend/lftpweb/api/settings_host.py` — how a secret (seedbox password) is encrypted
    via `core/crypto.py` and kept write-only (never echoed). The *arr API key follows the
    identical convention.
  - `backend/lftpweb/core/itemview.py` — `ITEM_VIEW_COLUMNS` / `item_view()`, the one
    projection. `arr_status` and `arr_status_at` join it; `arr_download_id` does NOT.
  - `backend/lftpweb/core/audit.py` — event-row conventions.
  - `backend/lftpweb/migrations/017_download_prefix.sql` — migration style/numbering.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files this
plan needs to modify. This run is authorized unattended: if a file you must touch is
dirty, STOP and report back rather than proceeding. Unrelated dirty files: surface once,
don't block. This prompt file is exempt.

## What to do

1. **Migration `018_arr_integration.sql`** — exactly the schema in the spec's "Data
   model" section (the `arr_instance` table, three `path_queue` columns, three `item`
   columns). No rows inserted.
2. **Promote `httpx` to a runtime dependency** in `pyproject.toml` (it is currently
   dev-only). Keep the dev-group entry compatible.
3. **`core/arrclient.py`** — one async client class over httpx (`X-Api-Key` header, 10s
   timeout), `kind: sonarr | radarr` switching only the command names/nouns. Methods:
   `system_status()`, `queue_records()` (walks ALL pages), `import_events(download_id=…,
   source_title=…)` (history query), `post_scan_command(path)` (phase B will call this —
   build it now so the client is complete, nothing calls it yet). Treat *arr numeric
   `eventType` codes and `trackedDownloadState` strings as data-driven constants in one
   place with a comment that they must be verified against a live instance.
4. **`ArrSettings`** dataclass in the `setting` store (`poll_interval_s`, default 60),
   same pattern as `BackupSettings`.
5. **`core/arrsync.py`** — the poller loop per the spec's "The poller" section:
   - Matching per the spec's "Matching" section: bound queues only, top-level items only,
     **logical** item names, basename-of-`outputPath` first then normalized-title
     fallback; record `downloadId` into `item.arr_download_id` at match time; a match on
     an item whose association was already terminal (`cleaned`/`gone`) with a *different*
     `downloadId` starts a fresh association (the upgrade-regrab case).
   - Import detection per the spec's lifecycle section, including all three layered
     requirements — the record-gone check, the ≥1 history import event check, and the
     **two-consecutive-passes quiescence guard**. `gone` for disappearance without an
     import event, also two-pass confirmed.
   - Per-instance failure isolation: unreachable instance → one WARNING + one event row
     + capped exponential backoff; never blocks other instances or touches the
     scan/transfer engine.
   - Every transition writes an `event` row (`arr_matched`, `arr_imported`, `arr_gone`)
     with the reasoning in the message, per the audit discipline.
   - Wire start/stop into the app lifecycle where `BackupScheduler` is started.
6. **`api/settings_arr.py`** — instance CRUD + `POST /api/settings/arr/{id}/test`
   (`system_status` round-trip), api_key write-only. Register the router alongside the
   other settings routers. Extend the queues settings API (`api/settings_queues.py`) with
   `arr_instance_id` (must reference an existing instance or be null),
   `arr_delete_completed` (reject true when no instance bound), `arr_visible_path`.
7. **Projection**: add `arr_status`, `arr_status_at` to `core/itemview.py`'s one
   projection so REST + WebSocket both carry them. Do not touch `item.state` anywhere —
   `arr_status` is a facet (spec: "a facet, not a lifecycle state").
8. **Tests** — a fake-*arr FastAPI fixture app speaking `/api/v3/system/status`,
   `/api/v3/queue` (paginated), `/api/v3/history`, `/api/v3/command` over real HTTP (same
   philosophy as the fake seedbox; look at how existing tests spin up in-process apps).
   Cover at minimum: client pagination walk; matching (basename, title fallback,
   non-top-level ignored, unbound queue ignored); the slow multi-file import scenario —
   record present in `importing` with per-file history events accreting must NOT produce
   `imported`; `imported` only after record-gone + history + two passes; `gone` path;
   upgrade-regrab fresh association; API-key encrypted at rest (assert the stored value
   is not the plaintext) and never echoed by GET; queues API validation; migration
   applies cleanly on a seeded DB.

## Conventions to honor

- Everything defaults OFF (instance `enabled` = 0). Migration inserts no rows.
- No new resolver for paths; nothing in this phase touches disk paths at all beyond
  matching on names.
- Update `prompts/startnewsession.md`: add a new "*arr integration build run (2026-08-15,
  unattended)" progress table under "Where we are" with a row for this phase, per the
  autonomous-run logging convention.
- Record non-obvious decisions in `docs/decisions.md`, newest at top.

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — if the fake seedbox compose isn't up, note the skip count; don't
   claim the skipped tests passed.
4. `cd frontend && npm run lint && npm test && npm run build` (should be untouched by
   this phase — run anyway to prove it).

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` this file into `prompts/done/` (create if needed) on success, or
   `prompts/failed/` on failure.
3. **Do not commit.** Prepare the working tree, then report back: the full file list, a
   proposed one-line `feat:` commit message, each gate's exact result (command + exit
   code + counts), and any decisions or deviations. The orchestrating session commits.
   Never `git add -A`, never push.
