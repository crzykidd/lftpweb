---
name: 2026-08-16-manual-delete-local-and-remote
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: |
  Implemented the delete dialog's independent Local/Source scopes exactly as specified.
  Backend: `POST /api/items/{item_id}/delete` takes an optional JSON body (`DeleteItemRequest`,
  `local: bool = True`, `source: bool = False` -- omitted body = today's pre-existing behavior
  byte-for-byte). Local runs first when requested (unchanged stop-then-delete, 409 on withhold,
  source never attempted); a source-only request refuses 409 on an active job rather than
  stopping it itself; a combined request needs no extra check since local's own stop already
  clears it. `core/postprocess.py.perform_remote_delete` gained a `caller` parameter
  (`"pipeline"` default, byte-identical messages; `"manual"` new, tagged "deleted by user
  request" on the same `remote_delete`/`remote_delete_failed` event kinds) -- reused, not a
  second SSH-delete implementation, per the task's own instruction. `PostprocessPipeline`
  gained a public `resolve_host()` around its existing `_host_provider` closure so
  `api/jobs.py._delete_source_manual` reuses the identical `remote_pool`/host seam `main.py`
  already wires, rather than a fresh `load_host_config` call. Idempotent against an
  already-gone remote copy (`item.remote_deleted_at` set short-circuits, no SSH round trip) and
  clears a stale `item.remote_delete_pending` on a genuine delete, so a mid-ladder manual
  source delete completes the ladder early -- `core/arrsync.py`'s own guards already handled
  "already deleted" gracefully without any change needed there. Suppression: a source-only
  success writes `auto_queue_suppressed=1, suppressed_reason='deleted_source'` (migration 020,
  widening the CHECK constraint via the same full-table-rebuild shape migration 008 used); a
  combined request deliberately skips this since `delete_local` already wrote the more complete
  `'deleted_local'`. Partial failure (local succeeds, source then fails) is a 200 with
  `source_deleted: false`/`source_reason` set, not a 409 -- `DeleteItemResponse` gained those
  two fields, both `null` when source wasn't requested.

  Frontend: `lib/fileTree.ts` gained four pure functions (`defaultSourceChecked`,
  `shouldOfferSourceScope`, `canConfirmDelete`, `showsCopyQueueSourceWarning`), unit-tested in
  `FileTree.test.ts`. `FileTree.tsx`'s delete dialog gained two checkboxes (Delete local copy /
  Delete source, the latter only rendered when at least one pending entry has a remote copy),
  seeded to their per-queue defaults on every new delete request, the §7.1 warning banner when
  Source is checked on a non-move queue, and a disabled Delete button when neither is checked.
  `runAction`'s bulk delete now computes Source per-entry (`hasRemoteCopy`, never a blanket
  flag across a mixed selection) and reads `source_deleted`/`source_reason` back out of an
  otherwise-fulfilled response so a partial failure surfaces in the "N of M succeeded" summary
  instead of silently counting as a success -- the one place `Promise.allSettled`'s binary
  alone would have hidden it. `client.ts.deleteItem` now takes explicit `local`/`source`
  booleans; `FilesPage.tsx` threads `queueSyncMode` down from the same `config?.sync_mode` read
  `QueueResetControls` already uses.

  Docs: DESIGN.md §9.2 (delete dialog's two scopes, not the originally-sketched separate
  "Delete remote" button) and §7's forward note updated to reflect this task as done;
  docs/decisions.md full settled-design entry (defaults reasoning, §7.1 interplay, partial-
  failure semantics, rejected alternatives); docs/concepts.md's suppression table gained
  `deleted_source`; CHANGELOG.md Unreleased/Added; prompts/startnewsession.md phase-R row;
  README.md's "Known gaps" row for "Files page has no Delete remote" removed (resolved) and the
  feature bullet list updated to describe the two-scope dialog.

  No agent can see the rendered UI -- the dialog's actual appearance (checkbox layout, warning
  banner styling) was never visually verified, only exercised through pure-function unit tests
  and TypeScript's own type checking.

  All gates green: `ruff check`/`format --check` clean on the whole repo; backend `uv run
  pytest` 1186 passed, 0 skipped (up from 1174 before this task -- 12 new tests: 10 in
  test_delete_api.py, 2 e2e in the new test_manual_source_delete_e2e.py against the real fake
  seedbox, both actually confirming the remote file is gone via an independent rescan); frontend
  `npm run lint`/`npm test`/`npm run build` all clean (378 frontend tests, up from 363 -- 15 new
  pure-function tests, no regressions).
