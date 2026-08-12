-- Phase 5 (DESIGN.md §6): the third per-queue post-processing toggle. `auto_verify` and
-- `auto_extract` already exist (migration 001) but were never wired to an API/UI field --
-- this migration adds their sibling, `auto_move` (staging -> final destination), and does
-- not touch the two existing columns at all.
--
-- DEFAULT 0 is load-bearing, not incidental, per this phase's own non-negotiable (see
-- docs/decisions.md and prompts/startnewsession.md's "SAFETY RULE for the unattended run"):
-- every existing queue's row -- including the user's one live queue -- picks up this column
-- already off. This migration makes no other change to any existing row; in particular it
-- does NOT touch `sync_mode`, which is a separate, deliberate decision recorded in
-- docs/decisions.md (the user's live queue already has `sync_mode = 'move'` stored from
-- before phase 4's guard existed, and this phase leaves that row exactly as it is).
ALTER TABLE path_queue ADD COLUMN auto_move INTEGER NOT NULL DEFAULT 0
    CHECK (auto_move IN (0, 1));
