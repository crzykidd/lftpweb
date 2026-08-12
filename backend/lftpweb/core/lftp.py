"""One lftp process per job (DESIGN.md §4.1, §4.2, §4.3).

This module builds the command, spawns it, and classifies a non-zero exit. It does **not**
decide *when* to spawn (that's `core/scheduler.py`) or supervise/reap/persist the result
(that's `core/queue.py`) — see DESIGN.md §12 on why the three are split.

**The thing this project exists to avoid:** nothing in this module ever reads lftp's stdout to
learn transfer progress. stdout/stderr are captured *only* so that, on a non-zero exit, the
last ~4 KB can be classified into an error class and shown to a human (§4.3) — never parsed for
"how far along" anything is. See `core/progress.py` for where progress actually comes from.

**Credentials (§4.2).** Two things must never appear in argv (world-readable via
`/proc/<pid>/cmdline` inside the container): the seedbox password, and — as a matter of the
same policy — anything that would let a ps listing reconstruct it. Both the password (for
`auth_method='password'`) and the `open` command that carries it live in a **per-job rc file**,
mode 0600, on the `/run` tmpfs, unlinked the moment the process exits (successfully or not —
`spawn()`'s caller is responsible for calling `cleanup()` from a `finally`). The `-c` command
string passed as argv contains only `source <rc-path>` plus the transfer command itself
(paths, not secrets), so a `ps aux` inside the container never shows a credential.

**Host-key verification for the lftp-spawned ssh child — a gap in DESIGN.md §4.2, resolved
here.** §5/§8's `known_hosts_policy` (accept-and-pin / strict / insecure) is specified for the
asyncssh scanning connection (`core/remote.py`) but DESIGN.md never says whether the *separate*
ssh process lftp spawns via `sftp:connect-program` should honor the same policy. Answer: yes —
the smallest reasonable call, and the only one that isn't a silent downgrade, is to reuse the
exact pin `core/remote.py`'s `KnownHostsStore` already holds for this host, write it into a
throwaway OpenSSH-format `known_hosts` file alongside the rc file (also `/run` tmpfs, mode
0600, unlinked with the rc file), and point `-o UserKnownHostsFile=` at it with
`-o StrictHostKeyChecking=yes`. A transfer job silently trusting an unpinned host key while the
scanning connection enforces pinning would make the whole policy decorative. `insecure` is
passed through as `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`, matching
`core/remote.py`'s own "insecure means never verify, unconditionally" reading. `strict`/
`accept-and-pin` with **no pin yet on file** refuses to spawn rather than trusting on the
transfer path what the scan path hasn't already vouched for — DESIGN.md never specifies this
either; it is surfaced in the phase 3 report rather than silently decided.

**A real lftp 4.9.2 parser quirk found while building this, unrelated to the above:** a script
passed to `lftp -c` (or a `source`d rc file) whose very first line is blank, immediately
followed by a `set key "value with spaces"` line, has the quotes taken *literally* into the
setting's value instead of being stripped — `sftp:connect-program` then contains a literal
`"ssh -a -x"` (quote characters and all), which the shell that eventually execs it treats as
one unfindable program name ("not found"). Reproduced directly against the fake seedbox; not
reproducible once the script's first line is real content. `build_rc_text()` never emits a
leading blank line for exactly this reason — a comment header would trigger the same bug.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DEFAULT_RUN_DIR = "/run/lftpweb"

# ~4 KB tail kept on the `job` row (DESIGN.md §3.1/§4.3) — enough for a human to see *why*,
# never the whole transcript (that would start drifting toward "parse this for progress").
OUTPUT_TAIL_BYTES = 4096

JobKind = Literal["mirror", "pget"]

# --- Error classification (DESIGN.md §4.3) --------------------------------------------------
#
# Every pattern below was matched against real lftp 4.9.2 output from the fake seedbox while
# building this module (see the phase 3 report for the exact commands): a bad password, a
# permission-denied source directory, a missing remote file, a full local disk, and a refused
# connection. Patterns are intentionally simple substring/regex checks over the captured tail,
# ordered most-specific-first, because lftp's error text is exactly the kind of thing this
# project otherwise refuses to depend on (§1.2/§1.3) — the one place it's unavoidable is
# classifying a *failure* after the fact, not tracking progress, and a wrong guess here only
# costs a UI label and a retry-or-not decision, never correctness of the transfer itself.

ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "AUTH_FAILED",
        re.compile(r"login (failed|incorrect)|permission denied \(publickey", re.IGNORECASE),
    ),
    ("PERMISSION_DENIED", re.compile(r"permission denied", re.IGNORECASE)),
    ("DISK_FULL", re.compile(r"no space left on device|disk full", re.IGNORECASE)),
    ("REMOTE_GONE", re.compile(r"no such file|not found on server|file not found", re.IGNORECASE)),
    (
        "HOST_UNREACHABLE",
        re.compile(
            r"connection refused|could not resolve hostname|name or service not known|"
            r"max-retries exceeded|network is unreachable|connection timed out|no route to host",
            re.IGNORECASE,
        ),
    ),
    ("TLS_ERROR", re.compile(r"tls|ssl|certificate", re.IGNORECASE)),
)

# Classes DESIGN.md §4.3 names as transient — retried with backoff up to max_attempts.
# Deliberately a whitelist, not "everything except the permanent four": §4.3 lists
# AUTH_FAILED/PERMISSION_DENIED/REMOTE_GONE/DISK_FULL as never-retry and HOST_UNREACHABLE/
# TLS_ERROR as retryable, but never says which bucket UNKNOWN falls into. Treating UNKNOWN as
# non-retryable (not adding it here) is the smaller-blast-radius reading: retrying a failure we
# failed to even classify risks hammering the seedbox on something a human should look at,
# rather than losing at most one retry cycle on a transient failure our patterns missed.
TRANSIENT_ERROR_CLASSES = frozenset({"HOST_UNREACHABLE", "TLS_ERROR"})


def classify_output(output: str) -> str:
    """Map captured lftp output to one of DESIGN.md §4.3's error classes. `UNKNOWN` if
    nothing matches — never raises, because a failure we can't classify is still a failure
    that must be recorded, not one that crashes the reaper.
    """
    for error_class, pattern in ERROR_PATTERNS:
        if pattern.search(output):
            return error_class
    return "UNKNOWN"


# --- Credentials (per-job rc file + known_hosts, DESIGN.md §4.2) ----------------------------


@dataclass(frozen=True)
class HostCreds:
    """The subset of `core/remote.py`'s `HostConfig` this module needs, kept separate so
    `core/lftp.py` doesn't import `core/remote.py`'s asyncssh-specific machinery for a plain
    dataclass. Field names mirror `HostConfig` so callers can pass it through by attribute.
    """

    address: str
    port: int
    username: str
    auth_method: str  # 'key' | 'agent' | 'password'
    key_path: str | None
    password: str | None
    known_hosts_policy: str  # 'accept-and-pin' | 'strict' | 'insecure'
    pinned_host_key: str | None = None  # exported OpenSSH pubkey line, if `core/remote.py` has one


class NoHostKeyPinError(Exception):
    """`known_hosts_policy` is 'strict' or 'accept-and-pin' but no key has been pinned for
    this host yet (the scanning connection has never succeeded). Refusing to spawn is the
    smaller mistake than trusting, on the transfer path, a key the scan path hasn't vouched
    for — see the module docstring.
    """


def _lftp_quote(value: str) -> str:
    """Quote a value for lftp's own command parser (single-quoted, `'` doubled the POSIX-shell
    way: `'\\''`). lftp's tokenizer follows shell-like quoting conventions; this is the same
    escaping `shlex.quote` would produce for the single-quote case, spelled out explicitly
    since we don't want the double-quote branch shlex sometimes chooses.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def _connect_program(creds: HostCreds, known_hosts_path: Path | None) -> str:
    """Build `sftp:connect-program`'s value — the ssh invocation lftp shells out to. Identity
    (key path) goes here for `auth_method='key'`; host-key policy is applied for every auth
    method, because it's a property of the connection, not of how we authenticated to it.
    """
    parts = ["ssh", "-a", "-x"]
    if creds.auth_method == "key":
        if not creds.key_path:
            raise ValueError("auth_method 'key' requires key_path")
        parts += ["-i", creds.key_path]

    if creds.known_hosts_policy == "insecure":
        parts += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    else:
        if known_hosts_path is None:
            raise NoHostKeyPinError(
                f"known_hosts_policy={creds.known_hosts_policy!r} but no host key is pinned for "
                f"{creds.address}:{creds.port} — run a successful scan/Test connection first"
            )
        parts += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts_path}"]

    return " ".join(parts)


