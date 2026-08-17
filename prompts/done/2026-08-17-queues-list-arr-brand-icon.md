---
name: 2026-08-17-queues-list-arr-brand-icon
status: done
created: 2026-08-17
model: sonnet
completed: 2026-08-17
result: |
  Settings -> Queues' queue list now shows the real Sonarr/Radarr brand logo beside the name of
  any queue with a bound arr_instance_id, muted (opacity-50) when the bound instance is
  currently disabled, falling back to a small text chip naming the instance id when the bound
  id isn't in the loaded instance list (deleted instance, or a failed/settled-empty fetch) --
  renders nothing only while unbound or while the instances fetch is still in flight.
  `LifecycleIcons.tsx` gained an exported `ArrBrandMark({ kind, title, muted? })` -- the plain
  logo-or-text-chip mark with no status overlay -- and `ArrRowChip` was rebuilt on top of it so
  there is still exactly one kind -> logo mapping in the file. `QueuesTab.tsx`'s `useArrInstances`
  hook now also returns a `loaded` flag (needed to tell "still fetching" apart from "fetch
  settled to a list without this id"), and gained the new pure `queueArrBindingMark` helper,
  covered by 5 new Vitest cases (bound+enabled sonarr, bound+disabled radarr, unbound,
  bound-but-missing instance, instances-not-yet-loaded) in QueuesTab.test.ts. No new dependency,
  no backend change. Docs: CHANGELOG.md Unreleased/Added. No docs/decisions.md entry -- no
  deviation from the prompt's settled rules. All gates green: frontend lint (oxlint, exit 0,
  pre-existing-pattern warnings only), npm test (455 passed), npm run build (tsc+vite, exit 0);
  backend ruff check + ruff format --check (both clean, untouched by this task); `uv run pytest`
  from the repo root, run in the foreground after a first background attempt's notification was
  mishandled -- 1279 passed, 0 failed, exit 0. No agent can see the rendered UI -- unviewed, per
  usual; this is a code-review-only verification, not a visual confirmation.
---

# Task: Settings → Queues list shows the Sonarr/Radarr brand icon on bound queues

User request (2026-08-17): the queue list table on Settings → Queues gives no visual
hint that a queue is bound to an *arr instance — you have to open Edit and look at the
dropdown. Show the same real Sonarr/Radarr brand logo the Files/Transfers/History rows
already use (one visual language everywhere — the explicit rationale of
`prompts/done/2026-08-16-files-brand-logo-icons.md`) beside the queue's name for any
queue with an `arr_instance_id` bound. Frontend-only.

## Before you start

- Read `CLAUDE.md`. Read before editing:
  - `frontend/src/pages/settings/QueuesTab.tsx` — the queue list table (~lines
    650–700: `queues.map((q) => …`, Name is the first `<td>`), and note the tab
    **already fetches `listArrInstances()`** (~line 161) for the edit form's dropdown —
    reuse that state, do not add a second fetch.
  - `frontend/src/components/LifecycleIcons.tsx` — `SonarrLogo`/`RadarrLogo` (real
    brand SVGs, currently module-private) and `ArrRowChip`/`ArrTextChip` (~lines
    440–560), to reuse rather than duplicate.
  - `prompts/done/2026-08-16-arr-chip-on-row-lines.md` and
    `…-files-brand-logo-icons.md` frontmatter/results for the one-visual-language
    history this extends.

## Working tree check

Before making any edits, run `git status --porcelain` and cross-reference the files
this plan needs to modify. If any of those files have uncommitted changes, list them
and ask the user before touching them. Surface unrelated dirty files once as
awareness; don't block. This file (the handoff prompt itself) is exempt.

## What to do

1. **A plain brand-logo component, shared not duplicated.** `ArrRowChip` is the wrong
   component here — it renders *item status* (overlay badge, `arr_status`-driven,
   returns null without one); this is a *binding* indicator. Extract/export the
   minimal piece instead: e.g. an exported `ArrBrandMark({ kind, title, muted? })` in
   `LifecycleIcons.tsx` that renders `SonarrLogo`/`RadarrLogo` by `kind` with an
   `ArrTextChip`-style text fallback for an unrecognized/future kind, and have
   `ArrRowChip` consume it internally so there is still exactly one kind→logo mapping.
   Do not just `export` the raw logo SVG components and re-implement the kind switch
   in the tab.
2. **QueuesTab renders it in the Name cell.** For each queue row with a non-null
   `arr_instance_id`, resolve the instance from the already-fetched instances state
   and render the mark inline after the queue name (small, `shrink-0`, vertically
   centered — match the row-chip sizing). Tooltip: `Bound to <kind-capitalized>
   instance '<name>'`, appending ` (instance disabled)` when the instance's
   `enabled` is false — and render the mark muted (reduced opacity) in that case,
   since a disabled instance means the binding is currently inert. A queue with no
   binding renders nothing new. A bound `arr_instance_id` whose instance isn't in the
   fetched list (deleted instance, or the fetch failed/hasn't resolved) falls back to
   the text chip with the tooltip naming the id — never silently nothing for a bound
   queue once instances have loaded; while the fetch is simply still in flight,
   rendering nothing is fine.
3. **Pure helper for the decision, tested.** Put the "what does this queue render"
   logic (kind/name/muted/tooltip | null) in a pure function — either alongside the
   existing pure predicates in `QueuesTab.tsx`'s testable helpers or in the `lib/`
   module the existing `pages/settings/QueuesTab.test.ts` imports from — and cover it
   in Vitest: bound+enabled sonarr, bound+disabled radarr, unbound, bound-but-missing
   instance, instances-not-yet-loaded.
4. **Docs, same commit:** `CHANGELOG.md` — one `### Added` (or `### Changed`) entry
   under Unreleased, appended after what's already there. A `docs/decisions.md` entry
   only if you make a genuinely non-obvious call (the muted-when-disabled and
   text-fallback rules above are settled by this prompt; don't re-record them unless
   you deviate).

## Conventions to honor

- Gates, each run separately, exit codes read: frontend `npm run lint`, `npm test`,
  `npm run build`; backend untouched — re-verify anyway (`uv run --project backend
  ruff check`, `uv run --project backend ruff format --check`, `uv run pytest` from
  the repo root).
- Comment style: dated, incident/rationale-naming — match the neighboring docstrings.
- No browser here — the rendered result ships unviewed; say so, never claim visual
  confirmation.
- Conventional-Commit prefix `feat:`; no `Co-authored-by:` trailers.

## When done

1. Update this file's frontmatter: set `status`, `completed`, `result`.
2. `git mv`/move this file into `prompts/done/` (success) or `prompts/failed/`
   (failure).
3. Hand off ONE commit covering this prompt file, the files modified, and the prompt
   move. Present the file list and a one-line message.
   - **You are a spawned agent:** do **not** commit. Prepare the working tree and
     report the file list + proposed message back to the orchestrating session.
   Never `git add -A`, never push, never auto-commit. Branch is `dev`.
