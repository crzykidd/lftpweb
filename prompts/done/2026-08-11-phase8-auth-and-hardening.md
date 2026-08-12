---
name: 2026-08-11-phase8-auth-and-hardening
status: done
created: 2026-08-11
model: sonnet
completed: 2026-08-12
result: |
  All three AUTH_MODEs (none/password/proxy), an API key mechanism independent of mode, and
  the credentials-need-re-entry finish all built and verified. AUTH_MODE defaults to none and
  the regression test proves an unauthenticated install behaves identically. 42 protected
  routes enumerated and each asserted 401 unauthenticated in password mode, with a drift
  check against the app's actual routes. Two lockout-recovery routes (LFTPWEB_AUTH_MODE env
  override; deleting the auth_user row) both actually exercised by tests, not just
  documented. 366 tests pass with the fake seedbox up (0 skipped). Both lint gates clean,
  frontend build/lint clean, all three compose files validate. Not committed per explicit
  instruction -- see the final report for the file list and proposed commit message. Not
  verified: actual browser rendering of the login page, Settings -> Auth, and the
  credentials banner (no browser available in this environment).
---

# Task: Phase 8 — auth and hardening

The app currently has **no authentication at all**. Anyone who can reach it controls the user's
seedbox config and can queue, stop, and — since phase 5 — delete remote files. This phase closes
that, without changing behaviour for an existing install until the user opts in.

**Done when:** all three auth modes work, an API key works independently of the mode, and a fresh
install with no configuration behaves exactly as it does today.

## Before you start

- **Read `DESIGN.md` §8 in full** (three modes, credential encryption, the trusted-proxy CIDR
  requirement, credentials-need-re-entry), §10.1 (redaction), §11.1 (compose hardening), §13
  phase 8, §15.
- Read `prompts/startnewsession.md` and `docs/decisions.md`. Credential *encryption* already
  shipped in phase 2 — this phase owns the rest of §8.
- Phases 1–7 are committed.

## Working tree check

`git status --porcelain` first. Anything dirty: list it and ask. This file is exempt.

## Non-negotiables

- **`AUTH_MODE` defaults to `none`.** The user's live instance must behave identically after
  pulling this. An app that starts demanding a password nobody set is a lockout, not a feature.
- **Locking the user out is the worst possible outcome of this phase.** Every path that could
  do so needs a documented recovery route (an env var, a CLI reset, a documented file to delete).
  Write that route down in the report *and* in the app's own docs.
- **Do NOT commit.** Prepare the tree and report back.

## What to do

### 1. The three modes (§8)

- **`none`** (default) — no auth, exactly today's behaviour.
- **`password`** — single user, **argon2id** hash stored in SQLite, HTTP-only `SameSite=Lax`
  session cookie, CSRF token required on mutating requests, rate-limited login.
- **`proxy`** — trust a configurable identity header (`Remote-User` by default), **only** when
  the request originates from a configured trusted CIDR. §8 is explicit that without the CIDR
  check this mode is a bypass, so it is not optional: refuse to enable `proxy` mode without one.

**API key** — `X-API-Key` header, accepted independently of the mode, for scripts. Store hashed,
show the plaintext once at creation and never again.

### 2. What must stay reachable

- `/api/health` must remain unauthenticated — the container `HEALTHCHECK` calls it and would
  otherwise fail the container permanently. Confirm by reading `docker/Dockerfile`.
- Static assets and the SPA shell must load so the login page can render.

Everything else — settings, files, jobs, history, logs, backup — requires auth when a mode is on.
**Enumerate the routes and assert the negative in tests**: an unauthenticated request to each
protected endpoint gets 401/403. A route accidentally left open is the whole failure mode here.

### 3. Credentials-need-re-entry (§8)

Phase 2 shipped encryption and a partial version of this. Finish it: if the credential blob
won't decrypt with the current install key (the case after restoring a backup to a fresh
install, since §10.2 deliberately excludes the key), mark the host **"credentials need
re-entry"**, hold transfers for that host, and surface a banner. Don't crash, don't spam
`AUTH_FAILED` jobs.

### 4. Hardening

- Rate-limit login attempts.
- Verify the §10.1 redactor covers anything auth adds to logs (never log a token, cookie, or
  key — not even truncated).
- Review the §11.1 compose hardening still holds; note anything auth requires that it forbids.

## Verify before reporting — actually run these

1. `uv run pytest` passes. Tests must include:
   - **with `AUTH_MODE=none` (the default), every endpoint behaves exactly as before** — this is
     the regression that matters most;
   - each protected route returns 401/403 unauthenticated in `password` mode (enumerate them);
   - `/api/health` stays reachable unauthenticated in **every** mode;
   - login success/failure, session cookie flags (HttpOnly, SameSite), CSRF rejection;
   - **`proxy` mode refuses to enable without a trusted CIDR**, and rejects a spoofed header
     from outside it;
   - API key accepted in every mode; a wrong key rejected;
   - argon2id is actually used (not a fallback), and the hash is never returned by any endpoint.
2. **Prove the lockout-recovery route works** — actually exercise it, don't just document it.
3. `npm run build` and `npm run lint` clean.
4. **Both lint gates repo-wide, exactly as CI runs them** — `check` alone missed 6 unformatted
   files in phase 7:
   ```
   uvx ruff@0.8.4 check  --config ruff.toml .
   uvx ruff@0.8.4 format --config ruff.toml --check .
   ```
5. `docker compose config --quiet` clean on all three compose files. Tear down anything you start.

State plainly anything you could not verify. No browser available — do not imply the login page
was click-tested.

## Surfacing decisions

The user is asleep and asked that **every decision made without them be documented**. Record each
in `docs/decisions.md` (newest at top) with rejected alternatives, and repeat them in your report.
If `DESIGN.md` is wrong or silent, make the smallest reasonable call, record it, and **do not edit
`DESIGN.md`**.

Security decisions especially: if you weaken something for practicality, say so explicitly rather
than letting it pass as an implementation detail.

## When done

1. `docs/decisions.md` entries.
2. Update `prompts/startnewsession.md` (phase table, "Where we are"), and **document the
   lockout-recovery route somewhere the user will find it** (`README.md` is reasonable).
3. Frontmatter: `status`, `completed`, `result`.
4. `git mv` this file to `prompts/done/` (or `prompts/failed/`).
5. **Do NOT commit.** Report the file list and a proposed one-line commit message (`feat:`
   prefix, no `Co-authored-by:`; branch `dev`).
