---
name: 2026-08-16-cleaned-icon-keeps-green-check
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: >
  cleaned now maps to the same green-check icon variant as imported
  (lib/fileTree.ts.ARR_ICON_VARIANTS). LifecycleIcons.tsx.ArrIcon and
  TransfersPage.tsx's *arr expand-panel group inherited it unmodified via the
  shared arrIconVariant/arrHoverLabel helpers -- no fork needed. Hover text
  already distinguished "imported" from "imported and cleaned up locally" and
  needed no change. Tests, docs/arr-integration-spec.md, CHANGELOG.md,
  startnewsession.md, and docs/decisions.md updated in the same pass. All
  gates green; not committed per instructions.
---

# Task: the *arr icon stays green-✓ through the `cleaned` grace window

User feedback (2026-08-16, first live Radarr run): with "Delete when imported" on,
`imported` is a seconds-long transient (cleanup runs on the next poller beat), so the
green ✓ flashes and is replaced by the `cleaned` presentation — which today is the *arr
mark + "Processed · Xm" countdown **without** the green check. The success indicator
effectively never gets seen. Decision: **`cleaned` renders the same green-✓ icon variant
as `imported`**, alongside the existing "Processed · Xm" countdown chip.

## What to do

1. `frontend/src/lib/fileTree.ts` — `arrIconVariant` (and `arrHoverLabel` if it
   distinguishes): map `cleaned` to the same green-check variant as `imported`; hover
   text stays distinct (e.g. "imported and cleaned up" vs "imported") so the two states
   remain tellable apart.
2. Anywhere else the variant mapping is consumed (Files row, Transfers expand panel's
   *arr group — it reuses these helpers) inherits the change automatically; verify, don't
   fork.
3. Tests: update the icon-state mapping tests (`cleaned` → green-check variant) and any
   snapshot-ish assertions.
4. Docs same commit: update the icon table in `docs/arr-integration-spec.md` (UI section)
   and its mirror in `DESIGN.md` §16 if the table is repeated there; `CHANGELOG.md`
   Unreleased; startnewsession.md arr build-run table row.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report.
- `fix:` prefix (restores the intended visibility of a shipped indicator).

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
   `fix:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
