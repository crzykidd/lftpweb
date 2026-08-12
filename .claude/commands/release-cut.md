---
description: Cut a GitHub release after the dev→main PR has merged and main CI is green
argument-hint: <version>   (e.g. 0.0.2 — must match what /release-prep prepared)
---

<!--
Instantiated from standards/release-prep-and-cut @ v1.0.0
(crzynet/homelab-configs/standards/release-prep-and-cut/README.md) for lftpweb.

Placeholder bindings for this project:

  <VERSION_FILE>          backend/lftpweb/__init__.py
  <MAIN_CI_WORKFLOW>      "CI"                              (.github/workflows/ci.yml)
  <PUBLISH_WORKFLOW>      "Build and publish Docker image"  (.github/workflows/publish.yml)
  <RELEASE_IMAGE_TAGS>    :latest, :<semver>, :<major> on ghcr.io/crzykidd/lftpweb
-->

# Release Cut

You are publishing the GitHub release for **v$ARGUMENTS**. Run this ONLY
after:

- `/release-prep $ARGUMENTS` has merged into `main`, and
- the push-to-`main` CI + image-publish workflows are green and `:latest`
  images are in the registry.

Publishing the release triggers the `release: published` event, which fires
`.github/workflows/publish.yml` and builds/pushes the production `:latest`,
`:$ARGUMENTS`, and `:<major>` images to `ghcr.io/crzykidd/lftpweb`. So this
step is the point of no return for production images — verify before tagging.

## Execution rules

- `$ARGUMENTS` SHOULD be bare semver (no `v` prefix). If a leading `v` was
  typed (`v0.0.2`), strip it silently. After stripping, if the value does not
  match `MAJOR.MINOR.PATCH` exactly, STOP and ask for a valid version.
- The bare value MUST equal the current version in
  `backend/lftpweb/__init__.py` on `main`. If it does not, STOP.
- The release tag is `v$ARGUMENTS` (with the `v` prefix). Before calling
  `gh`, assert the tag string matches `^v[0-9]+\.[0-9]+\.[0-9]+$` exactly. If
  it does not, STOP — never create a malformed tag.
- Do NOT add `Co-authored-by` lines anywhere.
- If any verification step fails, STOP and report. Do not create the tag.

## Step 1 — Verify we are releasing the right commit

1. `git fetch origin` and check out `main`: `git checkout main && git pull`.
2. Confirm `backend/lftpweb/__init__.py`'s `__version__` equals `$ARGUMENTS`.
   If not, the prep PR is not merged (or the wrong version was passed) — STOP.
3. Confirm the working tree is clean.
4. Confirm `git log` shows the `chore(release): prepare v$ARGUMENTS` commit on
   `main`. If absent, STOP — the PR has not been merged.

## Step 2 — Verify CI is green on main

1. `gh run list --branch main --limit 10` and confirm the most recent runs
   for the release commit concluded `success` for BOTH the `CI` workflow AND
   the `Build and publish Docker image` workflow.
2. If a run is still in progress, tell the user to wait and STOP.
3. If a run failed, STOP and report which job failed. Pay particular
   attention to the "Test suite (unit + fake-seedbox integration)" job's
   conclusion — a `success` that actually skipped the integration tests is a
   false green (the job itself fails loudly if that happens, per
   `.github/workflows/ci.yml`, but re-check the logs if anything looks off).

## Step 3 — Confirm the version tag does not already exist

`git tag -l "v$ARGUMENTS"` and `gh release view v$ARGUMENTS` — if either
exists, STOP and report. Never overwrite an existing release/tag.

## Step 4 — Assemble the release notes

Extract the `## [$ARGUMENTS] — <date>` section from `CHANGELOG.md` (everything
from that header up to, but not including, the next `## [` header). This is
the release body — the changelog is the single source of truth, matching the
PR description `/release-prep` created.

## Step 5 — Create the release

Write the extracted section to a temp file and pass it via `--notes-file`:

```
gh release create v$ARGUMENTS \
  --target main \
  --title "v$ARGUMENTS" \
  --notes-file <tmp>
```

Do not try to inline multi-line release notes.

## Step 6 — Verify the production build fired

1. `gh run list --workflow "Build and publish Docker image" --limit 3` and
   confirm a run triggered by the `release` event for `v$ARGUMENTS` has
   started or succeeded.
2. Report its status.

## Step 7 — Report

Print:

- The release URL.
- The tag created (`v$ARGUMENTS`).
- The status of the production image build.
- A reminder of the expected image tags once the build finishes:
  `:latest`, `:$ARGUMENTS`, `:<major>` on `ghcr.io/crzykidd/lftpweb`.

Done — the release is live.
