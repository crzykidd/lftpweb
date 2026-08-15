-- The settle gate (DESIGN.md §3.3, written alongside this migration and applied to the
-- document on 2026-08-12 -- see docs/decisions.md). A seedbox may still be writing a
-- top-level item when a scan observes it. Comparing remote-vs-local bytes
-- (core/reconcile.py) cannot tell a genuinely finished item from one still arriving one file
-- at a time: a release directory holding 3 of an eventual 8 files, each of those 3 fully
-- arrived, reads as complete by every byte-comparison rule DESIGN.md §3.2 has.
--
-- This table gives every top-level item (DESIGN.md §4.7's granularity -- the same one
-- core/autoqueue.py already uses) a second signal that CAN tell the difference: whether the
-- fingerprint of its whole remote subtree -- (file_count, total_bytes, max_mtime),
-- core/settle.py.compute_fingerprints -- has stopped changing across REQUIRED_SETTLE_SCANS
-- (currently 2) consecutive scans. One row per (queue, top-level rel_path); nested children
-- are not tracked here -- they inherit their root's verdict, per the agreed design.
--
-- Persisted, not kept in memory only, for two reasons: it must survive a restart (an item
-- mid-upload when lftpweb restarts shouldn't lose its settle progress and start over), and --
-- decisively -- nothing may publish a state it did not read back from a table (DESIGN.md's own
-- invariant, `core/itemview.py`'s module docstring); an in-memory counter could never be the
-- source for `item.substate = 'settling'` on the wire.
CREATE TABLE item_settle (
    queue_id      INTEGER NOT NULL REFERENCES path_queue (id) ON DELETE CASCADE,
    rel_path      TEXT NOT NULL,
    file_count    INTEGER NOT NULL,
    total_bytes   INTEGER NOT NULL,
    max_mtime     REAL,  -- NULL when the item has no remote files at all yet (a bare directory)
    matched_scans INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (queue_id, rel_path)
);
