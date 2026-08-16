-- The move-delete ladder (prompts/done/2026-08-16-move-delete-gate-ladder.md, resolving open
-- issue #2 / docs/audit-v0.1.0.md G1): a `move`-mode item's remote delete now waits on every
-- applicable rung -- completeness, verify, extract, and (only for an *arr-tracked item) *arr
-- import -- rather than firing between verify and extract as before this migration.
--
-- `core/postprocess.py._maybe_delete_remote` sets this column once verify+extract have both
-- cleared (rungs 1-3) but the item is *arr-tracked (`item.arr_status` non-null) and so must
-- also wait on rung 4: it stores the verify evidence ('VERIFIED' | 'SKIPPED') the delete will
-- eventually cite, so `core/arrsync.py`'s deferred delete -- fired on the confirmed `imported`
-- transition -- can write the same evidence-quality event message an immediate rung-3 delete
-- would, without re-deriving it from a run that already finished.
--
-- NULL means "nothing currently deferred to *arr import" -- the ordinary case for every
-- non-move queue, every non-*arr-tracked item, and any item still short of rung 3
-- (CORRUPT/EXTRACT_FAILED). `_maybe_delete_remote` explicitly clears it back to NULL on those
-- withhold/defer branches too, so a stale 'VERIFIED' from an earlier successful pass can never
-- authorize a delete for a release a later retry found CORRUPT.
ALTER TABLE item ADD COLUMN remote_delete_pending TEXT
    CHECK (remote_delete_pending IS NULL OR remote_delete_pending IN ('VERIFIED', 'SKIPPED'));
