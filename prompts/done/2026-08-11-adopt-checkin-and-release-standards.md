---
name: 2026-08-11-adopt-checkin-and-release-standards
status: completed
created: 2026-08-11
model: sonnet
completed: 2026-08-11
result: code-checkin-and-pr @ v1.2.0 and release-prep-and-cut @ v1.1.0 wired (CI, publish, retention workflows; ruff.toml; slash commands; CHANGELOG.md; standards.md; CLAUDE.md; docs/repo-setup.md runbook). repo-sandbox-permissions recorded as deliberately skipped. Known gaps: ruff check/format currently red against existing backend/tests code (not fixed, per scope); homelab-configs registry diff and docs/decisions.md entries reported as text, not written. Not committed — reported for the orchestrating session to land.
---

# Task: Adopt `code-checkin-and-pr` and `release-prep-and-cut`, ready for repo creation

lftpweb is about to get a **public** GitHub repo. Wire up the two standards that presuppose
one, so the repo is conformant from its first push rather than retrofitted later.

**Scope boundary:** you prepare everything in this repo. You do **not** create the GitHub repo,
push, or configure branch protection — those are the user's manual steps. Produce a short
runbook for them instead.

## ⚠ Another agent is working in this repo right now

A concurrent agent is building phase 3b (Transfers UI). To avoid collisions you must **not
touch**:

- `frontend/**`, `backend/**`, `tests/**`
- `docs/decisions.md` — the other agent is writing there. Put your decision notes in your final
  report instead; the orchestrating session will land them.
- `pyproject.toml` — put ruff configuration in a **separate `ruff.toml`** at the repo root
  rather than editing `pyproject.toml`.

Run `git status --porcelain` before you start and expect to see the other agent's in-progress
changes. **Leave them alone**, and do not `git add`, `git stash`, `git checkout`, or commit
anything.

## Before you start

Read these in full — they are the sources of truth, and you must not restate them in this repo,
only implement and link back (map, not copy):

1. `~/projects/homelab-configs/standards/README.md` — the adoption process and the
   `standards.md` table format.
2. `~/projects/homelab-configs/standards/code-checkin-and-pr/README.md` **and** its
   `CLAUDE-snippet.md`.
3. `~/projects/homelab-configs/standards/release-prep-and-cut/README.md` **and** its
   `CLAUDE-snippet.md` and both slash-command templates.
4. This repo's `DESIGN.md` §11 (container), `standards.md`, `CLAUDE.md`, `README.md`.

Decisions already made — do not re-litigate:

- **Repo will be public**, so CodeQL default setup is available free; all 7 required checks are
  achievable. Full conformance is the goal.
- **Full adoption of `code-checkin-and-pr`**, including the image publishing matrix and registry
  retention.
- **`release-prep-and-cut` adopted now**, even though `0.0.1` isn't releasable yet.
- **`repo-sandbox-permissions` is deliberately NOT adopted** — this is a dedicated dev host, and
  the same call was made when it was de-adopted from AmmoLedger. Record it as skipped-by-choice
  in `standards.md`, not as an oversight.
- Project name stays **`lftpweb`**; image will be `ghcr.io/crzykidd/lftpweb`.
- Version lives bare in `backend/lftpweb/__init__.py` (currently `0.0.1`). Read it; don't move it.

## What to do

### 1. CI — the 7 required checks

