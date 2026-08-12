# Start-new-session brief — lftpweb

Point a fresh session at this file. It is a **standing onboarding brief, not a task** — it
never moves to `done/`. It restates what the project is, where the build has got to, and the
rules to honor, so a new session is productive even with no conversation memory.

**Keep the "Where we are" section current.** Update it at the end of any phase or whenever a
significant decision lands, in the same commit as the work.

> **Note (2026-08-11):** phase 3b's session found this repo's working tree already dirty with
> an *unrelated* concurrent session's repo-bootstrap edits (GitHub repo creation, `LICENSE`,
> CI, `code-checkin-and-pr` adoption) mid-build, including to this very file. Phase 3b's own
> edits below are additive on top of that content rather than a rewrite — see
> `docs/decisions.md`'s "this session ran concurrently with another session" entry before
> assuming either session's version of this file is the complete picture.

---

## What this project is

**lftpweb** is a containerized web interface that keeps a local directory in sync with a
seedbox, using **lftp** as the transfer engine over SSH/SFTP. It browses the remote and local
trees as one view, queues and supervises downloads with live progress, auto-queues on patterns,
and optionally verifies / extracts / relocates finished items.

- **Stack:** Python 3.13 / FastAPI / SQLite / asyncssh backend; React + TypeScript + Vite +
  Tailwind frontend; one Alpine container; lftp for transfers.
- **First version:** `0.0.1`. Version lives in `backend/lftpweb/__init__.py`, bare (no `v`).
- **Licence: AGPL-3.0** (`LICENSE`). Bundled third-party programs in the image — lftp, OpenSSH,
  7-Zip, su-exec, tini — are aggregated, not linked, and are recorded in `NOTICE`.
- **Repo: https://github.com/crzykidd/lftpweb** — public, created 2026-08-11.

### The one idea everything hangs off

> **lftp is a transfer engine, not a status API.** Progress is derived from the filesystem
> (local bytes vs. known remote size); each transfer is its own short-lived lftp process.

Do **not** reintroduce `jobs -v` parsing as a source of truth. `DESIGN.md` §1.2 explains why at
length — it is the single most important thing to read before touching the transfer engine.

---

## Read first, in this order

1. **`DESIGN.md`** — the architectural source of truth, 15 numbered sections. Cite sections as
   `§4.5` when discussing it. **Required reading before writing any code.**
2. **`CLAUDE.md`** — per-session operating rules (the handoff-prompt workflow, in full).
3. **`docs/decisions.md`** — the "why" log, newest first. Check it before re-deriving anything;
   several decisions have non-obvious rejected alternatives.
4. **`standards.md`** — which homelab standards this repo implements, pinned.

---

## Repo, branches, and what has NOT been pushed

The GitHub repo exists and is **empty**. Nothing has been pushed yet — check before assuming.

```
* dev    all work lives here — every commit since repo init
  main   still at the repo-init commit, 6+ commits behind
```

**The bootstrap ordering matters, and doing it out of order is annoying to undo:**

1. Commit outstanding work on `dev`.
2. **Fast-forward `main` to `dev` while no branch protection exists.**
3. Push `main`, then `dev`.
4. Enable CodeQL default setup; let CI run once so GitHub learns the check names.
5. **Only then** apply branch protection with the required checks.

Do step 5 first and the first action on the new repo is opening a PR to catch `main` up on the
project's own history — which also can't merge until CI has run anyway. `docs/repo-setup.md`
carries the full runbook.

After protection is on, `code-checkin-and-pr`'s rules bind for real: never push to `main`, work
on `dev`, land via PR.

## Where we are

**Status: phases 1–5 done.** `DESIGN.md` is settled and reviewed. The skeleton (phase 1),
scanning + reconciliation + read-only Files view (phase 2), the transfer engine + scheduler
(phase 3a), the Transfers page / item drawer / Files actions / WebSocket delta fix (phase 3b),
auto-queue + patterns + the mount sentinel (phase 4), and post-processing + `move` mode
(phase 5) all exist and are verified — see `prompts/done/2026-08-11-phase1-skeleton-and-
container.md`, `prompts/done/2026-08-11-phase2-scanning-and-model.md`,
`prompts/done/2026-08-11-phase3a-transfer-engine.md`,
`prompts/done/2026-08-11-phase3b-transfers-ui.md`,
`prompts/done/2026-08-11-phase4-autoqueue-and-patterns.md`, and
`prompts/done/2026-08-11-phase5-postprocessing-and-move.md` for the exact commands run.

