-- The manual remote-delete dialog (prompts/done/2026-08-16-manual-delete-local-and-remote.md)
-- -- the first *manual* remote-delete path in the API, alongside the existing manual
-- local-delete button. Widens `item.suppressed_reason`'s CHECK to add 'deleted_source': the
-- marker `api/jobs.py.delete_item` writes, alongside `auto_queue_suppressed = 1`, on a
-- *source-only* manual delete (local copy left untouched) -- so a release that reappears on the
-- seedbox under the same `rel_path` later is not silently auto-queued right back, exactly the
-- guarantee 'deleted_local' (migration 008) already gives a local-only delete. A combined
-- local+source delete keeps writing 'deleted_local' (via `core/local_delete.py.delete_local`,
-- unchanged, since deleting the local copy is still the more complete fact about that row) --
-- this migration only adds the one new value the source-only case needs.
--
-- Same full-table-rebuild shape migration 008's own comment explains in full (SQLite has no
-- `ALTER TABLE ... ALTER COLUMN` / no way to widen a `CHECK` in place -- see that migration for
-- the FK/AUTOINCREMENT reasoning, unchanged here). Carries forward every column added to `item`
-- since 008's own rebuild (011 `local_mtime`, 017 `pending_download_prefix`, 018 the three *arr
-- columns, 019 `remote_delete_pending`) verbatim -- every one of those was a plain `ADD COLUMN`,
-- so this really is "the current schema plus one CHECK value," not a partial rebuild that would
-- silently drop a column added since 008.

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
            'user_stopped', 'retries_exhausted', 'permanent_error', 'deleted_local',
            'deleted_source'
        )
    ),
    error_class           TEXT,
    error_detail          TEXT,
    state_changed_at      TEXT,
    local_mtime           TEXT,
    pending_download_prefix TEXT,
    arr_status            TEXT,
    arr_status_at         TEXT,
    arr_download_id       TEXT,
    remote_delete_pending TEXT CHECK (
        remote_delete_pending IS NULL OR remote_delete_pending IN ('VERIFIED', 'SKIPPED')
    ),
    UNIQUE (queue_id, rel_path)
);

INSERT INTO item_new (
    id, queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate,
    first_seen_at, downloaded_at, extracted_at, verified_at, first_missing_at,
    remote_deleted_at, auto_queue_suppressed, suppressed_reason, error_class, error_detail,
    state_changed_at, local_mtime, pending_download_prefix, arr_status, arr_status_at,
    arr_download_id, remote_delete_pending
)
SELECT
    id, queue_id, rel_path, is_dir, remote_size, local_size, remote_mtime, state, substate,
    first_seen_at, downloaded_at, extracted_at, verified_at, first_missing_at,
    remote_deleted_at, auto_queue_suppressed, suppressed_reason, error_class, error_detail,
    state_changed_at, local_mtime, pending_download_prefix, arr_status, arr_status_at,
    arr_download_id, remote_delete_pending
FROM item;

DROP TABLE item;
ALTER TABLE item_new RENAME TO item;

CREATE INDEX idx_item_queue_id ON item (queue_id);
CREATE INDEX idx_item_state ON item (state);

-- Migration 006's triggers -- dropped along with the old `item` table above, so they must be
-- recreated verbatim against the rebuilt one (identical to migration 008's own recreation).
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
