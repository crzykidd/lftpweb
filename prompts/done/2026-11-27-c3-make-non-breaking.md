---
name: 2026-11-27-c3-make-non-breaking
status: completed
created: 2026-11-27
model: sonnet
completed: 2026-11-27
result: C3 reworked non-breaking (cross-site-only mutating guard, skip-and-report extra settings, opt-in WS Origin check); all gates green
---

# Task: make C3's network-exposure hardening non-breaking for existing installs

C3 is implemented but **uncommitted** in the working tree. Its handoff prompt is
`prompts/done/2026-11-26-c3-network-exposure-hardening.md`, and the audit is
`private_data/security-audit-2026-11-26.md`. The user's hard rule for `0.4.0` is that
**an existing install that upgrades and changes nothing must keep working exactly as before.**
That covers scripts calling the API, browser tabs left open across the upgrade, proxies that
rewrite Host, and stored settings values. Three parts of C3 break that rule. Rework them
in place, in the same uncommitted tree, so C3 lands as one commit.

## Before you start

- Read `CLAUDE.md`, then `DESIGN.md` (especially §9.3 and whatever section C3 added for request
  guarding/netguard), then `prompts/done/2026-11-26-c3-network-exposure-hardening.md` so you know
  what was built and why.
- Work in **this checkout on `dev`**. Do not create a worktree.
- Read the current C3 code before changing it:
  - `backend/lftpweb/middleware.py` (`RequestGuardMiddleware`, `REQUIRED_HEADER`, WebSocket
    4403 close)
  - `backend/lftpweb/core/netguard.py`
  - `backend/lftpweb/core/lftp.py` (`KNOWN_LFTP_SETTINGS`, `extra_lftp_settings_error`, the
    renderer)
  - `backend/lftpweb/core/queue.py` around `lftp_settings_error`, near line 2872
  - `backend/lftpweb/api/health.py`
  - the frontend `SecurityBanners.tsx` / `lib/securityBanners.ts`
  - `tests/test_request_guard.py` and `tests/test_extra_lftp_settings.py`

## Working tree check

The whole C3 change is deliberately uncommitted. The modified and untracked files listed by
`git status --porcelain` are **yours to edit** for this task; you don't need to ask. Ignore
`.claude/worktrees/`.

## What to do

### 1. Mutating-request guard: refuse only cross-site browser requests, require nothing

Replace "`X-Requested-With` required on every mutating `/api/` request" with a rule that rejects
only what a browser positively identifies as cross-site. For POST/PUT/PATCH/DELETE under
`/api/`, in every auth mode:

- If `X-Requested-With` is present, **allow**. A cross-site page cannot add it without a CORS
  preflight, and lftpweb never approves one. The UI keeps sending it, so leave
  `frontend/src/api/client.ts` as is.
- Otherwise, if `Sec-Fetch-Site` is present:
  - `same-origin` or `none`: **allow**.
  - `cross-site` or `same-site`: **reject with 403**, using a clear JSON detail.
- Otherwise, if `Origin` is present (an old browser) and its host matches neither `Host`,
  `X-Forwarded-Host` nor the `LFTPWEB_ALLOWED_HOSTS` entries: **reject**. Treat `Origin: null`
  as cross-site and reject it.
- Otherwise (no `Sec-Fetch-Site` and no `Origin`, i.e. curl, scripts or integrations):
  **allow**.

In `password` mode, keep exactly the CSRF-token behaviour that existed **before** C3. Don't add
any new requirement on top. Check that baseline with `git diff`/`git show HEAD:` on
`middleware.py`.

What this means for existing installs:
- Scripts are unaffected.
- Tabs opened before the upgrade (old JS without the header) send `Sec-Fetch-Site: same-origin`,
  so they're unaffected.
- A Host-rewriting proxy doesn't matter, because `Sec-Fetch-Site` is computed by the browser.

### 2. Stored `extra_lftp_settings`: skip bad lines, never hold transfers

- **Keep the write-time 422.** It only affects someone actively editing the field, which is
  fine.
- At spawn time, **remove the hold.** Render only the conforming lines (same re-quoting as now)
  and drop non-conforming ones: unknown keys, typos, denied settings and non-`set` lines.
  Transfers proceed.
- Expose the dropped lines (or a count plus the first few) through the existing
  `/api/health.lftp_settings_error` field, renamed if that reads better, e.g.
  `lftp_settings_ignored`. Keep the API/types/frontend consistent.
- Log a WARNING once per distinct stored value, not once per spawn.
- Change the banner wording from "transfers are held" to something like
  "N extra lftp setting line(s) are being ignored — fix in Settings → Transfer". Keep the
  existing link to the fix.