> **⚠ Phase 5 makes the user's live queue's `sync_mode = 'move'` row live.** It has been
> stored that way in the database since before phase 4's guard existed, inert until now
> because nothing implemented `move` or read `sync_mode` to act on it. As of this phase,
> `move` **deletes the verified remote copy after every download that queue completes.** The
> row was deliberately **not** touched or reset — see `docs/decisions.md`'s phase 5 entry,
> point 0, and the phase 5 report. **Tell the user this first, before anything else, when
> they're back.**

| Phase (`DESIGN.md` §13) | State |
|---|---|
| 1 — Skeleton + container | **done** (2026-08-11) |
| 2 — Scanning + model | **done** (2026-08-11) |
| 3a — Transfer engine + scheduler (backend) | **done** (2026-08-11) |
| 3b — Transfers UI, item drawer, WebSocket delta fix | **done** (2026-08-11) |
| 4 — Auto-queue + patterns | **done** (2026-08-11) |
| 5 — Post-processing + `move` | **done, not yet committed** (2026-08-12) — see below |
| 6 — History page | overnight run 2026-08-11 |
| 7 — Operations (logs, backup) | overnight run 2026-08-11 |
| 8 — Auth + hardening | overnight run 2026-08-11 |
| 9 — Polish | overnight run 2026-08-11 |
| `sync` mode | **not scheduled** — designed in §7, built only if it proves wanted |

**Current instruction (2026-08-11, overnight run):** phases 1–3 are done and **proven against
the user's real seedbox** — a 1.29 GB mkv transferred byte-exact, nested directories, resume
from partial, live progress. The user has authorised running **phases 4–9 in order,
unattended**: for each phase write the handoff prompt, execute it via a spawned agent, verify,
commit, push to `dev`, then start the next. Document every decision made without them.

**SAFETY RULE for the unattended run — every new capability ships defaulting to OFF.** The
user's live instance may pull `:dev`. Nothing landing overnight may change how their running
deployment behaves: auto-queue defaults disabled, remote deletion defaults off, auth defaults
to the current `none`. A capability that turns itself on while they sleep is a bug, not a
feature. **Phase 5 is the one deliberate, flagged exception to "nothing changes behavior":**
`move` mode itself was already stored as their live setting before any guard existed, so
implementing it changes what their existing configuration does even though every *new* toggle
this phase adds (global and per-queue post-processing switches, `auto_move`) still defaults
off exactly like every other phase.

**Their live config:** one queue, `sync_mode` stored as `move` in the database from before the
guard existed. As of phase 5 this is **no longer inert** — see the warning banner above. Not
silently rewritten; it is the user's call and they need to see it first.

## What real hardware taught us that the fake seedbox could not

Ten fixes came out of the first real deployment. They are the reason to keep testing against
real infrastructure rather than only the fixture:

- **OpenSSH fatals with "No user exists for uid N"** when the running uid has no `/etc/passwd`
  entry — which is exactly §11.2's identity model. lftp shells out to ssh, so *every* transfer
  died while scanning worked (asyncssh has an env fallback; OpenSSH has none). Fixed by
  symlinking `/etc/passwd` into the `/run` tmpfs and writing it in the entrypoint.
- **lftp retries forever by default.** `net:max-retries`/`net:timeout` were in §9.3's knob list
  but never written to the rc, so a failing connection hung as "DOWNLOADING, 0 bytes" instead
  of failing. Always set now.
- **`net:reconnect-interval-base` takes a bare number, not `5s`** — lftp rejected the line,
  carried on, and produced a misleading `HOST_UNREACHABLE`. `tests/test_lftp_settings_accepted.py`
  now feeds every generated setting to a real lftp binary; asserting the rc *contains* a string
  only proves we wrote what we meant.
