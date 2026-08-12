"""Remote scanning, connection testing, and (later) remote deletes over one pooled asyncssh
connection (DESIGN.md §5).

Two scan paths:

- **Primary** — one round trip, nothing deployed: ``find <path> -mindepth 1 -printf
  '%y\\t%s\\t%T@\\t%p\\n'``. Directory sizes are *not* taken from this output (a directory's
  own `%s` is meaningless — filesystem block size, not content); the reconciler sums child
  sizes instead, for both trees, the same way.
- **Fallback** — Alpine/BSD busybox `find` doesn't support `-printf`. `remote_agent/scan_fs.py`
  (stdlib only) is uploaded over SFTP and run with the remote `python3`, emitting the
  identical record format, so one parser (`parse_find_records`) serves both paths.

Every path — from a `find` process or from the fallback script — is parsed defensively:
`surrogateescape` end to end, and a record parser that anchors on the fixed-width header
(`<type>\\t<size>\\t<mtime>\\t`) rather than naive line-splitting, because a path can itself
contain a tab or a raw newline (DESIGN.md §15.10). See `parse_find_records` for the one place
this doesn't fully resolve: the wire format DESIGN.md §5 specifies is itself newline-terminated,
so a path containing the *exact* bytes of the next record's header would misparse. That is a
property of the specified command, not of this parser, and is called out in the phase 2 report
rather than silently patched by deviating from the specified `find` invocation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import socket
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncssh

logger = logging.getLogger(__name__)

# asyncssh.connect() unconditionally calls getpass.getuser() early in connection setup —
# for SSH-config username templating (~/.ssh/config's `%u`), independent of the `username=`
# kwarg we always pass. Found running lftpweb in its own container (DESIGN.md §11.2's numeric
# PUID/PGID identity, deliberately with no /etc/passwd entry): getpass.getuser() falls through
# to pwd.getpwuid(), which raises KeyError for an unregistered uid — and on Python 3.13,
# getpass.getuser() itself catches that KeyError and re-raises OSError("No username set in the
# environment"), which asyncssh's own `except KeyError:` around the call does *not* catch. The
# result: every asyncssh connection fails outright, for any auth method, whenever the process
# uid has no passwd entry and none of LOGNAME/USER/LNAME/USERNAME is set — exactly the
# convention this project already committed to for both the PUID/PGID entrypoint and compose's
# native `user:` (§11.2, docs/decisions.md). Setting one harmless fallback env var (only if
# none is already set — never overriding a real one) sidesteps `pwd` entirely, since
# `getpass.getuser()` checks the environment first.


def _ensure_local_username_env() -> None:
    """Idempotent; safe to call more than once. Split out from module-import-time so a test
    can exercise it directly against a scrubbed `os.environ` without relying on import order.
    """
    if not any(os.environ.get(name) for name in ("LOGNAME", "USER", "LNAME", "USERNAME")):
        os.environ["LOGNAME"] = "lftpweb"


_ensure_local_username_env()

SCAN_FS_PATH = Path(__file__).resolve().parent.parent / "remote_agent" / "scan_fs.py"

DEFAULT_SCAN_INTERVAL_S = 30.0
DEFAULT_CONNECT_TIMEOUT_S = 15.0

_FILE_TYPES = {"f"}
_DIR_TYPES = {"d"}

# Anchors on the fixed-width header GNU find / scan_fs.py both emit at the start of a
# record. Everything from the end of the header to the start of the *next* header (or end
# of output) is the path, so a path containing a literal tab or newline doesn't split the
# record apart the way naive `line.split('\t')` / `text.splitlines()` would.
_RECORD_RE = re.compile(r"(?m)^(?P<type>[a-zA-Z])\t(?P<size>-?\d+)\t(?P<mtime>-?\d+(?:\.\d+)?)\t")

_UNSUPPORTED_PRINTF_RE = re.compile(
    rb"unrecognized|invalid option|bad option|unknown option|unknown predicate", re.IGNORECASE
)


class RemoteScanError(Exception):
    """A scan could not be completed. Caught by the engine so one bad scan doesn't take the
    process down; the previous model is kept and the failure is logged/eventable.
    """


class RemoteDeleteError(Exception):
    """A remote delete (DESIGN.md §5, §7.4) could not be completed. Never silently swallowed
    by the caller -- `core/postprocess.py` records it as an `event` row and leaves the
    item's `remote_deleted_at` unset, exactly like a withheld delete.
    """


class DecryptionNeededError(Exception):
    """Raised when a host's password cannot be decrypted (DESIGN.md §8's "credentials need
    re-entry" state). Caught by the caller, not retried automatically.
    """


@dataclass(frozen=True)
class HostConfig:
    """Connection parameters for the seedbox. Mirrors the `host` table (DESIGN.md §3.1); the
    caller is responsible for decrypting `password` before constructing this (never stored
    encrypted in memory longer than needed, never logged — see `logsetup.CredentialRedactor`).
    """

    id: int
    address: str
    port: int
    username: str
    auth_method: str  # 'key' | 'agent' | 'password'
    key_path: str | None = None
    password: str | None = None
    known_hosts_policy: str = "accept-and-pin"  # 'accept-and-pin' | 'strict' | 'insecure'
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S


@dataclass(frozen=True)
class RemoteRecord:
    """One raw parsed line from `find -printf` / `scan_fs.py`, before root-relativization."""

    type_char: str
    size: int
    mtime: float
    path: str


@dataclass(frozen=True)
class RemoteEntry:
    """One remote filesystem entry, keyed by `rel_path` (POSIX-style, relative to the queue's
    `remote_path`). `size` is the file's own size; always 0 for directories — see module
    docstring on why directory totals are computed by the reconciler instead.
    """

    rel_path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


def parse_find_records(raw: str) -> list[RemoteRecord]:
    """Parse the `find -printf` / `scan_fs.py` wire format into records.

    `raw` must already be decoded with `errors="surrogateescape"` so odd bytes survive as
    lone surrogates rather than raising here.
    """
    matches = list(_RECORD_RE.finditer(raw))
    records: list[RemoteRecord] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        path = raw[start:end]
        if path.endswith("\n"):
            path = path[:-1]
        if not path:
            continue
        records.append(
            RemoteRecord(
                type_char=m.group("type"),
                size=int(m.group("size")),
                mtime=float(m.group("mtime")),
                path=path,
            )
        )
    return records


def records_to_entries(records: list[RemoteRecord], root: str) -> dict[str, RemoteEntry]:
    """Root-relativize records and drop types we don't model (symlinks, devices, fifos —
    none of which occur in a seedbox pickup directory in practice; skipped rather than
    guessed at, and logged once per scan at debug level).
    """
    root_norm = root.rstrip("/")
    prefix = root_norm + "/"
    entries: dict[str, RemoteEntry] = {}
    skipped = 0
    for rec in records:
        if rec.path == root_norm:
            continue  # shouldn't occur with -mindepth 1, but be defensive
        if rec.path.startswith(prefix):
            rel_path = rec.path[len(prefix) :]
        else:
            # Fallback script or an unusual find invocation returned a path that doesn't
            # share the expected root prefix. Keep the record under its own path rather
            # than dropping it silently — a filename must never crash a scan.
            rel_path = rec.path.lstrip("/")

        if rec.type_char in _DIR_TYPES:
            entries[rel_path] = RemoteEntry(rel_path=rel_path, is_dir=True, size=0, mtime=rec.mtime)
        elif rec.type_char in _FILE_TYPES:
            entries[rel_path] = RemoteEntry(
                rel_path=rel_path, is_dir=False, size=rec.size, mtime=rec.mtime
            )
        else:
            skipped += 1
    if skipped:
        logger.debug("remote scan of %s: skipped %d non-file/dir entries", root, skipped)
    return entries


def _summarize_find_stderr(stderr_text: str) -> str:
    """Turn `find`'s stderr (one or more `find: 'PATH': REASON` lines, GNU's own format) into
    a short, queue-level warning string — good enough to show inline in the UI without
    dumping raw stderr. Falls back to the trimmed raw text for a shape `find` has never
    actually produced during development, rather than hiding it.
    """
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    if not lines:
        return "some paths on the remote could not be scanned"
    if len(lines) == 1:
        return f"1 path skipped (could not be read): {lines[0]}"
    shown = "; ".join(lines[:5])
    more = f" (+{len(lines) - 5} more)" if len(lines) > 5 else ""
    return f"{len(lines)} paths skipped (could not be read): {shown}{more}"


@dataclass(frozen=True)
class PrimaryScanOutcome:
    """What `find -printf` produced, already classified (DESIGN.md §5). Split out from
    `_run_primary` so the exit-code-vs-partial-failure decision is unit-testable without a
    live SSH connection, the same way `parse_find_records` is testable without one.

    `raw` is `None` only when `-printf` itself is unsupported (busybox/BSD `find`) — the
    fallback-trigger signal `scan()` already handled before this existed. `warning` is set
    when the scan covered *most* of the tree but GNU find's own nonzero exit means at least
    one subtree could not be read.
    """

    raw: str | None
    warning: str | None


def interpret_primary_scan_result(
    exit_status: int, stdout: bytes, stderr: bytes
) -> PrimaryScanOutcome:
    """Classify one `find -printf` invocation's result (DESIGN.md §5).

    **The bug this exists to fix** (found live in phase 3, recorded in docs/decisions.md,
    fixed here in phase 3b): GNU `find` exits nonzero the moment it can't stat/read *one*
    subdirectory or file anywhere in the tree — even though it already printed every record
    it *could* reach to stdout, and kept scanning everything else. The previous
    implementation treated any nonzero exit that wasn't the "-printf unsupported" signature
    as a hard failure, discarding the entire queue's tree over one unreadable subtree — one
    permission-denied folder on the seedbox turned into "no folder renders at all," with no
    indication why. A truly failed scan (bad path, root itself unreadable) still produces no
    stdout at all and is raised as before; the distinguishing signal is whether `find`
    produced *any* usable output before it hit the error.
    """
    text = stdout.decode("utf-8", errors="surrogateescape")
    if exit_status == 0:
        return PrimaryScanOutcome(raw=text, warning=None)
    if _UNSUPPORTED_PRINTF_RE.search(stderr):
        return PrimaryScanOutcome(raw=None, warning=None)

    err_text = stderr.decode("utf-8", errors="surrogateescape")
    if text.strip():
        return PrimaryScanOutcome(raw=text, warning=_summarize_find_stderr(err_text))

    raise RemoteScanError(f"find failed (exit {exit_status}): {err_text.strip()}")


@dataclass
class TestConnectionResult:
    ok: bool
    error_class: str | None  # None when ok
    message: str


def _classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map a connect-time exception to a (error_class, message) pair useful to a human —
    auth vs. DNS vs. refused vs. timeout, not a boolean (per the phase 2 prompt).
    """
    if isinstance(exc, socket.gaierror):
        return "DNS_ERROR", f"could not resolve host: {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return "CONNECTION_REFUSED", f"connection refused: {exc}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "TIMEOUT", "connection timed out"
    if isinstance(exc, asyncssh.PermissionDenied):
        return "AUTH_FAILED", f"authentication failed: {exc}"
    if isinstance(exc, HostKeyMismatchError):
        return "HOST_KEY_MISMATCH", str(exc)
    if isinstance(exc, HostKeyUnknownError):
        return "HOST_KEY_UNKNOWN", str(exc)
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return "HOST_KEY_MISMATCH", f"host key could not be verified: {exc}"
    if isinstance(exc, asyncssh.ChannelOpenError):
        return "UNKNOWN", f"channel open failed: {exc}"
    if isinstance(exc, OSError):
        return "UNKNOWN", f"connection error: {exc}"
    return "UNKNOWN", str(exc)


