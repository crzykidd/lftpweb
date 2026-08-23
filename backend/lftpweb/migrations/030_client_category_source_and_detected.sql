-- Category escape hatch + persisted detection (2026-08-23,
-- prompts/2026-08-23-path-attribution-and-category-escape-hatch.md, findings #2/#11/#14 in
-- prompts/test-findings-2026-08-23.md). Two additive changes, no rows migrated -- every existing
-- install behaves identically after this migration until a category is manually added or an
-- instance is re-tested (this project's "every new capability ships off" rule).

ALTER TABLE download_client_category ADD COLUMN source TEXT NOT NULL DEFAULT 'client'
    CHECK (source IN ('client', 'manual'));
    -- Mirrors download_client_base_path.source (migration 028) exactly, for the identical
    -- reason: whether this row was produced by detection (the client's own `list_categories`
    -- answer, or the base-path-arithmetic fallback) or typed by hand via the "Add category"
    -- escape hatch this task restores. rTorrent's `list_categories` is DERIVED -- it can only
    -- report labels *currently in use* -- so a category that will exist later (e.g. "ar-movies"
    -- before the first movie is grabbed) can never be detected, and the prior redesign
    -- (prompts/done/2026-08-23-category-binding-redesign.md) removed the only way to enter one.
    -- Defaults 'client': every row saved before this column existed came from the redesigned
    -- control's own detected/guessed rows, never free text (findings #11b/#11c already
    -- eliminated that possibility), so 'client' is the honest default for existing data; only a
    -- category added after this migration via the restored escape hatch is ever 'manual'.

ALTER TABLE download_client ADD COLUMN detected_categories_json TEXT;
ALTER TABLE download_client ADD COLUMN detected_categories_at TEXT;
    -- The last successful Test's own `detected_categories` (spec §8.3), persisted alongside the
    -- instance -- before this they lived only in the settings page's own `testResults[editingId]`
    -- (React state, gone on reload), so re-opening a saved instance for edit in a fresh session
    -- showed an empty "never tested" hint even though a previous session's Test had reported
    -- real data (finding #14's own follow-up: "the previous round chose to reword the hint
    -- instead of persisting; on the user's evidence that was the wrong call"). Both NULL until
    -- the first successful Test after this migration -- an existing install shows "never tested"
    -- exactly as before until then, this project's usual additive-migration default. Refreshed
    -- on every successful `POST /api/settings/clients/{id}/test`, the same call that already
    -- refreshes `capabilities_json`/`version`.