- **The WebSocket omitted `item.id`**, so every Files row rendered with no action button — a
  remote file could be seen but never queued. The page renders purely from the WS stream.
- **`VOLUME` created a phantom root-owned `/downloads`**; the per-job `/run` dir was never
  created before privileges dropped; `pget -n 4` fanned a 16-byte file across four connections.
- **Jobs left `running` by a restart** became phantom transfers forever, and their items stayed
  stuck `DOWNLOADING` because scans deliberately don't overwrite lifecycle states.
- **A `sync_mode` the UI offered but nothing implemented** silently behaved as `copy`.

The pattern: none were reachable from unit tests or the fake seedbox. Job lifecycle logging and
`output_tail` are what turned each one from a guess into a diagnosis — keep them.

App ports are **8087** (API/SPA) and **5187** (Vite dev server) — not the more obvious
8080/5173 — chosen to avoid collisions with other stacks on the shared build host. See
`docs/decisions.md`.

Two design gaps phase 1 found in `DESIGN.md` and worked around (see `docs/decisions.md` for
the full reasoning): §11.1's `cap_drop: ALL` doesn't actually boot the §11.2 PUID/PGID
entrypoint without `CHOWN`/`SETUID`/`SETGID` added back, and `/api/health` had to grow a
`repo_url` field beyond §12's literal 4-field shape so the nav's version link can get a
runtime (not build-time) value.

Phase 2 found four more, all worked around and recorded in `docs/decisions.md` rather than
folded back into `DESIGN.md` (a deliberate corrected-in-conversation call per the workflow):
`asyncssh.connect()` crashes outright under §11.2's own numeric-uid convention on Python 3.13
(`getpass.getuser()` raises `OSError`, worked around in `core/remote.py`); `known_hosts=None`
silently disables asyncssh's host-key callback entirely, so the working accept-and-pin
implementation passes an empty `SSHKnownHosts()` instead; §3.2 rule 1 doesn't say what a
directory with *zero* local presence should read as, resolved as `REMOTE_ONLY` rather than
`PARTIAL`; and §4.7's narrow "item" (top-level entries only) and the `item` table's evident
full-tree scope disagree, resolved toward persisting one row per node.

**Every credential encryption gap is closed as of phase 2**, moved up from build phase 8
because phase 2 is where a seedbox password first exists (`core/crypto.py`; see
`docs/decisions.md`). Phase 8 still owns the rest of §8: auth modes, sessions, API keys, rate
limiting. Phase 3 landed the "hold transfers for a host with no usable credentials" half of that
by construction — `TransferQueue._admit` just doesn't spawn anything when
`core/engine.load_host_config` reports no host.

