-- state_changed_at: when this row's `state` last actually changed value. Backs the Files
-- page's "Downloaded 3 min ago" / "Remote 2 hr ago" readout -- one timestamp meaning "when
-- did this row last move," not a column per state (DESIGN.md §3.2 rule 9, proposed wording,
-- not yet approved -- see docs/decisions.md).
--
-- Enforced with triggers, not writer discipline, because `item.state` is written from three
-- separate modules -- `core/engine.py._persist`'s two `INSERT ... ON CONFLICT DO UPDATE`
-- statements, `core/queue.py` (QUEUED/DOWNLOADING/DOWNLOADED/STOPPED/FAILED), and
-- `core/postprocess.py` (`_set_item_state` plus the verify/extract branches). Requiring
-- every one of them to also stamp a timestamp guarantees one gets missed eventually, and a
-- timestamp that is silently wrong is worse than no timestamp at all.
--
-- Deliberately NOT the source for the planned local-retention feature, which must key on
-- `downloaded_at` instead: "when did it complete" and "when did it last move" are different
-- questions, and a DOWNLOADED item that dips to PARTIAL and back would otherwise earn a
-- fresh retention lease it never actually earned.
ALTER TABLE item ADD COLUMN state_changed_at TEXT;

-- Approximate backfill for every row that predates this migration: the closest thing already
-- on the row to "when did the current state begin," most-specific first. Exact from this
-- migration forward (the triggers below); a guess for everything already in the database.
UPDATE item SET state_changed_at = COALESCE(extracted_at, verified_at, downloaded_at, first_seen_at)
WHERE state_changed_at IS NULL;

-- New rows (a first-sighted item, whatever its initial state) get a value at insert time.
-- Deliberately NOT a column DEFAULT: SQLite's `ALTER TABLE ADD COLUMN` refuses a non-constant
-- default expression the moment the table already has rows ("Cannot add a column with
-- non-constant default"), and every real database this migration will ever run against does
-- (that's exactly why the backfill above exists). A rebuild-the-table approach would dodge
-- that restriction too, but is a much larger blast radius (recreate `item` with all its
-- CHECKs/FK/UNIQUE, copy every row, swap it in) for the same outcome an AFTER INSERT trigger
-- gets in four lines, in the same transaction, with no risk to existing data. See
-- docs/decisions.md.
CREATE TRIGGER item_state_changed_at_insert
AFTER INSERT ON item
WHEN NEW.state_changed_at IS NULL
BEGIN
    UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;

-- Every later state change, from any of the three writers, moves the stamp -- gated on
-- `IS NOT` so a rescan that persists the *same* state (the common case: a quiet item on its
-- 30s pass) does not churn the clock. Safe against re-entry: the trigger's own UPDATE touches
-- only `state_changed_at`, never `state`, so it can never re-trigger itself.
CREATE TRIGGER item_state_changed_at
AFTER UPDATE OF state ON item
WHEN NEW.state IS NOT OLD.state
BEGIN
    UPDATE item SET state_changed_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = NEW.id;
END;
