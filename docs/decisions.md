# Decision record

Non-obvious decisions for lftpweb — approach changes, rejected alternatives, workarounds.
Newest at top. Per the `handoff-prompt-workflow` standard, sessions append here rather than
leaving the reasoning only in a commit message.

---

## 2026-08-11 — Phase 3: the live-retune experiment (§4.5) is **verified working**

**Tested against a real running transfer, not left as a maybe.** Held `lftp`'s stdin open on a
read-write fd, fed it an initial script ending in `pget ... &` (backgrounding the transfer so
the command loop stays live), then wrote `set net:limit-total-rate <n>` to that same fd while
the job was running.

**Result: it works.** Clean before/after measurement against the fake seedbox, using
`.lftp-pget-status`'s own accounting (not a guess): capped at 200,000 B/s, a 3s window moved
611,085 bytes (203,695 B/s — matches the cap to within 2%). Immediately after writing `set
net:limit-total-rate 5000000` into the held-open stdin, the same job's throughput jumped
sharply and it finished far faster than the original cap could have allowed. A second run
retuning 300,000 → 3,000,000 mid-flight showed effective size accelerate from ~517 KB to ~8.15
MB over the following 2s (≈3.8 MB/s) — well above the old cap, consistent with the new one.

**Not adopted — admission control still stands alone**, exactly as the phase 3 prompt required.
`core/queue.py` spawns every job with `stdin=DEVNULL`; the held-open-pipe technique was only
exercised in a standalone script, never wired into production. This closes the "unverified"
qualifier on DESIGN.md §4.5's experiment and on §15.2 — a future phase could build on it to
reclaim the "half the pipe sits idle after a partner finishes" cost (§4.5's "residual
inefficiency"), but nothing forces that decision now.

---

## 2026-08-11 — Phase 3: `GET /api/files` must read `item.state` from the database, not
## `core/engine.py`'s in-memory scan model

**Found live, through the running HTTP API — not by static review.** Stopping a job via `POST
/api/jobs/{id}/stop` correctly wrote `item.state = 'STOPPED'` to the database (confirmed by
direct SQL in `tests/test_queue.py`), but `GET /api/files` kept reporting `PARTIAL` for the same
item immediately afterward. `api/files.py` was serving `core/engine.py`'s `engine.models` —
`core/reconcile.py`'s pure structural output (REMOTE_ONLY/LOCAL_ONLY/PARTIAL/DOWNLOADED,
recomputed from scratch on every scan), which has no notion of QUEUED/DOWNLOADING/STOPPED/FAILED
at all. That was the correct thing to serve in phase 2 (nothing else existed), but phase 3 adds
a second writer of `item.state` — `core/queue.py` — and the read path never learned to look at
its output.

**Fix:** `api/files.py.get_files()` now queries the `item` table directly for every field,
including `state`, rather than reproducing the merge from `engine.models` in Python. The
database is genuinely simpler here: `core/engine.py._persist` already knows how to merge
scan-derived and job-derived state (see the next entry), so re-deriving that merge a second time
at the API layer would only be a second place for the two to drift apart.

**Also fixed in the same pass:** `GET /api/files` never exposed the persisted `item.id` at all —
phase 2's read-only Files view never needed it, but `POST /api/jobs` (queue an item, §4.7) takes
exactly that id, and there was no way for a client to obtain one. `FileNode` gained an `id`
field.

---

## 2026-08-11 — Phase 3: a periodic rescan must not overwrite a job-lifecycle state back to a
## purely structural one — DESIGN.md doesn't say who wins