**Phase 3, in one paragraph:** `core/lftp.py` builds and spawns one lftp process per job
(pipes, never a PTY; credentials + tuning in a per-job `/run` tmpfs rc file, never argv) and
classifies non-zero exits. `core/scheduler.py` is a pure `(settings, running, queue) -> admit
list` function pinned by a table test covering every §4.5 worked example, the floor loop, the
fast lane, and start-now. `core/queue.py` ties it together — spawn/watch/reap, retry with
backoff on transient classes only, SIGTERM-then-grace-then-SIGKILL stop semantics, and
`auto_queue_suppressed` on every STOPPED/FAILED item even though phase 4's auto-queue doesn't
exist yet to read it. `core/progress.py` samples the active set at ~1 Hz via
`core/local_scan.py`'s sidecar math (reused, not reimplemented) and EMA-smooths speed/ETA.
`api/jobs.py` exposes all of it, plus the site-level transfer settings. The **live-retune
experiment is confirmed working** (holding lftp's stdin open + `set net:limit-total-rate` while
a job runs) but is **not** wired into production — admission control stands alone, as required.
Six non-obvious things were found running real lftp against the real fake seedbox — see
`docs/decisions.md`'s phase 3 entries, especially: `mirror`'s target must be the item's *parent*
directory (not the item's own directory, unlike `pget`); a bare `open sftp://user@host` makes
lftp prompt for a password itself even under key auth; `pget:save-status` defaults to a
sampler-breaking 10s; and `GET /api/files` was serving a state that a stop/queue action could
never actually reach until it was pointed at the database instead of the scan-only in-memory
model.

**Phase 3b, in one paragraph:** the WebSocket delta fix landed first, because it constrains
everything else — `core/engine.py.diff_nodes` turns `scan_queue`'s full-tree publish into a
`changed`/`removed` delta, and `core/queue.py._publish_item_state` pushes single-item deltas on
every lifecycle transition plus a per-tick batch for the active set, so the Files page updates
live without the WebSocket ever resending a whole queue's tree (proven by test across a 20-item
and a 5,000-item tree, and measured live: ~152–189 bytes/message vs. a 2,754-byte full snapshot
for the fake seedbox's 18-node tree). The Transfers page (`TransfersPage.tsx`) shows the
three-word visible vocabulary from DESIGN.md §9.2 with `STOPPED`/`FAILED` surfacing where they
apply, both allocated and current rate, and a one-time inline explanation for "Start now."
`ItemDrawer.tsx` is the side drawer (not a modal), `FileTree.tsx` gained virtualization
(`@tanstack/react-virtual`), multi-select with shift-range, and per-row/bulk Queue/Stop actions.
Two backend gaps found wiring the UI to the phase 3a API: `list_jobs()` excluded every
failed/cancelled job, which made DESIGN.md §9.2's own "failed rows show the error class" and the
phase 3b prompt's "stop it and see it go STOPPED" both impossible — fixed by including an item's
*most recent* terminal job; and the Files page had no way to stop an item at all (only
job-scoped `POST /api/jobs/{id}/stop` existed) — fixed by adding `POST /api/items/{id}/stop`.
Also fixed, out-of-scope-turned-in-scope per the phase 3b prompt: the phase-2 scan-abort bug
(one permission-denied subdirectory used to discard an entire queue's tree) — now a partial
scan plus a surfaced warning. Full detail, including two deliberately-flagged design deviations
(TanStack Query never adopted; a new `@tanstack/react-virtual` dependency), in
`docs/decisions.md`'s phase 3b entries.

**Phase 4, in one paragraph:** `core/patterns.py` is the one evaluator DESIGN.md §12 requires —
`select`/`skip` (item-name, case-insensitive, glob-when-metacharacters-else-substring, skip
beats select, empty-select matches everything unless *patterns-only*) and `file_exclude`
(file basename, any depth, also applied to loose top-level file items). It feeds two
consumers: `core/reconcile.py`'s `counts_predicate` seam (phase 2 left it; a matched file is
now marked `EXCLUDED` — a real state, not an absence — and doesn't count toward its parent
directory's completeness, so a `*.nfo` file_exclude leaves a release `DOWNLOADED` instead of
permanently `PARTIAL`), and `core/queue.py._spawn_decision`'s `exclude_globs` for lftp's own
`--exclude-glob`. `core/autoqueue.py` evaluates every eligible (`REMOTE_ONLY`/`PARTIAL`,
unsuppressed) top-level item against the compiled patterns at the end of every scan pass —
retroactive by construction, since it re-queries the whole known model rather than tracking
"newly seen" itself — and skips anything `auto_queue_suppressed` or in `STOPPED`/`FAILED`/
`REMOVED_LOCAL`/`REMOVED_BOTH`. **The mount sentinel and grace period landed here, not with
`sync`**, per this file's own 2026-08-11 entry: `core/mount_sentinel.py` writes/checks
`.lftpweb-mount-ok` at a queue's local root, `AutoQueue.on_scan()` refuses to act on
*anything* for a queue whose gate fails, and `resolve_absence()` — a pure function wired into
`core/engine.py._persist` — implements DESIGN.md §3.2 rule 3's `REMOVED_LOCAL` transition
with the ~10 minute grace period, which this phase had to build from scratch since phases 2-3
explicitly left it undone. Auto-queue and *patterns-only* both default off per queue
(migration 002 adds the one new column, `DEFAULT 0`, changing nothing for any existing row).
API: pattern CRUD, a live "what would this match" preview endpoint, and a queue-level
mount-gate status read. UI: Settings → Queues gained the two toggles and a patterns editor
with that live preview. Verified against the real fake seedbox
(`tests/test_autoqueue_e2e.py`): a `file_exclude` of `*.nfo` drove `AutoQueue` to queue a real
release, the `.mkv`/`.srt` arrived byte-exact, the `.nfo` never did, and the item reached
`DOWNLOADED`. Every decision made unattended is in `docs/decisions.md`'s phase 4 entry,
including two rejected alternatives worth a second look: whether `file_exclude` should support
path-aware (not just basename) matching, and whether the grace period belongs in the Settings
UI now rather than later.

**Phase 5, in one paragraph — the first phase that deletes data on a machine we don't own.**
`core/verify.py` checks `.sfv`/`.md5` sidecars, falling back (opt-in, off by default) to a
whole-file read as a weaker "readable end to end" guarantee when no sidecar exists.
`core/extract.py` extracts every archive found under an item via `7zz` — the image's only
archive tool, no `unrar` — including multi-part rar (first volume only) and compound tar
formats (two passes: strip compression, then unpack). `core/postprocess.py` is the pipeline
`core/queue.py._reap_one` triggers for a top-level item's job success: verify → (for a `move`
queue) the delete gate → extract → move-to-final, each step off by default at **two**
independent layers (a site-wide `PostprocessSettings` flag AND the queue's own `auto_verify`/
`auto_extract`/`auto_move` column must both be on), except verification for `move`, which is
forced on regardless of either toggle because it's the sole gate on an irreversible delete.
`move_tree` is the cross-device-safe staging→final relocator: `os.rename` fast path,
copy-to-a-same-filesystem-sibling-then-atomic-rename on `EXDEV` (the expected case — the
user's downloads are on NFS), verified to leave no partial file at the destination when the
copy itself fails partway. Deletion (`RemoteConnectionPool.delete_path`, `core/remote.py`)
goes out as `rm -rf --` over the same pooled asyncssh connection scanning already uses, never
lftp's `--Remove-source-files`; every delete and every delete withheld writes an `event` row
(`core/audit.py`) naming the item, queue, mode, and gating condition. `api/settings.py` now
accepts `move` in `IMPLEMENTED_SYNC_MODES` (`sync` still rejected) and force-sets `auto_verify`
server-side whenever `sync_mode == 'move'`. UI: the Settings → Queues mode selector's `move`
option is enabled with an inline misconfiguration warning and a required confirmation
checkbox (DESIGN.md §7.1), per-queue verify/extract/move toggles, and a filled-in Settings →
Post-processing page for the site-wide defaults. Verified end to end against the real fake
seedbox (`tests/test_postprocess_e2e.py`): a `move` queue transferred a freshly-uploaded file,
verified it, deleted the remote copy, and a **second, independent** remote scan confirmed it
gone. Every decision made unattended is in `docs/decisions.md`'s phase 5 entry — read point 0
first, it's about the live queue row above.

**Commits so far:** repo init + standard adoption, the design revisions, phase 1 (`b0109ae`),
phase 2 (`de6d74b`), phase 3a (`36b9123`), phase 3b (`c814aa0`), phase 4 (`db89b63`). All on
`dev`. Phase 5's work is prepared on the working tree but **not yet committed** — see the
phase 5 prompt's final report for the proposed commit message.

---

## Operating rules

**Scope**
- Work only the phase or task the user names. Don't fan out into later phases or add
  "while I'm here" changes. Offer them as a one-liner, then wait.
- **Surface major design decisions discovered during a build** rather than silently resolving
  them. If the build reveals that `DESIGN.md` is wrong or underspecified, say so — the doc gets
  corrected, it isn't quietly diverged from.

**Handoff prompts** (`handoff-prompt-workflow` @ v2.0.0 — full rules in `CLAUDE.md`)
- Anything beyond ~1–2 files goes into a `prompts/` file executed by a **spawned subagent**.
  Opus for research/planning, **Sonnet for coding**.
- The prompt self-updates its frontmatter and `git mv`s to `prompts/done/` (or `failed/`).
- **One commit at the end**, prompt bundled in. Ask `y/n`. Never `git add -A`, never
  auto-commit, never push.

**Git**
- Day-to-day work is on `dev`.
- **Never a `Co-authored-by:` trailer** — explicitly reaffirmed by the user 2026-08-11, and true
  of every commit in the history so far. Conventional-Commit prefixes required:
  `feat:` / `fix:` / `chore:` / `docs:`.
- `code-checkin-and-pr` and `release-prep-and-cut` were adopted 2026-08-11 alongside repo
  creation; `standards.md` is the in-repo source of truth for what is actually wired. Until
  branch protection is applied (see the bootstrap ordering above), treat `main` as
  push-once-then-protected rather than already-protected.
- `repo-sandbox-permissions` is **deliberately not adopted** — dedicated dev host, same call
  the user made when de-adopting it from AmmoLedger. Don't "helpfully" add it.

**Docs**
- Non-obvious decisions go in `docs/decisions.md`, newest at top, with rejected alternatives.
- Doc updates ship in the same commit as the code they describe.
- Local scratch, fixtures, and generated files go under `private_data/` (gitignored).

---

## Traps worth knowing before you touch the code

These are the places where the obvious implementation is wrong. Each is written up in
`DESIGN.md`; this list exists so a fresh session knows to go read it.

- **Excluded files break completeness** (§4.7, §3.2 rule 8). A `file_exclude` of `*.nfo` means
  those files never arrive — so if the reconciler counts them as missing, every filtered
  release is permanently `PARTIAL` and re-queued forever. One evaluator (`core/patterns.py`),
  used by both the lftp command builder and the reconciler.
- **Stop must suppress auto-queue** (§4.6). A stopped item still matches its pattern; without
  `auto_queue_suppressed`, auto-queue restarts it 30 s later, forever.
- **Sparse files lie** (§4.4). `pget` writes sparse files, so `st_size` is wrong — read the
  `.lftp-pget-status` sidecar, and account for the `.lftp` temp suffix.
- **Allocations are never re-shaped** (§4.5). Bandwidth is assigned at admission and fixed for
  the job's lifetime. That's what makes the missing lftp control channel a non-issue.
- **NFS + `root_squash`** (§11.2). Chown `/config` only; a chown failure on a data volume is a
  warning, not a fatal.
- **`cap_drop: ALL` breaks the PUID/PGID entrypoint unless you add capabilities back**
  (found in phase 1, see `docs/decisions.md`). `chown`/`setuid`/`setgid` are capability-gated
  even for uid 0; `docker-compose.yml` adds back `CHOWN`, `SETUID`, `SETGID` on top of
  `cap_drop: ALL`. Also: `read_only: true` means the entrypoint can never write
  `/etc/passwd`/`/etc/group` (no `addgroup`/`adduser` — use numeric `uid:gid` everywhere).
- **A venv's shebangs bake in an absolute path.** Build and copy it forward at the *same*
  path in every Docker stage, or `COPY --from=` carries a venv whose scripts point at a
  directory that no longer exists (phase 1 hit this: `docker/Dockerfile` uses `WORKDIR /app`
  everywhere for exactly this reason).
- **`asyncssh.connect()` crashes under lftpweb's own numeric-uid convention** (found in phase
  2, see `docs/decisions.md`). It unconditionally calls `getpass.getuser()` for SSH-config `%u`
  templating; on Python 3.13, an unregistered uid (exactly §11.2's PUID/PGID and native `user:`
  identity model) makes that raise `OSError`, which asyncssh's own `except KeyError:` doesn't
  catch — every connection fails, for every auth method. `core/remote.py` sets a fallback
  `LOGNAME` at import time (only if nothing already identifies the user) as the fix.
