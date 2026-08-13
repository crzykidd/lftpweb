-- Per-queue archive cleanup (prompts/2026-08-13-per-queue-archive-cleanup.md). Archive cleanup
-- (`PostprocessSettings.delete_archives_after_extract`, migration 010) shipped site-only --
-- the odd one out among the four post-processing steps, all of which DESIGN.md §6 describes as
-- "toggleable globally *and* per path queue" (`auto_verify`/`auto_extract`/`auto_move`,
-- migrations 001/003). It is also the most destructive of the four: on a `move` queue the
-- remote copy is already gone by the time cleanup runs, so the archive volumes it removes are
-- the last copy of those compressed bytes anywhere.
--
-- `DEFAULT 0`, the same non-negotiable every other per-queue post-processing column follows
-- (migration 003's own comment): a new capability changes nothing for an existing install --
-- ANDed with the site-wide flag exactly like `auto_verify`/`auto_extract`/`auto_move` are
-- (`core/postprocess.py.process_item`), never a tri-state.
ALTER TABLE path_queue ADD COLUMN auto_delete_archives INTEGER NOT NULL DEFAULT 0
    CHECK (auto_delete_archives IN (0, 1));
