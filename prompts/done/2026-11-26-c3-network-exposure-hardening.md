---
name: 2026-11-26-c3-network-exposure-hardening
status: completed        # pending | completed | failed
created: 2026-11-26
model: opus              # opus = research/planning, sonnet = coding (mixed -> opus)
completed: 2026-11-26     # filled when the work is done
result: "X-Requested-With required on mutating API calls (all modes), LFTPWEB_ALLOWED_HOSTS + WS Origin check, extra lftp settings restricted to allow-listed set lines (bad stored value holds transfers), HostIn.address validated; all gates green, 0 skips"
---

# Task: network-exposure hardening for every auth mode (audit batch C3)

The 2026-11-26 security audit (`private_data/security-audit-2026-11-26.md` — read §1, §3 F2, F12,
F13 and §5 C3 in full) found that in auth mode `none` (the default) and `proxy`, lftpweb has no
cross-site protection at all, and that settings write is equivalent to command execution:

- **CSRF:** a bodiless cross-site `POST /api/items/{id}/delete` is a CORS "simple request" and
  deletes local items (verified against FastAPI 0.141.1). Only `password` mode checks a CSRF token
  (`backend/lftpweb/middleware.py:147-171`); `proxy` skips it.
- **DNS rebinding:** no Host validation anywhere (`backend/lftpweb/main.py:217-227`), so a website a
  LAN user visits can rebind its own name to the instance and talk to it same-origin.
- **WebSocket:** no Origin check (`backend/lftpweb/api/ws.py:25`) — any site can read the live state
  stream in `none` mode.
- **`extra_lftp_settings`** is injected into every job's lftp rc file verbatim
  (`backend/lftpweb/core/lftp.py`, `build_rc_text` — line numbers moved in commit `3aa48d6`), so a
  `!cmd` line, a command separator, or a program-executing lftp setting runs code in the container.
- **Host address** has no API-level validation (`HostIn.address`, `backend/lftpweb/models.py`).
  Commit `3aa48d6` (C1) added a spawn-time check (`[A-Za-z0-9][A-Za-z0-9._:%\[\]-]*`) so an unsafe
  address fails the job, but Settings still *saves* one — the user only learns at transfer time.

## Decisions already made by the user (2026-11-26) — implement these, don't re-litigate

1. **Host allow-list: permissive + warn.** New env var `LFTPWEB_ALLOWED_HOSTS` (comma-separated
   hostnames / IPs, optional `:port`; decide and document whether wildcards like `*.example.com`
   are supported). **Unset = accept any Host, as today**, but:
   - log one WARNING at startup in `none`/`proxy` mode saying DNS-rebinding protection is off and
     how to enable it;
   - show a banner in the UI in `none`/`proxy` mode while it's unset (house style: an informative
     banner, **no dismiss/confirm dialog** — see how existing banners are built, e.g.
     `frontend/src/components/PreflightBox.tsx`; pick the right page — probably the app shell or
     Settings → Security/Auth, whichever the user sees; justify the choice).
   Set = requests with any other Host get 403 (with a body naming the env var), in every auth mode.
   `localhost`/`127.0.0.1`/`::1` should always be allowed so the container healthcheck
   (`curl http://127.0.0.1:8087/api/health`) and a local recovery path keep working — confirm the
   healthcheck still passes.
2. **Custom header required on every mutating API request (POST/PUT/PATCH/DELETE under `/api/`)
   in every auth mode.** The user has **no** outside API callers; any caller without it gets 403
   whose body names the header. The frontend must always send it. In `password` mode the existing
   CSRF token stays; decide whether its presence satisfies the new requirement or whether both are
   needed, and record why. Requiring a non-safelisted header forces a CORS preflight, which
   lftpweb never answers permissively — verify there is no CORS middleware that would allow it
   (grep `CORSMiddleware`), and if there is, explain how it interacts.

## Also in scope (your design; record each choice in `docs/decisions.md`)

3. **WebSocket Origin check:** if an `Origin` header is present it must match the request's Host
   (and the allow-list when set); reject otherwise. A missing Origin (non-browser client) — decide;
   the audit's reasoning is that a non-browser peer already has network access, which in `none`
   mode is full control anyway, so refusing it buys little. Justify.
4. **Restrict `extra_lftp_settings`.** Parse it into lines; accept only `set <key> <value>` lines
   (and blank lines / `#` comments if the current UI or docs suggest them). Re-emit each accepted
   line through C1's `_lftp_quote` rather than passing text through, so a value can never carry a
   separator (`;`, `&&`, `||`, `&`, `|`, newline) or a `!`. **Research which lftp settings themselves
   execute programs or write arbitrary files** (lftp 4.9.3's man page / `set -a` output in the image
   — e.g. `*:connect-program`, anything under `cmd:` that runs a shell, log-file paths) and refuse
   those keys. Allow-list vs deny-list: choose, justify, and keep the **existing deliberate ability
   to override a colliding lftpweb default** (`tests/...test_extra_lftp_settings_override_a_colliding_lftpweb_default`
   — find it) for any key that isn't dangerous. Validate at **write time** (422 with the offending
   line) **and** at **spawn time** (a stored value written before this release must not be trusted):
   at spawn, decide between dropping bad lines with a logged warning vs failing the job — prefer
   whatever can't silently weaken a transfer and can't silently run anything; justify. The test
   deployment's stored value is empty, so no live config is affected, but an unknown production
   value might be — the spawn-time behaviour must not take a working install down for a harmless
   non-`set` line without a clear, visible reason.
