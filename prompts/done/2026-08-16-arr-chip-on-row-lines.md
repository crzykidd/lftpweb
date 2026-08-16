---
name: 2026-08-16-arr-chip-on-row-lines
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: >
  Transfers/History row lines gained a real Sonarr/Radarr brand-logo chip with a green-check/
  red-dot status overlay, shared via LifecycleIcons.tsx.ArrRowChip and lib/fileTree.ts's new
  arrChipOverlay (thin wrapper over the existing arrIconVariant). Logo path data (SonarrLogo/
  RadarrLogo) sourced from the simple-icons dataset (CC0), itself citing Sonarr's/Radarr's own
  repos -- recorded in NOTICE with both provenance links. Unknown/future instance `kind` falls
  back to ArrTextChip (instance name, same status colors) -- never rendered nothing for a
  tracked item. Backend: JobOut and HistoryJobOut both gained arr_instance_kind (the chip's
  logo-selection field, since arr_instance_name is free text); HistoryJobOut also gained
  arr_status/arr_status_at/arr_instance_name, which it lacked before this task -- both wired
  via the same path_queue.arr_instance_id -> arr_instance LEFT JOIN core/queue.py.list_jobs()
  already used, now also selecting arr_instance.kind. Deliberately red (not the Files icon's
  amber) for `gone` on this chip -- two different specs for two different affordances, noted
  in-code. Tests: frontend (arrChipOverlay, all five statuses + null), backend (list_jobs and
  list_history_jobs both carry/null the new fields correctly). Docs: CHANGELOG.md Unreleased,
  startnewsession.md build-run table (row O), and docs/arr-integration-spec.md's UI section
  (light-touch addition beyond the prompt's literal doc list, to keep that section accurate).
  All gates green (frontend lint/test/build; backend ruff check+format; full `uv run pytest`,
  1166 passed, 0 skipped). Not committed per instructions.
---

# Task: *arr brand-logo chip with status overlay on Transfers and History job row lines

User request (2026-08-16, refined same day): on both the Transfers page and the History
jobs section, the collapsed/main row line shows an *arr chip — **the real Sonarr/Radarr
logo as a small inline SVG** — with the outcome as a status overlay: green when the *arr
processed it, red when it failed out, **absent entirely when the item wasn't
*arr-tracked**.

## The icon (user decision: real brand logos, not a generic mark)

- Small inline-SVG React components (`SonarrLogo`, `RadarrLogo`, ~14–16px) in
  `components/LifecycleIcons.tsx` alongside the existing icons. **Source the path data
  from the projects' own repositories or the simple-icons dataset — not an icon-scraper
  site** — and record both logos in `NOTICE` per the repo's bundled-third-party
  convention (name, source URL, license).
- Render the logos in their **brand colors** (Sonarr blue, Radarr gold) — recognition is
  the point; do not tint the whole logo by status.
- Unknown/future `kind` values fall back to a text chip of the instance name, same
  status colors — never render nothing for a tracked item just because the logo is
  missing.

## Status → overlay mapping

- `imported`, `cleaned` → small green check overlay ("processed")
- `gone` → small red dot/warn overlay (left the *arr's queue without importing)
- `detected`, `notified` → logo alone, no overlay (the *arr is watching; mid-flight) —
  note in your report so the user can veto in favor of hiding until an outcome exists.
- `arr_status` null → **no chip at all**.

Reuse/extend the status→variant logic that already exists (`lib/fileTree.ts` arr helpers,
as updated by `2026-08-16-cleaned-icon-keeps-green-check.md` — read its done/ result
first); one mapping, consumed everywhere. Hover = instance name + the status wording the
hover helpers already produce.

## What to do

1. **Transfers** (`TransfersPage.tsx`): chip on the collapsed row line (payload already
   has `arr_status` + `arr_instance_name` since the expand-panel task). Keep the line
   compact — the chip sits with the state chip cluster.
2. **History jobs** (backend first): `GET /api/history/jobs` rows gain `arr_status` +
   `arr_instance_name` via the same item/`arr_instance` LEFT JOINs `core/queue.py.
   list_jobs()` uses — two scalar columns on an already-paginated list, NOT a blob (the
   phase-6 trap doesn't apply, but say so). Then render the same chip on each job row in
   `HistoryJobsSection.tsx`.
3. Shared chip component (or one small function returning classes) — do not fork the
   color mapping between the two pages.
4. Tests: mapping (all five statuses + null → chip presence/color), backend join fields
   present in history rows and null for unbound queues.
5. Docs same commit: `CHANGELOG.md` Unreleased; startnewsession.md arr build-run table
   row.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report.
- `feat:` prefix. No new dependencies.

## Verification gates — run each separately and read its exit code

1. `cd frontend && npm run lint`
2. `cd frontend && npm test`
3. `cd frontend && npm run build`
4. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
5. `uv run pytest` — note skip counts honestly.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
