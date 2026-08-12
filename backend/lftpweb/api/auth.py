"""Auth endpoints (DESIGN.md §8, phase 8): login/logout/whoami (`/api/auth/*`, all reachable
without a session -- see `middleware.py.PUBLIC_API_PATHS`) and Settings → Auth's mode/user/
API-key management (`/api/settings/auth/*`, gated like every other settings endpoint once a
mode is on).

Two routers in one file, the same shape `api/backup.py` and `api/logs.py` use for their own
Settings sub-page -- everything auth-shaped lives together rather than folding into the
already-large `api/settings.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from lftpweb.config import settings as app_settings
from lftpweb.core import auth
from lftpweb.models import (
    ApiKeyCreatedOut,
    ApiKeyIn,
    ApiKeyOut,
    AuthSessionOut,
    AuthSettingsIn,
    AuthSettingsOut,
    ChangePasswordIn,
    LoginIn,
)

router = APIRouter(prefix="/api/auth")
settings_router = APIRouter(prefix="/api/settings/auth")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


async def _effective_mode(request: Request) -> str:
    stored = await auth.load_auth_settings(request.app.state.db)
    return auth.effective_mode(stored, app_settings.auth_mode)


# --- /api/auth/* -- login, logout, whoami (always reachable, module docstring) -----------


@router.post("/login", response_model=AuthSessionOut)
async def login(body: LoginIn, request: Request, response: Response) -> AuthSessionOut:
    db = request.app.state.db
    limiter: auth.LoginRateLimiter = request.app.state.login_rate_limiter
    ip = _client_ip(request)

    if limiter.is_blocked(ip):
        retry_after = int(limiter.retry_after_s(ip)) + 1
        # `headers=` on the HTTPException itself, not `response.headers` -- FastAPI builds
        # the actual error response from the raised exception, not from the injected
        # `Response` object, so headers set there are silently dropped on this path.
        raise HTTPException(
            status_code=429,
            detail="too many failed login attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )

    await auth.purge_expired_sessions(db)
    user = await auth.get_user(db)

    # Deliberately one generic 401 for "no user configured," "unknown username," and "wrong
    # password" -- distinguishing them would tell an attacker whether a username exists.
    if (
        user is None
        or user.username != body.username
        or not auth.verify_password(user.password_hash, body.password)
    ):
        limiter.record_failure(ip)
        raise HTTPException(status_code=401, detail="invalid username or password")

    limiter.record_success(ip)
    token, session = await auth.create_session(db)
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        token,
        max_age=auth.SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        # Dynamic, not hardcoded True: this app is routinely reached over plain HTTP on a
        # LAN (DESIGN.md §8's own framing -- "some users sit behind Authelia/Tailscale"), and
        # a cookie marked Secure is silently dropped by the browser over HTTP, which would
        # make login appear to succeed and then immediately look logged-out. Set it when the
        # request itself arrived over HTTPS (e.g. behind a TLS-terminating reverse proxy),
        # skip it otherwise -- see docs/decisions.md, flagged as a deliberate weakening for
        # plain-HTTP LAN deployments rather than a silent gap.
        secure=request.url.scheme == "https",
        path="/",
    )
    return AuthSessionOut(
        mode="password", authenticated=True, username=user.username, csrf_token=session.csrf_token
    )


@router.post("/logout")
async def logout(request: Request, response: Response) -> AuthSessionOut:
    db = request.app.state.db
    token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    await auth.delete_session(db, token)
    response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
    mode = await _effective_mode(request)
    return AuthSessionOut(mode=mode, authenticated=(mode == "none"))


@router.get("/session", response_model=AuthSessionOut)
async def session_info(request: Request) -> AuthSessionOut:
    """ "Whoami" -- the SPA calls this on load to decide whether to render the login form.
    Deliberately reachable with no credentials at all (`middleware.py`'s public allowlist):
    it has to be, since a browser that isn't authenticated yet is exactly who needs to call
    it to find out.
    """
    db = request.app.state.db
    stored = await auth.load_auth_settings(db)
    mode = auth.effective_mode(stored, app_settings.auth_mode)

    if mode == "none":
        return AuthSessionOut(mode=mode, authenticated=True)

    if mode == "password":
        user = await auth.get_user(db)
        if not auth.resolve_password_mode_gate(user):
            return AuthSessionOut(mode=mode, authenticated=True)
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        session = await auth.validate_session(db, token)
        if session is None:
            return AuthSessionOut(mode=mode, authenticated=False)
        return AuthSessionOut(
            mode=mode, authenticated=True, username=user.username, csrf_token=session.csrf_token
        )

    if mode == "proxy":
        client_host = request.client.host if request.client is not None else None
        if not auth.ip_in_trusted_cidrs(client_host, stored.proxy_trusted_cidrs):
            return AuthSessionOut(mode=mode, authenticated=False)
        identity = request.headers.get(stored.proxy_header)
        return AuthSessionOut(mode=mode, authenticated=bool(identity), username=identity)

    return AuthSessionOut(mode=mode, authenticated=False)


# --- /api/settings/auth/* -- mode, user, API keys -----------------------------------------


def _settings_out(stored: auth.AuthSettings, user: auth.AuthUser | None) -> AuthSettingsOut:
    return AuthSettingsOut(
        mode=stored.mode,
        proxy_header=stored.proxy_header,
        proxy_trusted_cidrs=list(stored.proxy_trusted_cidrs),
        has_user=user is not None,
        username=user.username if user is not None else None,
    )


@settings_router.get("", response_model=AuthSettingsOut)
async def get_auth_settings(request: Request) -> AuthSettingsOut:
    db = request.app.state.db
    stored = await auth.load_auth_settings(db)
    user = await auth.get_user(db)
    return _settings_out(stored, user)


@settings_router.put("", response_model=AuthSettingsOut)
async def put_auth_settings(body: AuthSettingsIn, request: Request) -> AuthSettingsOut:
    """DESIGN.md §8's two non-negotiables, enforced server-side (never only in the frontend
    form, per the same reasoning `api/settings.py._effective_auto_verify` already applies to
    a `move` queue's verification): `proxy` mode refuses to save without at least one trusted
    CIDR, and `password` mode can never be stored with nobody able to log in -- switching
    into it for the first time requires `username` + `new_password` in the same request.
    """
    db = request.app.state.db

    if body.mode == "proxy":
        if not body.proxy_trusted_cidrs:
            raise HTTPException(
                status_code=400,
                detail="proxy mode requires at least one trusted CIDR (DESIGN.md §8) -- "
                "without it, proxy mode is a bypass",
            )
        try:
            auth.parse_cidrs(body.proxy_trusted_cidrs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid CIDR: {exc}") from exc

    if not body.proxy_header.strip():
        raise HTTPException(status_code=422, detail="proxy_header must not be blank")

    existing_user = await auth.get_user(db)
    if body.mode == "password":
        if existing_user is None:
            if not body.username or not body.new_password:
                raise HTTPException(
                    status_code=400,
                    detail="password mode requires creating a user (username + new_password) "
                    "before it can be enabled",
                )
            await auth.set_user_password(db, body.username, body.new_password)
        elif body.new_password:
            await auth.set_user_password(
                db, body.username or existing_user.username, body.new_password
            )
            await auth.purge_all_sessions(db)
        elif body.username and body.username != existing_user.username:
            await db.execute("UPDATE auth_user SET username = ? WHERE id = 1", (body.username,))
            await db.commit()

    settings = auth.AuthSettings(
        mode=body.mode,
        proxy_header=body.proxy_header.strip(),
        proxy_trusted_cidrs=tuple(body.proxy_trusted_cidrs),
    )
    await auth.save_auth_settings(db, settings)
    user = await auth.get_user(db)
    return _settings_out(settings, user)


@settings_router.post("/password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request) -> None:
    db = request.app.state.db
    user = await auth.get_user(db)
    if user is None:
        raise HTTPException(
            status_code=409,
            detail="no user configured yet -- use PUT /api/settings/auth to create one",
        )
    if not auth.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    if not body.new_password:
        raise HTTPException(status_code=422, detail="new_password must not be empty")
    await auth.set_user_password(db, user.username, body.new_password)
    # Force re-login everywhere, including the browser that just made this change -- see
    # core/auth.py.purge_all_sessions's docstring.
    await auth.purge_all_sessions(db)


@settings_router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys_route(request: Request) -> list[ApiKeyOut]:
    infos = await auth.list_api_keys(request.app.state.db)
    return [
        ApiKeyOut(id=i.id, name=i.name, created_at=i.created_at, last_used_at=i.last_used_at)
        for i in infos
    ]


@settings_router.post("/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key_route(body: ApiKeyIn, request: Request) -> ApiKeyCreatedOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")
    key, info = await auth.create_api_key(request.app.state.db, name)
    return ApiKeyCreatedOut(
        id=info.id,
        name=info.name,
        created_at=info.created_at,
        last_used_at=info.last_used_at,
        key=key,
    )


@settings_router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key_route(key_id: int, request: Request) -> None:
    deleted = await auth.delete_api_key(request.app.state.db, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="api key not found")