One workflow (plus CodeQL's own), wiring every check the standard requires. What this repo
actually needs:

| Check | How |
|---|---|
| Backend lint | `ruff check` + `ruff format --check` (config in a new `ruff.toml`) |
| Frontend lint/typecheck | `npm run lint` (oxlint) and `tsc` via `npm run build` |
| Config validation | every checked-in YAML/JSON parses |
| Migration → head | already covered by `tests/test_db.py`; make sure it runs |
| Compose validation | `docker compose config --quiet` on **all three** compose files |
| Test suite | `uv run pytest` |
| Image build | PR-only, build without pushing |
| SAST | CodeQL default setup — `python` and `javascript-typescript` |

**The integration tests are worth real thought.** `tests/test_queue.py` and parts of the phase 2
suite are gated on the fake seedbox being reachable at `127.0.0.1:2222`; if CI doesn't start it,
they silently **skip** and the most valuable tests in the project never run. Bring up
`docker-compose.test.yml` in the workflow (run `docker/test-seedbox/gen_key.sh` first) so they
execute for real, and make it obvious in the job name that they did. A green CI that quietly
skipped the transfer tests is worse than no CI.

### 2. Publishing and retention

Per the standard's publishing matrix — read it there, don't guess:
push to `dev` → `:dev` + `:sha-<short>`; push to `main` → `:latest` + `:sha-<short>`;
`release: published` → `:latest` + `:<semver>` + `:<major>`. Plus the registry retention
workflow (the standard states the keep counts and which tags are protected).

Note `docker/Dockerfile` builds a multi-stage image whose default target is `runtime`.

### 3. `release-prep-and-cut`

Copy both slash-command templates into `.claude/commands/` and tailor them to this project:
the canonical version file is `backend/lftpweb/__init__.py`, and `CHANGELOG.md` is the single
source of release notes.

Create `CHANGELOG.md` (Keep-a-Changelog) with an `[Unreleased]` section. Since nothing has been
released, summarise what exists so far under Unreleased — phases 1–3 of 9 per `DESIGN.md` §13.
Do not invent a `0.0.1` release entry; `0.0.1` is the in-development version, not a release.

Also sync anything that references the version: `README.md` states `0.0.1` and "3 of 9 build
phases complete" — make sure the release-prep command knows to keep those in step.

### 4. `standards.md`, `CLAUDE.md`, and the registry

- **`standards.md`** — update the `code-checkin-and-pr` row from "not adopted" to `1.2.0`, dated
  today, with honest Notes on exactly what is wired. Add a `release-prep-and-cut` row at
  `1.1.0`. Add a row (or Notes line) recording `repo-sandbox-permissions` as deliberately
  skipped, with the reason. Follow the table format in the standards index.
- **`CLAUDE.md`** — paste **both** `CLAUDE-snippet.md` files verbatim. Remove the current
  paragraph saying `code-checkin-and-pr` is not adopted and its conventions are only followed
  voluntarily — that is about to be false. Keep the pointer to `standards.md`.
- **Registry** — `projects/lftpweb/README.md` and the table in `projects/README.md` live in
  `homelab-configs` and cannot be edited from here. Emit them as a **diff in your final report**
  for the user to land separately. A `projects/lftpweb/README.md` does not exist yet, so it
  needs creating; match the shape of `projects/filament-bridge/README.md`.

### 5. Repo-creation runbook

A short `docs/repo-setup.md`: create the public repo, push `main` and `dev`, enable CodeQL
default setup, then apply branch protection with the required status checks named exactly as
the workflows produce them.

**Call out the ordering trap:** `main` is currently 6 commits behind `dev`. It must be
fast-forwarded to `dev` and pushed **before** branch protection is enabled — otherwise the first
action on the new repo is opening a PR to catch `main` up on the project's own history. Note
that the standard's required-checks list can only be applied after the checks have run once and
GitHub knows their names.

## Verify before reporting

1. `ruff check` and `ruff format --check` pass against the existing code, or the config is tuned
   so they do. **Do not edit files under `backend/` or `tests/` to satisfy the linter** — if the
   code needs changes, report what and why instead; that is the other agent's territory today.
2. Every workflow YAML parses (`python -c "import yaml,sys; yaml.safe_load(open(...))"` or
   equivalent) and every action reference is pinned to a real version.
3. `docker compose config --quiet` still clean on all three compose files.
4. The `CLAUDE.md` snippets are **byte-identical** to their sources — diff them and say so.
5. Nothing outside your allowed file set has been modified: `git status --porcelain` shows the
   other agent's files untouched by you.

Report exactly what you ran. Anything you could not verify — notably that CI actually passes,
which cannot be known until the repo exists — must be stated plainly as unverified.

## Surfacing design decisions

Report prominently anything where a standard doesn't fit this project cleanly, or where you had
to choose. Partial or deviating adoption is acceptable **when documented honestly** in
`standards.md` Notes — the standards index is explicit that an admitted gap beats a clean-looking
row that lies.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result`.
2. `git mv` this file into `prompts/done/` (success) or `prompts/failed/` (failure).
3. **Do NOT commit** — another agent is mid-flight and committing would sweep in its partial
   work. Report the file list and a proposed one-line commit message (`chore:` prefix, no
   `Co-authored-by:`) and stop.
4. Put your `docs/decisions.md` entries in the report as text; do not write that file.
