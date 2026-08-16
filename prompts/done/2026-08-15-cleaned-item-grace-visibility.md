---
name: 2026-08-15-cleaned-item-grace-visibility
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Both hypotheses confirmed, mechanism more precise than either alone: (1) `_protected_rel_paths`
  treats any `auto_queue_suppressed = 1` row as frozen -- correct for `deleted_local()`/STOPPED/
  FAILED, wrong for arr cleanup (which sets that flag first, per spec, purely to block
  re-download) -- excluding the row from `_persist`'s vanished-from-both-trees sweep entirely, so
  `first_missing_at` never starts and the row drops out of `written`/`_project`'s published set
  the very next scan; (2) even once unprotected, a verify-skipped move-mode item rests at
  `state == "LOCAL_ONLY"` (not one of `resolve_absence`'s `_STICKY_PREV_STATES`), so it would
  fall straight to `resolve_vanished`'s instant-`REMOVED_BOTH` fallback with no grace at all --
  exactly what produced the live evidence's own "earlier REMOVED_BOTH rows in the same queue."
  Fixed narrowly in `core/engine.py._persist`: `_protected_rel_paths`'s SQL now exempts
  `arr_status = 'cleaned'` rows, and the vanished sweep remaps `LOCAL_ONLY` + `arr_status ==
  'cleaned'` to `"DOWNLOADED"` before calling `resolve_absence`, reusing its existing grace
  machinery unmodified. `core/mount_sentinel.py` untouched. Three new tests in
  `tests/test_state_persistence.py`; all gates green (backend 1154 passed, frontend 302 passed,
  both lints, build). The "stalled LOCAL_ONLY" scope note investigated and found to be
  intentional, pre-existing, correct behavior, not a bug -- documented in `docs/decisions.md`
  rather than filed to open-issues.md.
---

# Task: a cleaned item must stay visible with the "Processed · Xm" countdown — it vanishes instantly

Live defect, first real run of the *arr delete-completed flow (2026-08-16 ~04:29 UTC,
`NCIS New Orleans S05` on the ar-tv `move` queue). Everything worked (matched → notified
→ imported → cleanup deleted the local copy) **except** the promised UX: the spec
(`docs/arr-integration-spec.md`, Cleanup section) says the cleaned row stays visible
through the existing ~10-minute removal grace as "Processed · Xm", then leaves. Instead
the row vanished from the Files page immediately.

## Live evidence (gathered read-only; trust but verify against code)

- `GET /api/files` STILL returns the item minutes and several scan passes later:
  `state: LOCAL_ONLY`, `arr_status: "cleaned"`, `first_missing_at: null`,
  `state_changed_at` predating the cleanup. So the DB row exists, the grace clock never
  started, and REST and the user's WS-driven view disagree — the exact class of split the
  publish invariant exists to prevent.
- The user's Files page (rendered purely from the WS stream) dropped the row instantly.
- Queue is `move` mode: the **remote** copy was already deleted at download time, so
  after cleanup the item exists in **neither** tree.

## Working hypotheses — verify, don't assume

1. **Visibility:** `core/engine.py._project`'s `rel_paths` filter (documented as
   load-bearing) drops an item absent from both trees, so `diff_nodes` emits `removed`
   and the WS view loses the row immediately — before any grace logic runs. REST
   (`api/files.py`, reads `item` directly) keeps showing it.
2. **The stalled state:** `_persist`/`resolve_absence` never starts the absence grace
   (`first_missing_at` stays null) for this item — possibly because the absence path only
   considers items still present in one tree, or the `REMOVED_*` transition logic skips
   an item in neither tree, or something protects lifecycle states. Explain the actual
   mechanism you find; the earlier `REMOVED_BOTH` rows in the same queue prove *some*
   path handles both-absent items — find why this one differs (the active-at-the-time
   job? `LOCAL_ONLY` being a presence state? scan-cadence gating?).

## Required behavior (the spec's promise, now pinned)

After arr cleanup removes an item's local bytes:

1. The item **stays in the published view** (WS deltas AND snapshot AND REST — one
   projection, no disagreement) for the standard removal-grace window, carrying
   `arr_status: cleaned` so the frontend renders "Processed · Xm" (that wording already
   ships; it just never gets a row to render on).
2. The grace clock actually starts (`first_missing_at` or whatever field the countdown
   derives from gets set on the first scan that observes the absence).
3. When the grace expires, the item transitions through the normal machinery
   (`REMOVED_LOCAL`/`REMOVED_BOTH` as appropriate) and leaves the view through the normal
   `removed` delta — no new timer, no special-case lifetime.

## Constraints

- **Do not gut `_project`'s filter.** Its load-bearing purpose (never resurrect rows that
  left both trees long ago, keep `diff_nodes.removed` functional) must survive. Widen it
  *narrowly* — e.g. items inside an active grace window — in whatever form matches the
  code's actual shape. An item that left both trees months ago must stay invisible.
- Publish-invariant discipline: whatever becomes visible must be read back from the
  `item` table through `core/itemview.py` — never a structural candidate.
- Scope: this is about the post-cleanup grace window. Do not redesign absence handling
  generally; if you find a second, pre-existing bug outside this scope (e.g. the stalled
  `LOCAL_ONLY`), fix it only if it IS this bug's mechanism — otherwise document it
  precisely in your report and `prompts/open-issues.md` for triage.
- Tests: an integration-style test reproducing the exact scenario — `move`-queue item
  (remote already deleted), arr cleanup removes local bytes, then scan passes run:
  assert the item stays in the projected/published set with `arr_status=cleaned` during
  grace, `first_missing_at` gets set, and after grace expiry it transitions and leaves
  the projection. Plus whatever unit tests pin the mechanism you changed.

## Conventions to honor

- `fix:` prefix. Docs same commit: `docs/decisions.md` entry (mechanism + why the
  projection widening is safe), `CHANGELOG.md` Unreleased/Fixed, startnewsession.md arr
  build-run table row + a traps bullet if you learned one worth pinning.

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — note skip counts honestly.
4. `cd frontend && npm run lint && npm test && npm run build` (likely untouched; prove it).

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `fix:` message, each gate's exact result, the verified mechanism (hypotheses
   confirmed/refuted), and any decisions/deviations. Never `git add -A`, never push.
