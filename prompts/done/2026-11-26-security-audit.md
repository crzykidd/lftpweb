---
name: 2026-11-26-security-audit
status: completed        # pending | completed | failed
created: 2026-11-26
model: opus              # opus = research/planning, sonnet = coding
completed: 2026-11-26     # filled when the work is done
result: "22 scanner CRITICAL/HIGH in published 0.3.1 (16 in a fresh dev build), 2 reachable (openssh client, certifi); plus 3 non-scanner CRITICAL/HIGH (lftp quoting command injection from seedbox names - verified, 7-Zip XZ CVE, unrar 7.20 beta); 19 findings, 9 fix batches proposed; report in private_data/security-audit-2026-11-26.md"
---

# Task: full security audit of lftpweb ahead of 0.4.0 — trivy scan + code review, propose fixes

The user wants to know where lftpweb stands against the many CVEs published recently, and a
ranked set of proposed fixes that minimise risk. **This is an audit, not a fix pass: write NO
application code, change NO dependency pins, edit NO Dockerfile/compose/workflow.** The deliverable
is a report plus a proposed fix plan the user will approve item by item; fixes land later as their
own handoff prompts.

## Before you start

- Read `CLAUDE.md`, then `prompts/startnewsession.md` lines 1–650 (the live sections — the rest is
  history). Skim `DESIGN.md` for the auth, SSH, post-processing/delete, and §17 download-client
  sections, and `README.md`'s "Safety rails", "Support bundle" and "Known gaps".
- Stack: Python 3.13 / FastAPI / SQLite / asyncssh backend (`backend/`, `uv`, root
  `pyproject.toml` + `uv.lock`); React + TS + Vite frontend (`frontend/`, npm lockfile); one Alpine
  image built from `docker/Dockerfile`, bundling lftp, OpenSSH, 7-Zip, su-exec, tini
  (`NOTICE`); `docker-compose.yml`; CI in `.github/workflows/`.
- **The repo is PUBLIC.** The report goes in `private_data/` (gitignored) — never in a tracked file.
  Do not open GitHub issues, do not comment on anything, do not post findings anywhere external.

## Working tree check

Run `git status --porcelain`. The tree currently carries an unrelated, uncommitted feature change
(Preflight `unbound_clients`: `backend/lftpweb/api/jobs.py`, `models.py`, several frontend
Preflight files, docs) that the orchestrating session is gating and committing in parallel. **Do not
touch, stage, stash, or revert any of it.** You are read-only on the tree except for this prompt
file and files under `private_data/`.

## What to do

### 1. Get a trustworthy scanner

