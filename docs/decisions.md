# Decision record

Non-obvious decisions for lftpweb — approach changes, rejected alternatives, workarounds.
Newest at top. Per the `handoff-prompt-workflow` standard, sessions append here rather than
leaving the reasoning only in a commit message.

---

## 2026-08-11 — Phase 1 review: each migration must be atomic, or a failure wedges the install

**Found in review of the phase 1 build, not by the build itself.** The first migration runner
called `executescript(file)` and then, separately, inserted the `schema_version` row and
committed. `sqlite3.executescript()` commits any open transaction before it runs and then lets
the script's statements commit as they go — so it is not atomic.

Demonstrated rather than assumed. Given a migration `002` whose second statement fails:

- statement 1 stays **committed**, statement 2 fails,
- the `schema_version` row is never written,
- so the next start re-runs `002` from the top, hits `table beta already exists`, and the
  install is **permanently stuck** — no forward path without hand-written SQL repair.

That is the worst class of bug for this component: it corrupts the thing that is supposed to
make schema change safe, it only fires on the unhappy path, and §10.2's pre-migration backup
is build phase 7, so today there is no safety net behind it.

**Fix:** `migrate()` wraps each migration's text *and* its `schema_version` insert in a single
`BEGIN`/`COMMIT` inside the script it hands to `executescript()`, and rolls back on failure. It
has to be done by wrapping the script text — an outer `BEGIN` around the `executescript()` call
would be discarded by the implicit commit. Two rules now documented in `db.py`: migration files
must contain no transaction control of their own, and no pragmas that cannot run inside a
transaction (connection pragmas belong in `connect()`).

Covered by `tests/test_db.py::test_failed_migration_is_rolled_back_entirely`, which asserts the
partial migration leaves nothing behind *and* that a corrected migration then applies cleanly —
the property that actually matters.

---

## 2026-08-11 — Phase 1: app ports moved to 8087 (API/SPA) and 5187 (Vite dev), not 8080/5173

**Decision:** `LFTPWEB_PORT` defaults to `8087` (config, Dockerfile `ENV`/`EXPOSE`/`HEALTHCHECK`/
`CMD`, both compose files), and the Vite dev server defaults to `5187`. Plain literals in
`docker-compose.yml` and `docker-compose.dev.yml` — no `.env` interpolation.

**Why.** The build host already runs other stacks on 8080, 5173, 8090, and several other
common defaults. Chosen deliberately rather than discovered by collision on someone's
seedbox later. Anyone deploying this can still just edit the compose file port lines.

---

## 2026-08-11 — Phase 1: hand-rolled migrations, not Alembic

**Decision:** numbered SQL files in `backend/lftpweb/migrations/NNN_description.sql`,
applied in order by a small runner in `db.py`, tracked in a `schema_version` table.

**Rejected: Alembic.** The schema in DESIGN.md §3.1 is raw SQL with no ORM — there are no
SQLAlchemy models for Alembic to diff against, so it would only be driven manually via
`op.execute()`, which is friction without the autogeneration benefit that's Alembic's main
draw. §10.2's backup-before-migration hook is a few lines in `migrate()` either way, so
there's no capability Alembic buys that this repo needs.

---

## 2026-08-11 — Phase 1: `cap_drop: ALL` needs `CHOWN`/`SETUID`/`SETGID` added back

**Found during the container build, not anticipated by DESIGN.md.** §11.1 specifies
`cap_drop: ALL` and, separately in §11.2, a root-starting entrypoint that `chown`s `/config`
and drops privileges via `su-exec` (a `setuid`/`setgid` call). Tested literally: with
`cap_drop: ALL` and nothing added back, the container crash-loops before the app starts —
`chown(2)` and `setuid(2)`/`setgid(2)` are themselves capability-gated on modern kernels,
even for uid 0. Root without capabilities can't do either.

**Fix:** `docker-compose.yml` keeps `cap_drop: ALL` and adds back exactly `CHOWN`, `SETUID`,
`SETGID` — the standard "drop everything, re-grant the minimum" pattern. This only affects
the entrypoint's brief root phase; once `su-exec` drops to the unprivileged PUID/PGID, the
running app process has none of these capabilities. `DESIGN.md` §11.1's "the app needs no
capabilities at all" is true of the *running app* and should probably be read that way, but
the compose file as literally described doesn't boot — worth a look next design pass.

