---
name: 2026-11-26-g1-exposure-docs
status: completed
created: 2026-11-26
model: sonnet            # opus = research/planning, sonnet = coding
completed: 2026-11-26
result: README Network exposure section + Known gaps, CHANGELOG upgrade note, startnewsession update; docs only
---

# Task: document network exposure and the 0.4.0 security fixes for users (audit batch G1)

Docs only. The 2026-11-26 security audit (`private_data/security-audit-2026-11-26.md` — read §1,
§3 F2/F5/F6/F9/F11/F12, and §5 G1) found that lftpweb's default posture is dangerous if the port is
reachable by anyone untrusted, and three fix batches have since landed on `dev`:

- `3aa48d6` C1 — lftp quoting by lftp's real grammar; CR/LF/NUL names refused (`UNSAFE_PATH`)
- `2f717b7` D1+D2 — base image refresh (OpenSSH client CVE-2026-60002 fixed), certifi,
  7-Zip 26.06 and UnRAR 7.23 built from source
- `985eed7` C2 — archives listed before extraction; links / absolute / `..` members refused

**Still NOT fixed** (deliberately deferred; batches C3, C4, C5, D3 in the report): in auth mode
`none` there is no CSRF/Host/Origin protection, and `extra_lftp_settings` is passed to lftp
verbatim — so **anyone who can reach the port in `none` mode can run commands in the container and
read the seedbox credentials**; stored secrets can be redirected to a new endpoint by anyone with
settings write (F6); `/docs`/`/openapi.json` are public; the session cookie's `Secure` flag and the
login rate limiter don't understand a reverse proxy (F9/F10).

## Before you start

- `CLAUDE.md`; `prompts/startnewsession.md` lines 1–650; `README.md` in full (find the auth
  section — the audit quotes "Authentication is optional and defaults off" — the "Safety rails"
  section, and "Known gaps"); `docker-compose.yml` (how the port is published);
  `CHANGELOG.md`'s real `## [Unreleased]` section (NOT the commented-out skeleton at the top of
  the file — an earlier patch landed there by mistake).
- **The repo is PUBLIC.** Describe classes of risk and what to do; **never** write exploit steps,
  payloads, or a recipe (e.g. don't say which lftp command to put in which setting). "Can run
  commands in the container" is the right level.

## Working tree check

Main checkout `/home/manderse/projects/lftpweb` on `dev`. `git status --porcelain` should show only
this prompt and an untracked `.claude/worktrees/` (ignore it). Nothing else is running.

## What to do

1. **README — a "Network exposure" (or similarly named) section**, placed next to the existing auth
   text and linked from it. Content, in plain language:
   - `none` mode means anyone who can reach the port can control lftpweb fully — including running
     commands inside the container and reading the seedbox credentials. Only use it on a network
     where every device and every person is trusted, and remember a browser on that network
     visiting a malicious site counts as a device on it.
   - Prefer `password` mode, or `proxy` behind an authenticating reverse proxy.
   - Publish the port on `127.0.0.1` or a specific LAN address rather than all interfaces when a
     reverse proxy sits in front (show the compose `ports:` form). Check what `docker-compose.yml`
     actually ships before writing the example; **do not change docker-compose.yml** — just
     document the option (changing the shipped default is a decision for the user; mention it in
     your report as a suggestion).
   - Never expose lftpweb directly to the internet in any mode until the remaining hardening lands.
   - If an install ever had `none` mode reachable from an untrusted network: rotate the seedbox
     SSH credential and the SABnzbd / rTorrent / Sonarr / Radarr keys.
   - Deployments don't auto-update: pull the new image after `0.4.0` — earlier images carry an
     OpenSSH client with a known critical CVE and the quoting/extraction issues fixed in this release.
2. **README "Known gaps"** — add the still-open items above as named gaps (the user wants gaps
   written down, not hidden): no CSRF/Host/Origin checks in `none`/`proxy` mode,
   `extra_lftp_settings` unrestricted, secrets redirectable on endpoint change, public `/docs`,
   proxy-unaware cookie/rate limiter. One line each, no exploit detail.
3. **README extraction text** — C2 already updated the post-processing bullet; just make sure
   nothing in README still says or implies extraction of untrusted content is unsafe-until-fixed
   or describes the old behaviour.
4. **CHANGELOG** — in the real `## [Unreleased]` section, add a short **upgrade callout** at the
   top of the section (before `### Added`, or as the first `### Security` bullet — match how
   earlier releases in this file flag must-read notes; look at `[0.3.1]` and older for precedent):
   this release fixes security issues, pull the new image, and read README's network-exposure
   section if you run `none` mode. Don't duplicate the existing Security bullets.
5. **`prompts/startnewsession.md`** — update "Where we are" (top of the live section) with a short
   dated block: the 2026-11-26 security pass — audit report location (gitignored, private), the
   four commits, what's deferred (C3/C4/C5/D3 with one line each, pointing at the report §5), and
   that the test deployment is LAN-only per the user. Also correct anything in that section the
   fixes made false (e.g. the "Live validation loop" paragraph's list of unauthenticated endpoints
   is still true — keep it, but note it's because the test system runs `none` mode on a LAN).
6. **`docs/decisions.md`** — only if you make a real decision (e.g. not changing the compose
   default). Newest at top.

## Gates

Docs only, so no test gates are required — but run `uv run ruff check backend tests` anyway is
unnecessary; instead verify: every relative link you add resolves (`grep` the target heading),
`git diff --stat` touches only `.md` files, and no conflict markers exist. Do not touch code.

## When done

1. Update this prompt's frontmatter; plain `mv` it to `prompts/done/` (it's untracked).
2. **Do not commit.** Report back: a summary of each doc change, the exact file list, any
   suggestion for the user (e.g. compose default), and a proposed message (`docs:` prefix, no
   `Co-authored-by:`).
