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

**Status:** pre-implementation. `DESIGN.md` is under review; no application code exists yet.

## Stack

Python 3.13 / FastAPI / SQLite / asyncssh backend · React + TypeScript + Vite frontend ·
single Docker image · lftp for transfers.

## Standards (per-session rules)

This repo adopts crzynet `homelab-configs` standards. [`standards.md`](standards.md) lists each
standard and the pinned version — read it on session start whenever the work could touch
anything they govern.

`code-checkin-and-pr` is **not** adopted here — there is no remote yet, so its branch
protection / PR-check / image-publishing rules have nothing to bind to. Two of its conventions
are followed voluntarily so the history is already conformant when we do adopt it: commit
prefixes (`feat:` / `fix:` / `chore:` / `docs:`) and no `Co-authored-by:` trailers. Day-to-day
work is on `dev`; `main` is left alone.

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
