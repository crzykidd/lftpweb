---
description: Prepare a release — bump version, roll changelog, sync docs, validate, commit, push to dev, open PR
argument-hint: <version>   (e.g. 0.0.2)
---

<!--
Instantiated from standards/release-prep-and-cut @ v1.0.0
(crzynet/homelab-configs/standards/release-prep-and-cut/README.md) for lftpweb.

Placeholder bindings for this project:

  <VERSION_FILE>             backend/lftpweb/__init__.py
  <VERSION_LITERAL>          __version__ = "<current>"
  <README_BADGE_PATTERN>     none — README.md has no version badge. Instead it carries a
                              "Version `<current>`. All 9 build phases are built and
                              unit/integration tested." line inside the pre-release banner
                              under the title; treat that as the badge-equivalent. Match on
                              the "**Version `" prefix, not the full sentence — the wording
                              after it has already changed once (all 9 phases completed
                              2026-08-12) and only `<current>` is yours to touch.
  <README_WHATSNEW_SECTION>  README.md has no "What's New" section yet (pre-first-release).
                              Until one exists, Step 4 below only updates the version
                              line — it does not invent a What's New section.
  <DOCS_TO_SYNC>              - README.md: the "Version `<current>`. …" line in the
                                 pre-release banner. Only the version is yours to change;
                                 leave the build-status wording after it alone.
                               - CLAUDE.md: no version-bearing text as of adoption
                                 (2026-08-11) — check for one anyway; if a build-status
                                 block has been added since, sync it here too.
  <LOCAL_CHECKS>               - uvx ruff@0.8.4 check --config ruff.toml .
                               - uvx ruff@0.8.4 format --config ruff.toml --check .
                               - uv run pytest  (integration subset skips outside CI
                                 unless docker compose -f docker-compose.test.yml is up —
                                 run it if available, note if not)
                               - docker compose -f docker-compose.yml config --quiet
                               - docker compose -f docker-compose.dev.yml config --quiet
                               - docker compose -f docker-compose.test.yml config --quiet
                               - python3 -c "import yaml,json,pathlib; [yaml.safe_load(open(p)) for p in pathlib.Path('.').glob('**/*.y*ml') if '.venv' not in str(p) and 'node_modules' not in str(p)]"
  <CHANGELOG_ARCHIVE_DIR>     docs/   (archive files: docs/CHANGELOG-<minor>.x.md — none
                              exist yet; the first archive trigger creates the directory
                              use, not the directory itself, which already exists)

Full procedure: this file. Full standard/why: see the source link above.
-->

# Release Prep

You are preparing release **v$ARGUMENTS**. This command does ONLY the prep + PR
steps. It does **not** merge and does **not** create the GitHub release — the
human merges, and `/release-cut` (run after `main` CI is green) creates the
release.

## Execution rules

- Work on the `dev` branch. Never push directly to `main`.
- Do NOT add `Co-authored-by` lines to the commit.
- Do NOT create the GitHub release or tag in this command.
- If any validation step fails, STOP and report — do not commit broken state.
- Make exactly ONE commit covering version + changelog + all doc updates.
- `$ARGUMENTS` is the target version. It SHOULD be bare semver, no `v` prefix
  (e.g. `0.0.2`). If a leading `v` was typed (`v0.0.2`), strip it silently and
  proceed with the bare number. After stripping, if the value is empty or does
  not match `MAJOR.MINOR.PATCH` exactly (three integers, dot-separated, no
  pre-release/build suffix), STOP and ask for a valid version.
- Reminder on the `v` convention: the version is stored and used BARE
  everywhere (`backend/lftpweb/__init__.py`, changelog header, README status
  line, in-code image tags). The `v` prefix is added in exactly one place —
  the git tag / GitHub release — and that happens in `/release-cut`, not here.

## Step 0 — Preflight

1. Confirm the current branch is `dev`. If not, STOP and report.
2. Confirm the working tree is clean (`git status --porcelain` empty). If
   there are uncommitted changes, STOP and show them — the user must decide.
3. Read the current version from `backend/lftpweb/__init__.py`
   (`__version__ = "..."`). Parse both the current version and `$ARGUMENTS`
   into `(MAJOR, MINOR, PATCH)` integer triples for comparison.

### 0a — Hard stops (never proceed past these)

- **Not newer.** If `$ARGUMENTS` is not strictly greater than the current
  version (compared as integer triples, not string compare), STOP and report.
  Equal-to-current also stops.
- **Tag already exists.** Run `git fetch --tags` then check both
  `git tag -l "v$ARGUMENTS"` and `gh release view "v$ARGUMENTS"`. If either
  exists, STOP and report.

### 0b — Bump-tier classification (warn + confirm)

- **Patch bump**, PATCH+1 (e.g. `0.0.1` → `0.0.2`): proceed, no prompt.
- **Patch skip** (e.g. `0.0.1` → `0.0.4`): WARN, show expected next patch,
  require confirmation.
- **Minor bump** (e.g. `0.0.9` → `0.1.0`): ALWAYS warn — infrequent, fires the
  changelog archive trigger (Step 3). Require confirmation.
- **Major bump** (e.g. `0.9.0` → `1.0.0`): ALWAYS warn with strong language —
  produces a new `:<major>` image tag. Require confirmation.

When warning, always show the three "expected next" successors
(`MAJOR.MINOR.PATCH+1`, `MAJOR.MINOR+1.0`, `MAJOR+1.0.0`) so the user can spot
a typo. Do not proceed on any warned tier without an explicit affirmative.