- **`asyncssh.connect(known_hosts=None)` doesn't just skip verification — it skips your own
  callback too** (found in phase 2). `validate_host_public_key` is only invoked when
  `known_hosts` is a real (even empty) `SSHKnownHosts` object; passing `None` sets an internal
  flag that trusts any server key *and* never asks the client factory anything. Pass
  `asyncssh.SSHKnownHosts()` (empty, non-`None`) to actually enforce your own policy.
- **`find`'s `\n`-terminated wire format and "paths can contain newlines" are in tension**
  (§5 vs §15.10, phase 2). `core/remote.py`'s parser anchors on the record header rather than
  splitting lines, which handles it in practice, but a path containing the *exact* bytes of a
  header immediately after a literal newline would still misparse — a property of the
  specified `find -printf` command, not fixed by deviating from it. See the phase 2 report.
- **`mirror`'s local target is the item's *parent* directory, not the item's own directory**
  (found in phase 3). `mirror -c 'REMOTE/item' 'LOCAL/'` creates `LOCAL/item/...` itself —
  passing `LOCAL/item/`, the "obviously" symmetric choice with `pget`'s exact-file-path target,
  produces a doubly-nested `LOCAL/item/item/...` tree. `core/lftp.py.build_transfer_command`'s
  docstring has the full explanation; `core/queue.py` computes the two differently on purpose.
