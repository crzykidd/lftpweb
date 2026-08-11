# Start-new-session brief — lftpweb

Point a fresh session at this file. It is a **standing onboarding brief, not a task** — it
never moves to `done/`. It restates what the project is, where the build has got to, and the
rules to honor, so a new session is productive even with no conversation memory.

**Keep the "Where we are" section current.** Update it at the end of any phase or whenever a
significant decision lands, in the same commit as the work.

---

## What this project is

**lftpweb** is a containerized web interface that keeps a local directory in sync with a
seedbox, using **lftp** as the transfer engine over SSH/SFTP. It browses the remote and local
trees as one view, queues and supervises downloads with live progress, auto-queues on patterns,
and optionally verifies / extracts / relocates finished items.

- **Stack:** Python 3.13 / FastAPI / SQLite / asyncssh backend; React + TypeScript + Vite +
  Tailwind frontend; one Alpine container; lftp for transfers.
- **First version:** `0.0.1`. Version lives in `backend/lftpweb/__init__.py`, bare (no `v`).
- **Not yet on GitHub.** Local git only, no remote. Do not attempt to push.

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

## Where we are

**Status: phases 1–2 done, phase 2 not yet committed.** `DESIGN.md` is settled and reviewed.
The skeleton (phase 1) and scanning + reconciliation + read-only Files view (phase 2) both
exist and are verified — see `prompts/done/2026-08-11-phase1-skeleton-and-container.md` and
`prompts/done/2026-08-11-phase2-scanning-and-model.md` for the exact commands run. No transfer
engine yet — lftp itself never runs until phase 3; nothing moves a byte before then.

| Phase (`DESIGN.md` §13) | State |
|---|---|
| 1 — Skeleton + container | **done** (2026-08-11) |
| 2 — Scanning + model | **done** (2026-08-11) |
| 3 — Transfer engine + scheduler | not started |
| 4–9 | not started |
| `sync` mode | **not scheduled** — designed in §7, built only if it proves wanted |

**Current instruction:** build phases 1–3, one at a time — write the handoff prompt, execute it
via a spawned agent, validate, surface any major design decisions found during the build, then
stop after phase 3. **Next up: write and execute the phase 3 handoff prompt** — the load-bearing
one (§13): process supervision, the admission-control scheduler, FS-derived progress,
queue/stop/retry, the Transfers view and item drawer.

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
limiting, and the full "hold all transfers for this host" behavior once phase 3 has transfers.

**Commits so far:** repo init + standard adoption, the design revisions, then phase 1
(`b0109ae`). All on `dev`. Phase 2's work is prepared on the working tree but **not yet
committed** — see the phase 2 prompt's final report for the proposed commit.

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
- Day-to-day work is on `dev`. `main` exists but is left alone.
- `code-checkin-and-pr` is **not adopted** (no remote yet), but its conventions are followed
  voluntarily: `feat:` / `fix:` / `chore:` / `docs:` prefixes, no `Co-authored-by:` trailers.

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