**Ambiguity found building the transfer engine, resolved with the smallest reasonable call.**
`core/engine.py`'s scan loop persists `item.state` fresh on every pass (every `scan_interval_s`,
default 30s, plus on-demand). `core/queue.py` also writes `item.state` — QUEUED on enqueue,
DOWNLOADING on spawn, STOPPED/FAILED on stop or exhausted retries. Nothing in DESIGN.md's §3.2
or §4 says which writer wins when both are live for the same item at once. Left unresolved, a
`STOPPED` item with a still-partial file reads as `PARTIAL` again the moment the next scan runs
— indistinguishable from "never stopped", which quietly defeats §4.6's auto-queue suppression
rule (a state that reverts to non-STOPPED can't stay suppressed for the right reason).

**Fix:** `core/engine.py._persist` now treats an item as "protected" — and leaves its `state`
column alone, refreshing only size/mtime — whenever it currently has a `job` row in
`queued`/`running`, or `auto_queue_suppressed` is set (STOPPED/FAILED). Everything else still
gets the freshly computed structural state. `core/queue.py`'s own success path
(`_reap_one`) clears `auto_queue_suppressed` and sets `DOWNLOADED` itself, so the next scan is
free to confirm it rather than fight over it — the protection only ever applies while `queue.py`
is actively using the row.

---

## 2026-08-11 — Phase 3: three real lftp behaviors found running it for real, none documented
## anywhere in DESIGN.md or `lftp --help`

All three were found by running actual commands against the fake seedbox while building
`core/lftp.py` — see `tests/test_lftp.py` for the pinned regression coverage.

**1. `mirror -c 'REMOTE/item' 'LOCAL/'` creates `LOCAL/item/...` itself — it appends the
remote path's own basename onto the target.** The "obviously" symmetric choice with `pget`
(`LOCAL/item/`, matching the item's own local directory) produces a doubly-nested
`LOCAL/item/item/...` tree instead. `core/lftp.py.build_transfer_command` documents this
explicitly; `core/queue.py` passes the item's *parent* directory as `local_path` for a `mirror`
job, the item's own local directory for `pget`.

**2. A bare `open sftp://user@host` makes lftp's own sftp backend try to prompt for a password
itself — `GetPass() failed -- assume anonymous login` / `Login failed: Password required` —
even when the connect-program's ssh has already authenticated successfully via a key.**
`-u user,` with an *empty* password field suppresses lftp's own prompt and defers entirely to
whatever the connect-program's ssh already established. `core/lftp.py.build_rc_text` always
uses the `-u user,password` form now, with an empty password for `key`/`agent` auth.

**3. `pget:save-status` defaults to `10s`.** Far too coarse for a ~1 Hz progress sampler — a
transfer inspected at the 1s/2s/3s marks under the default had no `.lftp-pget-status` sidecar
at all yet. Every job's rc file now sets `pget:save-status 1s`. This is a genuinely
load-bearing tunable that DESIGN.md §4.4 never mentions, because §4.4 was written assuming the
sidecar simply exists whenever there's progress to read.

**Also found, cosmetic but worth recording:** a script passed to `lftp -c`/`source`d whose
*first line is blank* corrupts quote-stripping on the very next `set key "value with spaces"`
line — the literal quote characters end up in the stored value, and the shell that later execs
that value treats the whole quoted string (spaces and all) as one unfindable program name.
Reproducible on demand; not reproducible once the first line is real content.
`core/lftp.py.build_rc_text` never emits a leading blank line for this reason.

---

## 2026-08-11 — Phase 3: host-key verification for the lftp-spawned ssh child — DESIGN.md §4.2
## never says whether it should match the scanning connection's policy

**Ambiguity found in DESIGN.md, resolved with the smallest reasonable call.** §5/§8 specify
`known_hosts_policy` (accept-and-pin / strict / insecure) for the asyncssh connection
`core/remote.py` uses to scan and test the connection. §4.1/§4.2 describe the *separate* ssh
process `lftp` spawns via `sftp:connect-program` for an actual transfer, but never say whether
it should honor the same policy, default to something else, or fall back to OpenSSH's own
`~/.ssh/known_hosts`.

**Decision:** reuse the exact pin `core/remote.py`'s `KnownHostsStore` already holds for the
host — the same one the scanning connection trusted — written into a throwaway
`known_hosts`-format file alongside the job's rc file (`/run` tmpfs, mode 0600, unlinked with
it), with `-o StrictHostKeyChecking=yes`. `insecure` is passed straight through as
`StrictHostKeyChecking=no` / `UserKnownHostsFile=/dev/null`, matching `core/remote.py`'s own
"insecure means never verify, unconditionally" reading. **`strict`/`accept-and-pin` with no pin
on file yet refuse to spawn the job at all** (`NoHostKeyPinError`) rather than trusting an
unpinned key on the transfer path that the scan path hasn't already vouched for — a transfer job
silently trusting-on-first-use independently of the scanning connection would make the whole
policy decorative. In practice this can only happen if a job is queued before any scan has ever
succeeded, which the engine's own scan loop makes rare but not impossible.

---

## 2026-08-11 — Phase 3: `pget -o <path>` does not create its target's parent directory

**Found running a nested item through the real transfer queue, not anticipated.** `mirror`
creates whatever directory structure it needs under its own target; `pget` does not — queuing
an item whose local target directory didn't exist yet failed with lftp's own `No such file or
directory`, for the *local* side, from inside the container running as the right uid with
correct permissions. `core/queue.py._spawn_decision` now `mkdir -p`s the exact directory a
`pget` job's file will land in (and a `mirror` job's own target-parent) before spawning. For a
genuinely top-level item (DESIGN.md §4.7) this is a no-op — the parent is just the queue's
`local_path`, which the operator already provisioned — but nothing in the schema restricts
`item` rows (or manual queueing) to top-level entries (see the phase 2 decision on that), so it
has to hold generally.