- **A bare `open sftp://user@host` makes lftp prompt for a password itself, even under key
  auth** (found in phase 3) — `GetPass() failed -- assume anonymous login` /
  `Login failed: Password required`, despite the connect-program's ssh having already
  authenticated successfully via the key. Always use `open -u user,password`, with an *empty*
  password field for `key`/`agent` auth.
- **`pget:save-status` defaults to 10s** (found in phase 3) — far too coarse for a ~1 Hz
  progress sampler; a transfer inspected at the 1s/2s/3s marks under the default has no
  `.lftp-pget-status` sidecar yet at all. Every job's rc file sets `pget:save-status 1s`.
- **`GET /api/files` must read `item.state` from the database, not `core/engine.py`'s
  in-memory scan model** (found live in phase 3, through the running API). The in-memory model
  is `core/reconcile.py`'s pure structural output — it has no notion of QUEUED/DOWNLOADING/
  STOPPED/FAILED, so serving it from an API a stop/queue action is supposed to affect silently
  reverts the visible state on the very next read. `api/files.py` queries `item` directly.
- **A periodic rescan can silently overwrite a job-lifecycle state back to a structural one**
  (found in phase 3) — a `STOPPED` item with a still-partial file reads as `PARTIAL` again on
  the next scan unless something stops it. `core/engine.py._persist` leaves `state` alone for
  any item with a `queued`/`running` job or `auto_queue_suppressed` set; everything else still
  gets recomputed every pass.
