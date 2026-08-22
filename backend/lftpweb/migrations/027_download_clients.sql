-- Download-client connector instances (docs/download-client-framework-spec.md, stage 1b of
-- #18: "the download-client instance row, its API, and test-connection"). Mirrors
-- 018_arr_integration.sql's shape and defaults -- three additive tables, no rows inserted, so
-- every existing install behaves identically after this migration (this project's "every new
-- capability ships off" rule).

CREATE TABLE download_client (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,              -- display name, e.g. "SABnzbd"
    client_type  TEXT NOT NULL,              -- core/clients registry key, e.g. "sabnzbd" --
                                              -- not a CHECK'd enum: the registry (a Python
                                              -- module-level dict, spec §6) is the source of
                                              -- truth for which types exist, not the schema.
    config_json  TEXT NOT NULL DEFAULT '{}', -- the connector-declared config schema's
                                              -- non-secret values (spec §8.1), e.g. base_url
    secret_enc   TEXT,                       -- the connector-declared secret value(s),
                                              -- encrypted at rest via core/crypto.py, exactly
                                              -- like arr_instance.api_key_enc and
                                              -- host.password_enc. NULL = no secret configured
                                              -- (a future connector's schema may declare none).
    enabled      INTEGER NOT NULL DEFAULT 0, -- defaults OFF, per project rule

    -- The probed capability layer (spec §4.1): refined at test_connection time from the
    -- connector's static declaration, persisted here so the settings UI never has to hit the
    -- client to render what it supports. NULL/NULL/NULL = never successfully probed yet.
    capabilities_json      TEXT,
    capabilities_probed_at TEXT,
    version                TEXT,             -- the client's own reported version (test_connection)

    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Multiple per instance (spec §8.2 -- a seedbox routinely spreads content across several base
-- paths). User-configured, browsed via the existing path-browse dialog, and validated on save
-- against core/browse.py.remote_directory_error -- this is the §10.2 delete containment check's
-- and §11's scan roots' security boundary, not a convenience field, so it is a genuine child of
-- the instance: deleting the instance deletes its base paths outright (ON DELETE CASCADE),
-- the same "true child record" pattern 001_initial_schema.sql's pattern/job/event tables use
-- against their own parents, not the soft cross-reference pattern below.
CREATE TABLE download_client_base_path (
    id        INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES download_client (id) ON DELETE CASCADE,
    path      TEXT NOT NULL
);

-- Category -> queue binding (spec §8.3): a client instance is site-level, not per-queue, so
-- attribution comes from a configured category -> queue mapping rather than a second instance.
-- `client_id` is the same genuine-child relationship as the base-path table above (CASCADE).
-- `queue_id` is the soft cross-reference -- mirrors how 018_arr_integration.sql's own
-- `path_queue.arr_instance_id` handles its parent going away (ON DELETE SET NULL): deleting a
-- queue un-binds the mapping (queue_id -> NULL) rather than destroying the category row itself,
-- since the category name is still meaningful configuration even with nothing to route to yet.
CREATE TABLE download_client_category (
    id        INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES download_client (id) ON DELETE CASCADE,
    category  TEXT NOT NULL,
    queue_id  INTEGER REFERENCES path_queue (id) ON DELETE SET NULL
);
