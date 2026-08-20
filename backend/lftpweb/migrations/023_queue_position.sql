-- Replace the queue's boost-based ordering with a dense position model (2026-08-19,
-- docs/transfers-redesign-spec.md §3.4/§3.5, prompts/done/2026-08-19-queue-position-order-model.md
-- -- phase 1, stage 1). This is the prerequisite for per-row "move up one / down one"
-- reordering: `rank DESC, queued_at ASC` is a two-zone boost scheme (boosted zone, most-
-- recently-boosted first; natural zone, oldest-`queued_at`-first) that "Move to top" fits but
-- "move up one" structurally cannot -- see the prompt above for the three concrete ways it
-- breaks. `job.queue_position` is a **fractional** total order (`REAL`, not an integer level):
-- new jobs take `MAX(queue_position) + 1`, a move between two neighbours takes their midpoint
-- -- one `UPDATE`, no renumbering of anything else. New ordering key, everywhere: **`queue_position
-- ASC, id ASC`** (the `id ASC` final tiebreak was always implicit before -- ties on `rank`/
-- `queued_at` fell back to SQLite's own row order, which happened to be insertion/id order; it
-- is now explicit).
--
-- **Backfill.** Every currently-queued job gets an ascending position (1, 2, 3, ...) assigned
-- in the same order the *old* `rank DESC, queued_at ASC, id ASC` query would have served it, so
-- an upgrade does not reshuffle a deep production backlog. Deliberately scoped to every row in
-- the table, not just `state = 'queued'` ones: `TransferQueue.list_jobs()` (the Transfers page's
-- row set) sorts `running`/terminal rows by this same column too, and a `NULL` there would sort
-- first under SQLite's default NULL-ordering -- an unpositioned old row jumping to the top of a
-- *display* list is a smaller bug than a live queue reshuffle, but there's no reason to leave it
-- there when a single backfill pass covers every row for free.
--
-- (2026-08-19, mid-task direction from the maintainer: exact backfill-order preservation was
-- downgraded from a hard acceptance criterion to a nice-to-have -- a fresh install has no
-- production backlog to reshuffle, and existing installs were told this explicitly would not be
-- reshuffled if it stayed cheap. It stayed cheap -- `ROW_NUMBER() OVER (...)` is a single
-- ordinary `UPDATE`, not awkward SQL -- so the exact-order backfill shipped as designed. The
-- correctness floor that was *not* relaxed: every row gets a real, non-NULL, distinctly-ordered
-- `queue_position` -- see the correlated subquery below.)
ALTER TABLE job ADD COLUMN queue_position REAL;

UPDATE job SET queue_position = (
    SELECT ordered.rn
    FROM (
        SELECT id, ROW_NUMBER() OVER (ORDER BY rank DESC, queued_at ASC, id ASC) AS rn
        FROM job
    ) AS ordered
    WHERE ordered.id = job.id
);

-- **`rank` is left in place, not dropped.** SQLite's `ALTER TABLE ... DROP COLUMN` needs a full
-- table rebuild this codebase has never done for `job` (see migration 022's own comment on why
-- this repo's migrations are additive-only), and dropping the column the admission path used
-- until this exact release is not a risk worth taking in the same change that introduces its
-- replacement. `rank` is vestigial for **ordering** as of this migration -- the admission query
-- and `core/scheduler.py.QueuedJob` no longer read it at all -- but it is *not* dead: it keeps
-- being written by `TransferQueue.move_to_top` (unchanged) and is read in exactly one new,
-- narrow place, `TransferQueue._rescue_position` (`core/queue.py`), as an is-this-job-currently-
-- boosted marker -- `rank = 0` identifies the "natural zone" jobs whose `queue_position` is
-- guaranteed ordered the same as their `queued_at` (append-only), which the v0.2.6 startup-
-- rescue's re-derived positioning needs to stay correct without ever landing a rescued job ahead
-- of an explicit "Move to top" (see that method's own docstring for the counterexample this
-- guards against, and tests/test_queue_orphans.py's rescue-position tests for the proof). A
-- later cleanup, once the position model has run in production, can drop `rank` outright.