- **`pget -o <path>` does not create its target's parent directory** (found in phase 3, unlike
  `mirror`, which creates its own subtree). `core/queue.py._spawn_decision` `mkdir -p`s it
  first — a no-op for a genuinely top-level item, load-bearing for anything nested.
- **A leading blank line in an lftp `-c`/`source`d script corrupts quote-stripping on the next
  `set key "value with spaces"` line** (found in phase 3, real lftp 4.9.2). Reproducible on
  demand; `core/lftp.py.build_rc_text` never emits one.
- **Never publish a full node list except on WebSocket connect** (found/fixed in phase 3b, see
  `docs/decisions.md`). Every update after the initial `snapshot` must be a `queue_delta`
  (`core/engine.py.diff_nodes`, scan-driven) or an `item_delta` (`core/queue.py`, lifecycle- or
  progress-tick-driven) — both proportional to what changed, never to tree size. A future change
  that starts putting a full `nodes` array on anything but the connect-time `snapshot` message
  is this same regression coming back.
- **GNU `find -printf` exits nonzero the instant it can't read *one* subdirectory, but keeps
  scanning everything else and still prints what it found** (named in phase 3, fixed in phase
  3b — see `docs/decisions.md`). Any nonzero exit with usable stdout is a *partial* success
  (`core/remote.py.interpret_primary_scan_result`), not a hard failure — only an exit with *no*
  stdout at all means the scan genuinely failed.
- **`core/queue.py.list_jobs()` is not "queued + running jobs" — it also includes an item's most
  recent `failed`/`cancelled` job** (found in phase 3b). DESIGN.md §9.2 requires the Transfers
  page to show failed rows' error class/output tail and a stopped row going `STOPPED`; the
  phase 3a query structurally couldn't produce either, since a job vanishes from that query the
  instant it stops being active. A manual retry's fresh `queued` row naturally supersedes the
  old terminal one — no separate cleanup needed.
