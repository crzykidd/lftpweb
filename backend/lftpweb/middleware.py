"""The auth gate (DESIGN.md §8, phase 8): one raw ASGI middleware enforcing all three modes
across every HTTP request *and* the WebSocket handshake.

**Why one middleware and not a `Depends()` on each router** -- deliberately, because the
phase 8 prompt's own framing is "a route accidentally left open is the whole failure mode
here." A per-route `Depends(require_auth)` is opt-in: a new router, or a route someone forgot
to annotate, is open by default and silent about it. This middleware is default-**deny** for
everything under `/api/` except a short, explicit allowlist -- a newly added router is
protected the moment it's mounted, with no action required, and *allowing* a route is the
thing that has to be deliberately declared (`PUBLIC_API_PATHS` below), which is much easier
to audit than proving a negative across every router file.

**Why raw ASGI and not `BaseHTTPMiddleware`** -- `BaseHTTPMiddleware` only ever sees the
"http" ASGI scope; the WebSocket handshake (`/api/ws`) needs gating too (DESIGN.md §8 says
"everything else... requires auth," and the live file/job stream is exactly the kind of
"everything else"), and `BaseHTTPMiddleware` cannot intercept it at all. A plain ASGI
middleware class (`__init__(self, app)` / `async def __call__(self, scope, receive, send)`)
handles both scope types uniformly and also avoids `BaseHTTPMiddleware`'s known issues
buffering streaming responses -- relevant here because `/api/settings/backup/*/download` and
`/api/logs/*/download` (phase 7) stream files.

**Non-`/api/` paths are never gated.** The SPA shell and static assets always load
(`main.py`'s `spa_fallback`/`StaticFiles` mount) so the login page itself can render --
DESIGN.md §8's own requirement, restated in the phase 8 prompt's "what must stay reachable."
"""

from __future__ import annotations

import json
import logging
import secrets
from http.cookies import SimpleCookie

from starlette.datastructures import Headers
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from lftpweb.config import settings as app_settings
from lftpweb.core import auth, netguard

logger = logging.getLogger(__name__)

# Audit batch C3: the custom header the shipped UI sends on every request
# (`frontend/src/api/client.ts`). Its *presence* on a mutating request is proof of a same-origin
# caller: a browser will not send a non-CORS-safelisted header cross-origin without a successful
# preflight, and lftpweb answers none (no CORS middleware). It is never *required* -- scripts
# and integrations send nothing -- see `RequestGuardMiddleware`.
REQUIRED_HEADER = "X-Requested-With"

# Exact (method, path) pairs reachable with no authentication, in every mode. Kept minimal
# and reviewed here rather than via a prefix wildcard, since a broad prefix is exactly the
# kind of thing that quietly grows to cover more than intended.
PUBLIC_API_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        # DESIGN.md §10.3 / the container HEALTHCHECK (docker/Dockerfile) -- must stay
        # reachable unauthenticated in every mode or the container is permanently unhealthy.
        ("GET", "/api/health"),
        # The login flow itself, and "am I logged in" for the SPA to decide whether to show
        # the login form. Logout is intentionally public too: clearing an already-invalid or
        # absent session must never itself require a valid session.
        ("POST", "/api/auth/login"),
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/logout"),
    }
)

# Audit batch C5 (F11): FastAPI's generated documentation. Gated exactly like `/api/` (every auth
# mode's own rule applies), never removed -- someone logged in who uses them keeps access.
DOCS_PATHS: frozenset[str] = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)

# ASGI scope key holding the *socket* peer (the proxy, when `LFTPWEB_TRUSTED_PROXIES` rewrote
# `scope["client"]` to the forwarded client). Proxy auth mode must judge the peer, not the
# client, or setting the env var would silently change which requests count as "from the proxy".
PEER_SCOPE_KEY = "lftpweb_peer"