def build_rc_text(
    creds: HostCreds,
    known_hosts_path: Path | None,
    *,
    rate_limit_bps: int | None,
    connection_limit: int | None,
    parallel: int,
    pget_n: int,
    save_status_interval_s: int,
    extra_settings: str,
) -> str:
    """The sourced rc file's content — credentials plus per-job tuning. Never starts with a
    blank line (see the module docstring's lftp-quirk note). Every line is a `set` or `open`
    lftp command; no shell metacharacters are interpreted here because this file is `source`d
    by lftp's own parser, not by a shell.
    """
    lines: list[str] = [
        "# lftpweb per-job settings + credentials — mode 0600, /run tmpfs, unlinked on exit.",
        f'set sftp:connect-program "{_connect_program(creds, known_hosts_path)}";',
        # Sidecar freshness (DESIGN.md §4.4) — lftp's own default is 10s, far too coarse for a
        # ~1 Hz progress sampler (found running a real transfer against the fake seedbox: the
        # sidecar simply didn't exist yet at the 1s/2s/3s marks under the default). This is the
        # one non-obvious tunable this module owns that DESIGN.md doesn't mention.
        f"set pget:save-status {save_status_interval_s}s;",
        # DESIGN.md §4.4b: in-flight files carry a `.lftp` suffix so `core/local_scan.py`'s
        # temp-suffix handling has something to strip. lftp's own defaults are `no` / `.in.*`.
        "set xfer:use-temp-file yes;",
        'set xfer:temp-file-name "*.lftp";',
        "set mirror:parallel-directories yes;",
    ]
    if rate_limit_bps is not None:
        # net:limit-total-rate is process-wide (DESIGN.md §4.5) — one number bounds this job's
        # entire subtree of connections, which is exactly why it's the knob the scheduler uses.
        lines.append(f"set net:limit-total-rate {rate_limit_bps};")
    if connection_limit is not None:
        lines.append(f"set net:connection-limit {connection_limit};")
    if parallel > 1:
        lines.append(f"set mirror:parallel-transfer-count {parallel};")
    if pget_n > 1:
        lines.append(f"set mirror:use-pget-n {pget_n};")
        lines.append(f"set pget:default-n {pget_n};")
    if extra_settings.strip():
        # The free-text "extra lftp settings" box (DESIGN.md §9.3) — injected verbatim, after
        # everything above so a power user can override any of it. Not sanitized: it's a
        # site-level admin setting, not user-submitted-per-request data.
        lines.append(extra_settings.strip())

    # `open -u user,pass` is the only lftp-level way to hand sftp a plaintext password (there
    # is no generic `set *:password` — confirmed against a real lftp binary's `set -a` dump),
    # so the credential-bearing `open` itself lives here, never in argv. **Always** the `-u`
    # form, even for key/agent auth with an empty password field: found running this against
    # the fake seedbox — a bare `open sftp://user@host` makes lftp's own sftp backend try to
    # prompt for a password itself (`GetPass() failed -- assume anonymous login` / `Login
    # failed: Password required`) instead of deferring entirely to the connect-program's ssh,
    # even though that ssh already authenticates successfully via the key. `-u user,` with an
    # empty password suppresses lftp's own prompt and lets the connect-program's ssh identity
    # (the `-i <key>` on `sftp:connect-program`, or the agent) do the actual authenticating.
    if creds.auth_method == "password":
        if creds.password is None:
            raise ValueError("auth_method 'password' requires a password")
        password = creds.password
    else:
        password = ""
    lines.append(
        f"open -u {_lftp_quote(creds.username)},{_lftp_quote(password)} sftp://{creds.address}:{creds.port};"
    )

    return "\n".join(lines) + "\n"


