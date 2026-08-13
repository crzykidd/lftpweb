-- Local deletion -- manual (Files-page button) and scheduled (retention). The second
-- irreversible-delete feature in this codebase and the first that touches the user's own data
-- (prompts/open-issues.md "7 + 8 -- the deletion cluster", the specification for this task;
-- see docs/decisions.md).
--
-- Widens `item.suppressed_reason`'s CHECK to add 'deleted_local' -- the marker
-- `core/local_delete.py.delete_local` writes, alongside `auto_queue_suppressed = 1`, on every
-- item it removes. This is what makes it safe to also fix issue 4 (`REMOVED_LOCAL` was
-- excluded from `core/autoqueue.py.ELIGIBLE_STATES` outright, so a genuinely moved-away item
-- could never be re-queued): an item this codebase deleted on purpose now carries this reason
-- and stays suppressed regardless of state, while one a human or an *arr importer moved away
-- (no suppression set) becomes eligible again.
--
-- SQLite has no `ALTER TABLE ... ALTER COLUMN` / no way to widen a `CHECK` in place -- unlike
-- migration 006's `state_changed_at` (a plain `ADD COLUMN`, chosen there specifically to avoid
-- this), a `CHECK` constraint can only be changed by rebuilding the table: create the new
-- shape, copy every row across, drop the old table (which also drops its indexes and
-- triggers), rename, then recreate both. Confirmed empirically (not just from the SQLite docs)
-- that this survives `job`/`event`'s foreign keys into `item` with `PRAGMA foreign_keys = ON`
-- for the whole operation -- `DROP TABLE` does not run `ON DELETE` actions, and the FK simply
-- re-resolves by name once `item_new` is renamed back to `item`. `PRAGMA foreign_keys = OFF`
-- is deliberately not issued here: `db.py.migrate()` already wraps every migration's script in
-- `BEGIN ... COMMIT`, and SQLite treats a `foreign_keys` pragma as a no-op once a transaction
-- is open, so it would silently do nothing anyway.
--
-- `item` rows are never deleted (`core/engine.py._project`'s own docstring), so the copy below
-- carries every row's original `id` across intact and `sqlite_sequence` for the rebuilt table
-- ends up tracking the true historical maximum -- there is no case in this codebase where a
-- gap from a deleted row could make AUTOINCREMENT reuse an id post-rebuild.

CREATE TABLE item_new (
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
    first_missing_at      TEXT,
    remote_deleted_at     TEXT,
    auto_queue_suppressed INTEGER NOT NULL DEFAULT 0 CHECK (auto_queue_suppressed IN (0, 1)),
    suppressed_reason     TEXT CHECK (
        suppressed_reason IS NULL
        OR suppressed_reason IN (
            'user_stopped', 'retries_exhausted', 'permanent_error', 'deleted_local'
        )
    ),
    error_class           TEXT,
    error_detail          TEXT,
    state_changed_at      TEXT,
    UNIQUE (queue_id, rel_path)
);

INSERT INTO item_new (
    id, queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate,
    first_seen_at, downloaded_at, extracted_at, verified_at, first_missing_at,
    remote_deleted_at, auto_queue_suppressed, suppressed_reason, error_class, error_detail,
    state_changed_at
)
SELECT
    id, queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate,
    first_seen_at, downloaded_at, extracted_at, verified_at, first_missing_at,
    remote_deleted_at, auto_queue_suppressed, suppressed_reason, error_class, error_detail,
    state_changed_at
FROM item;

DROP TABLE item;
ALTER TABLE item_new RENAME TO item;

CREATE INDEX idx_item_queue_id ON item (queue_id);
CREATE INDEX idx_item_state ON item (state);

-- Migration 006's triggers -- dropped along with the old `item` table above, so they must be
-- recreated verbatim against the rebuilt one. See that migration for why an `AFTER INSERT`
-- trigger, not a column `DEFAULT`, backs the insert case.
CREATE TRIGGER item_state_changed_at_insert
AFTER INSERT ON item
WHEN NEW.state_changed_at IS NULL
BEGIN
    UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;

CREATE TRIGGER item_state_changed_at
AFTER UPDATE OF state ON item
WHEN NEW.state IS NOT OLD.state
BEGIN
    UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;
