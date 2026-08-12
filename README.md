# lftpweb

A containerized web interface for keeping a local directory in sync with a seedbox, using
**lftp** as the transfer engine over SSH/SFTP.

Browse the remote and local trees as one view, queue and supervise downloads with live
progress, auto-queue on patterns, and optionally verify, extract, and relocate finished items.

> ## ⚠ Pre-release — not ready to use
>
> **Version `0.0.1`. All 9 build phases are built and unit/integration tested.** There is no
> release, no published image, and no upgrade path. Things will change without notice,
> including the database schema. **No UI screen in this project has ever been opened in a
> browser** — every page was built, verified against the real backend and the real fake
> seedbox over HTTP, and confirmed to build/type-check/lint cleanly, but never visually
> confirmed. See "Known gaps" below before trusting any of it.
>
> Try it if you want to poke at it. Don't point it at anything you care about yet.

## What works today

- Connect to a seedbox over SSH/SFTP; browse the remote tree alongside the local one
- Named **path queues** — one remote → local mapping each, with their own settings
- Queue transfers manually, watch live progress, stop them, resume from the partial;
  multi-select with shift-range and bulk Queue/Stop that reports partial failure honestly
  ("7 of 10 queued, these 3 failed because …"), plus text/state filters, on the Files page
- Bandwidth ceiling and concurrency limits with an admission-control scheduler
- Auto-queue on select/skip/file-exclude patterns, with a live "what would this match" preview
- Post-processing: verify (sidecar or hash-on-disk), extract (7zz), and `move` mode's
  verification-gated remote delete, all with an audited trail on the History page
- The History page: every completed/failed/cancelled transfer and every audit event
  (including remote deletes and deletes withheld), filterable and grouped by queue
- Rotating log viewer, on-demand `VACUUM INTO` database backups (scheduled + manual), and a
  header readout for seedbox reachability and scheduler liveness (`/api/health`)
- Credentials encrypted at rest
- **Authentication is optional and defaults off** (`AUTH_MODE=none`) — turn on a single-user
  password login or trust a reverse proxy's identity header, both from Settings → Auth. See
  "Locked out?" below before you flip it on.

## What doesn't yet

All 9 build phases shipped (see `prompts/startnewsession.md` for exactly what each one built
and how it was verified), but real gaps remain:

| | Notes |
|---|---|
| Settings → Transfer has no UI | The site-wide bandwidth/concurrency/fast-lane API (§4.5) is complete and has been since phase 3 — there is just no form for it. Reachable today only by calling `/api/settings/transfer` directly. The §9.3 free-text "extra lftp settings" box and the live connection-count-vs-`net:connection-limit` warning live behind this same missing tab. |
| Files page has no bulk "Delete local" / "Delete remote" | §9.2 lists both alongside Queue/Stop; phase 9 built honest partial-failure reporting and filters for Queue/Stop only, per its own scope. Deletion still only happens automatically through `move` mode's verification-gated pipeline. |
| Propagating local deletes to the seedbox (`sync` mode) | Designed (`DESIGN.md` §7) but **not scheduled** — built only if it proves wanted. |

See "Known gaps" below for the rest — behavioral limitations and deliberate trade-offs, as
opposed to the unbuilt UI above.

## Known gaps

Named here rather than fixed silently or left to be rediscovered — each is a deliberate scope
reduction made during the build, recorded in full in `docs/decisions.md`:

- **No UI screen in this project has ever been opened in a browser.** No browser exists in any
  environment this project has been built in. Every page has been confirmed to build,
  type-check, and lint cleanly, and every backend endpoint it calls has been verified directly
  over real HTTP — but actual rendering, layout, and click-through behavior have never been
  visually confirmed. Click-test before relying on any of it.
- **Post-processing (verify/extract/move) triggers only when a job this app spawned succeeds**
  — not when a routine rescan finds a file that arrived some other way (a manual `cp`, a
  restore). A file placed by hand under a queue's `local_path` will sit there unverified,
  unextracted, and — for a `move` queue — with its remote copy never deleted, until something
  else re-touches that item (e.g. a manual re-queue). Phase 5's own deliberate call.
- **The `REMOVED_LOCAL` grace period is unit-tested but has never been exercised live across a
  real multi-scan window.** The ~10-minute window between "locally absent" and "treated as
  deliberately removed" (`DESIGN.md` §3.2 rule 3) is pinned by `tests/test_mount_sentinel.py`,
  not by watching it elapse against a real seedbox over real wall-clock time.
- **Date-range filters (History page) are UTC calendar days.** No timezone handling exists
  anywhere in the app — every stored timestamp is UTC and every render is a raw
  `toLocaleString()`. For a user well away from UTC, "yesterday" in the date picker can include
  a few hours of what they'd call "today," or vice versa.