---

## 2026-08-11 — Phase 3: out-of-scope bug found incidentally — one permission-denied
## subdirectory anywhere in a queue aborts that queue's *entire* scan

**Found live while verifying phase 3 through the API, not something phase 3 was asked to fix.**
`core/remote.py`'s primary scan path (`find <path> -mindepth 1 -printf ...`) treats any nonzero
exit as a hard failure unless it matches the "unsupported `-printf`" fallback trigger. GNU
`find` exits `1` the moment it can't `stat`/read one subdirectory's permissions — even though it
still printed every record it *could* read to stdout first. The whole queue's scan is discarded
and reported as failed, rather than the one inaccessible subtree being skipped. Not fixed here
(it's `core/remote.py`, phase 2's module, and out of the phase 3 prompt's scope) — recorded so a
future session doesn't have to rediscover it. Triggered by a test fixture (`chmod 000` on a
seedbox directory) removed before phase 3's verification continued.

---

## 2026-08-11 — Phase 3: two admission-control edge cases DESIGN.md's §4.5 worked examples
## don't cover, decided in code

**"Start now at max bandwidth" bypasses both the main-lane slot count and headroom, not just
headroom.** §4.5 says it "admits immediately with allocation = the full B, deliberately
oversubscribing past the ceiling" and separately that normal admission freezes "while `Σ
allocations > B − reserve`" — the bandwidth side is explicit, but whether it also ignores
`max_concurrent_transfers` (N) is never stated. Decided: yes, unconditionally — it's framed
throughout §4.5 as "the escape hatch", and a version that still queued behind a full N would be
indistinguishable from Move to Top. `core/scheduler.py.admit()` admits every `forced_full_rate`
queued item first, before computing `slots`/`ready` for anything else.

**`UNKNOWN` error class never retries.** §4.3 names the transient classes (`HOST_UNREACHABLE`,
`TLS_ERROR`, timeouts, resets) and the permanent ones (`AUTH_FAILED`, `PERMISSION_DENIED`,
`REMOTE_GONE`, `DISK_FULL`) but never places `UNKNOWN` in either bucket. Decided: retry is a
whitelist (`core/lftp.TRANSIENT_ERROR_CLASSES`), not "retry everything not explicitly
permanent" — a failure our classifier didn't recognize is exactly the case where blindly
hammering the seedbox on a timer is the wrong default; a human should see it once via `FAILED`
rather than have it retry silently up to `max_attempts` first.

---