5. **Validate `HostIn.address` at the API** with the same rule C1 enforces at spawn — import the
   one definition from `core/lftp.py` rather than copying the regex. 422 with a clear message.
   Existing stored values aren't migrated; they already fail clearly at spawn.
6. **Proxy mode** gets CSRF protection via item 2's header (F13) — confirm by test.

## Before you start

- `CLAUDE.md`; `prompts/startnewsession.md` lines 1–660; `DESIGN.md` §8 (auth and security) and §4
  (rc building); all of `backend/lftpweb/middleware.py`, `backend/lftpweb/core/auth.py`,
  `backend/lftpweb/api/auth.py`, `backend/lftpweb/api/ws.py`, `backend/lftpweb/main.py`; the
  frontend API client (`frontend/src/api/` — find the single fetch wrapper and the WebSocket
  opener); README's "Network exposure" and "Known gaps" (G1 just wrote them — this task closes
  three of those gaps, so update them, not add a second copy).
- **Out of scope:** C4 (secrets redirect), C5 (`/docs`, proxy headers, current-password check),
  D3 (CI scanning). Don't touch them.
- **Inbound non-browser callers:** check whether anything legitimately POSTs to lftpweb without the
  UI (webhooks from *arr/SABnzbd, the healthcheck, the support bundle, tests' own clients). If one
  exists, stop and report rather than exempting it silently.

## Working tree check

Main checkout `/home/manderse/projects/lftpweb` on `dev` (NOT a worktree — worktrees here start
from `main`). `git status --porcelain` should show only this prompt and untracked
`.claude/worktrees/` (ignore it; pass explicit paths to tools such as `ruff` so they don't recurse
into it). Nothing else is running in this tree.

## Tests — write them first, watch them fail, then implement

At minimum: bodiless cross-origin-style POST refused in `none` and in `proxy` mode; the same POST
with the header succeeds; `password` mode unchanged for the UI; foreign Host → 403 when the
allow-list is set, any Host accepted (and the banner/flag reported) when unset; loopback Host always
accepted; WS with a foreign Origin refused, matching Origin accepted; `extra_lftp_settings` with
`!id`, `set x y; !id`, a separator inside a value, and a dangerous key each → 422 at write and
neutralised at spawn (assert the generated rc text, and for one case run the real `lftp` binary
against the rc to prove nothing executes — C1's `tests/test_lftp_quoting.py` shows how); a benign
override of a colliding default still works; `HostIn.address` with a leading `-` or a space → 422.
Frontend: the fetch wrapper always sends the header (unit test), and the banner's visibility logic
as a pure function (this repo's pattern — see `frontend/src/lib/preflight.ts` / its tests).

## Gates (each FOREGROUND, explicit generous timeout, read each exit code)

From the repo root: `uv run ruff check backend tests`, `uv run ruff format --check backend tests`,
`uv run pytest` (full, ~5 min; the fake seedbox should be up — `docker ps`; if not, bring it up with
`docker compose -f docker-compose.test.yml up -d` so skips are 0 — report the count). Frontend, in
`frontend/`: `npm run lint`, `npx tsc -b`, `npm test`. Also build the image
(`docker build -f docker/Dockerfile --target runtime -t lftpweb:c3-local .`), run it with
`--read-only --tmpfs /run -e PUID=1000 -e PGID=1000`, and prove: the healthcheck command succeeds;
a POST without the header gets 403; with it, it doesn't. Stop the container afterwards.

## Docs (same commit)

`DESIGN.md` §8; `docs/decisions.md` (newest at top — one entry covering every choice above, with
rejected alternatives); `CHANGELOG.md` real `## [Unreleased]` section (NOT the commented skeleton at
the top): `### Security` for the fixes, `### Changed` for the user-visible behaviour (outside API
callers now need the header; `LFTPWEB_ALLOWED_HOSTS`; non-`set` lines in the extra lftp settings
are refused) — bold the behaviour change like the existing entries; README: document
`LFTPWEB_ALLOWED_HOSTS` and the header in "Network exposure", remove the now-closed items from
"Known gaps" (keep C4/C5 ones), and keep public text at the level G1 used — **no exploit recipes,
no naming of which lftp settings are dangerous** (the code and tests can name them; README says
"settings that could run programs are refused"). Update `prompts/startnewsession.md`'s
"2026-11-26 security pass" block: C3 done (commit pending), what remains.

## When done

1. Update this prompt's frontmatter; plain `mv` it to `prompts/done/` (it's untracked).
2. **Do not commit, do not push.** Report back: each design choice and why, tests failing-before /
   passing-after, every gate's exit code + the skip count, the image check, anything that could
   change behaviour for an existing install, the exact file list, and a proposed message (`fix:`
   prefix, no `Co-authored-by:`).