- Remove any code, tests or docs that describe the hold, and `git grep` for leftovers. Also
  check that nothing in the queue still treats a settings error as a reason to pause.

### 3. WebSocket Origin check: non-breaking by default

- **When `LFTPWEB_ALLOWED_HOSTS` is unset:**
  - Do **not** compare Origin against Host, because a Host-rewriting proxy would break live
    updates.
  - Close with 4403 only when `Sec-Fetch-Site` is `cross-site` or `same-site`. Browsers send
    `Sec-Fetch-*` on WebSocket handshakes, and the value doesn't depend on the proxy.
  - If `Sec-Fetch-Site` is absent, allow the handshake, and log a WARNING (once per distinct
    Origin) when Origin mismatches.
- **When `LFTPWEB_ALLOWED_HOSTS` is set**, keep C3's current Origin check. That configuration
  is opt-in.
- Before relying on `Sec-Fetch-Site` for WebSockets, confirm against current MDN/Fetch Metadata
  documentation that Chromium, Firefox and Safari send it on WebSocket handshakes. If you can't
  confirm it, drop the `Sec-Fetch-Site` rejection for WebSockets in unset mode (log-only) and
  say so in the report.

### 4. Unchanged

These parts of C3 already don't break anything. Leave them alone apart from doc wording:
- `LFTPWEB_ALLOWED_HOSTS` (unset accepts any Host, with the warning and banner)
- the `extra_lftp_settings` allow-list at write time
- `HostIn.address` validation, which applies only on save
- the Vite `xfwd: true` change

### 5. Tests

Update `tests/test_request_guard.py` and `tests/test_extra_lftp_settings.py`, plus any frontend
tests, to cover:
- curl-style (no headers) mutating request allowed in `none`/`proxy`/`password` (password still
  needs its pre-C3 CSRF token)
- `Sec-Fetch-Site: same-origin` without `X-Requested-With`, allowed (the stale-tab case)
- `cross-site` and `same-site` rejected
- `Origin` mismatch without `Sec-Fetch-Site`, rejected; `Origin: null`, rejected
- `X-Requested-With` present, allowed
- a stored value with one good line and one bad line: the job transfers, the good line is
  applied, the bad line is reported, and no hold happens
- WebSocket:
  - unset, with a Host-rewriting-proxy shape (mismatched Origin, no `Sec-Fetch-Site`):
    connects
  - unset with `cross-site`: 4403
  - set with a mismatched Origin: 4403

### 6. Docs (same commit)

Rewrite the C3 entries to describe the non-breaking design in:
- `CHANGELOG.md` `[Unreleased]`. The upgrade note should now say no action is required, and
  that `LFTPWEB_ALLOWED_HOSTS` is recommended.
- `DESIGN.md`
- `README.md`: the Network exposure section and Known gaps
- `docs/quick-start.md`
- `docker-compose.yml` comment, if it mentions behaviour that changed
- `prompts/startnewsession.md`

Add a new top entry to `docs/decisions.md` covering:
- why the header requirement became a cross-site refusal
- why the hold became skip-and-report
- why the WebSocket Origin comparison is opt-in

The user's rule is "no existing install breaks on upgrade."

Name the gaps this leaves in README Known gaps. Don't hide them. They include:
- a browser that sends neither `Sec-Fetch-Site` nor `Origin` on a POST (very old browsers)
  isn't protected
- in unset mode a WebSocket from a browser that omits `Sec-Fetch-Site` isn't refused

## Conventions to honor

- Every verification gate runs in the **foreground** with an explicit generous timeout (pytest
  ~4 min, so use a 600000 ms timeout). Never background a gate.
- From the **repo root**, run and read the exit codes separately:
  - `uv run ruff check backend tests`
  - `uv run ruff format --check backend tests`
  - `uv run pytest`: the fake seedbox containers are up, and the target is **0 skipped**. The
    pre-rework count was 2361 passed.
- From `frontend/`:
  - `npm run lint`
  - `npx tsc -b`
  - `npm test` (pre-rework: 858 passed)
- Report every gate's exit code and counts. If a count drops, explain why.

## When done

1. Set this file's frontmatter (`status`, `completed`, `result`) and `git mv` it into
   `prompts/done/`. The file is untracked, so `git add` it first or plain `mv` it; either is
   fine.
2. **Do not commit.** Report back:
   - what changed, per item 1–3 above
   - the gate results
   - the full explicit list of paths for the single C3 commit: all of C3's files plus this
     prompt in `prompts/done/`, excluding `.claude/worktrees/`
   - a proposed one-line `fix:` message with no `Co-authored-by` trailer
