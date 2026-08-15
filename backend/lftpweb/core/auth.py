"""Auth (DESIGN.md §8, phase 8): the three modes, sessions, API keys, rate limiting.

Pure-ish logic lives here; `backend/lftpweb/middleware.py` is the thin ASGI glue that calls
into it on every request, and `api/auth.py` is the thin HTTP glue for login/logout/settings —
the same "one evaluator, thin callers" shape every earlier phase used
(`core/patterns.py`, `core/mount_sentinel.py`, `core/verify.py`).

**AUTH_MODE defaults to `none`.** `AuthSettings()` with no stored row (a fresh install, or an
existing install that has never visited Settings → Auth) is `mode="none"` — this phase's own
non-negotiable, verified by
`tests/test_auth_api.py::test_default_mode_is_none_every_endpoint_behaves_as_before`.

**Two independent, exercised lockout-recovery routes** (see README.md's "Locked out?"
section and docs/decisions.md):

1. **`LFTPWEB_AUTH_MODE`** (`config.py`) — an env var, checked by `effective_mode()`, that
   overrides whatever is stored regardless of database state. Set it to `none`, restart the
   container, fix whatever is wrong from an unauthenticated session, then unset it.
2. **Deleting the `auth_user` row** — `resolve_password_mode_gate()` treats `mode="password"`
   with no user row as open access rather than "reject everyone forever." An operator with
   shell/DB access but no ability (or wish) to restart the container can
   `sqlite3 /config/lftpweb.db "DELETE FROM auth_user"` and immediately regain access, then
   create a fresh user via Settings → Auth. The alternative — refusing every request until a
   password nobody can supply is re-entered — is the exact lockout this phase exists to
   prevent.

Both routes are only reachable for `password` mode, which is the only mode capable of ever
producing a "nobody can log in" state. `proxy` mode's failure-to-authenticate case (no
trusted CIDR, or a proxy not forwarding the identity header) is recovered the same way as
any other misconfiguration: route 1, the env var override — there is no analogous "delete a
row" shortcut for it because there is no row whose absence should open the gate (an empty
`proxy_trusted_cidrs` list is refused at write time, not silently treated as "trust
everyone").
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

SETTING_KEY = "auth_settings"
DEFAULT_PROXY_HEADER = "Remote-User"
SESSION_COOKIE_NAME = "lftpweb_session"
SESSION_TTL_S = 14 * 24 * 3600  # 14 days -- long enough that a homelab user isn't re-logging
# in constantly, short enough that a stolen cookie doesn't work forever. Not specified by
# DESIGN.md; the smallest reasonable call (docs/decisions.md).

# argon2-cffi's PasswordHasher defaults to Argon2id (confirmed: `PasswordHasher().type ==
# Type.ID`, and every hash it produces starts with `$argon2id$`) -- DESIGN.md §8 asks for
# argon2id specifically, and the phase 8 prompt asks to prove it's not silently falling back
# to argon2i/2d. `tests/test_auth.py::test_password_hash_is_argon2id` pins the literal prefix
# rather than trusting the library's default not to change.
_hasher = PasswordHasher()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash_token(token: str) -> str:
    """SHA-256, not argon2 -- these tokens (session cookies, API keys) are 256 bits of
    `secrets.token_urlsafe` randomness, not a human-chosen low-entropy password. Argon2's
    memory-hard slowness defends against *guessing* a low-entropy secret; a random 256-bit
    token cannot be brute-forced regardless of hash speed, so a fast digest is the right tool
    and lets a busy install validate an API key on every request without a deliberately slow
    KDF in the hot path. See docs/decisions.md -- flagged as a deliberate scope difference
    from "argon2id" rather than left to look like an oversight.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Settings (JSON in `setting`, the same pattern every other *Settings dataclass uses:
# core/queue.py.TransferSettings, core/postprocess.py.PostprocessSettings,
# core/backup.py.BackupSettings) -------------------------------------------------------


@dataclass(frozen=True)
class AuthSettings:
    mode: str = "none"  # 'none' | 'password' | 'proxy'
    proxy_header: str = DEFAULT_PROXY_HEADER
    proxy_trusted_cidrs: tuple[str, ...] = ()


async def load_auth_settings(db: aiosqlite.Connection) -> AuthSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return AuthSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return AuthSettings()
    return AuthSettings(
        mode=data.get("mode", "none"),
        proxy_header=data.get("proxy_header") or DEFAULT_PROXY_HEADER,
        proxy_trusted_cidrs=tuple(data.get("proxy_trusted_cidrs", [])),
    )


async def save_auth_settings(db: aiosqlite.Connection, settings: AuthSettings) -> None:
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES "
        "(?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (
            SETTING_KEY,
            json.dumps(
                {
                    "mode": settings.mode,
                    "proxy_header": settings.proxy_header,
                    "proxy_trusted_cidrs": list(settings.proxy_trusted_cidrs),
                }
            ),
        ),
    )
    await db.commit()


