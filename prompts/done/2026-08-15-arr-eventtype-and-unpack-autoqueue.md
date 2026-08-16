---
name: 2026-08-15-arr-eventtype-and-unpack-autoqueue
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Both fixes landed. `core/arrclient.py`: IMPORT_EVENT_TYPES is now string-keyed
  ("downloadFolderImported"), normalized in one place (HistoryEvent.is_import_event()), with the
  legacy numeric code (3) kept as a tolerated fallback. `core/autoqueue.py`: a top-level item
  whose name starts with UNPACK_PREFIX/FAILED_PREFIX (imported from core/extract.py, not
  duplicated) is skipped before pattern matching, unconditionally -- remote scan visibility is
  untouched, manual queueing is untouched. Tests added in test_arrclient.py (unit-level
  HistoryEvent.is_import_event string + legacy-numeric coverage), test_arrsync.py (end-to-end
  detected->imported via string events, and via the legacy-numeric fallback), test_arr_cleanup.py
  and test_arrsync.py's shared _import_event helper switched to the string wire format, and
  test_autoqueue.py (_UNPACK_/_FAILED_ exclusion + the renamed-item-becomes-eligible-again case).
  docs/decisions.md gained two newest-at-top entries; prompts/startnewsession.md gained a new
  newest traps entry and a phase-D row + verification paragraph in the arr build-run table;
  CHANGELOG.md gained two Unreleased/Fixed entries. All four gates green: ruff check, ruff format
  --check, backend pytest (1132 passed, 0 skipped), frontend lint/test/build (285 passed, 0
  skipped). tests/fake_arr.py itself needed no change -- it's a dumb passthrough with no hardcoded
  eventType shape; the wrong assumption lived in the individual tests' fixture *data*, which is
  what was actually fixed.
---

# Task: fix *arr import detection (string eventTypes) + exclude remote _UNPACK_/_FAILED_ from auto-queue

Two live-testing fixes from the first real Sonarr run against the v0.1.1+arr build. Both
diagnosed read-only against the live instance's audit trail; the reasoning is settled —
this task is implementation.

## Before you start

- Read `docs/arr-integration-spec.md` (lifecycle + fully-done sections) and the module
  docstring of `backend/lftpweb/core/arrclient.py`.
- Context for fix 1: on the live instance, two releases (Gold Rush S16, NCIS New Orleans
  S07) were matched, transferred, notified, and genuinely imported by Sonarr — and both
  were classified `gone` ("no import history event"). Root cause: the *arr v3 API returns
  `eventType` in **response bodies as camelCase strings** (`"downloadFolderImported"`,
  `"grabbed"`, …); the numeric codes are only meaningful as query parameters.
  `IMPORT_EVENT_TYPES = {3}` can therefore never match a real record. The fake-*arr
  fixture encoded the same wrong assumption, which is why all tests were green.
- Context for fix 2: the user's seedbox runs SABnzbd, which stages into `_UNPACK_<name>`
  directories on the remote while unzipping, then renames to the final dir. The live
  instance shows 16 such remote dirs (~34 GB) as `REMOTE_ONLY` items — real auto-queue
  candidates currently held back only by the settle gate. **User decision (2026-08-15):
  keep them VISIBLE in the tree (they exist; people should see them), but auto-queue must
  never queue them. Manual queue stays allowed.** Do NOT filter them from the remote scan.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report.
This prompt file is exempt.

## What to do

1. **`core/arrclient.py`**: make the import-event classification match reality —
   `IMPORT_EVENT_TYPES` becomes the string names (`"downloadFolderImported"` for both
   kinds), while tolerating the numeric codes as a fallback (cheap, and keeps any *arr
   version or serializer setting working). Normalize in one place (the comparison or a
   small helper on `HistoryEvent`), not at call sites. Update the module docstring: the
   response-body vocabulary is now **verified against a live Sonarr v3** (2026-08-15, via
   the lftpweb audit trail + fix); `trackedDownloadState` strings (`"importing"`,
   `"imported"`) were already correct.
2. **`tests/fake_arr.py`**: serve `eventType` as the camelCase **string**, exactly like a
   real *arr — the fixture modeling the wire format wrongly is the reason this shipped.
   Adjust existing tests; add one test pinning that an int-typed `eventType` is *also*
   accepted (the tolerance), and one asserting the string path end-to-end
   (detected → imported via string events).
3. **Auto-queue exclusion** (`core/autoqueue.py`): a top-level item whose name starts
   with `_UNPACK_` or `_FAILED_` is never auto-queued, regardless of state or patterns —
   same rationale as `core/local_scan.py`'s local filter (someone's staging is not
   content), but applied at auto-queue eligibility instead of scan visibility, per the
   user's "show it, don't grab it" decision. Reuse/share the prefix constants with
   `local_scan.py` rather than duplicating the strings. Manual queueing (API) remains
   allowed — do not touch it. Add tests: an `_UNPACK_`-prefixed `REMOTE_ONLY` item with
   auto-queue on and matching patterns is not queued; the same item renamed to its final
   name is.
4. **Docs, same commit**: `docs/decisions.md` entries for both (newest at top — for fix 1
   include the string-vs-int wire fact; for fix 2 include the user's show-don't-grab
   reasoning and the SAB staging story). Add the eventType lesson to
   `prompts/startnewsession.md`'s traps list ("*arr enums are strings in bodies, ints in
   query params — the fixture must model the wire, not the assumption") and update the
   arr build-run table with this fix. `CHANGELOG.md` under Unreleased, matching style.

## Conventions to honor

- `fix:` prefix. No behavior changes beyond the two named.
- Note in your report that already-`gone` items on the live instance stay `gone`
  (terminal by design); the fix applies to future associations.

## Verification gates — run each separately and read its exit code

1. `uv run ruff check backend`
2. `uv run ruff format --check backend`
3. `uv run pytest` — note skip counts honestly.
4. `cd frontend && npm run lint && npm test && npm run build` (untouched; prove it).

## When done

1. Update this file's frontmatter; `git mv` to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `fix:` message, each gate's exact result, decisions/deviations. The orchestrating
   session surfaces the commit to the user. Never `git add -A`, never push.
