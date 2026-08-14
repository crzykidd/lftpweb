# lftpweb

A containerized web interface for keeping a local directory in sync with a seedbox, using
**lftp** as the transfer engine over SSH/SFTP.

Browse the remote and local trees as one view, queue and supervise downloads with live
progress, auto-queue on patterns, and optionally verify, extract, and relocate finished items.

> ## ⚠ Pre-release — not ready to use
>
> **Version `0.0.1`. All 9 build phases are built and unit/integration tested**, plus two runs
> of correctness fixes that only real use surfaced (2026-08-12 and 2026-08-13). There is no
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
  multi-select with shift-range and bulk Queue/Stop/Delete-local that reports partial failure
  honestly ("7 of 10 queued, these 3 failed because …"), plus text/state/"missing only" filters,
  on the Files page. Deleting local files is guarded (path containment, no active job, mount
  sentinel) and confirmed before it runs — irreversible, unlike Queue/Stop. A delete marks the
  whole subtree, and picks each row's state from whether a remote copy actually survives
- Files rows carry four lifecycle icons (**R**emote / **L**ocal / **V**erified / **E**xtracted —
  presence facets may go dark, milestones stay lit), an inline progress bar inside the state
  chip, when the row last changed state, and an info icon that opens a detail drawer with both
  sides' size and modified date, the lifecycle chronology, and recent transfers and audit
  events. The tree sorts by name / size / last change / percent complete, and remembers your
  sort and your expand-or-collapse choice across reloads
- Optional scheduled retention: delete local copies older than N days, off by default, with a
  dry-run preview endpoint (no Settings-page toggle yet — see "What doesn't yet")
- Bandwidth ceiling and concurrency limits with an admission-control scheduler; per-queue scan
  intervals (10s / 30s / 60s / on-demand-only), on an engine loop that scans only the queues
  actually due and cannot stack a second scan of a queue over its own overrun
- Auto-queue on select/skip/file-exclude patterns, with a live "what would this match" preview
- **A settle gate, on by default**: a release still being uploaded to the seedbox is held until
  its remote fingerprint (file count, total bytes, newest mtime) holds still across two
  consecutive scans *and* at least 60 seconds of wall clock. Without it, a directory caught
  mid-upload can read as byte-complete off whichever files arrived first — and then be
  extracted, relocated, and (on a `move` queue) deleted from the remote with files still
  missing. It costs up to about a minute per transfer; switch it off at Settings → Transfer if
  your seedbox's landing path is atomic end to end
- Post-processing: verify (sidecar or hash-on-disk, which now also checks total bytes so it
  cannot bless a truncated file), extract (`7zz` for zip/7z/tar/gz/bz2/xz, `unrar` for rar/rar5
  — see `NOTICE`), and `move` mode's verification-gated remote delete, all with an audited trail
  on the History page. Extraction stages into `_UNPACK_` and merges into place only on full
  success, is gated on cheap filesystem preconditions first (zero-length head volume, a gap in a
  multi-volume rar set), and can optionally delete a release's spent archive volumes once they
  have extracted — off by default
- The History page: every completed/failed/cancelled transfer and every audit event
  (including remote deletes and deletes withheld), filterable and grouped by queue
- Rotating log viewer, on-demand `VACUUM INTO` database backups (scheduled + manual), and a
  header readout for seedbox reachability and scheduler liveness (`/api/health`)
- Credentials encrypted at rest
- **Authentication is optional and defaults off** (`AUTH_MODE=none`) — turn on a single-user
  password login or trust a reverse proxy's identity header, both from Settings → Auth. See
  "Locked out?" below before you flip it on.
- **In-app user documentation**, under **Docs** in the left nav: a quick start walking the real
  first-run sequence, and a Concepts page covering the six things that actually confuse people
  (the settle gate, auto-queue suppression, the difference between Dismiss / Clear history /
  Reset item tracking, the lifecycle icons, `copy` vs `move`, and inherit-vs-override on the
  post-processing toggles). Every step links straight to the settings page it describes.
  Per-field help popups (`FieldHelp`) are being applied across the settings surface, starting
  with the fields whose wrong answer costs you data

## What doesn't yet

All 9 build phases shipped (see `prompts/startnewsession.md` for exactly what each one built
and how it was verified), but real gaps remain:

| | Notes |
|---|---|
| Files page has no "Delete remote" | §9.2 lists it alongside "Delete local" (which now exists, per-item and bulk, with a confirmation dialog — 2026-08-12). Deliberately deferred, not forgotten: the only remote deletion is still `move` mode's verification-gated pipeline; a manual remote-delete button is a materially larger safety conversation. |
| Propagating local deletes to the seedbox (`sync` mode) | Designed (`DESIGN.md` §7) but **not scheduled** — built only if it proves wanted. |
| Local retention (delete-local-files-older-than-N-days) has no Settings-page UI | Backend, API (including a dry-run preview), and the background scheduler all exist (2026-08-12) and default off; only the settings screen to turn it on hasn't shipped yet — same "backend first" gap as the settle gate and Settings → Transfer before it. |