---

## 2026-08-11 — Phase 1: entrypoint never creates a passwd/group entry for PUID/PGID

**Found during the container build.** `docker-compose.yml`'s `read_only: true` (§11.1) makes
the whole root filesystem read-only except `/config`, `/downloads`, `/staging`, and a `/run`
tmpfs. An `addgroup`/`adduser` step — needed only to give PUID/PGID a friendly name for
logging — writes to `/etc/passwd` and `/etc/group`, both on the read-only root, and fails
outright under that profile.

**Fix:** the entrypoint (`docker/entrypoint.sh`) never calls `addgroup`/`adduser`. `su-exec`
and `chown` both accept raw numeric `uid:gid` without an NSS entry, so nothing actually
needed one; log lines just print the numeric ids instead of a resolved username. Also fixed
in the same pass: an early version of `check_writable()`'s non-fatal path returned a nonzero
exit status from its own `if` test, which `set -e` treated as a script failure and aborted
startup even though the check was designed to only warn — every non-fatal branch now ends
with an explicit `return 0`.

---

## 2026-08-11 — Phase 1: `/api/health` carries `repo_url`, beyond §12's literal 4-field shape

**Ambiguity found during the build.** DESIGN.md §12 defines `/api/health` as
`{status, version, db, uptime_s}`, but separately requires the nav's version link to use
`LFTPWEB_REPO_URL` (§9.1, §12) — a container env var, i.e. a *runtime* value, set after the
SPA has already been built into static files in the Docker image. A Vite build-time constant
can't carry a value that isn't known until the container starts, so the frontend has to fetch
it from the backend, and health is already the request the UI makes to render the version.

**Decision:** added a fifth field, `repo_url`, to `HealthResponse` rather than introducing a
new endpoint. Smallest change that satisfies both requirements; flagged here since it
deviates from the literal shape the design doc states.

---

## 2026-08-11 — Phase 1: `docker-compose.yml`'s `image:` is a placeholder

**Decision:** `image: ghcr.io/crzynet/lftpweb:0.0.1`, not a digest. DESIGN.md §11.2 describes
production as "pulled by digest from the registry," but this repo has no GitHub remote and no
CI (`code-checkin-and-pr` deferred — see below), so no image has ever been published for a
digest to pin. The placeholder documents the eventual shape; replace with a real
`ghcr.io/<owner>/lftpweb@sha256:...` once that standard's registry side is adopted.

---

## 2026-08-11 — Phase 1: venv kept at the identical absolute path across every Docker stage

**Found during the container build.** `uv sync` bakes an *absolute* path to the venv's own
python into every console-script shebang (e.g. `#!/build/.venv/bin/python`) and into
`pyvenv.cfg`. An earlier draft of the Dockerfile built the venv at `/build/.venv` in the
python-builder stage and `COPY --from=`'d it to `/opt/venv` in the runtime stage — every
script under `/opt/venv/bin` (including `uvicorn`) then had a shebang pointing at
`/build/.venv/bin/python`, which doesn't exist in the runtime stage, so every attempt to run
it failed with a bare `No such file or directory` and no other clue. Fixed by using `WORKDIR
/app` — and therefore `/app/.venv` — identically in `python-base`, `python-builder`, `dev`,
and `runtime`, so the `COPY --from=` carries a venv forward that's still valid at its own
recorded path.

---

## 2026-08-11 — Stop is terminal; auto-queue must never resurrect it

**Decision:** stopping a job is a user action with no automatic retry. The item lands in
`STOPPED` with its partial data kept, and carries `auto_queue_suppressed` so auto-queue skips
it. Same flag on `FAILED` after exhausted retries. Only a deliberate manual re-queue clears it.
See `DESIGN.md` §4.6.

**Why it needs saying at all.** The retry policy in §4.3 (transient classes retry with backoff
to `max_attempts`, permanent classes never retry) is meaningless without this. Auto-queue runs
on a scan cadence and matches on patterns; a stopped job still matches its pattern, so the next
pass would re-queue it ~30 s later, forever. That is an unbounded retry loop wearing a
different hat, and a UI that ignores an explicit user instruction. The suppression flag is what
makes "stop" mean stop.

