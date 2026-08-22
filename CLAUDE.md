# CLAUDE.md

lftpweb is a containerized web interface for keeping a local directory in sync with a seedbox,
using **lftp** as the transfer engine over SSH/SFTP. It browses the remote and local trees as
one view, queues and supervises downloads with live progress, auto-queues on patterns, and
optionally verifies / extracts / relocates finished items.

**Read [`DESIGN.md`](DESIGN.md) before writing any code.** It is the architectural source of
truth and its sections are numbered for reference. The one decision everything hangs off is
**§1.3 — lftp is a transfer engine, not a status API**: progress is derived from the filesystem
(local bytes vs. known remote size) and each transfer is its own short-lived lftp process. Do
not reintroduce `jobs -v` parsing as a source of truth (§1.2 explains why).

**Status:** beta `0.3.1`, all 9 build phases complete (`DESIGN.md` §13) plus three sessions of
fixes driven by real use against a real seedbox — it connects, scans, reconciles, transfers,
auto-queues, post-processes, and has auth, ops (logs/backups/health), and a Files page that has
been used in anger. Real gaps remain, named in `README.md`'s "Known gaps."
`prompts/startnewsession.md` is the current state-of-play brief and the canonical source for
exactly what's built vs. not — read it on session start rather than trusting this summary line,
which a future phase updating this file may forget to touch again.

## Stack

Python 3.13 / FastAPI / SQLite / asyncssh backend · React + TypeScript + Vite frontend ·
single Docker image · lftp for transfers.

## Standards (per-session rules)

This repo adopts crzynet `homelab-configs` standards. [`standards.md`](standards.md) lists each
standard and the pinned version — read it on session start whenever the work could touch
anything they govern.

<!--
Source: standards/code-checkin-and-pr @ v1.2.0 (crzynet/homelab-configs).
Paste the section below verbatim into the adopting project's CLAUDE.md.
The full standard (publishing matrix, retention, CI check definitions) lives at:
https://gitea.crzynet.com/crzynet/homelab-configs/src/branch/main/standards/code-checkin-and-pr/README.md
-->

## Code check-in (operational rules)

This project adopts the `code-checkin-and-pr` standard. The full why-and-how lives at
the source above; the rules below are the per-session do/don'ts a coding agent must
honor by default:

- **Never push directly to `main`.** `main` is protected. All changes land via a pull
  request from `dev` → `main`, and only when every required check is green.
- **Day-to-day work happens on `dev`** (or a short-lived branch off `dev`). Push to
  `dev` freely.
- **Commit message prefixes are required** — Conventional-Commits style:
  - `feat:` — new user-facing feature
  - `fix:` — bug fix
  - `chore:` — config, tooling, dependencies, maintenance
  - `docs:` — documentation-only changes
- **Do not add `Co-authored-by:` trailers** unless the user explicitly asks.
- **Doc updates ship in the same commit as the code they describe** — never as a
  follow-up commit.
- **Never run a verification gate in the background — always foreground, with a generous
  explicit timeout.** The full `pytest` run takes ~3.5 minutes. A **spawned subagent never
  receives a background-task completion notification**, so an agent that backgrounds a gate
  waits on a signal that cannot arrive and stalls indefinitely. This has now happened to
  several agents in this repo. Run each gate as its own foreground command and read its exit
  code directly.
- **Run `uv run pytest` from the REPO ROOT, never from `backend/`.** `testpaths` is defined in
  the root `pyproject.toml` and `tests/` is a sibling of `backend/`, not inside it — so running
  from `backend/` collects **zero** tests and exits 0, which is indistinguishable from a pass at
  a glance. Same for `ruff`. And `ruff check` passing is **not** `ruff format --check` passing:
  they are separate gates, run both and read each exit code.
- **Never bypass hooks** (no `--no-verify`, `--no-gpg-sign`, etc.) unless the user
  explicitly asks. If a hook fails, fix the underlying issue.
- **Stable releases are tagged from `main` only.** Don't tag from `dev`.

If you're unsure whether an action would violate one of the above, stop and ask before
acting.

<!-- end code-checkin-and-pr snippet -->

<!--
Source: standards/release-prep-and-cut @ v1.0.0 (crzynet/homelab-configs).
Paste the section below verbatim into the adopting project's CLAUDE.md.
The full standard (two-phase prep/cut workflow, archive trigger, validation
steps, adoption checklist) lives at:
https://gitea.crzynet.com/crzynet/homelab-configs/src/branch/main/standards/release-prep-and-cut/README.md
-->

## Release process (operational rules)

