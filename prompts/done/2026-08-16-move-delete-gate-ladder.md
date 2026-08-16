---
name: 2026-08-16-move-delete-gate-ladder
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: |
  Implemented the four-rung delete ladder as specified. core/postprocess.py: the delete-gate
  call moved from between verify/extract to the tail of _process_item (after verify, extract,
  move/rename, and notify); `_maybe_delete_remote` gained an `extract_state` parameter and now
  defers (not withholds) on EXTRACT_FAILED, and defers to core/arrsync.py when the item is
  already *arr-tracked (arr_status non-null), recording the handoff in a new
  `item.remote_delete_pending` column (migration 019, stores the verify evidence 'VERIFIED' |
  'SKIPPED'). The actual asyncssh delete + event bookkeeping was factored into a module-level
  `perform_remote_delete`, reused by core/arrsync.py. core/arrsync.py: `_check_import` and
  `_commit_terminal` now take the full `queue` row (not just queue_id); `_commit_terminal`
  calls a new `_maybe_delete_remote_on_import` on a confirmed `imported` verdict, before that
  pass's `arr_delete_completed` cleanup sweep, giving "import green -> delete source -> delete
  local" ordering within one poll. ArrSyncScheduler gained `remote_pool`/`host_provider`
  constructor seams, wired in main.py from the same app.state.engine.pool/_host_provider
  postprocess already uses. CORRUPT still vetoes at every rung, including rung 4 (postprocess
  never sets remote_delete_pending on that branch). Docs updated: DESIGN.md §7/§7.3 (ladder +
  sync-mode-served-without-building note), docs/arr-integration-spec.md Cleanup + poller
  sections, docs/concepts.md's move-mode explanation, README.md Known gaps (entry removed,
  resolved), docs/decisions.md (full settled-design entry), CHANGELOG.md Changed,
  prompts/startnewsession.md (new phase-P row), prompts/open-issues.md (#2 struck through,
  resolved) and docs/audit-v0.1.0.md (G1 struck through, resolved). Also fixed two stale
  ordering comments in core/engine.py and one in core/queue.py that described the old
  verify-delete-extract order. Added 8 new backend tests covering every settled behavior
  (extraction-failure defer + fixed-and-rerun, arr-tracked defer, unmatched-item-on-bound-queue
  still deletes at rung 3, CORRUPT vetoes even when arr-tracked, deferred item is scan-stable,
  arr delete-survives-detected/notified-fires-only-on-imported, never-on-gone, and cleanup
  composes source-then-local) plus updated one pre-existing test whose docstring asserted the
  old ordering. All gates green: ruff check/format clean, backend pytest 1174 passed (0 skipped,
  up from 1166 before this task), frontend lint/test/build all clean (362 frontend tests, no
  regressions).
---

# Task: move-mode remote delete becomes the LAST gate (the delete ladder)

User-approved design (discussed and settled 2026-08-16; this resolves **open issue #2 /
audit item G1**, the move-delete ordering question). Today a `move` queue deletes the
source between verify and extract — so an extraction failure, or an *arr that never
imports, discovers a problem *after* the only other copy is gone. The redesign: **the
source is deleted only after the last enabled check passes.** Files exist on both sides
until then, so any failure is inspectable on both sides.

## The ladder — settled rules

On a `move` queue, the remote delete fires when ALL of the applicable rungs below have
passed, and not before:

1. **Completeness** (always) — local bytes match the remote inventory. Unchanged.
2. **Verify** — if it ran. `CORRUPT` is a hard veto at every rung, exactly as today
   ("verification must not have *failed*"; `SKIPPED` passes). Verify stays forced-on for
   `move` queues as today.
3. **Extract** — if archives were present and extraction is enabled: extraction must
   have succeeded. An extraction failure now *withholds* the delete (today it can't —
   it runs after the delete).