class HostKeyMismatchError(Exception):
    """The server presented a host key that does not match the one previously pinned. Under
    every known_hosts_policy this refuses the connection — accept-and-pin means trust-on-
    *first*-use, not trust-always.
    """


class HostKeyUnknownError(Exception):
    """`known_hosts_policy = 'strict'` and no key has ever been pinned for this host."""


class KnownHostsStore:
    """A tiny JSON-backed pin store — deliberately not OpenSSH's `known_hosts` file format,
    because we need exactly one policy decision (`validate_host_public_key` below) rather
    than a general-purpose matcher, and owning the format means the accept-and-pin logic
    doesn't have to reverse-engineer asyncssh's own known_hosts matching.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))
        self._path.chmod(stat_module.S_IRUSR | stat_module.S_IWUSR)

    @staticmethod
    def _key(host: str, port: int) -> str:
        return f"{host}:{port}"

    def get(self, host: str, port: int) -> str | None:
        return self._load().get(self._key(host, port))

    def pin(self, host: str, port: int, exported_key: str) -> None:
        data = self._load()
        data[self._key(host, port)] = exported_key
        self._save(data)


def _make_client_factory(
    policy: str, store: KnownHostsStore, result_holder: dict
) -> type[asyncssh.SSHClient]:
    """Build an `SSHClient` subclass implementing `known_hosts_policy` via
    `validate_host_public_key` (DESIGN.md §5, §8) — see client.py's own docs: returning
    `True` here means "trust this key"; AsyncSSH then verifies the server actually holds
    the matching private key before the connection proceeds, so this can't be spoofed by a
    server merely claiming a key we'd accept.

    Deliberately returns `False` rather than raising a custom exception on rejection:
    asyncssh's `_validate_host_key` only translates a *`ValueError`* (its own, on a `False`
    return) into the catchable `HostKeyNotVerifiable` — anything else raised from inside this
    callback propagates through asyncssh's key-exchange internals uncaught, which is not a
    safe place to introduce a new exception type. `result_holder` is how the *reason*
    (mismatch vs. never-pinned) escapes anyway, for `_connect()` to turn into a specific
    error after catching `HostKeyNotVerifiable`.
    """

    class PinningSSHClient(asyncssh.SSHClient):
        def validate_host_public_key(
            self, host: str, addr: str, port: int, key: asyncssh.SSHKey
        ) -> bool:
            # 'insecure' means exactly that — never consult or update the pin store, trust
            # whatever key is presented every time. Checked first and unconditionally, so a
            # pin recorded under a *different* policy earlier can't leak into an "insecure"
            # host's behavior (e.g. rejecting it as a "mismatch" would be exactly backwards).
            if policy == "insecure":
                return True

            exported = key.export_public_key("openssh").decode("ascii").strip()
            pinned = store.get(host, port)
            fingerprint = key.get_fingerprint()

            if pinned is not None:
                if pinned == exported:
                    return True
                result_holder["reason"] = "mismatch"
                result_holder["fingerprint"] = fingerprint
                return False

            if policy == "strict":
                result_holder["reason"] = "unknown"
                result_holder["fingerprint"] = fingerprint
                return False
            # accept-and-pin (default): a seedbox's host key is not knowable in advance, so
            # trust it on this first connection and pin it for every connection after.
            store.pin(host, port, exported)
            logger.info("pinned new host key for %s:%s (fingerprint %s)", host, port, fingerprint)
            return True

    return PinningSSHClient


class RemoteConnectionPool:
    """Owns exactly one reused asyncssh connection, serving scanning, *Test connection*, and
    (later) remote deletes — DESIGN.md §5: "the same connection serves scanning, Test
    connection, and remote deletes."
    """

    def __init__(self, known_hosts_dir: Path) -> None:
        self._known_hosts_dir = known_hosts_dir
        self._lock = asyncio.Lock()
        self._conn: asyncssh.SSHClientConnection | None = None
        self._host: HostConfig | None = None
        self._supports_printf: bool | None = None

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await self._conn.wait_closed()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            self._conn = None
        self._host = None
        self._supports_printf = None

    def _store_for(self, host: HostConfig) -> KnownHostsStore:
        return KnownHostsStore(self._known_hosts_dir / "known_hosts.json")

    async def _connect(self, host: HostConfig) -> asyncssh.SSHClientConnection:
        result_holder: dict = {}
        client_factory = _make_client_factory(
            host.known_hosts_policy, self._store_for(host), result_holder
        )
        kwargs: dict = {
            "host": host.address,
            "port": host.port,
            "username": host.username,
            # An *empty* SSHKnownHosts object, not None: asyncssh's own host-key checking
            # is bypassed either way, but passing None short-circuits validate_host_public_key
            # entirely (asyncssh treats known_hosts=None as "trust unconditionally" and never
            # calls the callback below) — verified against the fake seedbox while building
            # this. An empty (non-falsy) SSHKnownHosts has zero trusted keys, so asyncssh
            # always defers to our callback, which is where known_hosts_policy is enforced.
            "known_hosts": asyncssh.SSHKnownHosts(),
            "client_factory": client_factory,
            "connect_timeout": host.connect_timeout,
        }
        if host.auth_method == "key":
            if not host.key_path:
                raise ValueError("auth_method 'key' requires key_path")
            kwargs["client_keys"] = [host.key_path]
            kwargs["password"] = None
        elif host.auth_method == "agent":
            kwargs["agent_path"] = None  # use SSH_AUTH_SOCK / platform default
        elif host.auth_method == "password":
            if host.password is None:
                raise DecryptionNeededError("host password is not available (decryption failed)")
            kwargs["password"] = host.password
            kwargs["client_keys"] = []
            kwargs["preferred_auth"] = "password"
        else:
            raise ValueError(f"unknown auth_method: {host.auth_method}")

        try:
            return await asyncio.wait_for(asyncssh.connect(**kwargs), timeout=host.connect_timeout)
        except asyncssh.HostKeyNotVerifiable as exc:
            reason = result_holder.get("reason")
            fingerprint = result_holder.get("fingerprint", "unknown")
            if reason == "mismatch":
                raise HostKeyMismatchError(
                    f"host key for {host.address}:{host.port} does not match the pinned key "
                    f"(fingerprint {fingerprint}) — refusing to connect"
                ) from exc
            raise HostKeyUnknownError(
                f"no pinned host key for {host.address}:{host.port} "
                f"(fingerprint {fingerprint}) and known_hosts_policy is 'strict'"
            ) from exc

    @property
    def is_connected(self) -> bool:
        """DESIGN.md §10.3: `/api/health`'s "host reachability" -- read from the pooled
        connection this class already maintains (via the engine's own periodic scans and
        *Test connection*) rather than opening a fresh SSH connection on every health poll,
        which would make the health endpoint itself a load-bearing (and slow) network call on
        a path the UI hits continuously.
        """
        return self._conn is not None and not self._conn.is_closed()

    async def get_connection(self, host: HostConfig) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is not None and self._host == host and not self._conn.is_closed():
                return self._conn
            await self._close_locked()
            self._conn = await self._connect(host)
            self._host = host
            return self._conn

    async def test_connection(self, host: HostConfig) -> TestConnectionResult:
        try:
            conn = await self.get_connection(host)
            await conn.run("true", check=True, timeout=host.connect_timeout)
            return TestConnectionResult(ok=True, error_class=None, message="connected")
        except Exception as exc:  # noqa: BLE001 - classified below for the caller
            error_class, message = _classify_exception(exc)
            logger.warning(
                "test_connection to %s:%s failed: %s (%s)",
                host.address,
                host.port,
                message,
                error_class,
            )
            return TestConnectionResult(ok=False, error_class=error_class, message=message)

    async def scan(
        self, host: HostConfig, remote_path: str
    ) -> tuple[dict[str, RemoteEntry], str | None]:
        """Scan `remote_path` on `host`, trying the primary `find -printf` path first and
        falling back to the uploaded stdlib scanner only when the primary path is detected
        (not assumed) to be unsupported. The detection result is cached per connection so a
        busybox seedbox doesn't retry the failing primary command on every scan.

        Returns `(entries, warning)` — `warning` is a human-readable, queue-level note
        (DESIGN.md §5) when the primary path skipped one or more unreadable subtrees rather
        than failing outright (`interpret_primary_scan_result`); `None` otherwise.
        """
        conn = await self.get_connection(host)

        if self._supports_printf is not False:
            raw, warning = await self._run_primary(conn, remote_path)
            if raw is not None:
                self._supports_printf = True
                records = parse_find_records(raw)
                return records_to_entries(records, remote_path), warning
            self._supports_printf = False
            logger.info(
                "find -printf unsupported on %s:%s; falling back to remote_agent/scan_fs.py",
                host.address,
                host.port,
            )

        raw = await self._run_fallback(conn, remote_path)
        records = parse_find_records(raw)
        return records_to_entries(records, remote_path), None

    async def _run_primary(
        self, conn: asyncssh.SSHClientConnection, remote_path: str
    ) -> tuple[str | None, str | None]:
        cmd = f"find {shlex.quote(remote_path)} -mindepth 1 -printf '%y\\t%s\\t%T@\\t%p\\n'"
        result = await conn.run(cmd, check=False, encoding=None)
        stdout = result.stdout if isinstance(result.stdout, (bytes, bytearray)) else b""
        stderr = result.stderr if isinstance(result.stderr, (bytes, bytearray)) else b""
        outcome = interpret_primary_scan_result(result.exit_status, stdout, stderr)
        if outcome.warning:
            logger.warning("scan of %s: %s", remote_path, outcome.warning)
        return outcome.raw, outcome.warning

    async def delete_path(self, host: HostConfig, remote_path: str) -> None:
        """Remove a remote file or directory tree (DESIGN.md §5, §7.4) over this same pooled
        asyncssh connection -- **never** lftp's `mirror --Remove-source-files`. §7.4 gives the
        full reasoning; the short version is that this is the one place deletion happens, so
        it is the one place verification gates it and the one place an `event` row is
        guaranteed.

        Issued as a shell `rm -rf --` over `conn.run`, the identical mechanism
        `_run_primary`/`_run_fallback` already use for scanning -- not asyncssh's SFTP
        protocol layer, which has no single call for "remove a possibly-non-empty directory
        tree" and would need this module to reimplement recursive removal by hand. `--` stops
        a path that happens to start with `-` from being read as a flag. Idempotent: a path
        that is already gone is not an error (`rm -rf` never fails on a missing target).

        Deliberately refuses an empty or root-looking path rather than ever asking the remote
        shell to `rm -rf` something that could expand to "everything" -- the caller
        (`core/postprocess.py`) always passes `<queue.remote_path>/<item.rel_path>` with a
        non-empty `rel_path`, so this is defense in depth, not the primary safeguard.
        """
        stripped = remote_path.strip()
        if not stripped or stripped in ("/", ".", ".."):
            raise ValueError(
                f"refusing to delete an empty or root-looking remote path: {remote_path!r}"
            )

        conn = await self.get_connection(host)
        result = await conn.run(f"rm -rf -- {shlex.quote(stripped)}", check=False, encoding=None)
        if result.exit_status != 0:
            stderr = result.stderr if isinstance(result.stderr, (bytes, bytearray)) else b""
            raise RemoteDeleteError(
                f"remote delete of {stripped!r} failed (exit {result.exit_status}): "
                f"{stderr.decode('utf-8', errors='surrogateescape').strip()}"
            )

    async def _run_fallback(self, conn: asyncssh.SSHClientConnection, remote_path: str) -> str:
        remote_tmp = f"/tmp/.lftpweb_scan_fs_{uuid4().hex}.py"
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(SCAN_FS_PATH), remote_tmp)
            cmd = f"python3 {shlex.quote(remote_tmp)} {shlex.quote(remote_path)}"
            result = await conn.run(cmd, check=False, encoding=None)
            stdout = result.stdout if isinstance(result.stdout, (bytes, bytearray)) else b""
            stderr = result.stderr if isinstance(result.stderr, (bytes, bytearray)) else b""
            if result.exit_status != 0:
                raise RemoteScanError(
                    f"fallback scan_fs.py failed on {remote_path} (exit {result.exit_status}): "
                    f"{stderr.decode('utf-8', errors='surrogateescape').strip()}"
                )
            return stdout.decode("utf-8", errors="surrogateescape")
        finally:
            await conn.run(f"rm -f {shlex.quote(remote_tmp)}", check=False)
