"""Unit tests for core/auth.py -- DESIGN.md §8, phase 8. `tests/test_auth_api.py` covers the
HTTP/middleware surface; this file covers the pure(ish) logic underneath it: settings
persistence, password hashing, sessions, API keys, CIDR matching, and the login rate limiter.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

from lftpweb.core import auth
from lftpweb.db import migrate


async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await migrate(db)
    return db


@pytest.fixture
async def db():
    conn = await _make_db()
    yield conn
    await conn.close()


# --- AuthSettings: default is 'none', the phase's own non-negotiable ---------------------


async def test_default_auth_settings_is_none_mode(db):
    settings = await auth.load_auth_settings(db)
    assert settings.mode == "none"
    assert settings.proxy_header == auth.DEFAULT_PROXY_HEADER
    assert settings.proxy_trusted_cidrs == ()


async def test_save_and_load_auth_settings_round_trip(db):
    saved = auth.AuthSettings(
        mode="proxy", proxy_header="X-Remote-User", proxy_trusted_cidrs=("10.0.0.0/24",)
    )
    await auth.save_auth_settings(db, saved)
    loaded = await auth.load_auth_settings(db)
    assert loaded == saved


def test_effective_mode_env_override_wins_over_stored():
    stored = auth.AuthSettings(mode="password")
    assert auth.effective_mode(stored, "none") == "none"
    assert auth.effective_mode(stored, "proxy") == "proxy"


def test_effective_mode_falls_back_to_stored_when_no_override():
    stored = auth.AuthSettings(mode="password")
    assert auth.effective_mode(stored, None) == "password"


# --- Passwords: argon2id, not a fallback --------------------------------------------------


def test_password_hash_is_argon2id_not_a_fallback():
    h = auth.hash_password("hunter2")
    assert h.startswith("$argon2id$"), h
    assert not h.startswith("$argon2i$")
    assert not h.startswith("$argon2d$")


def test_verify_password_correct_and_incorrect():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(h, "correct horse battery staple") is True
    assert auth.verify_password(h, "wrong") is False


def test_verify_password_never_raises_on_garbage_hash():
    assert auth.verify_password("not-a-real-hash", "anything") is False


# --- The single local user ------------------------------------------------------------------


async def test_get_user_none_when_unconfigured(db):
    assert await auth.get_user(db) is None


async def test_set_user_password_creates_then_updates(db):
    await auth.set_user_password(db, "alice", "first-password")
    user = await auth.get_user(db)
    assert user is not None
    assert user.username == "alice"
    assert auth.verify_password(user.password_hash, "first-password")

    await auth.set_user_password(db, "bob", "second-password")
    user2 = await auth.get_user(db)
    assert user2 is not None
    assert user2.id == user.id  # still the single row, id=1
    assert user2.username == "bob"
    assert auth.verify_password(user2.password_hash, "second-password")
    assert not auth.verify_password(user2.password_hash, "first-password")


async def test_delete_user_is_the_lockout_recovery_route(db):
    await auth.set_user_password(db, "alice", "hunter2")
    assert await auth.get_user(db) is not None
    await auth.delete_user(db)
    assert await auth.get_user(db) is None


def test_resolve_password_mode_gate():
    assert auth.resolve_password_mode_gate(None) is False
    user = auth.AuthUser(id=1, username="alice", password_hash="x")
    assert auth.resolve_password_mode_gate(user) is True


# --- Sessions --------------------------------------------------------------------------


async def test_create_and_validate_session(db):
    token, session = await auth.create_session(db)
    assert token
    assert session.csrf_token

    validated = await auth.validate_session(db, token)
    assert validated is not None
    assert validated.id == session.id
    assert validated.csrf_token == session.csrf_token


async def test_validate_session_rejects_wrong_or_missing_token(db):
    await auth.create_session(db)
    assert await auth.validate_session(db, "not-a-real-token") is None
    assert await auth.validate_session(db, None) is None
    assert await auth.validate_session(db, "") is None


async def test_validate_session_rejects_expired_session(db):
    token, session = await auth.create_session(db)
    # Force it into the past directly -- the point of this test is the expiry check, not the
    # TTL math, so back-dating the row is more direct than waiting out a real TTL.
    await db.execute(
        "UPDATE session SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
        (session.id,),
    )
    await db.commit()
    assert await auth.validate_session(db, token) is None
    # And it's actually gone, not just rejected -- opportunistic cleanup.
    cursor = await db.execute("SELECT COUNT(*) AS n FROM session WHERE id = ?", (session.id,))
    assert (await cursor.fetchone())["n"] == 0


async def test_delete_session(db):
    token, _ = await auth.create_session(db)
    await auth.delete_session(db, token)
    assert await auth.validate_session(db, token) is None


async def test_purge_all_sessions(db):
    token1, _ = await auth.create_session(db)
    token2, _ = await auth.create_session(db)
    await auth.purge_all_sessions(db)
    assert await auth.validate_session(db, token1) is None
    assert await auth.validate_session(db, token2) is None


async def test_session_token_hash_never_stores_the_raw_token(db):
    token, _ = await auth.create_session(db)
    cursor = await db.execute("SELECT token_hash FROM session")
    row = await cursor.fetchone()
    assert row["token_hash"] != token
    assert token not in row["token_hash"]


# --- API keys --------------------------------------------------------------------------


async def test_create_and_validate_api_key(db):
    key, info = await auth.create_api_key(db, "sonarr")
    assert key
    assert info.name == "sonarr"
    assert info.last_used_at is None

    assert await auth.validate_api_key(db, key) is True
    assert await auth.validate_api_key(db, "wrong-key") is False
    assert await auth.validate_api_key(db, None) is False


async def test_validate_api_key_updates_last_used_at(db):
    key, info = await auth.create_api_key(db, "sonarr")
    assert await auth.validate_api_key(db, key) is True
    keys = await auth.list_api_keys(db)
    assert keys[0].id == info.id
    assert keys[0].last_used_at is not None


async def test_delete_api_key(db):
    key, info = await auth.create_api_key(db, "sonarr")
    assert await auth.delete_api_key(db, info.id) is True
    assert await auth.validate_api_key(db, key) is False
    assert await auth.delete_api_key(db, info.id) is False  # already gone


async def test_api_key_never_stores_the_plaintext(db):
    key, _ = await auth.create_api_key(db, "sonarr")
    cursor = await db.execute("SELECT key_hash FROM api_key")
    row = await cursor.fetchone()
    assert row["key_hash"] != key
    assert key not in row["key_hash"]


# --- Proxy mode: CIDR matching ------------------------------------------------------------


def test_ip_in_trusted_cidrs_matches_within_range():
    assert auth.ip_in_trusted_cidrs("10.0.0.5", ["10.0.0.0/24"]) is True
    assert auth.ip_in_trusted_cidrs("10.0.0.5", ["10.0.0.5/32"]) is True


def test_ip_in_trusted_cidrs_rejects_outside_range():
    assert auth.ip_in_trusted_cidrs("10.0.1.5", ["10.0.0.0/24"]) is False


def test_ip_in_trusted_cidrs_empty_list_never_trusts_anyone():
    # DESIGN.md §8: without the CIDR check, proxy mode is a bypass -- an empty list must
    # never be read as "trust everyone."
    assert auth.ip_in_trusted_cidrs("10.0.0.5", []) is False
    assert auth.ip_in_trusted_cidrs(None, []) is False


def test_ip_in_trusted_cidrs_handles_unparseable_input_without_raising():
    assert auth.ip_in_trusted_cidrs("not-an-ip", ["10.0.0.0/24"]) is False
    assert auth.ip_in_trusted_cidrs("10.0.0.5", ["not-a-cidr"]) is False


def test_parse_cidrs_raises_on_garbage():
    with pytest.raises(ValueError):
        auth.parse_cidrs(["not-a-cidr"])


def test_parse_cidrs_accepts_v4_and_v6():
    nets = auth.parse_cidrs(["10.0.0.0/24", "::1/128", "192.168.1.5"])
    assert len(nets) == 3


# --- Login rate limiting -----------------------------------------------------------------


def test_login_rate_limiter_blocks_after_max_failures():
    limiter = auth.LoginRateLimiter(max_failures=3, window_s=60.0)
    key = "1.2.3.4"
    assert limiter.is_blocked(key) is False
    for _ in range(3):
        limiter.record_failure(key)
    assert limiter.is_blocked(key) is True
    assert limiter.retry_after_s(key) > 0


def test_login_rate_limiter_success_resets_the_bucket():
    limiter = auth.LoginRateLimiter(max_failures=2, window_s=60.0)
    key = "1.2.3.4"
    limiter.record_failure(key)
    limiter.record_failure(key)
    assert limiter.is_blocked(key) is True
    limiter.record_success(key)
    assert limiter.is_blocked(key) is False


def test_login_rate_limiter_window_expires(monkeypatch):
    limiter = auth.LoginRateLimiter(max_failures=1, window_s=0.05)
    key = "1.2.3.4"
    limiter.record_failure(key)
    assert limiter.is_blocked(key) is True
    time.sleep(0.1)
    assert limiter.is_blocked(key) is False


def test_login_rate_limiter_buckets_are_independent_per_key():
    limiter = auth.LoginRateLimiter(max_failures=1, window_s=60.0)
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") is True
    assert limiter.is_blocked("5.6.7.8") is False