trivy is not installed on the host; Docker is. **Trivy itself had a supply-chain compromise in
2026** (malicious release artifacts / hijacked `trivy-action` tags — confirm the details and the
affected versions from Aqua's own advisory before choosing a version). So: pick a release
documented as clean, pull the official image (`aquasec/trivy:<version>` or the GHCR equivalent),
**pin it by digest**, and record the version + digest in the report. Do not use `:latest`. If you
cannot establish a known-good version with reasonable confidence, stop and report that rather than
running an unverified scanner. Cache trivy's DB under `private_data/` or the scratchpad, not in the
repo tree.

### 2. Scans (each run in the FOREGROUND with a generous timeout — never background)

1. **Build the production image locally** from `docker/Dockerfile` (tag it e.g.
   `lftpweb:audit-local`; don't push anything). Also pull the currently published image
   (`:latest` and `:0.3.1` — find the registry from the publish workflow) so the report can say
   what *deployed* installs are exposed to vs. what `dev` would ship.
2. `trivy image` on each: OS packages (Alpine apk — note lftp, openssh, 7zip, musl, busybox, openssl)
   and language packages (the Python venv in the image, plus any node remnants). All severities,
   then summarise by CRITICAL/HIGH with fixed-version availability.
3. `trivy fs` on the repo: `uv.lock`, `frontend/package-lock.json`, plus `--scanners
   misconfig,secret` over `docker/`, `docker-compose*.yml`, `.github/workflows/`.
4. Cross-check the Python side with `uv run pip-audit` (or `uvx pip-audit` against an export of the
   lock) and the frontend with `npm audit --omit=dev` and full `npm audit` — distinguish runtime
   from dev/build-only deps, because only the built static bundle ships for the frontend.
5. Note the Alpine base tag and whether a newer patch tag exists.

### 3. Triage, not a dump

For every CRITICAL/HIGH (and any MEDIUM that is plausibly reachable): **is it reachable in
lftpweb?** e.g. a CVE in an OpenSSH *server* component when we only use the client; a 7-Zip archive
parsing bug — highly relevant, because lftpweb extracts archives downloaded from a seedbox, i.e.
attacker-influenceable input; an lftp/OpenSSL bug on the transfer path; a Starlette/FastAPI/
python-multipart/h11/httpx/asyncssh bug on an exposed path. Mark each `reachable` / `not reachable`
/ `unclear` with one line of reasoning. A long table of unreachable CVEs with "upgrade anyway" is
fine; a reachable one buried in it is the failure mode to avoid.

### 4. Code and configuration review (read the code — don't guess)

Look specifically at:

- **Authentication/session:** what is unauthenticated? `startnewsession.md` says the test system
  answers `/api/health`, `/api/history/events`, `/api/logs/*`, `/api/settings/*` and `/api/files`
  **unauthenticated** from the dev host — determine from code whether that is a deliberate
  allow-list, a trusted-network/auth-disabled mode, or a gap, and what `/api/settings/*` and
  `/api/logs/*` could disclose (credentials, API keys, host keys, paths). Session cookie flags,
  CSRF on state-changing routes, password hashing, login rate limiting, WebSocket auth.
- **Secret handling:** SSH keys/passwords, SABnzbd API key (rides in a query string — see the httpx
  log-floor trap), rTorrent creds, *arr API keys, announce URLs (credentials — hostname only, ever).
  Where stored, whether ever returned by an API, logged, or put in the support bundle.
- **Command / argument injection:** everything that builds an lftp rc/script, an ssh/`find`
  command, or a 7-Zip invocation from remote-controlled names (seedbox filenames are untrusted
  input). Quoting in `core/lftp.py`, `core/remote.py`, extraction code.
- **Path traversal / containment:** `POST /api/items/{id}/delete` (local + remote delete), Move
  step, extraction staging (`_UNPACK_`), archive members with `../` or absolute paths or symlinks
  (zip-slip), path-browse endpoints, `is_authorized_delete_target`.
- **SSRF:** user-configured download-client / *arr base URLs and the Test buttons — what can an
  authenticated (or unauthenticated) caller make the server fetch, and does any response body get
  reflected back?
- **SSH host-key policy:** confirm the `known_hosts` handling described in the brief really
  enforces verification (TOFU? pinned?).
- **Frontend:** `dangerouslySetInnerHTML`, rendering of remote filenames/log text, links built
  from data.
- **Container/deploy:** user, capabilities (`CHOWN`/`SETUID`/`SETGID` added back), `read_only`,
  exposed ports, default bind address, entrypoint privilege drop.
- **CI/supply chain:** GitHub Actions pinned by SHA or tag? `permissions:` scoped? any
  `pull_request_target`? Does CI use trivy-action or any action affected by 2026's compromises?

You MAY make plain read-only `GET` requests to the **test** deployment
(https://lftpweb.crzynet.com) to confirm which endpoints answer unauthenticated and what shape they
return. No fuzzing, no POST/PUT/DELETE, no attempts to exploit anything, no brute force. **Never
touch the production deployment** (it is unreachable anyway). If a response contains a secret,
record *that it does* and the field name — never copy the secret value into the report.

### 5. Write the report

`private_data/security-audit-2026-11-26.md`:

1. **Executive summary** — 5–10 lines: overall posture, the top 3 risks, and whether anything
   needs fixing *before* 0.4.0 ships.
2. **Scanner provenance** — trivy version + digest, DB date, how its integrity was established.
3. **Findings table** — ID, severity (your judgement, not only CVSS), component, reachable?,
   evidence (`file:line` or scan line), proposed fix.
4. **CVE appendix** — the full triaged list, image vs. repo, published `:0.3.1` vs. local `dev` build.
5. **Proposed fix plan** — grouped into batches the user can approve one at a time, each batch
   sized as a future handoff prompt (title, files it would touch, model, risk of the change
   itself, how to verify it). Order by risk reduced per unit of effort. Separate "base image /
   dependency bumps" from "code changes" from "deploy/config guidance for users".
6. **What was not covered** — name the gaps honestly (e.g. no dynamic testing, no auth'd surface
   probing).

## Conventions to honor

- Foreground every scan with an explicit generous timeout; read exit codes. A spawned agent never
  receives background-task notifications.
- No code edits, no commits, no pushes, no external posting. No secrets in the report.
- Cite evidence as `file:line`. Mark uncertainty explicitly rather than smoothing it over.

## When done

1. Update this file's frontmatter: `status`, `completed`, `result` (one line, include counts:
   e.g. "N CRITICAL/HIGH, M reachable; K fix batches proposed").
2. `git mv` this file into `prompts/done/` (or `prompts/failed/`). Note `git mv` requires the file
   to be tracked — if it is untracked, plain `mv` it and say so.
3. Do not edit `docs/decisions.md` unless the audit forces a real decision (it shouldn't — it only
   proposes).
4. **Do not commit.** Report back: the executive summary, the findings table's top rows, the
   fix-batch list, the report path, and the proposed commit (just this prompt's move,
   `docs: record the 0.4.0 pre-release security audit prompt`).