See "Known gaps" below for the rest — behavioral limitations and deliberate trade-offs, as
opposed to the unbuilt UI above.

## Known gaps

Named here rather than fixed silently or left to be rediscovered — each is a deliberate scope
reduction made during the build, recorded in full in `docs/decisions.md`:

- **No UI screen in this project has ever been opened in a browser.** No browser exists in any
  environment this project has been built in. Every page has been confirmed to build,
  type-check, and lint cleanly, and every backend endpoint it calls has been verified directly
  over real HTTP — but actual rendering, layout, and click-through behavior have never been
  visually confirmed. Click-test before relying on any of it. This applies hardest to the
  newest work: **almost none of the 2026-08-13 Files-page revamp** — the lifecycle icons, the
  inline progress bars, sorting, the persisted collapse preference, the detail drawer, the
  hover tooltip — **has been seen by a human, and none of it by any agent.**
- **The frontend test runner (Vitest + happy-dom) covers the pure logic, not the components.**
  `frontend/src/**/*.test.ts` pins `lib/format.ts`, `lib/storage.ts`, `lib/resetWarning.ts`, and
  `components/FileTree.tsx`'s tree-sorting (the sibling-preserving invariant explicitly),
  default-plus-exceptions collapse preference, facet filter, and column-width clamping — run in
  CI's "Frontend lint + typecheck" job via `npm test`. What it does **not** cover: any component
  actually renders — `FileTree`, `ItemDrawer`, `LifecycleIcons`, `StateChip`, and everything else
  with JSX in it are exercised only by `tsc -b`/`vite build`/`oxlint`, not by a test that mounts
  them. That gap is what "no UI has ever been click-tested" (above) still means in full. The
  newest instance: `components/FieldHelp.tsx` (2026-08-13) — its *placement* arithmetic is unit-
  tested via `lib/popoverPosition.ts`, but its open/close behaviour (click toggles, Escape and
  outside-click dismiss, hover assists on mouse only) is verified by reading, not by a test.
- **Post-processing (verify/extract/move) triggers from two narrow places — a job this app
  spawned succeeding, and the settle gate releasing its own hold on an item.** It does *not*
  trigger when a routine rescan finds a file that arrived some other way (a manual `cp`, a
  restore) with no gate hold behind it. Such a file will sit under a queue's `local_path`
  unverified, unextracted, and — for a `move` queue — with its remote copy never deleted, until
  something re-touches that item (e.g. a manual re-queue). Phase 5's own deliberate call; the
  settle-gate half of it was closed on 2026-08-12, the placed-by-hand half was not.
- **Encrypted-rar password retry is implemented but has never been tested against a real
  encrypted archive.** No RAR compressor exists anywhere in this project's toolchain — `unrar`
  only extracts, and no Alpine package ships one — so there is no way to build the fixture. The
  equivalent 7zz path *is* tested against a real encrypted zip; the rar path follows the same
  shape and is assumed correct on that basis, which is weaker than every other claim here.
- **Real-archive rar coverage is old-style multi-volume only.** The two committed fixtures are
  hand-built RAR4 archives (a single file, and a genuine two-volume `.rar` + `.r00` split).
  New-style `.partNN.rar` multi-volume extraction has no real-archive test — only the
  fake-bytes precondition tests that exercise its naming and gap detection.
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
- **`net:connection-limit` (DESIGN.md §4.5/§9.3, "a first-class setting, host-level") has no
  way to be set from the UI at all.** It lives only in a JSON `connection_overrides` blob on
  the `host` row (`core/remote.py.parse_connection_limit`); Settings → Connection has no field
  for it, and there is no `PUT` surface anywhere that writes to it. Settings → Transfer's live
  connection-count readout (§9.3) reads whatever happens to already be in that blob and
  surfaces it read-only (`HostOut.net_connection_limit`) — on a fresh install, and on every
  install today, that's `null`, so the "⚠ over net:connection-limit" warning cannot fire until
  something writes to the blob some other way (direct SQL, for now).

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

**Once it's up, the rest of the setup guide is in the app**, under **Docs → Quick start** — it
walks connecting to the seedbox, creating a queue, the first scan, and queueing a transfer, with
every step linking to the page it describes. **Docs → Concepts** explains the behaviours that
surprise people (why a transfer waited a minute before starting, why an item won't re-download,
and what the three different "clear this" actions each actually remove). That lives in the app
rather than being repeated here on purpose: duplicated prose drifts, and only the in-app copy can
link you to the setting it's talking about.

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

The container image also bundles third-party programs — unmodified Alpine packages (lftp,
OpenSSH, 7-Zip, su-exec, tini) plus `unrar`, built from RARLAB source in the image's builder
stage — each under its own licence. lftpweb runs them as separate processes rather than linking
against them. See [`NOTICE`](NOTICE).
