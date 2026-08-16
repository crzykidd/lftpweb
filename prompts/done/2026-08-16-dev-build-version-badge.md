---
name: 2026-08-16-dev-build-version-badge
status: done
created: 2026-08-16
model: sonnet
completed: 2026-08-16
result: |
  Implemented end to end. docker/Dockerfile's runtime stage (only) accepts BUILD_SHA/
  BUILD_CHANNEL build args, baked to LFTPWEB_BUILD_SHA/LFTPWEB_BUILD_CHANNEL env vars;
  .github/workflows/publish.yml computes a short SHA + dev/release channel and passes
  both via build-args:. config.Settings gained the two fields plus a field_validator
  folding a baked-but-blank env var back to None. /api/health carries build_sha/
  build_channel (both null unless baked). Frontend: new lib/versionBadge.ts (pure,
  unit-tested) computes the nav badge; VersionLink.tsx renders it (amber "DEV: v0.1.1 ·
  <sha>" linking to the commit for a dev build, today's plain rendering otherwise).
  Docs updated same commit: CHANGELOG.md Unreleased, docs/decisions.md, startnewsession.md
  row N. All gates green (see agent report for exact commands/output); no CI job renamed.
---

# Task: dev builds identify themselves — "DEV: v0.1.1 · <short-sha>" in the UI footer

User request (2026-08-16): when running a `:dev` image, the version readout in the lower
left of the UI should show `DEV: v0.1.1 · <short hash>` so a test instance is never
mistaken for a release. Release builds keep showing plain `v0.1.1` exactly as today.

## The shape

1. **Bake at image build time** (the container has no git): the publish workflow
   (`.github/workflows/`, the job that pushes `:dev` on dev pushes and `:latest`/semver
   on main) passes two build args — the commit SHA (`GITHUB_SHA`) and a channel string
   (`dev` for the dev-push matrix leg, `release` for the main/tag legs). `docker/
   Dockerfile` accepts the ARGs and lands them where the runtime stage can read them
   (env vars or a small baked file — match whatever the Dockerfile already does for
   similar values; remember the runtime root filesystem is read-only).
2. **`/api/health`** grows `build_sha` (short, 7–10 chars, `null` when unset) and
   `build_channel` (`"dev" | "release" | null`). Precedent: this endpoint already grew
   `repo_url` beyond DESIGN.md §12's literal 4-field shape for the nav's needs — note
   the addition the same way (docs/decisions.md), don't silently diverge. The container
   `HEALTHCHECK` only reads the status code, so it's unaffected.
3. **Frontend**: the existing version readout (lower-left nav — find where `version` /
   `repo_url` from `/api/health` is rendered) becomes:
   - channel `dev` → `DEV: v<version> · <short-sha>` (visually distinct enough to catch
     the eye — e.g. an amber tint — but small; it's a badge, not a banner). Link target:
     the commit on GitHub (`<repo_url>/commit/<sha>`) instead of the release link, when
     both sha and repo_url are present.
   - channel `release` or null (local uv run, compose dev stack — no args baked) →
     exactly today's rendering. **Absence degrades to current behavior everywhere.**
4. Tests: backend — health payload includes the two fields, null when env absent, set
   when present; frontend — badge rendering for dev channel, plain rendering for
   release/null, link target switch.
5. Docs same commit: `CHANGELOG.md` Unreleased; `docs/decisions.md` (the §12 shape
   addition); startnewsession.md arr build-run table row. Do NOT add a `v` prefix
   anywhere in stored/source versions — the `v` here is display-only, same as today's
   footer (release-prep rules).

## Working tree check

Run `git status --porcelain`; if a file you must touch is dirty, STOP and report. This
prompt file is exempt.

## Conventions to honor

- **The CI job names are live required status checks on `main`** — do not rename any
  workflow job. Adding build args must not change job names or the check set.
- **No agent can see the rendered UI** — say so in your report.
- `feat:` prefix. No new dependencies, no migration.

## Verification gates — run each separately and read its exit code

1. From the **repo root**: `uvx ruff@0.8.4 check --config ruff.toml .` and
   `uvx ruff@0.8.4 format --config ruff.toml --check .` (CI's exact pinned commands).
2. `uv run pytest` — note skip counts honestly.
3. `cd frontend && npm run lint && npm test && npm run build`
4. Validate the workflow YAML (`gh workflow view` parse or a YAML lint) and
   `docker compose config` for every compose file if the Dockerfile changed.

## When done

1. Update this file's frontmatter; move to `prompts/done/` (or `failed/`).
2. **Do not commit.** Prepare the tree and report back: file list, proposed one-line
   `feat:` message, each gate's exact result, decisions/deviations. Never `git add -A`,
   never push.