**Also decided:** stop sends SIGTERM, not SIGKILL, so lftp flushes its `.lftp-pget-status`
sidecar and the partial stays resumable; SIGKILL only after a ~10 s grace period.

---

## 2026-08-11 — Three pattern kinds, one evaluator, used by both lftp and the reconciler

**Decision:** auto-queue patterns split into `select` / `skip` (matched against the item name,
enforced by us) and `file_exclude` (matched against paths inside an item, enforced by lftp via
`--exclude-glob`). Matching is case-insensitive, glob when the pattern contains `*?[` and plain
substring otherwise, with skip beating select. `DESIGN.md` §4.7.

**Rejected: SeedSync's substring-OR-glob on every pattern.** Friendlier, but ambiguous as soon
as a pattern contains a metacharacter — `*.nfo` would match both ways with different results.
Dispatching on whether metacharacters are present keeps the convenience (`1080p` works without
`*1080p*`) and drops the ambiguity.

**The bug this uncovered — the important part.** File excludes are passed to lftp, so those
files never arrive. But completeness (§3.2 rule 1) compares every remote child against local,
so an excluded `.nfo` reads as missing and the directory is **permanently `PARTIAL`** — never
`DOWNLOADED`, never verified, never extracted, never deleted under `move`, and re-queued on
every pass. A single exclude pattern would have quietly broken the pipeline for every item it
touched.

**Fix:** one compiled pattern set, used in two places — building the lftp command line *and*
deciding what the reconciler expects an item to contain. Excluded children are marked
`EXCLUDED`, a real state rather than an absence, and don't count toward completeness. The
consequence, accepted: changing `file_exclude` patterns retroactively changes completeness in
both directions, so the pattern preview has to show it rather than let it be discovered.

**Follow-on: an item is a top-level entry, directory *or* loose file.** A root-level
`Movie.mkv` is an item in its own right, matched by a `*.mkv` select and transferred with
`pget`; a directory is matched on its own name, so `*.mkv` does not match `Movie.2024/`
containing an mkv. Item patterns see item names, never contents.

That raised two edge cases, both resolved toward "an intended absence is not a missing one":

- **`file_exclude` also applies to loose top-level files.** Otherwise `*.nfo` would suppress
  nfos inside releases while happily downloading a stray `notes.nfo` at the root. When the item
  is a file, both `skip` and `file_exclude` are tested against its name — making the user enter
  the same pattern twice would be a trap, not a feature.
- **A directory whose children are all excluded is vacuously `DOWNLOADED`, and its local
  directory may not exist at all**, because lftp does not create a directory it has nothing to
  put in. Completeness must not require it. Same bug class as the exclusion bug above, one
  level up.

---

## 2026-08-11 — Alpine base, and `7zz` as the single extraction tool

**Decision:** `python:3.13-alpine` runtime (`node:22-alpine` builder), with the `7zip` package
(7-Zip proper, `7zz`) as the only archive tool. See `DESIGN.md` §11 and §11.1.

This deliberately departs from the sibling projects (`filament-bridge`, `labelforge`,
`partfolder3d`), which all run `python:*-slim` on Debian. Consistency lost to "smallest secure
image that does this job" on request.

**Why Alpine.** ~3× smaller with a much smaller installed package set, which is most of the CVE
surface. The historical objections are largely spent: musl gained DNS TCP fallback in 1.2.4
(Alpine 3.19+), and every dependency we need — `cryptography`, `pydantic-core`, `argon2-cffi` —
publishes `musllinux` wheels, so no Rust toolchain lands in the runtime image.

**Rejected: Debian slim.** Larger, and its archive story is worse — `unrar` is non-free and
`unrar-free` historically cannot read RAR5, which is what scene releases actually ship.

**Rejected: distroless / Chainguard.** Lower CVE counts, but we need `lftp`, `ssh`, and `7zz`
plus a shell for the PUID/PGID entrypoint. Fighting those images to install arbitrary packages
buys little over Alpine.

**`7zz` instead of `unrar` + `p7zip`.** 7-Zip 21.07+ extracts RAR and RAR5 natively, so one
binary covers rar / rar5 / zip / 7z / tar / gz / bz2 / xz — no non-free repo to enable, no
second tool to keep current. Its RAR decoder derives from the unRAR source, whose licence
forbids building a RAR-compatible *compressor*; we only extract, so this is a footnote rather
than a constraint.

