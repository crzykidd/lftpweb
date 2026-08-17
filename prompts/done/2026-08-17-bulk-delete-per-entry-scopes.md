---
name: 2026-08-17-bulk-delete-per-entry-scopes
status: completed
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: Added `effectiveDeleteScope` (lib/fileTree.ts) and made `FileTree.tsx`'s `runAction` compute Local/Source per entry instead of a blanket Local flag; skipped rows now get their own `BulkOutcome.skipped` bucket. Frontend-only; all gates green.
---

# Task: Bulk delete applies Local/Source per entry, not as blanket flags

A Files-page multi-select delete with both Local and Source checked errors for every
selected row that has no local content (user-reported, live). The Source scope is
already computed per entry in `FileTree.tsx`'s `runAction` — `sourceRequestedFor(e)`
only requests source when `hasRemoteCopy(e)` — but Local is a blanket flag: every row
gets `local: true` whenever the checkbox is checked. For a no-local-content row
(`REMOTE_ONLY`, or a stranded `REMOVED_LOCAL` row — exactly what `canDeleteLocal`'s
2026-08-17 widening made selectable), the backend's `delete_local` withholds with
"does not exist — nothing to delete", `api/jobs.py.delete_item` turns any local
withhold into a **409 before source is ever attempted**, and the row lands in the bulk
failure list with its source delete never tried. The mirror-image case is latent too:
Local unchecked + Source checked sends `{local: false, source: false}` for any row
without a remote copy → the backend's 400 "at least one of local/source must be
requested".

This task makes Local per-entry (symmetric to Source), skips rows where the checked
scopes leave nothing applicable, and reports those skips honestly. **Frontend-only —
no backend change.** A `REMOTE_ONLY` row correctly takes the backend's source-only
path, which already handles suppression (`deleted_source`) and the active-transfer
refusal.

## Before you start

- Read `CLAUDE.md` (per-session rules) and `DESIGN.md` §9.2 (Files-page actions).
- Read the current mechanism end to end before editing:
  - `frontend/src/lib/fileTree.ts` — `hasLocalContent`, `hasRemoteCopy`,
    `canDeleteLocal`, `shouldOfferLocalScope`, `shouldOfferSourceScope`,
    `canConfirmDelete`, `defaultSourceChecked` (the block starting ~line 260). The new
    helper belongs beside these and must read the same two underlying facts
    (`hasLocalContent`, `hasRemoteCopy`) rather than inventing a third predicate.
  - `frontend/src/components/FileTree.tsx` — `runAction` (~line 1450: the
    `sourceRequestedFor` closure, the `Promise.allSettled` map, the fulfilled-response
    `source_deleted` read-back, the `BulkOutcome`/`BulkFailure` reporting),
    `requestDeleteRow`/`requestDeleteSelected`/`confirmDelete`, and the
    `BulkOutcome` rendering further down so you know how failures are shown.
  - `backend/lftpweb/api/jobs.py.delete_item`'s docstring (~line 372) — the contract
    this must stop violating. Do **not** change it.
- Relevant history: `prompts/done/2026-08-16-manual-delete-local-and-remote.md`
  (the independent-scopes design) and
  `prompts/done/2026-08-17-stranded-source-delete-retry.md` (the `canDeleteLocal`
  widening that made no-local-content rows selectable — the proximate cause here).
- No browser exists in this environment; verification is lint + Vitest + build, and
  say so rather than claiming visual confirmation.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. Surface unrelated dirty files once as
awareness; don't block. This file (the handoff prompt itself) is exempt — it's
expected to be modified by "When done" below.

## What to do

1. **New pure helper in `frontend/src/lib/fileTree.ts`** — e.g.

   ```ts
   export function effectiveDeleteScope(
     node: FileNode,
     checked: { local: boolean; source: boolean },
   ): { local: boolean; source: boolean } | null
   ```

   Rules: `local` is requested only when `checked.local && hasLocalContent(node)`;
   `source` only when `checked.source && hasRemoteCopy(node)`; if both come out false,
   return `null` — meaning "send no request for this row at all". Docstring should
   name the incident (blanket Local flag → 409 per no-local row in a mixed bulk
   delete) the way the neighboring helpers name theirs.

