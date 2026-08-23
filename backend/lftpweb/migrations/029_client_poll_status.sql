-- Download-client poll status (finding #2, 2026-08-23,
-- prompts/2026-08-23-tilde-and-visibility.md) -- before this, a fully-working, authenticating,
-- enabled client instance was indistinguishable from a broken one on the Clients page: the only
-- thing that ever proved the poller had touched an instance was it failing
-- (`core/clientsync.py`'s own audit events fire only on a failure *transition*, spec §9), and the
-- page's own "Test" column reflects the last manual click, not the poller's own most recent pass
-- -- an instance whose credential broke after setup looked exactly as green as one still working.
--
-- Four additive columns, all NULL by default -- an existing install renders every instance "never
-- polled" until the poller's next pass runs, the same "every new capability ships off/unknown"
-- rule this project's other migrations follow, never a false "healthy" or "broken" guess.
--
--   last_poll_at      -- when the poller last actually attempted this instance (success or not).
--   last_poll_ok       -- 1/0/NULL: the outcome of that attempt. NULL means "never attempted."
--   last_poll_message  -- `core/clientsync.py._FAILURE_VERB`'s own wording on a failure ("rejected
--                         the configured credential", "unreachable", ...), NULL on a success --
--                         so "credential rejected" reads as that, never as "unreachable" (the
--                         same distinction the audit log already draws, now on this row too).
--   last_success_at    -- the positive signal this finding asked for: when this instance last
--                         reported successfully, independent of `last_poll_at`/`last_poll_ok`
--                         which describe only the *most recent* attempt. Also doubles as "has
--                         this instance ever worked at all" (NULL = never).
--
-- Written every poll pass (`core/clientsync.py._record_poll_result`), not just on a transition --
-- unlike the audit *event* log, which stays transition-only on purpose (spec: "not per failed
-- pass, or a dead client floods the event log"). A per-pass row UPDATE is not a log entry; it
-- costs one cheap write to one row and never accumulates, so the "no per-poll event" rule this
-- task's own handoff prompt states does not apply to it.

ALTER TABLE download_client ADD COLUMN last_poll_at TEXT;
ALTER TABLE download_client ADD COLUMN last_poll_ok INTEGER;
ALTER TABLE download_client ADD COLUMN last_poll_message TEXT;
ALTER TABLE download_client ADD COLUMN last_success_at TEXT;
