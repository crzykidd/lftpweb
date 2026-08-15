-- Phase 4 (DESIGN.md §4.7): the per-queue "patterns-only" switch. When on, an empty select
-- list means "match nothing" instead of the default "match everything." `auto_queue_enabled`
-- already exists (migration 001, DEFAULT 0) -- this column follows the identical convention.
--
-- DEFAULT 0 is load-bearing, not incidental: every existing queue's row picks up this column
-- already off, so this migration cannot change what auto-queue does for anyone who already
-- has a queue configured. A capability that turns itself on for an existing row is a bug
-- (docs/decisions.md, this phase's non-negotiables).
ALTER TABLE path_queue ADD COLUMN auto_queue_patterns_only INTEGER NOT NULL DEFAULT 0
    CHECK (auto_queue_patterns_only IN (0, 1));