def build_transfer_command(
    kind: JobKind,
    remote_path: str,
    local_path: str,
    *,
    parallel: int,
    pget_n: int,
    exclude_globs: tuple[str, ...],
) -> str:
    """The transfer command itself (DESIGN.md §4.1) — paths only, no secrets, safe to place in
    the `-c` argv string. `-c` (continue) on both `pget`/`mirror` is what makes every restart
    resumable (§4.1); `set cmd:fail-exit true` (added by the caller) is what makes a plain exit
    code 0 mean success with no further inference needed (§4.3).

    **`local_path` means something different for each kind — found running both against the
    fake seedbox, not documented anywhere in lftp's own `--help`.** For `pget`, it's the exact
    destination *file* path. For `mirror`, it is the item's **parent** directory, not the
    item's own directory: `mirror -c '<remote>/<item>' '<local-parent>/'` creates
    `<local-parent>/<item>/...` itself (it appends the remote path's own basename). Passing
    `<local-parent>/<item>/` as the target — the "obvious" symmetric choice with `pget` —
    produces a doubly-nested `<item>/<item>/...` tree instead. The caller (`core/queue.py`) is
    responsible for passing the *parent* for a `mirror` job.
    """
    if kind == "pget":
        cmd = f"pget -c -n {pget_n} {_lftp_quote(remote_path)} -o {_lftp_quote(local_path)}"
    else:
        local_dir = local_path if local_path.endswith("/") else local_path + "/"
        parts = ["mirror", "-c", f"--parallel={parallel}", f"--use-pget-n={pget_n}"]
        for glob in exclude_globs:
            parts.append(f"--exclude-glob {_lftp_quote(glob)}")
        parts += [_lftp_quote(remote_path), _lftp_quote(local_dir)]
        cmd = " ".join(parts)
    return cmd


