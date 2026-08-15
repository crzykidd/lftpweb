-- Per-queue post-processing toggles become "inherit global, or explicit override"
-- (2026-08-13, prompts/2026-08-13-postprocess-inherit-or-override.md).
--
-- `core/postprocess.py.process_item` used to AND the site-wide flag with the per-queue one
-- (`effective = site_flag AND queue_flag`). The AND was standing in for "no override," badly:
-- an operator flips a queue's checkbox on and nothing happens because the global is off, with
-- no on-screen sign why. The four `path_queue` columns were `NOT NULL DEFAULT 0`
-- (`auto_verify`/`auto_extract` migration 001, `auto_move` migration 003, `auto_delete_archives`
-- migration 012), so they could only ever say on or off, never "whatever the site says." This
-- migration makes them nullable, where NULL means inherit; the companion code change (same
-- task) replaces the AND in `core/postprocess.py` with
-- `effective = queue_value if queue_value is not None else site_value`.
--
-- **Not behaviour-preserving, and deliberately so.** An earlier draft of this migration
-- computed each existing row's new value from today's site settings (read out of
-- `setting.value` via JSON1) so that no install's *effective* post-processing behaviour would
-- change on upgrade. The user overrode that mid-task: this is pre-release, there is exactly
-- one install (the developer's own, in a test environment), and it doesn't matter whether an
-- existing queue's toggles happen to read on or off after this migration runs. So every
-- existing row's four columns are simply set to NULL here -- every queue starts out inheriting
-- the site-wide default on every one of the four toggles. See docs/decisions.md (2026-08-13)
-- for the two migration designs and why the simpler one was chosen once nothing had shipped
-- yet to make behaviour-preservation load-bearing.
--
-- SQLite has no `ALTER TABLE ... ALTER COLUMN` -- dropping `NOT NULL` means rebuilding the
-- table the same way migration 008 rebuilt `item`: create the new shape, copy every row
-- across, `DROP TABLE` the old one, rename. `path_queue` is itself the parent of `item` and
-- `pattern` via `ON DELETE CASCADE` -- unlike 008's rebuild of `item` (whose own children are
-- `job`/`event`), losing those FKs to an implicit cascade here would wipe every item and
-- pattern in the database. `db.py.migrate()` now turns `PRAGMA foreign_keys` off for the
-- whole batch of pending migrations and back on afterward specifically because of this
-- migration -- see that function's own comment for why 008's claim that this was already safe
-- did not hold up under an actual reproduction, and docs/decisions.md for the full story.
CREATE TABLE path_queue_new (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id                   INTEGER NOT NULL REFERENCES host (id) ON DELETE CASCADE,
    name                      TEXT NOT NULL,
    remote_path               TEXT NOT NULL,
    local_path                TEXT NOT NULL,
    staging_path              TEXT,
    enabled                   INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    sync_mode                 TEXT NOT NULL DEFAULT 'copy' CHECK (sync_mode IN ('copy', 'move', 'sync')),
    auto_queue_enabled        INTEGER NOT NULL DEFAULT 0 CHECK (auto_queue_enabled IN (0, 1)),
    auto_queue_patterns_only  INTEGER NOT NULL DEFAULT 0 CHECK (auto_queue_patterns_only IN (0, 1)),
    -- NULL = inherit the site-wide default (core/postprocess.py's new resolution rule); 0/1 =
    -- an explicit per-queue override. Was `NOT NULL DEFAULT 0` -- see this migration's header.
    -- The CHECK is unchanged from migrations 001/003/012: a CHECK constraint is satisfied
    -- whenever its expression evaluates to NULL, so `col IN (0, 1)` already permits NULL
    -- without rewriting it.
    auto_verify               INTEGER CHECK (auto_verify IN (0, 1)),
    auto_extract              INTEGER CHECK (auto_extract IN (0, 1)),
    auto_move                 INTEGER CHECK (auto_move IN (0, 1)),
    auto_delete_archives      INTEGER CHECK (auto_delete_archives IN (0, 1)),
    scan_interval_s           REAL CHECK (scan_interval_s IS NULL OR scan_interval_s >= 0),
    created_at                TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO path_queue_new (
    id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode,
    auto_queue_enabled, auto_queue_patterns_only,
    auto_verify, auto_extract, auto_move, auto_delete_archives,
    scan_interval_s, created_at
)
SELECT
    id, host_id, name, remote_path, local_path, staging_path, enabled, sync_mode,
    auto_queue_enabled, auto_queue_patterns_only,
    NULL, NULL, NULL, NULL,
    scan_interval_s, created_at
FROM path_queue;

DROP TABLE path_queue;
ALTER TABLE path_queue_new RENAME TO path_queue;

CREATE INDEX idx_path_queue_host_id ON path_queue (host_id);
