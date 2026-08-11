-- Initial schema, all of DESIGN.md §3.1 in one migration. §3.1 is annotated pseudo-SQL;
-- this is the real DDL, including columns later phases fill in (auto_queue_suppressed,
-- suppressed_reason, lane, rank, forced_full_rate, first_missing_at, remote_deleted_at) —
-- getting the shape right once beats five migrations that each add a column.
--
-- Booleans are stored as INTEGER 0/1 (SQLite has no native boolean) with a CHECK.
-- Timestamps are stored as TEXT ISO-8601 UTC (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')).
-- JSON-typed columns (§3.1) are stored as TEXT holding serialized JSON; SQLite has no
-- native JSON type and this project has no other JSON1-specific querying needs yet.

-- Global settings, typed key/value, so the UI can edit anything without a migration.
CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,  -- JSON
    updated_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- The seedbox. v1 has exactly one row and no multi-host UI, but it's a record with a
-- stable id rather than fields inlined into `setting`, so a second seedbox is a schema
-- addition instead of a migration of every queue.
CREATE TABLE host (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    address              TEXT NOT NULL,
    port                 INTEGER NOT NULL DEFAULT 22,
    protocol             TEXT NOT NULL DEFAULT 'sftp' CHECK (protocol IN ('sftp')),
    username             TEXT NOT NULL,
    auth_method          TEXT NOT NULL CHECK (auth_method IN ('key', 'agent', 'password')),
    key_path             TEXT,
    password_enc         TEXT,
    known_hosts_policy   TEXT NOT NULL DEFAULT 'strict',
    connection_overrides TEXT NOT NULL DEFAULT '{}',  -- JSON: net:connection-limit, socket-buffer, timeouts, retry
    created_at           TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A named remote-path -> local-path mapping, plus the policy for that mapping. Shown in
-- the UI as a "Queue" with the user's own name ("TV", "Movies", "Music"). No bandwidth /
-- concurrency / parallelism columns here — those are site-level (§4.5, held in `setting`);
-- a queue governs what and where, never how fast.
CREATE TABLE path_queue (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id            INTEGER NOT NULL REFERENCES host (id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    remote_path        TEXT NOT NULL,
    local_path         TEXT NOT NULL,
    staging_path       TEXT,
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    sync_mode          TEXT NOT NULL DEFAULT 'copy' CHECK (sync_mode IN ('copy', 'move', 'sync')),
    auto_queue_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auto_queue_enabled IN (0, 1)),
    auto_extract       INTEGER NOT NULL DEFAULT 0 CHECK (auto_extract IN (0, 1)),
    auto_verify        INTEGER NOT NULL DEFAULT 0 CHECK (auto_verify IN (0, 1)),
    created_at         TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_path_queue_host_id ON path_queue (host_id);

-- Three kinds, doing three different jobs (§4.7): 'select' / 'skip' match an item's own
-- name and are enforced by us; 'file_exclude' matches paths inside an item and is enforced
-- by lftp via --exclude-glob. queue_id NULL = applies to every queue.
CREATE TABLE pattern (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id   INTEGER REFERENCES path_queue (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('select', 'skip', 'file_exclude')),
    expr       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_pattern_queue_id ON pattern (queue_id);

-- One row per item we have ever cared about — the durable lifecycle record (§3.2 lists
-- the full state vocabulary; enforced here via CHECK so a bad state value can't be written).
CREATE TABLE item (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id              INTEGER NOT NULL REFERENCES path_queue (id) ON DELETE CASCADE,
    rel_path              TEXT NOT NULL,
    is_dir                INTEGER NOT NULL CHECK (is_dir IN (0, 1)),
    remote_size           INTEGER,
    local_size            INTEGER,
    remote_mtime          TEXT,
    state                 TEXT NOT NULL CHECK (state IN (
        'REMOTE_ONLY', 'QUEUED', 'DOWNLOADING', 'PARTIAL', 'STOPPED', 'DOWNLOADED',
        'EXCLUDED', 'VERIFYING', 'VERIFIED', 'CORRUPT', 'EXTRACTING', 'EXTRACTED',
        'EXTRACT_FAILED', 'FAILED', 'LOCAL_ONLY', 'REMOVED_LOCAL', 'REMOVED_BOTH'
    )),
    substate              TEXT,
    first_seen_at         TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    downloaded_at         TEXT,
    extracted_at          TEXT,
    verified_at           TEXT,
    first_missing_at      TEXT,  -- when local absence was first observed -> grace period (§7.3, deferred)
    remote_deleted_at     TEXT,  -- when we deleted the remote copy, if we did
    auto_queue_suppressed INTEGER NOT NULL DEFAULT 0 CHECK (auto_queue_suppressed IN (0, 1)),
    suppressed_reason     TEXT CHECK (
        suppressed_reason IS NULL
        OR suppressed_reason IN ('user_stopped', 'retries_exhausted', 'permanent_error')
    ),
    error_class           TEXT,
    error_detail          TEXT,
    UNIQUE (queue_id, rel_path)
);

CREATE INDEX idx_item_queue_id ON item (queue_id);
CREATE INDEX idx_item_state ON item (state);

-- One row per transfer attempt — the audit trail SeedSync lacks (§3.1).
CREATE TABLE job (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          INTEGER NOT NULL REFERENCES item (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('mirror', 'pget')),
    state            TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    lane             TEXT NOT NULL DEFAULT 'main' CHECK (lane IN ('main', 'small')),
    rank             REAL NOT NULL DEFAULT 0,  -- sortable; default order is rank DESC, queued_at ASC
    attempt          INTEGER NOT NULL DEFAULT 1,
    queued_at        TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    pid              INTEGER,
    argv             TEXT,  -- JSON
    lftp_settings    TEXT,  -- JSON
    bytes_start      INTEGER NOT NULL DEFAULT 0,
    bytes_done       INTEGER NOT NULL DEFAULT 0,
    bytes_total      INTEGER,
    rate_limit_bps   INTEGER,  -- the allocation this process was spawned with (§4.5); fixed for its lifetime
    forced_full_rate INTEGER NOT NULL DEFAULT 0 CHECK (forced_full_rate IN (0, 1)),
    started_at       TEXT,
    finished_at      TEXT,
    exit_code        INTEGER,
    error_class      TEXT,
    output_tail      TEXT
);

CREATE INDEX idx_job_state ON job (state);
CREATE INDEX idx_job_item_id ON job (item_id);

-- Structured, queryable lifecycle + audit records; drives the History page (§9.2).
CREATE TABLE event (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    level   TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    item_id INTEGER REFERENCES item (id) ON DELETE SET NULL,
    job_id  INTEGER REFERENCES job (id) ON DELETE SET NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX idx_event_ts ON event (ts);