## 2026-08-11 — Phase 2: `asyncssh.connect()` fails outright under DESIGN.md §11.2's own
## numeric-uid convention — `getpass.getuser()` raises `OSError` on Python 3.13

**Found running the actual built container against the fake seedbox, not anticipated by
DESIGN.md.** `core/remote.py`'s connections all failed with `"No username set in the
environment"` the moment lftpweb ran inside its own container (uid 1000 via compose's native
`user:`, no `/etc/passwd` entry — exactly §11.2's documented identity model, and exactly what
the PUID/PGID entrypoint also produces). Traced to `asyncssh.connect()`: it unconditionally
calls `getpass.getuser()` early in connection setup, for SSH-config `%u` templating, completely
independent of the `username=` kwarg we always pass. `getpass.getuser()` falls through to
`pwd.getpwuid(os.getuid())`, which raises `KeyError` for an unregistered uid — and on Python
3.13, `getpass.getuser()` itself catches that `KeyError` and re-raises `OSError('No username
set in the environment')`. asyncssh's own `except KeyError:` around the call does not catch an
`OSError`, so the exception propagates and every connection attempt fails, for every auth
method, before authentication is ever reached.

**Fix:** `core/remote.py` sets `LOGNAME` at import time — but only if none of
`LOGNAME`/`USER`/`LNAME`/`USERNAME` is already set, so a real environment value is never
overridden. `getpass.getuser()` checks the environment before touching `pwd`, so this sidesteps
the crash entirely without touching container identity, `/etc/passwd`, or asyncssh itself.
Covered by `tests/test_remote_username_env.py`, and reproduced for real: verified failing
against the fully-built runtime image before the fix, and succeeding after, both against the
fake seedbox over the container network (see the phase 2 report for the exact commands).

