-- Auto-persisted observed categories, defaulting to "not used here" + the calm "new since you
-- last looked" signal (2026-08-23, prompts/2026-08-23-auto-add-categories-default-excluded.md,
-- three defects reported from live use the same day: a poller-observed category never reached
-- Settings at all; a newly recorded category landed undecided, not excluded, which is unsafe for
-- the two-lftpweb-instances-one-seedbox shape finding #16 named; and silencing the unattributed
-- banner for excluded categories meant the user's own genuinely new category could go unnoticed
-- indefinitely with no nagging warning to catch it). Two additive columns, no rows migrated -- an
-- existing install's categories are all pre-existing (`first_seen_at IS NULL`), so nothing looks
-- "new" the moment this migration runs.

ALTER TABLE download_client_category ADD COLUMN first_seen_at TEXT;
    -- Stamped only by `core.clientsync.persist_observed_categories` when it inserts a category
    -- this instance has never reported before (whether observed by a poll pass or a Test -- "any
    -- category observed by EITHER route is recorded," this task's own fix for the first defect).
    -- NULL for every row that predates this migration, and NULL forever for a category a person
    -- typed by hand (the manual "Add category" escape hatch) or that survives a Settings save
    -- (`api/settings_clients.py._replace_categories` carries a pre-existing row's own
    -- `first_seen_at` across its delete-then-reinsert, but a save's own newly-introduced row gets
    -- NULL, not `now()` -- the user just typed it, so it was never "new to them" the way an
    -- unattended observation is). NULL therefore doubles as "never flag this row's own novelty,"
    -- which is exactly right for all three origins above.

ALTER TABLE download_client ADD COLUMN categories_acknowledged_at TEXT;
    -- The other half of the "new since you last looked" signal (`ClientsTab.tsx`) -- stamped by
    -- the new `POST /api/settings/clients/{id}/acknowledge-categories`, fired the moment a person
    -- opens this instance for edit (no separate button, no confirmation -- this project's own
    -- "fewer clicks, not confirmations" house style). A category counts as new when its own
    -- `first_seen_at` postdates this column's value (or this column is still NULL -- "never
    -- acknowledged," so every observed-but-unseen category counts). This is the replacement for
    -- the unattributed-clients banner's old always-on nagging for an undecided category: now that
    -- a newly observed category defaults to excluded and the banner falls silent for it, this
    -- column is what still lets a person notice their OWN brand-new category arrived, without
    -- reintroducing a warning that never stops.