- **API keys are hashed with SHA-256, not argon2id** — deliberate, not an oversight. A key is
  256 bits of random `secrets.token_urlsafe`, not a guessable human-chosen secret, so argon2's
  memory-hard slowness buys nothing and would add latency to every API-key-authenticated
  request. Session tokens are hashed the same way, for the same reason.
- **Login response timing is not normalized between "unknown username" and "wrong password."**
  A deliberate, minor simplification for a single-user homelab app where the one valid username
  is typically visible elsewhere anyway; both cases return an identical body and are
  rate-limited identically, only wall-clock timing differs, and only under repeated automated
  probing.
- **`password` auth mode with no user configured is treated as open access, not a lockout** —
  see "Locked out?" below. This is deliberate (the alternative bricks the instance on a typo),
  but it means anyone who can reach the API while no user row exists is in without a password.

## Locked out?

`AUTH_MODE` defaults to `none` — a fresh pull of this image, or an existing install that has
never visited Settings → Auth, behaves exactly as if auth didn't exist. If you turn on
`password` mode and get locked out (forgot the password, or the container starts refusing you
for any other reason), there are two independent ways back in — no browser needed for either:

1. **Set `LFTPWEB_AUTH_MODE=none`** (or `password`/`proxy`, to force a specific mode) as a
   container environment variable and restart. This overrides whatever is stored in the
   database, unconditionally, until you unset it again. Fastest if you can edit your compose
   file / restart the container.
2. **Delete the local user row directly**, if you have shell/DB access but don't want to
   restart:
   ```bash
   sqlite3 /config/lftpweb.db "DELETE FROM auth_user"
   ```
   With `password` mode on and no user configured, lftpweb treats that as open access rather
   than rejecting every request forever — sign back in via Settings → Auth to create a fresh
   user once you're in. **Say this plainly: this is a deliberate fail-open, not a fail-closed.**
   Between the `DELETE` and creating a fresh user, anyone who can reach the API has full
   access, no password required — the trade this project makes is that a five-second mistake
   (or, on a shared host, someone else's access) recovering the instance beats a five-second
   `DELETE` permanently bricking it. See "Known gaps" above.

Both routes are exercised by `tests/test_auth_api.py::test_lockout_recovery_env_var_override`
and `::test_lockout_recovery_delete_user_row`, not just documented here.

## Running it

Requires Docker and a seedbox you can reach over SSH.

```bash
docker compose up -d          # production-shaped
docker compose -f docker-compose.dev.yml up --build    # local development
```

The UI is on **`8087`** (`5187` for the Vite dev server). Configure the host and your path
queues under **Settings**.

| Volume | Holds |
|---|---|
| `/config` | SQLite database, logs, backups, encryption key |
| `/downloads` | where lftp writes and what the reconciler scans (`local_path`) — this is the download target, not a landing zone that gets relocated out of |
| `/staging` | optional; the *destination* a `move`-mode queue's post-processing step relocates a verified, finished item **to**, once it's fully downloaded and verified in `/downloads`. Labeled "Final destination" in Settings → Queues to match this — see `docs/decisions.md`'s phase 5 entry for why this reads backwards from the field's own name (`staging_path`) |

`PUID` / `PGID` / `UMASK` are honored, which matters if your downloads live on an NFS share —
see `DESIGN.md` §11.2.

## Design

**[`DESIGN.md`](DESIGN.md)** is the architectural source of truth, in 15 numbered sections.
Worth reading §1.3 before anything else, because the whole codebase follows from it:

> **lftp is a transfer engine, not a status API.** Progress is derived from the filesystem —
> local bytes on disk versus known remote size — and each transfer is its own short-lived lftp
> process.

[SeedSync](https://github.com/nitrobass24/seedsync) is the prior art and is worth your time if
you want something that works today. lftpweb shares no code with it; §1.2 explains what was
learned from it and where this deliberately diverges.

Non-obvious decisions, with their rejected alternatives, are logged in
[`docs/decisions.md`](docs/decisions.md).

## Development

```bash
uv run pytest                                        # test suite
docker/test-seedbox/gen_key.sh                       # throwaway key, not committed
docker compose -f docker-compose.test.yml up -d      # fake seedbox to test against
```

The fake seedbox is two sshd containers seeded with an identical known-size tree — one with GNU
`findutils`, one with busybox — so both remote-scan paths get exercised for real.

## Licence

**[AGPL-3.0](LICENSE)** — if you run a modified lftpweb as a service for other people, you have
to publish your changes.

The container image also bundles unmodified third-party programs (lftp, OpenSSH, 7-Zip, su-exec,
tini), each under its own licence. lftpweb runs them as separate processes rather than linking
against them. See [`NOTICE`](NOTICE).