**Why this belongs in code, not compose.** The trigger is the numeric-uid-with-no-passwd-entry
convention §11.2 already committed to for *both* supported identity mechanisms (PUID/PGID and
compose's native `user:`), so every deployment shape this project supports hits it. Fixing it
by adding `environment: USER=...` to the compose files would work for the two committed compose
files but silently reintroduce the bug for anyone deploying with their own compose/Kubernetes
manifest that follows the same PUID/PGID convention — the fix belongs where the assumption that
breaks it (§11.2) is made, which is the application, not any one deployment's config.

---

## 2026-08-11 — Phase 2: `known_hosts=None` in asyncssh silently disables host-key
## verification *and* skips the `validate_host_public_key` callback entirely

**Found while building the accept-and-pin flow, not anticipated.** The natural-looking way to
say "we're doing our own host-key checking" is `asyncssh.connect(..., known_hosts=None,
client_factory=OurClient)`, expecting `OurClient.validate_host_public_key` to be consulted for
every key. It never is: asyncssh's `_connection_made()` sets `self._trusted_host_keys = None`
whenever `known_hosts is None`, and `validate_host_public_key` is only called when
`self._trusted_host_keys is not None`. The practical effect: with `known_hosts=None`, asyncssh
trusts *any* server host key unconditionally and never asks our callback anything — the
accept-and-pin policy (DESIGN.md §5, §8) silently never ran, and *every* `known_hosts_policy`,
including `strict`, would have behaved as `insecure`.

**Fix:** pass `known_hosts=asyncssh.SSHKnownHosts()` — a real, empty, in-memory known-hosts
object, not `None` and not an empty string/list/bytes (any of which cause asyncssh to fall back
to probing `~/.ssh/known_hosts` on whatever filesystem the process happens to see, which is
worse). An empty `SSHKnownHosts` is non-falsy and holds zero trusted keys, so asyncssh always
defers to `validate_host_public_key`, which is where `core/remote.py`'s
`known_hosts_policy` (`accept-and-pin` / `strict` / `insecure`) is actually enforced, via a
small JSON pin store (`KnownHostsStore`) rather than OpenSSH's own known_hosts file format.
Verified live against the fake seedbox: first connection pins and logs the key; a corrupted
pin is rejected as `HOST_KEY_MISMATCH` on the next fresh connection; `strict` against a never-
pinned host reports `HOST_KEY_UNKNOWN`. See `tests/test_known_hosts_store.py` and the phase 2
report's edge-case script for the exact assertions.

**Also decided: `insecure` bypasses the pin store entirely, checked first.** An earlier draft
checked the stored pin before checking policy, so an `insecure` host that happened to have a
pin recorded under a different policy earlier would be rejected as a "mismatch" — exactly
backwards for a policy that means "never verify." `insecure` is now the first check in
`validate_host_public_key`, unconditional, and never reads or writes the pin store.

---

## 2026-08-11 — Phase 2: credential encryption at rest ships now, not in build phase 8

**Decision, mandated by the phase 2 prompt rather than discovered during the build:** §8's
encryption scheme (`core/crypto.py`) — a per-install secret in `<config_dir>/secret.key`, mode
0600, generated on first run; a Fernet key derived from it via HKDF-SHA256; `host.password_enc`
encrypted at rest — ships in phase 2, the phase where a seedbox password first exists, rather
than waiting for phase 8 as `DESIGN.md` §13's build order literally lists it. Phase 8 still
owns the *rest* of §8: auth modes, sessions, API keys, rate limiting.

**The secret is deliberately not backed up** (§8/§10.2 — `core/backup.py` is phase 7 and will
need to exclude `secret.key` from `VACUUM INTO` targets when it lands), so a restore to a fresh
install cannot recover a stored password. `DecryptionError` is how `core/engine.py` and
`api/settings.py` detect that case: `load_host_config` catches it and proceeds with
`password=None` rather than crashing, and `GET /api/settings/host` reports
`credentials_need_reentry: true` so the UI can surface it — the full "hold all transfers for
this host" behavior §8 describes waits for phase 3's job engine to have transfers to hold.

---

## 2026-08-11 — Phase 2: `item` rows persist per-node (file *and* directory), not just
## §4.7's top-level "item" concept

**Ambiguity found in `DESIGN.md`, resolved with the smallest reasonable call.** §4.7 defines
"item" narrowly — a top-level entry of a queue's `remote_path`, either a directory or a loose
file, the granularity auto-queue patterns match against. But the `item` table (§3.1) has
`UNIQUE(queue_id, rel_path)` with no depth restriction, and §9.2's item drawer promises
"per-file status... over the whole tree" for everything inside a release. Read literally, §4.7's
item definition and the `item` table's evident scope disagree.

**Resolution:** `core/engine.py` persists one `item` row per node the reconciler produces —
every file and every directory in the merged tree, not only top-level entries — because that's
what the Files page (a full tree, not a flat item list) and the future item drawer both need,
and nothing in §3.1's schema forbids it. §4.7's narrower "item" remains the correct unit for
auto-queue pattern matching (phase 4); the two uses of the word describe different granularities
of the same table, and phase 4 should pattern-match against top-level rows specifically rather
than assuming every persisted row is an auto-queue item.

---

## 2026-08-11 — Phase 2: a directory with zero local presence reads as `REMOTE_ONLY`, not
## `PARTIAL` — `DESIGN.md` §3.2 rule 1 doesn't say

**Ambiguity found in `DESIGN.md`, resolved with the smallest reasonable call — surfaced for
review, not silently decided.** Rule 1 states a directory is `DOWNLOADED` only when every
relevant descendant file is complete, "otherwise `PARTIAL`" — a strict binary, with no
carve-out for a directory that has *zero* local presence at all (nothing queued or downloaded
yet). Read literally, a totally-untouched remote-only release directory would show `PARTIAL`,
which reads to a user as "download interrupted," not "nothing has happened yet."

**Decision:** `core/reconcile.py` computes three directory states from rule 1's own
completeness accounting (already computed for `DOWNLOADED` vs not): `DOWNLOADED` when every
relevant file is complete (or vacuously, when there are none), `REMOTE_ONLY` when *no* relevant
file has any local copy, and `PARTIAL` only for the genuine in-between. This is additive to
rule 1, not a departure from it — the `DOWNLOADED`/not-`DOWNLOADED` boundary rule 1 specifies is
unchanged; only the "otherwise" is split into two states instead of collapsed into one. Pinned
by `tests/test_reconcile.py::test_directory_remote_only_with_zero_local_presence` and the
directory-state table alongside it.

---

## 2026-08-11 — Phase 2: one combined scan interval, not `DESIGN.md` §5's separate 30s
## remote / 10s local cadences

**Deviation recorded rather than silently taken.** §5 specifies remote scans every 30s and a
faster local-only walk every 10s, with the gap covered by phase 3's 1 Hz active-file
`ProgressSampler`. `core/engine.py` runs one interval (default 30s, `LFTPWEB_SCAN_INTERVAL_S`)
that scans both sides together, plus `request_rescan()` for an immediate on-demand pass (used
by `POST /api/files/rescan` and after a host/queue config change).

**Why this is acceptable now.** The faster local-only cadence exists to catch local filesystem
changes (an import finishing, a manual delete) between the more expensive remote round-trips —
a scale/responsiveness optimization. With no active transfers yet (phase 3), nothing is
producing local changes on that kind of timescale, and every phase 2 verification (including
the delete/restore flip test) uses `request_rescan()` rather than waiting on a timer. Splitting
the cadence is deferred to whenever it's actually needed, not dropped — `Engine.scan_queue`
already separates the remote scan, the local scan, and the reconcile call, so adding a second,
faster local-only loop later doesn't require restructuring it.

---

## 2026-08-11 — Phase 2: WebSocket "deltas" are per-queue full snapshots, not row-level diffs

**Scoped-down interpretation of `DESIGN.md` §2/§9, recorded rather than silently taken.** "A
full model snapshot on connect, deltas thereafter" is read literally as row-level diffing
elsewhere in the doc's vocabulary (e.g. the `item` table's change tracking). Phase 2's
`api/ws.py` instead sends one `queue_snapshot` message — the complete fresh state of one
queue — every time `core/engine.py` finishes scanning that queue, and the frontend
(`useFilesSocket.ts`) merges it into a `queue_id`-keyed map. A queue that hasn't rescanned since
connecting keeps showing its last-known state rather than vanishing.