2. **`runAction` in `frontend/src/components/FileTree.tsx` consumes it.** Replace the
   `sourceRequestedFor` closure and the blanket `deleteScope?.local` with one
   per-entry `effectiveDeleteScope` computation, computed once per entry and reused
   for (a) building the request, (b) deciding whether to send one, and (c) the
   fulfilled-response read-back that checks `source_deleted === false` — that check
   must key off "was source requested *for this entry*", exactly as
   `sourceRequestedFor` does today.

3. **Skipped rows are reported, not errored and not silently dropped.** A row whose
   effective scope is `null` gets no HTTP request. It must not be counted as succeeded
   (it should stay selected, like failures do) and must not appear as a failure with a
   fake error. Extend the bulk outcome reporting with a distinct skipped bucket —
   e.g. `BulkOutcome` gains `skipped: { rel_path, name, reason }[]` (reason along the
   lines of "no local copy — only Local was selected" / "no remote copy — only Source
   was selected") — and render it in the outcome summary as its own line, visually
   distinct from failures. Keep the summary arithmetic honest:
   `total = succeeded + failures + skipped`. If, after filtering, *no* row has an
   applicable scope, don't fire a zero-request "success" — surface the same skipped
   summary with 0 succeeded.

4. **Leave the dialog's checkbox seeding/validation logic alone** —
   `shouldOfferLocalScope`/`shouldOfferSourceScope`/`canConfirmDelete`/
   `defaultSourceChecked` already do the right thing at the dialog level; this task is
   about the per-entry requests behind Confirm. Queue/Stop paths through `runAction`
   are untouched.

5. **Tests** (Vitest, `frontend/src/lib/fileTree.test.ts` or wherever the existing
   helper tests live — match the file the current `canDeleteLocal`/
   `shouldOfferLocalScope` tests are in):
   - `effectiveDeleteScope` truth table: local-content+remote row × each checkbox
     combination; `REMOTE_ONLY` row with both checked → `{local: false, source: true}`;
     `REMOTE_ONLY` row with Local only → `null`; local-only row with Source only →
     `null`; local-only row with both → `{local: true, source: false}`.
   - The regression that matches the user's report: a mixed selection (one
     local-content row, one `REMOTE_ONLY` row), both boxes checked → the remote-only
     row's request is `{local: false, source: true}`, never `{local: true, …}`.
   - If the skipped-bucket logic is factored into a testable pure function (do so if
     it's more than trivial), cover it too.

6. **Docs, same commit:**
   - `CHANGELOG.md` — add a `### Fixed` entry under the Unreleased section (create the
     section if the v0.2.2 roll left none), user-voiced: bulk delete of a mixed
     selection no longer errors rows that have no local copy; each row now gets only
     the scopes that apply to it, and rows with nothing applicable are reported as
     skipped.
   - `docs/decisions.md` — one entry, newest at top (2026-08-17): the per-entry-scope
     decision, the skipped-not-errored call, and the rejected alternative (making the
     backend treat "nothing local to delete" as an idempotent success when source is
     also requested — rejected because the 409-on-local-withhold contract is
     deliberate for single-row deletes and the frontend already holds the per-row
     facts).

## Conventions to honor

- Frontend gates, all green before handing off: `npm run lint`, `npm test`,
  `npm run build` (run from `frontend/`). Backend untouched — re-verify anyway
  (`uv run ruff check`, `uv run ruff format --check`, `uv run pytest` from
  `backend/`), per this repo's standing practice.
- Comment style: match the surrounding code's dated, incident-naming docstrings —
  state the constraint and the incident, not the edit.
- Every UI-visible claim is "builds, lints, tests green", never "renders correctly" —
  no browser exists here.
- Conventional-Commit prefix `fix:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter: set `status` (completed/failed), `completed` (the
   date), and `result` (one line).
2. `git mv` this file into `prompts/done/` (on success) or `prompts/failed/` (on
   failure).
3. Record the non-obvious decisions in `docs/decisions.md` (step 6 above).
4. Hand off ONE commit covering this prompt file, the files this session modified, and
   the prompt move (the prompt is **not** pre-committed — it bundles in here). Present
   the file list and a one-line message summarising the changes.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree, then
     report the file list + proposed message back to the orchestrating session, which
     surfaces the `y/n` to the user.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