**Special case for this project — `0.0.x`:** every release while MAJOR.MINOR
is `0.0` is technically a "patch" by the tier rules above, but this project
has shipped **zero** releases yet, and `DESIGN.md` §13 lays out 9 build phases
before v1 is done. Treat every `0.0.x` bump as build-phase progress, not a
promise of stability — the confirmation prompts above are about catching
typos, not about whether `0.0.x` is "ready." Do not infer readiness from the
version number.

### 0c — Remaining setup

4. Determine whether this is a new minor/major or a patch within the current
   minor — decides whether Step 3 (archive) fires.
5. Capture today's date as `YYYY-MM-DD`.

## Step 1 — Bump the version

Update `backend/lftpweb/__init__.py`: change
`__version__ = "<current>"` to `__version__ = "$ARGUMENTS"`. Do not touch the
module docstring or anything else in the file.

## Step 2 — Roll the changelog

In `CHANGELOG.md`:

1. Change `## [Unreleased]` to `## [$ARGUMENTS] — <today>`.
2. Insert a fresh empty `## [Unreleased]` block above it, matching the
   existing skeleton (Added / Changed / Fixed / etc. subheadings, empty).
3. Sanity-check categorization of the rolled entries; fix obvious
   miscategorization only, don't rewrite prose.
4. If `[Unreleased]` is empty, STOP — nothing to release.

## Step 3 — Per-minor archive trigger (MINOR/MAJOR ONLY)

Skip entirely for a patch release. On a minor/major bump, archive every
**closed** minor series still in `CHANGELOG.md` (all of them below the new
current minor) into `docs/CHANGELOG-<minor>.x.md`, newest-first, full detail
preserved, replaced in the active file by a `## [<version>] — <date> (summary)`
block (one bullet per major feature/fix, judgment on what's trivial) ending in
a deep link to the archive. Prepend the new archive file to the "Archived
releases" index at the bottom of `CHANGELOG.md` (create the index if absent).

lftpweb has shipped no releases yet, so this step is a no-op until the first
minor bump past whatever series is active at the time.

## Step 4 — Sync `README.md`

README.md has no version badge and no "What's New" section (pre-first-release
project). Instead:

1. Update the pre-release banner line. As of 2026-08-12 it reads:
   `**Version `<current>`. All 9 build phases are built and unit/integration tested.**`
   Replace `<current>` with `$ARGUMENTS` and **leave the rest of the sentence
   alone** — all nine phases are complete, so there is no longer a phase count
   to advance. Match on the `**Version \`` prefix rather than the whole
   sentence, since the wording after it has changed once already and may again.
   If the line is missing entirely, do not invent one — flag it in the Step 9
   report.
2. If a "What's New" section exists by the time this command runs (it may,
   once the project starts shipping), add a
   `### v$ARGUMENTS (<today>)` entry at its top, summarizing this release in
   user-facing language drawn from the changelog. Do not invent the section
   if it still doesn't exist.

## Step 5 — Sync long-form docs

Check `CLAUDE.md` for any version-bearing text (none existed at adoption time,
2026-08-11) and update it if present, using the same "don't invent sections"
rule as Step 4. Do not touch `DESIGN.md` — it's architecture, not a version
tracker, and nothing in it should ever need syncing to a release number.

## Step 6 — Validate locally BEFORE committing

Run, in order, and STOP on any failure (report exactly what failed, don't
commit):

```
uvx ruff@0.8.4 check --config ruff.toml .
uvx ruff@0.8.4 format --config ruff.toml --check .
uv run pytest
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.dev.yml config --quiet
docker compose -f docker-compose.test.yml config --quiet
```

Note on `uv run pytest`: the fake-seedbox-gated integration tests
(`tests/test_queue.py`, part of `tests/test_remote.py`) silently SKIP unless
`docker compose -f docker-compose.test.yml up --build -d` (with
`docker/test-seedbox/gen_key.sh` run first) is already up. If it isn't, run it
before this step so `/release-prep` exercises the same suite CI does — a
release prepared against a suite that quietly skipped its integration tests
is a false green.

Also grep for stale `<old-version>` references across `README.md`,
`backend/lftpweb/__init__.py`, and `CLAUDE.md`. Report any other occurrences
found rather than blindly editing them.

## Step 7 — Commit

Stage everything and make ONE commit:

```
chore(release): prepare v$ARGUMENTS

- backend/lftpweb/__init__.py bumped to $ARGUMENTS
- CHANGELOG: rolled [Unreleased] -> [$ARGUMENTS] -- <today>
- README: version/phase-count line updated
<- archive line ONLY if a new-minor archive was performed>
```

No `Co-authored-by` lines.

## Step 8 — Push and open the PR

1. `git push origin dev`.
2. `gh pr create` for `dev` → `main`:
   - Title: `Release v$ARGUMENTS`
   - Body: this release's rolled `## [$ARGUMENTS]` CHANGELOG section verbatim.
3. Capture the PR URL.

## Step 9 — Report and STOP

Print:

- The PR URL.
- Confirmation that local validation passed (and whether the integration
  tests actually ran or were skipped).
- Whether the README phase-count line was changed, and if not, an explicit
  note that it needs a human decision.
- The exact next steps, verbatim:
  1. Review the PR on GitHub and wait for CI to go green — including the
     "Test suite (unit + fake-seedbox integration)" job actually exercising
     the integration tests, not skipping them.
  2. Merge the PR into `main`.
  3. Wait for the push-to-`main` build (`.github/workflows/publish.yml`) to
     publish `:latest` to `ghcr.io/crzykidd/lftpweb`.
  4. Run `/release-cut $ARGUMENTS` to tag and publish the GitHub release.

Do NOT proceed past this point. Do not merge. Do not tag.