**Why this is acceptable now.** A reconciled tree is idempotent state, not an event log — the
whole tree is cheap to hold and to replace outright (`RemoteConnectionPool`'s find output for
the seed tree is a few KB), and there is no job/lifecycle history yet whose *transitions*
specifically need to be pushed. Row-level diffing becomes worth the complexity once phase 3's
per-file progress ticks at ~1 Hz on the active set (§4.4) — pushing a whole queue's tree on
every progress tick would not scale the way pushing one row's bytes-done does. Flagged here so
phase 3 doesn't inherit this shape by default.

---

## 2026-08-11 — Phase 2: the fake seedbox's SSH keypair and password are committed, on purpose

**Decision.** `docker/test-seedbox/test_key`(`.pub`) and the hardcoded `testpass123` in
`sshd_config`/the two Dockerfiles are committed to the repo, despite the general rule (§12.1,
`.gitignore`) that credentials never get committed. This is not an exception to that rule —
these are not credentials to anything real: the containers they authenticate are built from
this repo, reachable only on `127.0.0.1`, hold a synthetic tree of known sizes, and are torn
down after every verification run. Requiring them to be generated fresh on every `docker
compose -f docker-compose.test.yml up` would only add friction for zero safety benefit, since
there is nothing behind them to protect.

**Why a real GNU + a real busybox container, not one container with two `find` shims.**
DESIGN.md §15.7 records `find -printf` as GNU-specific and calls for verifying the fallback
against the real thing. Faking busybox's behavior inside a GNU environment (a wrapper script,
an alias) would test the fallback *trigger* logic but not the actual busybox error text
`core/remote.py`'s detection regex has to match — and that text (`"find: unrecognized:
-printf"`) was itself discovered by running the real binary, not by reading busybox's source.
`docker/test-seedbox/Dockerfile.busybox` deliberately does not install `findutils`, which is
the one thing that would silently stop testing what it exists to test.

