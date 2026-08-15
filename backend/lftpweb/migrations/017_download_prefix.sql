-- "Folder prefix during transfer" (2026-08-14, prompts/2026-08-14-in-flight-folder-prefix.md):
-- a directory item downloads into `<local_path>/<prefix><name>/` instead of `<local_path>/<name>/`
-- so an importer polling the download tree can never see a partial multi-file `mirror` release
-- (live incident: Sonarr imported finished episodes, then deleted the release folder while lftp
-- was still writing the last two). See `core/download_prefix.py` for the full design and
-- docs/decisions.md for why this reverses part of phase 5's `staging_path` reasoning.
--
-- Per-queue is inherit-or-override (`3500b3f`'s shape), not the AND-of-two-toggles that commit
-- deliberately removed elsewhere -- `NULL` on either column means "inherit the matching
-- site-wide `DownloadPrefixSettings` field," resolved independently per field
-- (`core/download_prefix.py.resolve_for_queue`). Both nullable, no CHECK needed on
-- `download_prefix_enabled` beyond the same `IN (0, 1)` shape migration 015 already uses for
-- the post-processing toggles -- consistent with this table's existing nullable-boolean
-- convention. A plain `ADD COLUMN` is enough for both (migration 009's own precedent for a
-- nullable column with a CHECK, migration 011's for one without): this migration changes
-- nothing about what any existing queue does the moment it runs -- every existing row inherits
-- the site-wide default, which is itself off (`core/download_prefix.py.DownloadPrefixSettings`,
-- this project's "every new capability ships off" rule).
ALTER TABLE path_queue ADD COLUMN download_prefix_enabled INTEGER
    CHECK (download_prefix_enabled IN (0, 1));
ALTER TABLE path_queue ADD COLUMN download_prefix TEXT;

-- `item.pending_download_prefix` -- the prefix string actually in use for an item's *current*
-- (or most recently interrupted) transfer, `NULL` once nothing is in flight under a prefixed
-- name. Set once, at spawn (`core/queue.py._spawn_decision`), fixed for that job's lifetime --
-- the same "fixed at spawn, never re-shaped" convention DESIGN.md §4.5 already uses for a job's
-- bandwidth allocation -- and cleared only once `_reap_one` renames the directory back to its
-- real name on successful completion. This is what makes a *stale* prefix (the site or queue
-- setting changed, or the feature was turned off, while an item was mid-transfer or sat
-- `STOPPED`) safe rather than orphaning: a resume always re-reads this column first and reuses
-- whatever is physically on disk instead of recomputing from today's settings, and every scan
-- of this queue folds every non-NULL value of this column into `core/local_scan.py`'s filter
-- alongside today's resolved prefix, so a directory written under an old prefix never becomes a
-- phantom LOCAL_ONLY node just because the setting moved on. See `core/queue.py._spawn_decision`
-- and `core/engine.py.Engine._active_download_prefixes`.
ALTER TABLE item ADD COLUMN pending_download_prefix TEXT;
