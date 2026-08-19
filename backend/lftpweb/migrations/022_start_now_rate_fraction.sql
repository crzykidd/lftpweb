-- "Start now" becomes a menu -- 10% / 25% / 50% / 75% / Max of the site bandwidth limit
-- (2026-08-19, prompts/done/2026-08-19-start-now-bandwidth-fractions.md), a deliberate
-- extension of DESIGN.md §4.5's "Start now at max bandwidth" escape hatch. Before this, the
-- only choice was Max (`job.forced_full_rate`, migration 001) -- the button forced admission
-- at the full site ceiling, unconditionally.
--
-- **Widened in place at the application layer, additive at the schema layer.** DESIGN.md §4.5's
-- `AdmitDecision`/`QueuedJob` now carry `forced_rate_fraction: float | None` throughout
-- (`core/scheduler.py`, `core/queue.py`) -- `1.0` reads as byte-identical to today's Max, `None`
-- as "not forced" -- rather than a fraction living alongside a separate boolean at that layer.
-- The column below is a plain `ADD COLUMN`, this repo's only migration shape (no prior migration
-- drops or renames a column; SQLite's `ALTER TABLE ... DROP COLUMN` would need a full table
-- rebuild this codebase has never done) -- `forced_full_rate` stays exactly as migration 001
-- defined it, still written on every start-now/enqueue path, still what the raw-SQL job-row
-- inserts in `tests/test_queue.py` set directly. The two columns are kept in lockstep by every
-- writer from this task forward: `forced_full_rate = 1` if and only if `forced_rate_fraction`
-- is not NULL. `docs/decisions.md` has the full call, including why a parallel column (not a
-- widened/replaced one) is what "additive-only migrations" actually buys here.
--
-- NULL means "this job was never force-started" -- every job before this task, and every job
-- queued normally after it. A non-NULL value is the fraction of the site's `max_bandwidth_bps`
-- (Settings -> Transfer) it was force-admitted at: 0.1/0.25/0.5/0.75 for the new menu options,
-- 1.0 for Max. The CHECK bounds it to (0, 1] -- a fraction can't be zero (that's "not forced",
-- i.e. NULL) or exceed the site ceiling (fractions *of* the limit, never past it -- unlike Max's
-- own deliberate oversubscription of *other jobs'* allocations, which this column doesn't touch).
ALTER TABLE job ADD COLUMN forced_rate_fraction REAL
    CHECK (forced_rate_fraction IS NULL OR (forced_rate_fraction > 0 AND forced_rate_fraction <= 1));

-- Backfill: every already-forced row predates fractions and was always Max.
UPDATE job SET forced_rate_fraction = 1.0 WHERE forced_full_rate = 1;