---

## 2026-08-11 — Phase 2: the Files tree is not yet virtualized

**Deviation recorded rather than silently taken.** `DESIGN.md` §9.2 calls for a virtualized
tree "smooth at 10k+ rows." `frontend/src/components/FileTree.tsx` renders the full DOM tree
with plain React recursion and no virtualization library — none is installed yet, and adding
one is a dependency decision worth making deliberately rather than as a side effect of phase 2.
§13's build order lists "virtualization tuning" explicitly under phase 9 (Polish), so this is
read as on-schedule rather than a phase 2 gap: the fake seedbox's tree (17 nodes) and any
realistic dev-scale queue are nowhere near where non-virtualized rendering degrades, and
nothing about the read-only, collapsible, per-row-state-chip shape this phase built needs to
change to add virtualization later — only the row-rendering internals of `FileTree.tsx` would.

---

## 2026-08-11 — Phase 3a review: a small bandwidth ceiling silently deadlocked the whole queue

**Found reviewing phase 3a, by setting a 400 KB/s cap and watching a job sit in `queued`
forever.** `DESIGN.md` §4.5 specified the fast-lane reserve as *"10% of B, min 1 MB/s"*. That
floor is unconditional, so:

| ceiling B | reserve | headroom = B − reserve | admits |
|---|---|---|---|
| 400 KB/s | 1 MB/s | **−600 KB/s** | 0 |
| 1 MB/s | 1 MB/s | **0** | 0 |
| 5 MB/s | 1 MB/s | 4 MB/s | 1 |

Any ceiling at or below 1 MB/s produced `headroom <= 0`, so the main lane admitted **nothing,
ever** — jobs accepted, queued, and never run, with no error, no failed state, and no log line.
A user throttling lftpweb to be polite to their uplink would get a permanently dead queue and
no way to tell why.

The design error is worth naming precisely: the fast lane exists to stop small items being
blocked by large ones, and the unclamped floor let it block *everything* instead — the exact
failure it was introduced to prevent, inverted.

**Fix, in both code and `DESIGN.md` §4.5:** the reserve is capped at `B/2`, so it can never
consume the ceiling it is carved from. Explicit user-set reserves are clamped too, not just the
derived default. Regression test `test_low_ceiling_still_admits_work` parametrises ceilings from
100 KB/s upward and asserts work is still admitted.

**Also fixed: the silence.** When the scheduler admits nothing while work is waiting, it now
logs the arithmetic that produced that decision (ceiling, reserve, allocated, headroom, slots).
Admitting nothing is usually correct, but it was previously indistinguishable from a wedged
queue — which is how this hid.

---

## 2026-08-11 — Phase 3a review: a spawn failure left the job queued and the tick hot-looping

**Found in the same session, accidentally**, by running the backend outside its container
without setting `LFTPWEB_RUN_DIR`: `/run/lftpweb` isn't writable by a normal uid, so
`lftp.spawn()` raised `PermissionError` inside `_spawn_decision`. (The misconfiguration was
mine — `run_dir` is configurable and documented. The *failure mode* is the bug.)

`_loop`'s blanket `except Exception` caught it, logged `transfer queue tick failed`, and
continued — so the job stayed `queued`, the tick retried once a second forever, and the API
reported a perfectly healthy job that simply never started. Every real deployment failure of
this shape (read-only `/run`, missing `lftp` binary, wrong uid) would look identical.

**Fix:** `_admit()` catches per-decision, marks that job `failed` with `error_class =
SPAWN_FAILED` and the exception detail on the row, and suppresses the item like any other
permanent error (§4.3, §4.6) — so it surfaces in the UI, doesn't spin, and doesn't take the
other decisions in the same tick down with it. Covered by
`test_spawn_failure_fails_the_job_instead_of_hot_looping`.

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