- **The Files page needs `POST /api/items/{id}/stop`, not `POST /api/jobs/{id}/stop`** (added in
  phase 3b). Unlike the Transfers page, the Files page only ever has an item id, never the job
  id currently servicing it — `GET /api/files` deliberately doesn't expose one, since an item
  can outlive several job attempts. `TransferQueue.stop_item` resolves item → active job.
- **A `file_exclude` pattern must reach the reconciler, not just lftp's `--exclude-glob`**
  (phase 4). `core/patterns.py.build_counts_predicate` marks the matched file `EXCLUDED` and
  removes it from its parent directory's completeness accounting; skip this and every
  filtered release sits `PARTIAL` forever. `core/reconcile.py` and `core/queue.py` both
  consume the identical compiled pattern set for exactly this reason — see docs/decisions.md.
- **`CompiledPatterns.compile()` iterating its input three times silently breaks on a
  generator** (found building the pattern-preview endpoint, phase 4, before it ever shipped).
  Fixed by materializing the iterable first. A reminder that "one evaluator, two consumers"
  doesn't protect against a bug *inside* the evaluator itself.
- **The mount gate blocks all auto-queue action for a queue, not just the `REMOVED_LOCAL`
  transition** (phase 4). A blanket per-queue check (`AutoQueue.on_scan` returns immediately
  if `core/mount_sentinel.py.check()` fails) is what also protects a **brand-new** queue
  whose local root never mounted — every item would read `REMOTE_ONLY` from the very first
  scan, with no history to compare against, so only a blanket gate stops auto-queue from
  queueing transfers into a directory that isn't really there.
- **DESIGN.md §9's "TanStack Query for REST" was never actually adopted** (found in phase 3b,
  flagged rather than silently followed or silently fixed). Phases 1–3a built a hand-rolled
  `fetch` client + poll hook instead, with no record of the substitution. Phase 3b's `useJobs.ts`
  continues that convention on purpose rather than introducing the library mid-project — a
  future session should either correct DESIGN.md §9 or do the migration as its own scoped phase,
  not as a side effect of whichever phase next touches data-fetching.
- **`path_queue.local_path` is still where lftp downloads to and what the reconciler scans —
  `staging_path` is the phase 5 post-processing Move step's *destination*, not a download
  target** (phase 5, resolved ambiguity — see docs/decisions.md). DESIGN.md names the field
  `staging_path` and describes Move as "staging → final destination," which reads naturally as
  "downloads land in staging first," but making that true would mean the reconciler comparing
  remote vs. local at a *different* root during a transfer than after one completes — reaching
  back into phase 2/3's already-verified scan/reconcile code for a phase whose brief is
  post-processing. The chosen reading needs zero changes there; the frontend labels the field
  "Final destination" to match, without renaming the column.
- **A `move`-mode item's verification always runs, bypassing the "global setting AND per-queue
  toggle" rule every other post-processing step follows** (phase 5). It is the sole gate on an
  irreversible remote delete (DESIGN.md §7.3); muting it via an unrelated site-wide default
  (`PostprocessSettings.verify_enabled`) would silently turn `move` into "downloads, never
  deletes, never explains why." `core/postprocess.py.process_item`'s `verify_effective`
  computation is the one step that ORs in `sync_mode == "move"` rather than ANDing two toggles.
- **A `move`-mode delete sets `item.remote_deleted_at` but never changes `item.state`** (phase
  5). DESIGN.md's `REMOVED_BOTH` is the wrong state for this — its own definition implies local
  absence too, and `move` never removes the local copy. The item's state stays whatever verify/
  extract last set (`VERIFIED`/`EXTRACTED`); if the item is *also* relocated by the Move step,
  the resulting local absence is picked up by phase 4's existing `REMOVED_LOCAL` grace-period
  machinery on the next scan, exactly as if a human or an `*arr` importer had moved it — no new
  state, no new code path.
- **The user's live queue's `sync_mode = 'move'` row went from inert to live the moment phase
  5 shipped, and was deliberately left untouched** — see the warning near the top of this
  file's "Where we are" and docs/decisions.md's phase 5 entry, point 0. The first thing to tell
  the user when they're back.
