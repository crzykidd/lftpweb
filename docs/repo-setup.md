# Repo-creation runbook

Manual steps to take this repo from "prepared for GitHub" to "actually on GitHub, with
`code-checkin-and-pr` fully enforced." Nothing here runs itself — an agent prepared the CI,
publishing, and retention wiring under `.github/workflows/`, but repo creation, the initial
push, and branch protection are steps only a human (or a session with `gh` auth and the
authority to touch org/repo settings) should take deliberately.

See [`standards.md`](../standards.md) for what's already wired and why, and the standards
themselves for the full rules:
[`code-checkin-and-pr`](https://gitea.crzynet.com/crzynet/homelab-configs/src/branch/main/standards/code-checkin-and-pr/README.md),
[`release-prep-and-cut`](https://gitea.crzynet.com/crzynet/homelab-configs/src/branch/main/standards/release-prep-and-cut/README.md).

## ⚠ The ordering trap — read this before doing anything else

As of this writing, **`main` is 6 commits behind `dev`**:

```
main:  47f2e92  chore: initialize repo, adopt handoff-prompt-workflow @ v2.0.0
dev:   36b9123  feat: phase 3a transfer engine — ... (6 commits ahead of main)
```

`main` has never been fast-forwarded to track `dev`'s actual history. If branch protection
goes on **before** `main` is caught up, the first thing anyone can do on the new repo is
open a PR that merges the project's own existing history into `main` through the exact
gate meant to review *incoming* changes — which is backwards, and likely to be confusing
enough that someone "fixes" it by disabling protection temporarily. Avoid the whole
situation by doing the fast-forward first, while there's still no protection in the way.

**Order matters. Do not skip ahead.**

## Step 1 — Create the repo (public)

```
gh repo create crzykidd/lftpweb --public --source=. --remote=origin --description "Containerized web interface for keeping a local directory in sync with a seedbox, using lftp as the transfer engine over SSH/SFTP."
```

Confirm it was created **public** — CodeQL default setup (step 3) is free only on public
repos, and that's the assumption the whole `code-checkin-and-pr` adoption was done under.

Do not push yet.

## Step 2 — Fast-forward `main` to `dev`, then push both branches

```
git fetch origin
git checkout main
git merge --ff-only dev      # fails loudly if main isn't a strict ancestor of dev — good,
                              # that means something unexpected happened; stop and look.
git push origin main
git checkout dev
git push origin dev
```

After this, both branches exist on GitHub and `main` == `dev` at the same commit. This is
the last moment it's this simple — from here on, `main` only moves via reviewed PRs.

## Step 3 — Enable CodeQL default setup

This is the 7th required check (`SAST`) and is **not** a workflow file in this repo —
GitHub manages default-setup CodeQL runs itself, dynamically, outside `.github/workflows/`.

1. Repo → **Settings → Code security** (or **Security → Code scanning**).
2. **Code scanning** → **Set up** → **Default**.
3. Confirm the detected languages are **Python** and **JavaScript/TypeScript** (matching
   `docker/Dockerfile`'s two language stacks). Default setup should auto-detect both from
   `backend/` and `frontend/`; if it only picks up one, add the other manually in the
   language dropdown.
4. Save. This queues an initial scan — let it run once before step 5, so its check-run
   name is known.

## Step 4 — Let CI run once on `dev` (or open a throwaway PR) before configuring branch protection

Branch protection needs to reference required-status-check **names**, and GitHub only
offers names it has actually seen a check run report. Two ways to get there:

- Push a trivial commit to `dev` (or just wait for the next real one) — `.github/workflows/ci.yml`
  and `.github/workflows/publish.yml` both trigger on `push: [dev]`. Note `ci.yml`'s
  `pull_request` trigger is scoped to `branches: [main]` only, so a `dev` push alone won't
  exercise the PR-only **Image build** step's actual build (the job still reports a
  conclusion either way — see the in-file comment on why it's structured that way) — that's
  expected; it'll properly report once step 5 opens a PR.
- Or open the `dev` → `main` PR now (skip to step 6, then come back) — this exercises every
  `ci.yml` job including the PR-gated image build.

Either way, **wait for at least one full run to complete** before step 5.

## Step 5 — Configure branch protection on `main`

```
gh api repos/crzykidd/lftpweb/branches/main/protection \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks[strict]=true \
  -f 'required_status_checks[contexts][]=Backend lint' \
  -f 'required_status_checks[contexts][]=Frontend lint + typecheck' \
  -f 'required_status_checks[contexts][]=Config validation' \
  -f 'required_status_checks[contexts][]=Compose validation' \
  -f 'required_status_checks[contexts][]=Image build' \
  -f 'required_status_checks[contexts][]=Test suite (unit + fake-seedbox integration)' \
  -f 'required_status_checks[contexts][]=<CodeQL check-run name from step 3 — confirm exact text in the repo''s checks UI after the first scan completes>' \
  -f enforce_admins=true \
  -f required_pull_request_reviews[required_approving_review_count]=0 \
  -f restrictions=null
```

Or via the UI: **Settings → Branches → Add branch protection rule** → branch name pattern
`main` → check **Require a pull request before merging**, **Require status checks to pass
before merging**, then pick the checks above from the list GitHub now offers (it only
lists checks it has seen run — this is why step 4 has to happen first).

**Important — the check names above are the bare `name:` fields from
`.github/workflows/ci.yml`'s jobs, not `CI / <job>`.** GitHub binds required-status-check
contexts to the check-run name a job actually reports, and for a workflow with unique job
names (no matrix, no duplicate names across workflows) that's just the job's own `name:`.
If GitHub's UI offers something prefixed differently, use exactly what it offers — don't
guess from this file if the two disagree, this file could drift.

The exact CodeQL context name (last row above) depends on how GitHub's default setup labels
its own check run — commonly `CodeQL` for a single combined check, or per-language names.
Confirm the actual text in the repo's checks list after step 3/4's first scan and use that,
not a guess.

Also add **Do not allow bypassing the above settings** if you want `enforce_admins` truly
absolute (no admin override) — matches the standard's "never push directly to `main`" rule
in `CLAUDE.md`, but is a call worth making deliberately since it also blocks emergency
fixes.

## Step 6 — Open the first real PR

Once steps 1–5 are done, day-to-day work is: commit on `dev`, push freely, and open
`dev` → `main` PRs when ready to ship — exactly what `CLAUDE.md`'s "Code check-in
(operational rules)" section already tells every session to do by default.

## What this repo cannot verify before the above happens

- **That CI actually goes green.** `.github/workflows/ci.yml`'s YAML parses and its action
  references resolve to real pinned commits (verified locally — see the adoption report),
  but no workflow has ever executed against GitHub's runners. In particular:
  - The **Backend lint** job is very likely to fail on the first run — `ruff format --check`
    currently reports ~22 files that predate `ruff.toml` and were never run through
    `ruff format`, and `ruff check` reports one unused import
    (`backend/lftpweb/api/settings.py:15`). See `standards.md`'s `code-checkin-and-pr` row
    for the exact count. Fixing these is explicitly **not** done by the adoption that wrote
    this runbook — it's a normal follow-up commit for whoever owns `backend/` next
    (`uv run ruff format --config ruff.toml .` plus removing the one unused import).
  - The **Test suite** job depends on `docker compose -f docker-compose.test.yml up --build`
    succeeding on a GitHub-hosted runner exactly like it does locally — plausible (it's
    plain `sshd` containers, no exotic requirements) but unverified.
- **The exact CodeQL check-run name** — depends on GitHub's own default-setup labeling,
  only knowable after step 3 runs once.
- **Registry retention's package-scope assumption.** `.github/workflows/retention.yml`
  calls `/user/packages/container/lftpweb/versions` (personal-account scope, matching
  `ghcr.io/crzykidd/lftpweb`). If the image ever moves to an org-owned package, the
  endpoint changes to `/orgs/<org>/packages/...` and this workflow needs updating.