---

# Task: manual delete dialog — independent Local and Source scopes, source checked by default on move

User-approved design (2026-08-16, second half of the delete-ladder rework — read
`prompts/done/2026-08-16-move-delete-gate-ladder.md`'s result first; this task assumes
the ladder is in). The manual cleanup step: when the user deletes an item whose **source
still exists on the seedbox**, the delete dialog offers both scopes independently so
failed/never-imported items can be cleaned up entirely from the app, without SSHing into
the seedbox.

## The dialog

Extending the existing per-item delete confirmation (the manual local-delete flow):

- Two checkboxes: **Delete local copy** and **Delete source (seedbox)**. The source
  checkbox only renders when the remote copy actually exists.
- Defaults by queue semantics (settled): `move` queue → **both checked**; `copy` queue →
  local checked, **source unchecked** (§7.1 — a copy queue may point at live torrent
  data; deleting source there can destroy a seed). The dialog shows the §7.1 warning
  text when source is checked on a `copy` queue.
- At least one box must be checked to proceed. Local-only keeps today's exact behavior;
  source-only is now possible (clear the seedbox, keep local).
- Applies wherever the existing delete confirmation appears (per-item; if a bulk delete
  flow exists, same scopes with honest partial-failure reporting — check what actually
  exists, don't invent a bulk flow that doesn't).

## Backend

- The first **manual** remote-delete path in the API: extend the existing delete
  endpoint (or add the narrow sibling that fits `api/`'s conventions) with the source
  scope. It MUST reuse `RemoteConnectionPool.delete_path` and write the same audit
  events the automatic pipeline writes (`remote_delete`, `remote_delete_failed`),
  message marked as manual ("deleted by user request") — the audit trail must
  distinguish manual from pipeline deletes.
- Refuse (409-style, clear message) when a job for the item is active; stop-then-delete
  stays the existing two-step it is today.
- Suppression: a manual source delete on an auto-queue-eligible item must not strand a
  re-download loop — apply the same `auto_queue_suppressed` handling the local-delete
  path already uses; verify rather than assume which scopes need it.
- Deleting source for an item mid-ladder (deferred delete pending) simply completes the
  ladder early — make sure the pipeline/arrsync handle "already deleted" gracefully
  (idempotent, event says so).

## Tests

- Dialog logic (pure functions): checkbox defaults per queue mode, at-least-one rule,
  source checkbox hidden when no remote copy.
- Backend: source scope deletes remotely + writes the manual-marked event; active-job
  refusal; suppression applied; idempotent when remote already gone; copy-queue source
  delete works but is never implied by local-only requests.
- E2E-style (fake seedbox): manual source delete actually removes the remote tree,
  confirmed by an independent scan.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report.
- `feat:` prefix. Docs same commit: `docs/decisions.md` (defaults reasoning, §7.1
  interplay), `CHANGELOG.md` Unreleased, startnewsession.md table row, and update
  `README.md`'s "Known gaps" if it still claims no manual delete endpoint exists.

## Verification gates — run each separately and read its exit code

1. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
2. `uv run pytest` — note skip counts honestly; bring up the fake seedbox for the e2e if
   the suite needs it, tear down after.
3. `cd frontend && npm run lint && npm test && npm run build`.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   commit message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
