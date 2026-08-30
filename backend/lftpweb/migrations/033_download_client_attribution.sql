-- Which download-client instance fetched an item (prompts/2026-08-30-downloader-icon-on-rows.md,
-- docs/download-client-framework-spec.md §8.4) -- mirrors 018_arr_integration.sql's own shape:
-- an additive, nullable column pair on `item`, no rows touched, so every existing install
-- behaves identically after this migration until the poller (`core/clientsync.py.
-- ClientSyncScheduler._write_client_attribution`) actually writes one.

ALTER TABLE item ADD COLUMN download_client_id INTEGER
    REFERENCES download_client (id) ON DELETE SET NULL;
    -- NULL = no client recorded -- either every item downloaded before this migration shipped
    -- (forward-only, below), or one this poller has never matched a transfer's own `content_path`
    -- to. `ON DELETE SET NULL`, not CASCADE: deleting a download-client instance in Settings must
    -- never take the item down with it -- the item is still real, downloaded, real bytes on disk;
    -- only the record of *who fetched it* goes away, the same "the child is real, the reference to
    -- a removed parent isn't" reasoning `path_queue.arr_instance_id` (migration 018) and
    -- `download_client_category.queue_id` (migration 027) both already apply to their own parents.
ALTER TABLE item ADD COLUMN download_client_matched_at TEXT;
    -- When `download_client_id` was first set, or last changed to a *different* instance (a
    -- release genuinely re-fetched by a different client) -- never rewritten on an unchanged
    -- repeat match against the same instance, `core/clientsync.py`'s own "write once and leave it"
    -- rule. Not nulled when the referenced instance is deleted (SQLite has no trigger to do that
    -- as a side effect of the `ON DELETE SET NULL` above, and there is no need to invent one --
    -- once `download_client_id` reads NULL, this timestamp is simply unread by anything).

-- **Forward-only, by explicit, informed user decision -- no backfill guesses one.** The
-- alternative considered and rejected was resolving this live, at *read* time, from the poller's
-- own in-memory transfer cache (the same `content_path` match `_write_client_attribution` uses,
-- run on demand instead of persisted) -- rejected because SABnzbd and rTorrent both age old jobs
-- out of their own history/queue, which would make a History row's icon silently vanish the
-- moment the client forgets a job that lftpweb still remembers forever. Persisting once, forward
-- only, means the icon is either right or absent -- never flickering. Every item downloaded before
-- this migration ships has no recorded client and never will; see docs/decisions.md.