@dataclass(frozen=True)
class JobSpec:
    job_id: int
    kind: JobKind
    creds: HostCreds
    remote_path: str
    local_path: str
    rate_limit_bps: int | None = None
    connection_limit: int | None = None
    parallel: int = 1
    pget_n: int = 1
    exclude_globs: tuple[str, ...] = field(default_factory=tuple)
    extra_settings: str = ""
    save_status_interval_s: int = 1
    run_dir: str = DEFAULT_RUN_DIR


@dataclass
class SpawnedJob:
    """A live subprocess handle plus the paths that must be cleaned up when it exits."""

    proc: asyncio.subprocess.Process
    pid: int
    rc_path: Path
    known_hosts_path: Path | None
    _stdout_buf: bytearray = field(default_factory=bytearray)
    _stderr_buf: bytearray = field(default_factory=bytearray)

    def cleanup(self) -> None:
        """Unlink the credential-bearing files. Idempotent, never raises — a missing file
        (already cleaned up, or a run-dir that vanished) is not an error at this point.
        """
        for path in (self.rc_path, self.known_hosts_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("job %s: could not unlink %s", self.pid, path)


def _write_secret_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create with a restrictive mode from the start rather than chmod-after-write, so the
    # content is never briefly world/group readable on a filesystem that honors umask loosely.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    path.chmod(stat_module.S_IRUSR | stat_module.S_IWUSR)


def _known_hosts_line(creds: HostCreds) -> str | None:
    """OpenSSH `known_hosts` format is `[host]:port <exported-key-line>` for a non-default
    port, `host <exported-key-line>` for port 22. `pinned_host_key` is already the
    `<type> <base64>` portion (what `KnownHostsStore`/asyncssh's `export_public_key` produce).
    """
    if not creds.pinned_host_key:
        return None
    host_field = creds.address if creds.port == 22 else f"[{creds.address}]:{creds.port}"
    return f"{host_field} {creds.pinned_host_key}\n"


async def spawn(spec: JobSpec, *, lftp_bin: str = "lftp") -> SpawnedJob:
    """Build the rc file (+ known_hosts pin, if any), build the `-c` command, and exec lftp
    with **pipes for stdin/stdout/stderr — never a PTY** (DESIGN.md §4.1): with stdin not a
    tty, lftp disables readline, so none of §1.2's escape/wrapping problems can occur.
    """
    run_dir = Path(spec.run_dir)
    rc_path = run_dir / f"job-{spec.job_id}.rc"
    known_hosts_path: Path | None = None

    kh_line = _known_hosts_line(spec.creds)
    if kh_line is not None:
        known_hosts_path = run_dir / f"job-{spec.job_id}.known_hosts"
        _write_secret_file(known_hosts_path, kh_line)

    rc_text = build_rc_text(
        spec.creds,
        known_hosts_path,
        rate_limit_bps=spec.rate_limit_bps,
        connection_limit=spec.connection_limit,
        parallel=spec.parallel,
        pget_n=spec.pget_n,
        save_status_interval_s=spec.save_status_interval_s,
        extra_settings=spec.extra_settings,
    )
    _write_secret_file(rc_path, rc_text)

    transfer_cmd = build_transfer_command(
        spec.kind,
        spec.remote_path,
        spec.local_path,
        parallel=spec.parallel,
        pget_n=spec.pget_n,
        exclude_globs=spec.exclude_globs,
    )
    script = f"source {_lftp_quote(str(rc_path))}; set cmd:fail-exit true; {transfer_cmd}"

    try:
        proc = await asyncio.create_subprocess_exec(
            lftp_bin,
            "-c",
            script,
            stdin=asyncio.subprocess.DEVNULL,  # never a PTY, never fed anything on the happy path
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # one combined stream is enough for classification
        )
    except BaseException:
        rc_path.unlink(missing_ok=True)
        if known_hosts_path is not None:
            known_hosts_path.unlink(missing_ok=True)
        raise

    assert proc.pid is not None
    return SpawnedJob(proc=proc, pid=proc.pid, rc_path=rc_path, known_hosts_path=known_hosts_path)


async def wait_and_capture(job: SpawnedJob) -> tuple[int, str]:
    """Wait for exit, capturing at most the last `OUTPUT_TAIL_BYTES` of combined output —
    never held in full for a long-running transfer (DESIGN.md §1.3: lftp's chatter is never a
    source of progress, and there's no reason to buffer more of it than the failure tail needs).
    """
    assert job.proc.stdout is not None
    tail = bytearray()
    while True:
        chunk = await job.proc.stdout.read(65536)
        if not chunk:
            break
        tail += chunk
        if len(tail) > OUTPUT_TAIL_BYTES:
            del tail[: len(tail) - OUTPUT_TAIL_BYTES]
    exit_code = await job.proc.wait()
    return exit_code, tail.decode("utf-8", errors="replace")


async def terminate(job: SpawnedJob, *, grace_s: float = 10.0) -> None:
    """SIGTERM, then SIGKILL after a grace period (DESIGN.md §4.6). SIGTERM (not SIGKILL) is
    what lets lftp flush its `.lftp-pget-status` sidecar on the way out, so the partial file
    stays resumable rather than needing a full restart. SIGKILL only fires if lftp is still
    alive after the grace window — a wedged connect-program, a stuck NFS write, etc.
    """
    if job.proc.returncode is not None:
        return
    job.proc.terminate()
    try:
        await asyncio.wait_for(job.proc.wait(), timeout=grace_s)
    except TimeoutError:
        logger.warning(
            "job pid %s did not exit within %.0fs of SIGTERM; sending SIGKILL", job.pid, grace_s
        )
        job.proc.kill()
        await job.proc.wait()
