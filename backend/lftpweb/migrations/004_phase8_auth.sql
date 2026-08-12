-- Phase 8 (DESIGN.md §8): auth. `auth_settings` (mode, proxy header, trusted CIDRs) lives in
-- the `setting` table like every other *Settings dataclass (core/auth.py.SETTING_KEY) -- no
-- new table needed for it. This migration adds the three tables that DO need real rows:
-- the single local user, sessions, and API keys.
--
-- Nothing here changes behaviour for an existing install: no row is inserted by this
-- migration, `auth_settings` is absent until someone visits Settings -> Auth (defaults to
-- `mode: "none"`, core/auth.py.DEFAULT_AUTH_SETTINGS), and AUTH_MODE stays `none` until a user
-- explicitly turns it on. This phase's own non-negotiable (docs/decisions.md).

-- Single local user for AUTH_MODE=password. `id = 1` enforced by CHECK -- v1 is single-user,
-- the same "exactly one row, no multi-* UI" shape `host` already uses (§3.1). No username
-- UNIQUE constraint needed for the same reason.
CREATE TABLE auth_user (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,  -- argon2id, via argon2-cffi's PasswordHasher default
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One row per active session. The cookie carries the raw token; only its SHA-256 digest is
-- stored, so a stolen database backup (which already excludes the credential-encryption key,
-- §10.2) doesn't also hand over every live session for free. `csrf_token` is issued once at
-- login and handed back on every session read (`GET /api/auth/session`) -- the double-submit
-- pattern without a second cookie, since the value only ever needs to round-trip through the
-- same-origin JS that already holds the session.
CREATE TABLE session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash    TEXT NOT NULL UNIQUE,
    csrf_token    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_session_token_hash ON session (token_hash);
CREATE INDEX idx_session_expires_at ON session (expires_at);

-- `X-API-Key`, accepted independently of AUTH_MODE (DESIGN.md §8). Only the SHA-256 digest of
-- the key is stored -- an API key is a 256-bit random token, not a low-entropy secret a human
-- picked, so a fast digest is the right tool (unlike the password, which is argon2id
-- specifically because humans choose guessable passwords); see docs/decisions.md. The
-- plaintext is shown once, at creation, and is never recoverable from this table.
CREATE TABLE api_key (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at  TEXT
);

CREATE INDEX idx_api_key_key_hash ON api_key (key_hash);
