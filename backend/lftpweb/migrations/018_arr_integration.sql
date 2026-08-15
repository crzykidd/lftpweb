-- Sonarr/Radarr integration, phase A ("backend foundation" --
-- prompts/2026-08-15-arr-integration-backend.md, docs/arr-integration-spec.md "Data model").
-- Exactly the schema the spec specifies -- no rows inserted, so every existing install behaves
-- identically after this migration: icons need an instance created + enabled + bound, three
-- explicit acts (this project's "every new capability ships off" rule).

CREATE TABLE arr_instance (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,                 -- display name, e.g. "Sonarr", "Radarr 4K"
    kind        TEXT NOT NULL CHECK (kind IN ('sonarr','radarr')),
    base_url    TEXT NOT NULL,                 -- e.g. https://sonarr.crzynet.com
    api_key_enc TEXT NOT NULL,                 -- encrypted at rest via core/crypto.py,
                                               -- exactly like the seedbox password
    enabled     INTEGER NOT NULL DEFAULT 0,    -- defaults OFF, per project rule
    notify_on_complete INTEGER NOT NULL DEFAULT 0,  -- push the scan command after postprocess
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

ALTER TABLE path_queue ADD COLUMN arr_instance_id INTEGER
    REFERENCES arr_instance (id) ON DELETE SET NULL;   -- NULL = no integration
ALTER TABLE path_queue ADD COLUMN arr_delete_completed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE path_queue ADD COLUMN arr_visible_path TEXT;
    -- this queue's local_path AS THE BOUND *ARR SEES IT (its container/host namespace).
    -- NULL = same namespace, no translation. See docs/arr-integration-spec.md "Path namespaces".

ALTER TABLE item ADD COLUMN arr_status TEXT;           -- NULL | detected | notified
                                                       -- | imported | cleaned | gone
ALTER TABLE item ADD COLUMN arr_status_at TEXT;        -- when arr_status last changed
ALTER TABLE item ADD COLUMN arr_download_id TEXT;      -- the *arr queue record's downloadId
                                                       -- (infohash), recorded at match time;
                                                       -- makes history lookup exact. Not
                                                       -- published in the item projection.