**The base image is the smaller half of "secure."** The rest is runtime posture and lives in
compose: non-root, `cap_drop: ALL`, `no-new-privileges`, read-only rootfs, digest-pinned base,
and credentials confined to a `/run` tmpfs at mode 0600 (§11.1).

---

## 2026-08-11 — Admission-control scheduler; allocations are never re-shaped

**Decision:** bandwidth is handed out at admission and fixed for a job's lifetime. Site-level
`max_bandwidth` and `max_concurrent_transfers`, a fast lane for small items, and a sortable
rank for priority. Full algorithm and worked examples in `DESIGN.md` §4.5.

**The insight.** `lftp -c` exits with its transfer and offers no control channel, so a running
job cannot be retuned. Earlier drafts treated that as a defect to work around — first by
dividing by max concurrency, then by dividing by active jobs at spawn. Both were workarounds
for a constraint that a different scheduler simply never encounters. Allocating at admission
and never re-shaping turns the limitation into the design.

**Rejected: re-shaping running jobs.** Requires the control channel we don't have. The
stdin-held-open experiment (§4.5) might supply one, but it is unverified and nothing may depend
on it.

**Rejected: dividing by `max_concurrent`.** Wastes the most throughput in the commonest case —
one large download at a time.

**Rejected: an unmetered fast lane.** Queue 300 small files and it saturates the uplink at its
concurrency cap, starving the rate-limited main lane and blowing past the ceiling precisely
when the ceiling matters. The reserve is carved off `B` instead, so the total stays bounded.

**Accepted cost.** A job admitted at B/2 keeps B/2 after its partner finishes, leaving half the
pipe idle with nothing to claim it (§15.4).

**Fast lane rationale.** Not about small files being special — about head-of-line blocking. A
3 MB `.nfo` arriving while a 40 GB release holds the whole ceiling would otherwise wait an hour
to move a file it could have finished alongside in under a second.

**Site-level, not per-queue.** Parallelism and bandwidth multiply into a single host-wide
connection ceiling; letting each queue raise them independently makes that ceiling
unenforceable. A queue governs *what* and *where*, never *how fast*.

---

## 2026-08-11 — `sync` mode deferred indefinitely; hardlink pickup dir is what makes deletion safe

**Decision:** lftpweb ships `copy` and `move`. `sync` — propagating local deletes back to the
remote — is designed in full now (`DESIGN.md` §7) but **not scheduled**. It is a possible later
feature, built only if it proves wanted. No build phase depends on it.

An earlier draft of this entry called it "phase 2", which read as a commitment. It isn't one.
The design is kept because the seam (`event` audit, §7.4 deletion path, the state model) is v1
work for `move` regardless, and because the safety reasoning below is what a future session
would need in order to decide whether to build it at all — reconstructing that from scratch is
exactly how an irreversible feature ships with the wrong rails.

**Why remote deletion is safe here at all.** The torrent client hardlinks completed files into a
separate pickup directory, and lftpweb points at the pickup dir, never at the torrent data
directory. Unlinking there drops one link; the seeding torrent keeps its own, so the data, the
seed, and the ratio survive. This is a property of *the directory you point at*, not of
lftpweb — hence the misconfiguration warning in §7.1 and inline at the mode selector.

**Rejected: torrent-client API gating.** The usual correct answer (ask qBittorrent/rTorrent
whether the seed goal is met before deleting). Unnecessary here — the hardlink already encodes
the answer — and it would pull a whole integration in for nothing.

**Rejected: minimum-file-age gating.** A poor proxy: it proves neither that seeding finished nor
that the download completed. Here it would gate an operation that is already safe, adding
friction and buying nothing.

**Rejected: a count-based circuit breaker.** This is the subtle one. Sonarr/Radarr import by
*moving* files out, so a local file disappearing is the normal end state of every successful
import — deletes are **routine, not anomalous**. A "more than N deletes is suspicious" breaker
false-positives on every bulk import. Anomaly detection is therefore unavailable as a
safeguard, which concentrates the entire safety load on the mount sentinel gate (§15.1). That
concentration is the reason `sync` defers: it gets built after the surrounding machinery is
proven, not alongside it.

