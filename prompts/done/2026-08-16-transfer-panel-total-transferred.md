---
name: 2026-08-16-transfer-panel-total-transferred
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: >
  Added transferredSummary() and wired it into transferGroupFields() in
  frontend/src/lib/transferPanel.ts: terminal jobs now show one "Transferred"
  field ("X in Y (Z avg)") in place of separate Elapsed/Average speed fields;
  running jobs unchanged. 6 new/updated tests in transferPanel.test.ts cover
  the normal case, missing bytes_total, the zero-elapsed no-divide-by-zero
  guard, and the redundancy collapse. Docs updated in the same change:
  CHANGELOG.md Unreleased and prompts/startnewsession.md. All gates green:
  frontend lint/test/build, ruff check/format (0.8.4), pytest (1162 passed).
---

# Task: Transfers expand panel — show total transferred, not just elapsed + avg speed

User feedback (2026-08-16, live use): clicking a terminal Transfers row open, the panel's
Transfer group shows elapsed time and average speed but **not the total bytes moved**.
The natural reading they asked for: "**14.8 GB in 6m 12s (40 MB/s avg)**".

## What to do

1. `frontend/src/lib/transferPanel.ts` — `transferGroupFields`: add a **Transferred**
   field for terminal jobs composing bytes_done + elapsed + average speed into the single
   "X in Y (Z avg)" reading above (reuse the existing byte/duration/rate formatters —
   don't add new ones). If separate Elapsed / Average speed fields become redundant next
   to it, collapse them into it rather than showing the same numbers twice; active jobs'
   fields stay as they are.
2. Tests: the composed field's formatting (normal case, missing bytes_total, zero
   elapsed guard — no divide-by-zero rate), redundancy collapse if done.
3. Docs same commit: `CHANGELOG.md` Unreleased; startnewsession.md arr build-run table
   row.

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **No agent can see the rendered UI** — say so in your report.
- `fix:` prefix (completes the panel's promised "every figure" coverage).

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
