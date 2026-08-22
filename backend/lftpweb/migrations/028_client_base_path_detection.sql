-- Base paths become detected-from-the-client and SSH-verified, not typed in (docs/download-
-- client-framework-spec.md §8.2 correction, 2026-08-22; prompts/2026-08-22-client-base-paths-
-- detected.md). The earlier "user-configured, client is a prefill" reasoning (migration 027)
-- was wrong: the real reason user input is ever needed is path-namespace translation, which
-- this repo already solved for the *arr with `path_queue.arr_visible_path` (018_arr_
-- integration.sql). These columns mirror that design, inverted to match this direction --
-- three additive columns, no rows to migrate (migration 027 shipped no data), so every
-- existing install behaves identically after this migration until a client is re-tested
-- (this project's "every new capability ships off" rule).

ALTER TABLE download_client_base_path ADD COLUMN kind TEXT NOT NULL DEFAULT 'unknown'
    CHECK (kind IN ('content', 'working', 'unknown'));
    -- The role this path plays (core.clients.models.BasePathKind) -- not cosmetic, it decides
    -- what deleting there means (spec §10.5): freeing a `content` root that is hardlinked from
    -- a seeding torrent frees nothing; freeing a `working` root frees the space and kills the
    -- seed. Defaults `unknown` so a manually-added path (no connector ever classified it) is
    -- honest rather than mislabelled.

ALTER TABLE download_client_base_path ADD COLUMN client_path TEXT;
    -- This base path AS THE CLIENT ITSELF SEES IT (its own container/host namespace), when it
    -- differs from `path`. NULL = no translation needed -- the client and lftpweb agree.
    -- `path` stays the SSH-visible path lftpweb actually scans and deletes within (spec
    -- §10.2's containment boundary); `client_path` records the client's own view for display
    -- and diagnosis only. Exactly `path_queue.arr_visible_path`'s semantics, inverted: that
    -- column records the foreign (*arr) view of an lftpweb-native path; this one records the
    -- foreign (client) view of an lftpweb-native path -- same split, same reason, opposite
    -- direction.

ALTER TABLE download_client_base_path ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
    CHECK (source IN ('detected', 'manual'));
    -- Whether this row came from detection (the client's own `list_base_paths` answer, SSH-
    -- verified) or was typed by hand via the manual-add escape hatch (a path the connector
    -- doesn't expose). Lets a re-detect leave manual rows -- and any translation the user
    -- already supplied for a detected one -- alone rather than clobbering them.
