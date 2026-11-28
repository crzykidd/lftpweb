---
name: 2026-11-27-c5-auth-proxy-hardening
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: F9, F10, F11, F14 done non-breaking; D3 declined and recorded; all gates green
---

# Task: C5, small auth and proxy hardening (non-breaking)

This task covers **audit batch C5** in `private_data/security-audit-2026-11-26.md`: findings
**F9, F10, F11 and F14**. Read the C5 section and those four rows first.

**The user's hard rule is that nothing breaks.** An existing install that upgrades and changes
no configuration must keep working exactly as before. Anything stricter has to be opt-in or
only affect a new action someone is actively taking.

## Before you start

- Read `CLAUDE.md` and the auth and middleware sections of `DESIGN.md`.
- Work in **this checkout on `dev`**. Do not create a worktree. Ignore `.claude/worktrees/`.
- C3 (`b251d35`) and C4 (`c95d1a9`) were done non-breaking. Read their `docs/decisions.md`
  entries and match that approach.
- Read the code: `backend/lftpweb/main.py`, `backend/lftpweb/middleware.py`
  (`AuthMiddleware`), `backend/lftpweb/api/auth.py`, `backend/lftpweb/core/auth.py` (around
  the login rate limiter, about lines 400–435), `backend/lftpweb/core/netguard.py`,
  `backend/lftpweb/config.py`, and the final `CMD` in `docker/Dockerfile`.

## Working tree check

Run `git status --porcelain`. If anything other than `.claude/worktrees/` and this prompt is
dirty, stop and report it.

## What to do

1. **F11: put `/docs`, `/redoc` and `/openapi.json` behind auth.** Don't remove them.
   - Gate them in `AuthMiddleware` exactly like `/api/`. In mode `password`, an
     unauthenticated request should get a 401 or a redirect to login, whichever is consistent
     with how the app treats unauthenticated page loads. Modes `none` and `proxy` behave as
     they do today.
   - Make sure the `RequestGuardMiddleware` and Host allow-list from C3 still apply.
   - Anyone who used them while logged in keeps access.
2. **F9 and F10: add `LFTPWEB_TRUSTED_PROXIES`, opt-in.**
   - The value is a comma-separated list of IPs or CIDRs. A single `*` is allowed but should be
     documented as unsafe.
   - **Unset:** behaviour is byte-for-byte unchanged. Same uvicorn defaults, same cookie logic,
     same rate-limiter key.
   - **Set:** requests from those peers have `X-Forwarded-For` / `X-Forwarded-Proto` honoured.
     Two things follow from that:
     - the session cookie gets `Secure` when the original scheme was https
     - the login rate limiter keys on the real client IP
   - Prefer doing this in the app, for example by wrapping with uvicorn's
     `ProxyHeadersMiddleware(trusted_hosts=...)` when the env is set, rather than editing the
     Dockerfile `CMD`. That way it's testable and works for non-Docker runs too. If you find a
     reason it must go in the `CMD`, explain why.
   - Make sure the image healthcheck (curl to `127.0.0.1`) and C3's `X-Forwarded-Host`
     handling still work.
   - Don't add a per-username limiter, because an attacker could use it to lock out the admin.
     Record that as a decision.
3. **F14: ask for the current password in `PUT /api/settings/auth`.**
   - Require `current_password` when a password is already set **and** the request would
     replace that password or switch away from mode `password`.
   - It must be verified with the same constant-time check that `POST /password` uses, and
     count toward the same rate limiter.
   - Don't require it when no password exists yet, for example when first switching from
     `none` or `proxy` to `password`.
   - Requests that change neither the password nor the mode are unaffected.
   - Update the Settings → Auth form to show a "Current password" field when it's needed and
     send it. Show the server error inline, and don't add a confirm dialog.
   - Check how the existing "forgot password" or env reset path works, and confirm it still
     works.
4. **D3 is declined.** The user will run trivy by hand from time to time rather than in CI.
   - Record this in `docs/decisions.md` as its own short entry.
   - Update the security-pass block in `prompts/startnewsession.md`: C5 is done, D3 is declined
     and trivy is a manual check. Point at the pinned trivy digest the audit used.
   - Do none of D3's other items.
5. **Tests**
   - **Docs pages:** gated in `password` mode, open in `none` mode.
   - **Trusted proxies, unset:** `X-Forwarded-For` / `X-Forwarded-Proto` are ignored and the
     cookie is not `Secure` over http.
   - **Trusted proxies, set** (request from a trusted peer):
     - the cookie is `Secure` with `X-Forwarded-Proto: https`
     - the limiter buckets are per real client IP, so two forwarded IPs don't share a bucket
   - **Trusted proxies, untrusted peer:** headers are ignored even when the env is set.
   - **`PUT /settings/auth`:** a password change without `current_password` gets 422 or 403
     (follow existing conventions); a wrong one is refused and counted; the right one works.
     Also cover first-time enabling with no current password, and a non-password field change.
   - **Frontend:** the field appears and is sent.
6. **Docs, in the same commit**
   - `CHANGELOG.md` `[Unreleased]` Security section. Say "no action needed on upgrade", and
     recommend `LFTPWEB_TRUSTED_PROXIES` behind a TLS proxy.
   - `DESIGN.md`
   - `README.md`: the Network exposure section and environment variable docs, and update the
     Known gaps.
   - `docs/quick-start.md`, if it lists env vars
   - the `docker-compose.yml` comment, commented-out example only
   - `docs/decisions.md`

## Conventions to honor

- Run every verification gate in the **foreground** with an explicit timeout. For pytest that's
  600000 ms. You will never receive a background-task notification, so read each exit code
  yourself.
- From the **repo root**, run and read each of these separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`. The fake seedbox containers are up, so the target is **0 skipped**.
    The baseline is 2415 passed.
- From `frontend/`, run:
  - `npm run lint`
  - `npx tsc -b`
  - `npm test`. The baseline is 865 passed.
- Report each gate's exit code and the counts.

## When done

1. Set this file's frontmatter (`status`, `completed`, `result`) and move it into
   `prompts/done/`. It's untracked, so a plain `mv` is fine.
2. **Do not commit.** Report back:
   - what changed per item
   - anything that could behave differently for an existing install, even slightly
   - the gate results
   - the full explicit path list for a single commit, including this prompt in
     `prompts/done/`
   - a proposed one-line `fix:` message with no `Co-authored-by` trailer
