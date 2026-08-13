-- Delete-archives-after-extract (prompts/2026-08-13-delete-archives-after-extract.md). Once an
-- item's `.rar`/`.r00`/... volumes have been fully, successfully extracted, they are dead
-- weight on local disk -- but simply `unlink`ing them reproduces the exact bug reverted in
-- `6d3bd95` (`REMOVED_LOCAL`, prompts/open-issues.md "4"): local drops below remote, the next
-- scan reads `PARTIAL` (DESIGN.md §3.2 rule 2), and rule 9 says `PARTIAL` beats any
-- post-processing outcome -- so `EXTRACTED` would not protect the item and auto-queue
-- re-fetches, re-extracts, re-deletes it every scan interval, forever.
--
-- `core/patterns.py.build_counts_predicate` already solves this exact shape for a different
-- cause (a `file_exclude` pattern): a file the predicate rejects is marked `EXCLUDED`, a real
-- state, and stops counting toward its parent directory's completeness (DESIGN.md §3.2 rule 8,
-- §4.7). This table is the data-driven analogue for a *deletion this codebase performed*
-- rather than a pattern match: one row per file `core/local_delete.py.delete_extracted_
-- archives` actually removed, so `core/engine.py.scan_queue` can fold membership in this table
-- into the same counts_predicate seam it already builds from patterns -- one completeness
-- rule, fed from two sources, never a second rule.
--
-- Persisted, not kept in memory, for the same reason `item_settle` (migration 007) is: the
-- reconciler must reach the same conclusion after a restart with only the database and the
-- filesystem, and nothing may publish a state it did not read back from a table.
--
-- No `ON DELETE CASCADE` cleanup beyond the queue itself going away (dropping a queue drops its
-- rows here too, like every other per-queue table) -- an `item` row is never deleted
-- (`core/engine.py._project`'s own docstring), so there is no "the item went away, garbage
-- collect its deleted-archive rows" case to handle. A `rel_path` that later reappears with a
-- genuinely new file of the same name would still read `EXCLUDED` from a stale row here; this
-- is a known, accepted limitation of the same shape `item_settle`/`REMOVED_BOTH` already have
-- (nothing in this codebase retroactively clears bookkeeping when a path's remote identity
-- changes without the row's own `rel_path` changing) -- see docs/decisions.md.
CREATE TABLE deleted_archive (
    queue_id   INTEGER NOT NULL REFERENCES path_queue (id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (queue_id, rel_path)
);

CREATE INDEX idx_deleted_archive_queue ON deleted_archive (queue_id);