def effective_mode(stored: AuthSettings, env_override: str | None) -> str:
    """The mode actually enforced, after the lockout-recovery override (module docstring,
    route 1). `env_override` is `config.settings.auth_mode` (`LFTPWEB_AUTH_MODE`) -- `None`
    (unset, the default) means "use whatever is stored," which itself defaults to `none`.
    """
    if env_override is not None:
        return env_override
    return stored.mode


# --- Passwords (argon2id) ---------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False
    return True


# --- The single local user (`auth_user`, id=1) -------------------------------------------


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    password_hash: str


async def get_user(db: aiosqlite.Connection) -> AuthUser | None:
    cursor = await db.execute("SELECT id, username, password_hash FROM auth_user WHERE id = 1")
    row = await cursor.fetchone()
    if row is None:
        return None
    return AuthUser(id=row["id"], username=row["username"], password_hash=row["password_hash"])


async def set_user_password(db: aiosqlite.Connection, username: str, password: str) -> None:
    """Create the single user row, or update it in place (new username and/or password) --
    one call either way, so `api/auth.py` never has to branch on "does a user already exist."
    """
    password_hash = hash_password(password)
    await db.execute(
        "INSERT INTO auth_user (id, username, password_hash) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO UPDATE SET username = excluded.username, "
        "password_hash = excluded.password_hash, "
        "updated_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')",
        (username, password_hash),
    )
    await db.commit()


async def delete_user(db: aiosqlite.Connection) -> None:
    """Lockout-recovery route 2 (module docstring) -- an operator with DB access drops
    `password` mode back to open access by removing this one row, without touching the
    container's environment or restarting it.
    """
    await db.execute("DELETE FROM auth_user WHERE id = 1")
    await db.commit()


def resolve_password_mode_gate(user: AuthUser | None) -> bool:
    """Whether `password` mode is actually enforceable right now. `False` (meaning: don't
    enforce -- let the request through) when no user row exists. See the module docstring's
    "route 2": the alternative (refuse every request when `mode == "password"` and no user
    exists) turns a five-second `DELETE FROM auth_user` into a bricked instance that still
    demands a password nobody can ever supply -- worse than the brief open-access window this
    produces, and an operator who can run that DELETE already has full control of the
    database, so nothing is being given away that direct DB access didn't already grant.
    """
    return user is not None


# --- Sessions (DESIGN.md §8: "HTTP-only SameSite=Lax session cookie") --------------------


@dataclass(frozen=True)
class Session:
    id: int
    csrf_token: str
    expires_at: str


async def create_session(db: aiosqlite.Connection) -> tuple[str, Session]:
    """Returns `(raw_token, Session)`. Only `_hash_token(raw_token)` is ever persisted --
    the raw token exists solely to become the cookie value and is never stored, so a stolen
    database (already missing the credential-encryption key, §10.2) doesn't also hand over
    every live session.
    """
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(UTC) + timedelta(seconds=SESSION_TTL_S)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    cursor = await db.execute(
        "INSERT INTO session (token_hash, csrf_token, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), csrf_token, expires_at),
    )
    await db.commit()
    return token, Session(id=cursor.lastrowid, csrf_token=csrf_token, expires_at=expires_at)


