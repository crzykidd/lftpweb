---
name: 2026-11-26-compose-loopback-default
status: completed
created: 2026-11-26
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-11-26
result: shipped compose binds 127.0.0.1:8087; README, quick-start, CHANGELOG, decisions updated
---

# Task: ship docker-compose.yml bound to loopback by default

The user decided (2026-11-26, after the security audit — `private_data/security-audit-2026-11-26.md`
§3 F2) that the shipped `docker-compose.yml` should publish the UI on `127.0.0.1` instead of all
interfaces, so a fresh install is not reachable from the network until the operator chooses that.
This is a deliberate, user-approved behaviour change: someone who copies the shipped file and
browses from another machine without a reverse proxy will now have to edit one line.

## Working tree check

Main checkout `/home/manderse/projects/lftpweb` on `dev`. `git status --porcelain` should show only
this prompt and untracked `.claude/worktrees/` (ignore it).

## What to do

1. `docker-compose.yml` line ~35: `"8087:8087"` → `"127.0.0.1:8087:8087"`. Extend the existing
   comment above it (keep the "Not 8080" note) to say: loopback by default so nothing on the
   network can reach it until you decide; put a reverse proxy in front, or change it to a specific
   LAN address (`"192.168.1.10:8087:8087"`) or `"8087:8087"` (all interfaces) — and point at
   README's "Network exposure" section before doing the latter in `none` mode. Leave the
   healthcheck alone (it already uses 127.0.0.1 inside the container). Do not touch
   `docker-compose.dev.yml` or `docker-compose.test.yml`.
2. `README.md`:
   - "Network exposure" (~line 490–497): the shipped file now binds loopback; rewrite that bullet
     so it explains how to *widen* it (specific LAN address preferred over all interfaces) rather
     than how to narrow it.
   - The "Running it" section (~line 575, "The UI is on **`8087`**"): add that the shipped compose
     file listens only on `127.0.0.1`, so from another machine you need a reverse proxy or to edit
     the `ports:` line — with a pointer to "Network exposure".
3. `docs/quick-start.md` (bundled into the app's Docs page) step 1, "The UI is on port `8087`":
   same one-sentence note. Keep it short — it's in-app copy.
4. `CHANGELOG.md` — the **real** `## [Unreleased]` section (NOT the commented-out skeleton at the
   file's top), under `### Changed`: the shipped compose file now binds `127.0.0.1:8087`; existing
   deployments that use their own compose file are unaffected; anyone using the shipped file and
   browsing from another machine must put a proxy in front or edit the line. Bold the behaviour
   change the way the existing settle-gate "defaults ON" entry does.
5. `docs/decisions.md` — newest-at-top entry: the decision, why (fresh installs default to `none`
   auth, which the audit showed is full control for anyone who can reach the port), the rejected
   alternative (leave all-interfaces and only document it — what G1 did first), and the cost
   (one-line edit for LAN users without a proxy).

## Gates (foreground)

`docker compose -f docker-compose.yml config -q` (exit 0 — CI has a compose-validation job; read
`.github/workflows/ci.yml` and run its exact compose check if it differs). Confirm `git diff
--stat` shows only the five files above. No other gates needed — no code changes.

## When done

1. Update frontmatter; plain `mv` this prompt to `prompts/done/`.
2. **Do not commit or push.** Report the file list, the compose check's exit code, and a proposed
   message: `fix: bind the shipped compose file to loopback by default` (no `Co-authored-by:`).
