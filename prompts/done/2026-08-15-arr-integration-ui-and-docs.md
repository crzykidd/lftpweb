---
name: 2026-08-15-arr-integration-ui-and-docs
status: done
created: 2026-08-15
model: sonnet
completed: 2026-08-15
result: >
  Settings -> Integrations tab (instance CRUD + write-only API key + Test button, new
  api/settings_arr.py client wrappers); Settings -> Queues gained the *arr instance dropdown,
  the delete-when-imported checkbox (disabled-with-hint unless bound, backed by two exported
  pure predicates for testing), and the visible-path field with FieldHelp; Files page gained
  its own resizable *arr icon column (multi-faceted: neutral/imported-green-check/gone-amber-
  warning, per lib/fileTree.ts.arrIconVariant) plus "*arr-tracked"/"gone" facet filters, and the
  removal-grace countdown chip now reads "Processed . Xm" instead of "Missing . Xm" for a
  cleaned item (same clock, reworded). The instance's own name for the icon's hover card is
  resolved client-side in FilesPage.tsx (queue's arr_instance_id -> a new listArrInstances()
  fetch) since the item projection itself carries only arr_status/arr_status_at, never the
  instance's identity -- recorded in docs/decisions.md, along with the icon-column-vs-R/L/V/E
  decision. Docs: DESIGN.md Sec16, README feature bullet + Known-gaps unviewed-UI note,
  CHANGELOG Unreleased entry, docs/concepts.md new section (+ jump entry, "seven"->"eight"
  fixed in both concepts.md and README). New frontend tests: arrIconVariant/arrHoverLabel (all
  five statuses + null + unknown), matchesFacetFilter's two new predicates, removalGraceLabel/
  removalGraceShortLabel's cleaned-wording branches, and QueuesTab's two exported pure
  predicates -- no component-render harness exists for Settings tabs in this suite, so those
  three are tested as pure functions per TransfersPage.tsx's own precedent. One pre-existing
  test (docMarkdown.test.ts's concepts.md section-count pin) updated 7->8 sections to match the
  new doc section, not touched otherwise. All 5 verification gates green: frontend lint/test
  (285 passed)/build; backend ruff check + ruff format --check (untouched, both clean) + full
  pytest (1125 passed, 0 skipped). No browser exists in this environment -- every screen this
  phase shipped is unviewed; visual correctness is not claimed anywhere.
---

# Task: Sonarr/Radarr integration — UI + docs (phase C of 3)

Build the frontend for the *arr integration (Settings → Integrations, the queue-level
binding controls, and the multi-faceted Files-page icon), and fold the feature into the
project docs. Phases A and B are committed; the backend is complete.

## Before you start

- Read **`docs/arr-integration-spec.md`** — especially the "UI" section's icon-state
  table and the `cleaned` countdown wording, and "Resolved decisions". The spec wins on
  any disagreement.
- Read phases A and B's result notes in `prompts/done/2026-08-15-arr-integration-*.md`
  and the API surface they actually shipped (routers `api/settings_arr.py`,
  `api/settings_queues.py`; projection fields `arr_status`, `arr_status_at`).
- Study before writing:
  - `frontend/src/` settings tabs — copy the structure of an existing CRUD-ish tab (the
    Auth tab's API-key management is the closest shape) for the new Integrations tab;
    `nav.ts` for tab registration.
  - The Queues settings form, for adding the three new fields; `FieldHelp` usage
    conventions (swept across Settings in `8dc3c15`).
  - `FileTree.tsx` + `lib/fileTree.ts` — note the recent split: **pure logic goes in
    `lib/fileTree.ts`**, keep the component thin. The existing "Missing · Xm" countdown
    chip (`3ae2873`) is what the `cleaned` state re-words.
  - How Files-page filters are implemented (client-side, phase 9).

## Working tree check

Run `git status --porcelain` before editing. This run is authorized unattended: if a file
you must touch is dirty, STOP and report back. This prompt file is exempt.

## What to do

1. **Settings → Integrations** (new tab): instance list; add/edit form (name, kind
   sonarr/radarr, base URL, API key — write-only, placeholder when set — enabled,
   notify-on-complete); delete with confirm; a Test button per instance calling
   `POST /api/settings/arr/{id}/test` and showing reachability + version.
2. **Settings → Queues**: per-queue "*arr instance" dropdown (instances fetched from the
   CRUD endpoint; "None" default), "Delete when imported" checkbox disabled-with-hint
   unless an instance is selected, and "Path as seen by the *arr" text field with
   FieldHelp explaining the namespace translation and that it describes the **post-move**
   location for a queue whose Move step relocates.
3. **Files page** — implement the spec's icon-state table exactly:
   - `detected`/`notified` → neutral *arr mark; hover names the instance + `arr_status_at`
     (and "importing…" courtesy text is backend-hover territory only if the projection
     carries it — do not invent a field).
   - `imported` → *arr mark + green ✓.
   - `gone` → *arr mark + amber ⚠.
   - `cleaned` → *arr mark + the removal countdown chip re-worded "Processed · Xm"
     instead of "Missing · Xm" (same clock, different words — presentational only).
   - Filters: an "*arr-tracked" facet, and `gone` filterable on its own.
   - Icon state derives purely from `arr_status` on the WS/REST item payload — add the
     fields to the TypeScript item types.
4. **Frontend tests** (Vitest): the icon-state mapping (all five statuses → expected
   variant), the countdown re-wording for `cleaned`, and the queues-form
   disabled-with-hint logic. Component-render tests to the depth the existing suite goes.
5. **Docs, same commit:**
   - `DESIGN.md`: add **§16 — Sonarr/Radarr integration**, a concise architectural
     summary (the three namespaces, the facet-not-state rule, the three escalating
     opt-ins, the fully-done gate) referencing `docs/arr-integration-spec.md` for detail.
     Do not renumber or rewrite existing sections.
   - `README.md`: a feature bullet; note in "Known gaps" anything shipped-but-unviewed.
   - `CHANGELOG.md`: entries under the unreleased section, matching its existing style.
   - `docs/how-it-works.md` / in-app docs: a short section on the integration if the
     existing structure has an obvious home for it; skip rather than force it.

## Conventions to honor

- **No agent can see the rendered UI.** Every screen this phase ships is unviewed until
  the user opens it — say so plainly in your report and in the startnewsession.md row;
  never claim visual correctness.
- Tailwind + existing component idioms; no new dependencies.
- Update the "*arr integration build run" table in `prompts/startnewsession.md` (this
  phase's row + mark the run complete). Record non-obvious decisions in
  `docs/decisions.md`.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. `uv run ruff check backend` and `uv run ruff format --check backend` (untouched;
   prove it)
5. `uv run pytest` — note skip counts honestly.

## When done

1. Update this file's frontmatter (`status`, `completed`, `result`).
2. `git mv` into `prompts/done/` (success) or `prompts/failed/` (failure).
3. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. The orchestrating
   session commits. Never `git add -A`, never push.
