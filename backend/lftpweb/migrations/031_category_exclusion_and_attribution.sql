-- Three-state categories, path exclusion as a safety boundary, and observed attribution stats
-- (2026-08-23, prompts/2026-08-23-category-tristate-and-exclusion.md, findings #15/#16 in
-- prompts/test-findings-2026-08-23.md). Three additive changes, no rows migrated -- every
-- existing install behaves identically after this migration until a category is explicitly
-- marked "not used," an excluded path is added, or the poller runs its next pass.

ALTER TABLE download_client_category ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0
    CHECK (excluded IN (0, 1));
    -- Finding #15: a category row already carried `queue_id` (bound) or `NULL` (everything
    -- else, undecided and "deliberately not used" collapsed into one state). This is the third
    -- state, saved explicitly rather than inferred from the dropdown's own default -- "not used"
    -- must be a decision on file, not the absence of one, or the unattributed-clients banner
    -- (`core/clientsync.py`) nags forever about a category the user has already dismissed.
    -- Mutual exclusion with `queue_id` (a row is never both bound *and* excluded) is enforced at
    -- the API layer (`DownloadClientCategoryIn`'s own validator), not by a table-level CHECK --
    -- SQLite's `ALTER TABLE ADD COLUMN` cannot add a multi-column constraint without a full
    -- table rebuild, and this project's other cross-column invariants (e.g. base-path kind vs.
    -- source) are enforced the same way. Defaults `0`: every row saved before this column
    -- existed is exactly as undecided as it always looked, which is honest -- nothing before
    -- this migration could have recorded "not used" at all.

CREATE TABLE download_client_excluded_path (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES download_client (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Finding #16's own enforceable primitive: "implement path exclusion as the enforceable
-- primitive and category exclusion as the convenience that resolves into it where it can."
-- A row here means "never scan, never propose as debris, never inside the delete containment
-- boundary" for this path (or anything under it) on this client's behalf
-- (`core/disk_review.py.reconcile`'s new `excluded_paths` parameter) -- the direct expression of
-- "this tree belongs to the other lftpweb instance sharing this seedbox," which is the only
-- primitive that works for a connector (rTorrent) whose category has no relationship to any
-- path at all. A category marked `excluded` above is resolved into rows *like* this one at scan
-- time (`core/disk_review.py.run_scan`), computed fresh from the client's current base paths
-- rather than persisted redundantly here -- so this table only ever holds paths a person typed
-- directly, never a derived one, and the two can never drift apart from each other.
-- `ON DELETE CASCADE` mirrors `download_client_base_path`/`download_client_category` exactly:
-- a genuine child record of the instance, gone when it is.

ALTER TABLE download_client ADD COLUMN attribution_sample_size INTEGER;
ALTER TABLE download_client ADD COLUMN attribution_matched_by_path INTEGER;
    -- Part 3: "derive the per-client 'do you even need this control' copy from OBSERVED
    -- attribution counts ... never from client_type." Written every poll pass
    -- (`core.clientsync.ClientSyncScheduler._update_preflight`, the same "success writes"
    -- pattern `_persist_capabilities`/`_persist_detected_categories` already use) --
    -- `attribution_sample_size` is how many of this pass's transfers had something to attribute
    -- (a `content_path` or a `category`), `attribution_matched_by_path` is how many of those
    -- were resolved by path alone, needing no category mapping. Both `NULL` until the first
    -- pass after this migration -- an existing install reads as "not yet observed," never a
    -- fabricated 0-of-0.