async def validate_session(db: aiosqlite.Connection, token: str | None) -> Session | None:
    if not token:
        return None
    cursor = await db.execute(
        "SELECT id, csrf_token, expires_at FROM session WHERE token_hash = ?",
        (_hash_token(token),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now_iso():
        # Expired: clean it up opportunistically (no separate reaper task needed for a
        # homelab-scale session table) and treat it as invalid either way.
        await db.execute("DELETE FROM session WHERE id = ?", (row["id"],))
        await db.commit()
        return None
    await db.execute(
        "UPDATE session SET last_seen_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
        (row["id"],),
    )
    await db.commit()
    return Session(id=row["id"], csrf_token=row["csrf_token"], expires_at=row["expires_at"])


async def delete_session(db: aiosqlite.Connection, token: str | None) -> None:
    if not token:
        return
    await db.execute("DELETE FROM session WHERE token_hash = ?", (_hash_token(token),))
    await db.commit()


async def purge_expired_sessions(db: aiosqlite.Connection) -> int:
    """Opportunistic cleanup (called from the login endpoint) -- there is no dedicated reaper
    task for a table sized for a single-user homelab install; a session that is never
    presented again just sits until this runs, which is harmless bloat, not a correctness
    issue (`validate_session` already refuses an expired row on its own).
    """
    cursor = await db.execute("DELETE FROM session WHERE expires_at <= ?", (_now_iso(),))
    await db.commit()
    return cursor.rowcount


async def purge_all_sessions(db: aiosqlite.Connection) -> None:
    """Called after a password change (`api/auth.py`) so a stolen-but-not-yet-used cookie
    from before the change stops working immediately, rather than staying valid until its own
    TTL expires. Every browser the user was logged in on -- including the one making the
    change -- must log in again afterwards; this is standard practice and is worth the minor
    inconvenience for a security-sensitive action.
    """
    await db.execute("DELETE FROM session")
    await db.commit()


# --- API keys (DESIGN.md §8: "X-API-Key header, accepted independently of the mode") ------


@dataclass(frozen=True)
class ApiKeyInfo:
    id: int
    name: str
    created_at: str
    last_used_at: str | None


async def create_api_key(db: aiosqlite.Connection, name: str) -> tuple[str, ApiKeyInfo]:
    """Returns `(plaintext_key, ApiKeyInfo)`. The plaintext is returned exactly once, here --
    only its SHA-256 digest is ever persisted (see `_hash_token`'s docstring for why SHA-256
    rather than argon2), so there is no code path that can show it again later.
    """
    key = secrets.token_urlsafe(32)
    cursor = await db.execute(
        "INSERT INTO api_key (name, key_hash) VALUES (?, ?)", (name, _hash_token(key))
    )
    await db.commit()
    row_cursor = await db.execute(
        "SELECT id, name, created_at, last_used_at FROM api_key WHERE id = ?",
        (cursor.lastrowid,),
    )
    row = await row_cursor.fetchone()
    info = ApiKeyInfo(
        id=row["id"], name=row["name"], created_at=row["created_at"], last_used_at=None
    )
    return key, info


async def list_api_keys(db: aiosqlite.Connection) -> list[ApiKeyInfo]:
    cursor = await db.execute("SELECT id, name, created_at, last_used_at FROM api_key ORDER BY id")
    rows = await cursor.fetchall()
    return [
        ApiKeyInfo(
            id=r["id"], name=r["name"], created_at=r["created_at"], last_used_at=r["last_used_at"]
        )
        for r in rows
    ]


async def delete_api_key(db: aiosqlite.Connection, key_id: int) -> bool:
    cursor = await db.execute("DELETE FROM api_key WHERE id = ?", (key_id,))
    await db.commit()
    return cursor.rowcount > 0


async def validate_api_key(db: aiosqlite.Connection, key: str | None) -> bool:
    if not key:
        return False
    cursor = await db.execute("SELECT id FROM api_key WHERE key_hash = ?", (_hash_token(key),))
    row = await cursor.fetchone()
    if row is None:
        return False
    await db.execute(
        "UPDATE api_key SET last_used_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
        (row["id"],),
    )
    await db.commit()
    return True


# --- Proxy mode: trusted-CIDR + identity header (DESIGN.md §8) ---------------------------


def parse_cidrs(cidrs: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Raises `ValueError` on anything that isn't a valid IPv4/IPv6 network or bare address --
    the caller (api/auth.py's settings endpoint) turns that into a 422 rather than silently
    storing a CIDR that will never match anything.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        raw = raw.strip()
        if not raw:
            continue
        networks.append(ipaddress.ip_network(raw, strict=False))
    return networks


def ip_in_trusted_cidrs(client_host: str | None, cidrs: Sequence[str]) -> bool:
    """DESIGN.md §8: "only when the request originates from a configured trusted CIDR."
    `client_host` is the direct TCP peer address (the ASGI server's own view of who opened
    the socket), never a client-supplied header like `X-Forwarded-For` -- trusting a header
    for the *address* check would let anyone spoof their way into the one gate that makes
    `proxy` mode not a bypass (DESIGN.md §8's own words). An empty `cidrs` list always
    returns `False` -- never "trust everyone" -- which is what makes "proxy mode refuses to
    enable without a trusted CIDR" hold at request time too, not just at settings-write time.
    """
    if not client_host or not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    try:
        networks = parse_cidrs(cidrs)
    except ValueError:
        logger.warning("proxy_trusted_cidrs contains an unparseable entry; denying by default")
        return False
    return any(addr in net for net in networks)


# --- Login rate limiting (DESIGN.md §8: "rate-limited login") -----------------------------


class LoginRateLimiter:
    """Per-client-IP sliding-window limiter on failed login attempts. In-memory only --
    single-process (§2), so no cross-instance coordination is needed, and a restart clearing
    every counter is an acceptable trade for the simplicity (docs/decisions.md): an attacker
    who can restart the container already has a far bigger problem to explain than a reset
    rate limit.
    """

    def __init__(self, max_failures: int = 5, window_s: float = 300.0) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._buckets: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        times = [t for t in self._buckets.get(key, []) if now - t < self.window_s]
        self._buckets[key] = times
        return times

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        times = self._prune(key, now)
        times.append(now)
        self._buckets[key] = times

    def record_success(self, key: str) -> None:
        self._buckets.pop(key, None)

    def retry_after_s(self, key: str) -> float:
        now = time.monotonic()
        times = self._prune(key, now)
        if not times:
            return 0.0
        return max(0.0, self.window_s - (now - min(times)))