4. ***arr import** — ONLY if the item is *arr-tracked (`arr_status` non-null) by
   postprocess time: the delete waits for `imported` (the existing three-layer,
   two-pass-confirmed signal in `core/arrsync.py`). An item on a bound queue that never
   matched (hand-dropped file, replaced grab) stops at rung 3 — it must NOT wait
   forever on an *arr that has never heard of it.

Settled behaviors:
- **No timeout, no automatic fallback.** A `gone`/failed/corrupt item keeps its source
  until the user acts (the manual-delete dialog, a separate prompt, is the cleanup
  path). Deliberate: failure states stay inspectable on both sides.
- **Not a toggle** — this is simply how `move` works now. The change is strictly in the
  later/safer direction for existing installs. CHANGELOG under Changed.
- Every deferral writes an event naming the rung being waited on
  (`remote_delete_deferred`, message e.g. "source retained -- awaiting *arr import"),
  and the eventual delete keeps its existing `remote_delete` event. History must be
  able to answer "why is this still on the seedbox" in one call.

## Implementation notes

- `core/postprocess.py`: the delete-gate step moves from its current slot (after verify,
  before extract) to after the whole pipeline for non-arr-tracked items; for
  arr-tracked items the delete is handed off to `core/arrsync.py`, which performs it on
  the `imported` transition — BEFORE the (optional) `arr_delete_completed` local
  cleanup, so the ordering is: import green → delete source → (optionally) delete
  local. Reuse `RemoteConnectionPool.delete_path` and the existing gating/event helpers;
  do not write a second delete implementation.
- The remote copy may have been re-scanned in the window between DOWNLOADED and the
  deferred delete — make sure states/facets don't flap (the item has a local copy and a
  remote copy; that's an ordinary DOWNLOADED-shaped reading; verify with a test that a
  deferred-delete item's state is stable across scans while waiting).
- **UI legibility**: the Files drawer/hover for a `move`-queue item whose source still
  exists shows where it sits on the ladder ("Source retained — awaiting *arr import"),
  derived from existing fields (mode, `remote_deleted_at` null, `arr_status`,
  verify/extract timestamps) — no new state machine, a display-side derivation.
- `DESIGN.md`: update §7.3's delete-gate description to the ladder (sanctioned design
  change — cite this prompt and issue #2), and add the note to §7 that `sync` mode
  remains unscheduled AND its primary workflow (importer took it → clean up source) is
  now served by move-with-ladder + the manual delete dialog — so a future session
  doesn't build `sync` out of tidiness.
- `docs/arr-integration-spec.md`: the Cleanup section's claim that "on a move-mode queue
  the remote is already deleted by the delete gate" changes — update it.

## Tests (the point of the phase — delete timing is the risk)

- Non-arr item, no archives: delete fires after verify (or completeness when SKIPPED) —
  the previous behavior, now at the pipeline tail.
- Archive release, extraction fails: source NOT deleted, `remote_delete_deferred` →
  withheld path exercised; extraction later fixed/re-run → delete fires.
- arr-tracked item: source survives verify+extract, persists while `detected`/
  `notified`, deletes only on `imported` (two-pass), and never on `gone`.
- arr-tracked + `arr_delete_completed`: ordering is source-delete then local-cleanup.
- Bound queue, unmatched item: deletes at rung 3, does not wait.
- CORRUPT vetoes at every rung.
- Deferred item is scan-stable (no state flapping while both copies exist).

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- `feat:` prefix (behavior change by design decision). Docs same commit:
  `docs/decisions.md` (full reasoning incl. the four settled rules above and the
  rejected alternative — keeping the early delete with a toggle), `CHANGELOG.md`
  Changed, startnewsession.md table row, and close/annotate issue #2 references where
  the repo tracks them (`docs/audit-v0.1.0.md` G1, `prompts/open-issues.md`) as
  resolved-by-design pointing here.

## Verification gates — run each separately and read its exit code

1. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
2. `uv run pytest` — note skip counts honestly.
3. `cd frontend && npm run lint && npm test && npm run build`.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   commit message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