def peer_host(scope) -> str | None:
    """The address of whoever opened the TCP connection, whether or not forwarded headers were
    honoured for the client address."""
    if PEER_SCOPE_KEY in scope:
        return scope[PEER_SCOPE_KEY]
    client = scope.get("client")
    return client[0] if client else None


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _get_cookie(headers: Headers, name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        jar: SimpleCookie = SimpleCookie()
        jar.load(raw)
    except Exception:  # noqa: BLE001 - a malformed Cookie header must never crash a request
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


async def _send_json(send, status_code: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class TrustedProxyMiddleware:
    """Audit batch C5 (F9, F10): honour `X-Forwarded-For` / `X-Forwarded-Proto`, but only from
    the peers listed in `LFTPWEB_TRUSTED_PROXIES`, by delegating to uvicorn's own
    `ProxyHeadersMiddleware` (the same code `--forwarded-allow-ips` uses, so edge cases are
    uvicorn's, not ours). Outermost middleware.

    **Unset (the default): a pure pass-through** -- the scope is untouched, so behaviour is
    byte-for-byte what it was. Set: a request from a listed peer gets `scope["client"]` and
    `scope["scheme"]` rewritten, which is what the login rate limiter (`api/auth.py._client_ip`)
    and the session cookie's `Secure` flag read. A request from any other peer is ignored, so a
    client reaching lftpweb directly cannot forge either. The env var is read per request (it is
    a plain attribute lookup) and the wrapped middleware cached per value.

    In every case the socket peer is kept at `scope[PEER_SCOPE_KEY]`, which proxy auth mode uses
    so its trusted-CIDR check keeps meaning "the connection came from the proxy".
    """

    def __init__(self, app) -> None:
        self.app = app
        self._cached_raw: str | None = None
        self._cached: ProxyHeadersMiddleware | None = None

    def _inner(self, raw: str | None) -> ProxyHeadersMiddleware | None:
        if raw != self._cached_raw:
            parsed = netguard.parse_trusted_proxies(raw)
            self._cached = (
                ProxyHeadersMiddleware(self.app, trusted_hosts=parsed) if parsed else None
            )
            self._cached_raw = raw
        return self._cached

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        scope[PEER_SCOPE_KEY] = client[0] if client else None
        inner = self._inner(app_settings.trusted_proxies)
        if inner is None:
            await self.app(scope, receive, send)
            return
        await inner(scope, receive, send)


class RequestGuardMiddleware:
    """Cross-site and DNS-rebinding defences that hold in **every** auth mode (audit batch C3,
    DESIGN.md §8). Runs outside `AuthMiddleware`, so it applies before any credential -- an
    `X-API-Key`, a session cookie, a proxy identity -- is even looked at. **Non-breaking by
    design**: it refuses only what a browser positively identifies as cross-site; a request that
    identifies nothing (curl, scripts, integrations) passes.

    1. **Host allow-list** (`LFTPWEB_ALLOWED_HOSTS`, `core/netguard.py`): when set, any HTTP
       request or WebSocket handshake, on any path, whose `Host` is not listed (loopback always
       is) gets a 403 naming the env var. Unset: everything passes, as before.
    2. **Mutating API requests** (`POST`/`PUT`/`PATCH`/`DELETE` under `/api/`), in order:
       `X-Requested-With` present -> allow; else `Sec-Fetch-Site` present -> `same-origin`/`none`
       allow, `cross-site`/`same-site` 403; else `Origin` present -> must match `Host`,
       `X-Forwarded-Host` or the allow-list, otherwise 403 (`Origin: null` included); else allow.
       In `password` mode the session CSRF token is still required by `AuthMiddleware`,
       unchanged from before C3.
    3. **WebSocket Origin**: with `LFTPWEB_ALLOWED_HOSTS` set, the handshake's `Origin` must
       match as in `netguard.origin_allowed` (opt-in). Unset, no Origin-vs-Host comparison (a
       Host-rewriting proxy would break live updates): the handshake is closed 4403 only when
       `Sec-Fetch-Site` is `cross-site`/`same-site`; with no `Sec-Fetch-Site` it is allowed and
       an Origin mismatch is logged once per distinct Origin.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._warned_origins: set[str] = set()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host = headers.get("host")
        allowed = netguard.parse_allowed_hosts(app_settings.allowed_hosts)

        if not allowed.allows(host):
            await self._deny(
                scope,
                send,
                f"Host {host!r} is not allowed: add it to {netguard.ENV_VAR} "
                "(comma-separated) and restart lftpweb",
            )
            return

        if scope["type"] == "websocket":
            if not self._websocket_ok(headers, host, allowed):
                await self._deny(scope, send, "Origin does not match Host")
                return
        elif scope["path"].startswith("/api/") and scope["method"] in _MUTATING_METHODS:
            reason = self._cross_site_reason(headers, host, allowed)
            if reason is not None:
                await self._deny(scope, send, reason)
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _cross_site_reason(headers: Headers, host: str | None, allowed) -> str | None:
        """`None` to allow, else the 403 detail. See the class docstring, item 2."""
        if headers.get(REQUIRED_HEADER):
            return None
        fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site:
            if fetch_site in ("same-origin", "none"):
                return None
            return (
                f"cross-site request refused (Sec-Fetch-Site: {fetch_site}): a page on "
                "another site cannot change lftpweb state"
            )
        origin = headers.get("origin")
        if origin is not None and not netguard.origin_allowed(
            origin, host, headers.get("x-forwarded-host"), allowed
        ):
            return (
                f"cross-site request refused (Origin {origin!r} does not match Host): "
                f"scripts should send no Origin, or send `{REQUIRED_HEADER}: lftpweb`"
            )
        return None

    def _websocket_ok(self, headers: Headers, host: str | None, allowed) -> bool:
        origin = headers.get("origin")
        if allowed.enforced:
            if origin is not None and not netguard.origin_allowed(
                origin, host, headers.get("x-forwarded-host"), allowed
            ):
                logger.warning(
                    "refused WebSocket handshake: Origin %r does not match Host %r", origin, host
                )
                return False
            return True
        fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
        if fetch_site in ("cross-site", "same-site"):
            logger.warning(
                "refused WebSocket handshake: Sec-Fetch-Site %s (Origin %r)", fetch_site, origin
            )
            return False
        if (
            not fetch_site
            and origin is not None
            and origin not in self._warned_origins
            and not netguard.origin_allowed(origin, host, headers.get("x-forwarded-host"), allowed)
        ):
            self._warned_origins.add(origin)
            logger.warning(
                "WebSocket Origin %r does not match Host %r and the browser sent no "
                "Sec-Fetch-Site; allowing it. Set %s to enforce an Origin check.",
                origin,
                host,
                netguard.ENV_VAR,
            )
        return True

    @staticmethod
    async def _deny(scope, send, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        await _send_json(send, 403, detail)


class AuthMiddleware:
    """Raw ASGI middleware. See module docstring for why this shape was chosen over
    `BaseHTTPMiddleware` + `Depends()`.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if not (path.startswith("/api/") or path in DOCS_PATHS):
            # Everything else is the SPA shell / static assets -- always public so the login
            # page itself can load (non-negotiable #2). The generated API docs are NOT in this
            # group (audit F11): they describe every route, so they sit behind the same gate.
            await self.app(scope, receive, send)
            return

        method = "GET" if scope["type"] == "websocket" else scope["method"]
        if (method, path) in PUBLIC_API_PATHS:
            await self.app(scope, receive, send)
            return

        fastapi_app = scope["app"]
        db = fastapi_app.state.db
        headers = Headers(scope=scope)

        # API key: accepted independently of AUTH_MODE (DESIGN.md §8), checked first so a
        # script using `X-API-Key` never has to care what mode the browser-facing UI is in.
        api_key = headers.get("x-api-key")
        if api_key and await auth.validate_api_key(db, api_key):
            scope.setdefault("state", {})["auth"] = {"method": "api_key"}
            await self.app(scope, receive, send)
            return

        stored = await auth.load_auth_settings(db)
        mode = auth.effective_mode(stored, app_settings.auth_mode)

        if mode == "none":
            scope.setdefault("state", {})["auth"] = {"method": "none"}
            await self.app(scope, receive, send)
            return

        if mode == "password":
            user = await auth.get_user(db)
            if not auth.resolve_password_mode_gate(user):
                # Lockout-recovery route 2 (core/auth.py's module docstring): no user row
                # yet -- open access rather than a password nobody can ever supply.
                scope.setdefault("state", {})["auth"] = {"method": "password_unset"}
                await self.app(scope, receive, send)
                return

            token = _get_cookie(headers, auth.SESSION_COOKIE_NAME)
            session = await auth.validate_session(db, token)
            if session is None:
                await self._deny(scope, send, 401, "authentication required")
                return

            if method in _MUTATING_METHODS:
                csrf_header = headers.get("x-csrf-token")
                if not csrf_header or not secrets.compare_digest(csrf_header, session.csrf_token):
                    await self._deny(scope, send, 403, "CSRF token missing or invalid")
                    return

            scope.setdefault("state", {})["auth"] = {
                "method": "session",
                "username": user.username,
            }
            await self.app(scope, receive, send)
            return

        if mode == "proxy":
            if not stored.proxy_trusted_cidrs:
                # Defense in depth: the settings endpoint already refuses to store `proxy`
                # mode without a trusted CIDR (DESIGN.md §8's own non-negotiable), but if this
                # state is ever reached anyway (a direct DB edit), fail closed -- never treat
                # an empty CIDR list as "trust everyone."
                await self._deny(scope, send, 401, "proxy mode is misconfigured (no trusted CIDR)")
                return

            client_host = peer_host(scope)
            if not auth.ip_in_trusted_cidrs(client_host, stored.proxy_trusted_cidrs):
                await self._deny(scope, send, 401, "request did not originate from a trusted proxy")
                return

            identity = headers.get(stored.proxy_header)
            if not identity:
                await self._deny(
                    scope, send, 401, f"missing identity header {stored.proxy_header!r}"
                )
                return

            scope.setdefault("state", {})["auth"] = {"method": "proxy", "username": identity}
            await self.app(scope, receive, send)
            return

        # An unrecognized stored mode value (e.g. hand-edited JSON in `setting`). Fail closed
        # rather than guessing -- the one thing every other branch above avoids is silently
        # treating an unexpected state as "allow."
        logger.warning("unrecognized auth mode %r; denying by default", mode)
        await self._deny(scope, send, 401, "authentication required")

    @staticmethod
    async def _deny(scope, send, status_code: int, detail: str) -> None:
        if scope["type"] == "websocket":
            # The handshake hasn't been accepted yet; closing here rejects the upgrade
            # outright rather than accepting and then immediately disconnecting, which would
            # otherwise look like a successful connection to the client for a moment.
            await send({"type": "websocket.close", "code": 4401 if status_code == 401 else 4403})
            return
        await _send_json(send, status_code, detail)


# Static, request-independent defense-in-depth headers (audit S4, docs/audit-v0.1.0.md).
# Deliberately the *safe* subset that cannot break a same-origin SPA served over plain HTTP:
#
#   * X-Content-Type-Options: nosniff  -- stop a browser MIME-sniffing a response into script.
#   * X-Frame-Options: SAMEORIGIN      -- clickjacking: no embedding in a cross-origin frame.
#   * Referrer-Policy: same-origin     -- don't leak the app URL to anything off-origin.
#
# A Content-Security-Policy is intentionally NOT set here: getting one wrong silently breaks the
# built SPA (inline styles/scripts, the WebSocket, the assets mount), and there is no browser in
# the build/CI environment to verify it against -- so it belongs in a reviewed, browser-checked
# change, not this blanket pass. HSTS is also omitted on purpose: the session cookie's own
# `Secure` flag is already scheme-conditional for plain-HTTP LAN use (api/auth.py), and sending
# HSTS over such a deployment would wedge the browser onto an https:// the host may not serve.
_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Append the static security headers above to every HTTP response. Raw ASGI (matching
    `AuthMiddleware`) so it covers streamed file downloads and the SPA/static mount uniformly,
    which `BaseHTTPMiddleware` cannot do without buffering. WebSocket and lifespan scopes pass
    through untouched -- these headers are meaningless on either.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                # Don't clobber a header a handler set deliberately -- only add ours if absent.
                existing = {name.lower() for name, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                for name, value in _SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
