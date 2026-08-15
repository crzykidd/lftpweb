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

from lftpweb.config import settings as app_settings
from lftpweb.core import auth

logger = logging.getLogger(__name__)

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
        if not path.startswith("/api/"):
            # Everything else is the SPA shell / static assets -- always public so the login
            # page itself can load (non-negotiable #2).
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

            client = scope.get("client")
            client_host = client[0] if client else None
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
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