This project adopts the `release-prep-and-cut` standard. The full why-and-how
lives at the source above; the rules below are the per-session do/don'ts a
coding agent must honor by default:

- **The version is stored BARE in the source-of-truth file** — no `v` prefix
  anywhere in code. The `v` prefix is added in exactly one place: the git tag
  and matching GitHub release name. Don't add it to README badges, CHANGELOG
  headers, in-code image tags, or anywhere else.
- **`CHANGELOG.md` is the single source of truth for release notes.** The PR
  description (set by `/release-prep`) and the GitHub release body (set by
  `/release-cut`) reuse the **same section verbatim**. Never author release
  notes twice.
- **One commit per release prep.** Version bump + changelog roll + every doc
  sync ship in a single `chore(release): prepare v<version>` commit. No
  `Co-authored-by:` trailers.
- **Never re-tag.** If `v<version>` already exists as a local tag, a remote
  tag, or a GitHub release, STOP. Never delete-and-recreate; never `--force`.
  Pick the next version instead.
- **`/release-cut` only after the PR has merged and CI is green.** The
  publish-to-`main` workflow must have already pushed `:latest` images to the
  registry before `/release-cut` runs. If you cannot confirm both — STOP and
  tell the user to wait.
- **The release tag is the only thing the cut command writes to `main`.** Both
  the prep commit and any follow-up docs commit land on `dev` and reach `main`
  only via PR. Never push directly to `main` as part of a release.

If you're unsure whether an action would violate one of the above, stop and
ask before acting.

<!-- end release-prep-and-cut snippet -->

<!--
Source: standards/handoff-prompt-workflow @ v2.0.0 (crzynet/homelab-configs).
Pasted verbatim per the standard's adoption checklist.
https://gitea.crzynet.com/crzynet/homelab-configs/src/branch/main/standards/handoff-prompt-workflow/README.md
-->

## Handoff prompts (operational rules)

This project adopts the `handoff-prompt-workflow` standard. The full why-and-how lives at
the source above; the rules below are the per-session do/don'ts an agent must honor by
default:

- **Edit-size threshold — decide by how much you'll change:**
  - A genuinely small change — roughly **one or two files and a few lines** (a typo, one
    config value, a one-line fix) — do it **in-session**, no prompt.
  - **Anything bigger requires a handoff prompt** — more than ~2 files, a multi-step
    change, a new feature, or any edit large enough that a fresh context would run it
    more cleanly. **When in doubt, write the prompt.**
- **A handoff prompt is a file in `prompts/`** — one per task, from `prompts/TEMPLATE.md`,
  with frontmatter (`name`, `status`, `created`, `model`, `completed`, `result`). Set
  `model:` from the task type: **Opus** for research/planning, **Sonnet** for coding;
  mixed defaults to Opus.
- **Execute the prompt by spawning a subagent — don't hand the user a command.** Spawn an
  agent on the prompt's `model:`, let it run the prompt end-to-end, and **report the
  outcome back**. The agent gets a fresh context; you stay in the loop.
  - **Manual fallback only on explicit request.** If the user says e.g. "use manual
    prompts for this," give them
    `claude --model <model> "Read prompts/<file>.md and execute it as your task."`
    instead of spawning.
- **Check the working tree before editing.** Run `git status --porcelain`, cross-reference
  the files the plan touches; if any have uncommitted changes, list them and ask before
  touching. Surface unrelated dirty files once; they don't block.
- **The prompt self-updates and moves when done.** The executing agent sets its
  frontmatter (`status`/`completed`/`result`) and `git mv`s the file into `prompts/done/`
  (success) or `prompts/failed/` (failure).
- **One commit at the end; the prompt bundles in.** The prompt file is **not** committed
  up front — it lands in the single end commit alongside the work and the prompt move.
  Propose ONE commit (files list + one-line message), ask `y/n`, stage only those specific
  paths. **Never `git add -A`, never auto-commit, never push.** A spawned agent prepares
  the tree and reports the proposed commit back; the orchestrating session surfaces the
  `y/n`.
- **Record non-obvious decisions** (approach changes, rejected alternatives, workarounds)
  in `docs/decisions.md`, newest at top.

If you're unsure whether an action would violate one of the above, stop and ask before
acting.

<!-- end handoff-prompt-workflow snippet -->

## Repo layout

```
DESIGN.md        architectural source of truth (numbered sections)
standards.md     which homelab-configs standards this repo implements, pinned
prompts/         handoff prompt live queue; done/ and failed/ created lazily
docs/decisions.md  decision record, newest at top
```