**What defers with it:** the sentinel gate, grace period / `item.first_missing_at`, dry-run, and
the rate-based backstop. **What does not:** `move` deletes too, so verification-before-delete,
deletes through our own asyncssh path (§7.4), and the `event` audit trail are all v1.

---

## 2026-08-11 — `code-checkin-and-pr` deferred until the first GitHub push

**Decision:** do not adopt `code-checkin-and-pr` yet; follow two of its conventions voluntarily.

Every rule in that standard binds to a remote — protected `main`, `dev → main` PRs, seven
required CI checks, image publishing with registry retention. lftpweb has no remote and no CI,
so a `standards.md` row claiming adoption would assert conformance that cannot exist. The
standards index explicitly warns against exactly this ("a clean-looking row that lies").

Instead: commit-prefix conventions (`feat:` / `fix:` / `chore:` / `docs:`), no
`Co-authored-by:` trailers, and the `dev` / `main` branch shape are followed from commit one,
so the history is already conformant when the standard is adopted for real. That adoption
should land in the same change that adds the remote and CI, re-pinning the row to the
then-current version.

---

## 2026-08-11 — Bootstrap adoption done in-session, not via a handoff prompt

**Decision:** the `handoff-prompt-workflow` adoption commit is the one task exempt from the
workflow it installs.

The standard's v2.0.0 threshold pushes any edit beyond ~1–2 files into a `prompts/` file
executed by a spawned subagent. This scaffolding touched six files, so by the letter it wanted
a prompt — but that prompt would have had to live in the `prompts/` directory it was itself
creating, inside a git repo that did not yet exist, and the mandated
`git status --porcelain` working-tree check had no tree to inspect.

Rejected alternative: `git init` first, then write the prompt and spawn an agent for the rest.
Workable, but it splits an atomic, fully-prescribed checklist across two contexts for no gain —
the standard's own adoption section *is* the spec, so a fresh context adds nothing.

Scope of the exemption is exactly one commit. Every task after it goes through the workflow.

---

## 2026-08-11 — lftp is a transfer engine, not a status API

**Decision:** derive transfer progress from the filesystem — local bytes on disk versus known
remote size — and use lftp purely to move bytes. One short-lived lftp process per transfer,
driven over plain pipes. See `DESIGN.md` §1.3 and §4.

**Rejected alternative — SeedSync's approach:** one long-lived interactive lftp per path-pair
over a pexpect PTY, with all transfer state reconstructed by polling `jobs -v` every 0.5 s and
regex-parsing lftp's human-readable verbose output.

**Why rejected.** That parser is ~15 interlocking regexes plus an order-dependent line
dispatcher, and it must survive readline's ANSI/bracketed-paste escapes, PTY line wrapping when
`COLUMNS` isn't honored, and lftp's inconsistent progress grammar (in `` `f' at 2976 (12%) ``
the number is *not* the local size and the percentage is *not* the local percentage). SeedSync's
maintainer records it in fork issue #294 as "the most fragile part of the codebase… the root
cause remains", closed as "do nothing for now". Sharing one process per pair also means one
parse failure or pexpect timeout degrades *every* transfer on that pair, and stopping a job
carries an acknowledged kill-wrong-job race because ids can shift between the status read and
the kill.

**What this buys.** Liveness becomes an exit code, stopping becomes a SIGTERM to one PID,
failures are contained to one transfer, and per-file progress covers the whole tree rather than
whichever files lftp happens to mention. ETA is computed and smoothed uniformly by us, fixing
the directory-ETA problem lftp causes by never emitting an ETA on a mirror header.

**Cost accepted.** Two lftp on-disk conventions must be understood instead:
`<file>.lftp-pget-status` sidecars (sparse-file accounting) and the `xfer:use-temp-file` /
`*.lftp` suffix. Both are short, stable, machine-oriented formats, unlike the verbose output,
which is formatted for humans and has never been a stable interface. If either changed,
progress degrades to raw size (still monotonic) and completion is unaffected, because
completion is the exit code.

**Status:** recorded from `DESIGN.md`, which is still under review. If §1.3 is overturned in
review, supersede this entry rather than editing it.
