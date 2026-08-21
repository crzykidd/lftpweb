"""**The one predicate that splits the Queue tab's two boxes** (2026-08-20,
docs/transfers-redesign-spec.md §3.2, `prompts/done/2026-08-20-active-box-holds-inflight-
pipeline.md`) -- "is this release still moving?"

Phase 1 stage 4b split Active/pending from Complete on **job termination**: lftp exits 0, the row
moves to Complete. The user's own browser review, and they were right: *"Shouldn't a job live in
that state until the sonarr/radarr hook lands if they are enabled? Currently they move to complete
but they technically aren't."* The item's pipeline continues well past the job -- verify, extract,
staging move, notify the *arr, wait for a confirmed import, delete the seedbox source. A row sat
under "Complete" while the release demonstrably was not.

**The rule, decided with the user 2026-08-20: split on pipeline completion, not job termination,
and apply it consistently whether or not a queue is *arr-bound.** The consistent rule was chosen
explicitly over a narrower *arr-only one: one definition of "done," because post-processing a
large release is not instant either.

**Why this module exists at all: the two boxes MUST use one definition.** The Active box is
client-side over `TransferQueue.list_jobs()`; the Complete box is a *server-side paginated* query
(`list_complete_jobs`) with its own `total`; and "Dismiss list"/the Dismiss menu
(`dismiss_all_terminal`) is a third `WHERE` that has to agree with the Complete box's `total` (a
property that already has its own test). Written separately, those three would drift, and a row
would show up in **both boxes or neither**, with the readout disagreeing with what's on screen.
So the predicate is **SQL text, defined exactly once, here**, and every caller pastes the *same
string* into its own query:

- `list_jobs()` selects it as `pipeline_in_flight` (plus `pipeline_waiting_reason`) -- the client
  never re-derives it,
- `list_complete_jobs()` puts `NOT (...)` in both its page query and its `COUNT(*)`,
- `dismiss_all_terminal()` excludes the same item ids (`item_pipeline_busy_subquery`), so an
  in-flight row can't be bulk-dismissed out from under the work still being done on it.

There is no Python mirror of this expression, deliberately: a second encoding is the drift.

## The four blocking conditions -- and every one of them has a bounded exit

A row belongs in Active/pending while **any** of these hold. The load-bearing design constraint
is the second half of each bullet: *rows must not be able to accumulate here forever*, or the box
silently stops being trustworthy, which is worse than the bug this fixes.

1. **The job itself is `queued`/`running`** -- today's rule, unchanged. Exits when lftp exits.

2. **Post-processing is in flight** -- `PostprocessPipeline.in_flight_item_ids()`, passed in as a
   literal id set, **not** `item.state IN ('VERIFYING','EXTRACTING')`. That is the whole point of
   that method (see `core/postprocess.py`'s `TRANSIENT_STATES` comment): a row still carrying a
   transient state after a restart means the worker *died*, not that work is in progress. Keying
   off the live worker's existence means a crashed worker cannot wedge an item here -- the set is
   in-memory, so it is empty the instant the process that owned it is gone.

3. **The queue is bound to a *currently enabled* *arr instance and `arr_status` is non-terminal**
   (`detected`/`notified`/`dropped`). Three exits, in order of how they actually fire:

   - **`enabled = 1`, not merely "an instance is bound."** If the user disables Sonarr, every item
     sitting at `notified` would otherwise block permanently -- nothing polls a disabled instance,
     so nothing can ever move it on. Disabling the integration files those rows immediately.
   - **`dropped` is not terminal but is bounded**: `core/arrsync.py._check_dropped_items` re-checks
     it every pass and commits `gone` once `DROPPED_GONE_GRACE_S` (6h) has elapsed. `gone` (like
     `imported`/`cleaned`) is terminal and lands in Complete.
   - **`ARR_WAIT_MAX_S`, the backstop.** The two exits above both assume the poller is running and
     the *arr is reachable. An *enabled* instance whose *arr is permanently unreachable is a real,
     reachable state in which nothing ever observes the item leaving the queue, so `notified` never
     advances -- the poller's per-instance backoff keeps retrying forever, by design. The age bound
     is what makes "no row blocks forever" literally true rather than true-if-everything-works.

4. **A deferred source delete is still owed** -- `item.remote_delete_pending` non-null, rung 4 of
   the move-mode delete ladder (DESIGN.md §7.3/§7.4). This is the subtle one, and it is written to
   mirror `core/arrsync.py._sweep_stranded_source_deletes`'s **own** eligibility query exactly,
   because that sweep is the only actor that will ever clear the debt automatically:

   - `arr_status IN ('imported','cleaned')` -- the sweep never touches any other row. In
     particular a `gone` row can carry a stranded `remote_delete_pending` forever (rung 4 never
     fires on `gone`, by design), so a naive "`remote_delete_pending` non-null ⇒ still in flight"
     test would block those rows permanently.
   - `remote_deleted_at IS NULL` -- the debt is settled once the delete lands.
   - `arr_instance.enabled = 1` -- same reasoning as (3): the sweep runs per *polled* queue.
   - **`SOURCE_DELETE_WAIT_MAX_S`, and this one is a deliberate, documented approximation.**
     `_sweep_stranded_source_deletes` gives up after `MAX_SOURCE_DELETE_RETRY_ATTEMPTS` and writes
     one `remote_delete_retries_paused` event, but **leaves `remote_delete_pending` set** -- on
     purpose, so a manual Files-page delete or a restart's clean in-memory slate can still act. So
     the paused state is *not* readable from the item row at all. It is technically visible in the
     `event` table, but that is not a usable signal here: `event` has no index on `item_id`
     (migration 001 indexes `ts` only), so an `EXISTS` subquery would table-scan the audit log on
     an endpoint the browser polls every ~2s; and the event is not even authoritative, since the
     retry state is in-memory and a restart resumes sweeping with no event to say so. **Chosen
     instead: a bounded age.** The retry ladder pauses within roughly 20 minutes of the confirmed
     import (5 attempts, 60s-doubling backoff, one attempt per ~60s poll pass), so an hour is
     comfortably past "anything is still trying" while staying far short of "the user forgot they
     were watching this."

**Everything is measured from `item.arr_status_at`**, the persisted wall clock the *arr poller
already maintains -- the same column `core/arrsync.py._dropped_grace_expired` compares against,
and for the same reason (a restart-surviving clock for a restart-surviving column). Rung 4's
retries begin at exactly the moment `arr_status` became `imported`, so it is also the right clock
for (4).

**Unknown is never blocking.** Every expression below is wrapped so that a NULL -- an unparseable
timestamp, a missing `arr_status_at`, no bound instance -- reads as *not* in flight. That is the
fail-safe direction: a mistake files a row as Complete (visible, dismissable, honest chips still
on the row) rather than wedging it in Active where nothing can clear it.

## The manual override

`item.manual_outcome` (migration 025) short-circuits conditions 2-4 -- that is the whole point of
it, the override of last resort for a genuinely wedged row. It deliberately does **not**
short-circuit condition 1: a job that is actually `queued`/`running` is not something a
classification button gets to hide, and "Stop" is the control for that. It is read **here and
nowhere else** -- see migration 025's own comment for the full list of things it must never be
mistaken for.
"""

from __future__ import annotations

from collections.abc import Iterable

# --- Vocabulary ------------------------------------------------------------------------------

# `item.arr_status` values that mean "the *arr has not finished with this yet" (migration 018,
# docs/arr-integration-spec.md). `imported`/`cleaned`/`gone` are the three terminal ones and are
# deliberately absent -- see the module docstring on `dropped`'s own bounded path to `gone`.
ARR_IN_FLIGHT_STATES: tuple[str, ...] = ("detected", "notified", "dropped")

# The two `arr_status` values `core/arrsync.py._sweep_stranded_source_deletes` will retry a
# deferred source delete for. Named from that query rather than restated loosely, since blocking
# on a debt no sweep will ever pick up is exactly how a row wedges here forever.
ARR_TERMINAL_IMPORT_STATES: tuple[str, ...] = ("imported", "cleaned")

# --- The two backstops (module docstring, conditions 3 and 4) ---------------------------------

# Longer than `core/arrsync.py.DROPPED_GONE_GRACE_S` (6h) on purpose: this must never pre-empt
# the *arr ladder's own `dropped -> gone` exit, only catch the case where that ladder can never
# run at all (an enabled instance whose *arr is permanently unreachable). A day is long enough
# that a genuinely slow import is still shown as moving, short enough that a dead integration
# doesn't quietly fill the box.
ARR_WAIT_MAX_S = 24 * 3600.0

# The rung-4 retry ladder pauses ~20 minutes after the confirmed import at the outside
# (`MAX_SOURCE_DELETE_RETRY_ATTEMPTS` = 5, 60s-doubling backoff, one attempt per ~60s poll pass).
# An hour is past that with room to spare, and the pause itself is unreadable from persisted item
# state -- see the module docstring.
SOURCE_DELETE_WAIT_MAX_S = 3600.0

# --- Waiting reasons -- what the row says it is waiting on -------------------------------------
#
# "Rather than one vague label, the row says what it is waiting on." Derived from the *same*
# clause strings that do the splitting (`waiting_reason_expr` below), so the label and the box can
# never disagree: it is impossible for a row to be in Active with no reason, or to carry a reason
# while sitting in Complete.

REASON_PROCESSING = "processing"
REASON_VERIFYING = "verifying"
REASON_EXTRACTING = "extracting"
REASON_AWAITING_IMPORT = "awaiting_import"
REASON_DELETING_SOURCE = "deleting_source"

WAITING_REASONS: frozenset[str] = frozenset(
    {
        REASON_PROCESSING,
        REASON_VERIFYING,
        REASON_EXTRACTING,
        REASON_AWAITING_IMPORT,
        REASON_DELETING_SOURCE,
    }
)

# --- SQL construction -------------------------------------------------------------------------
#
# Literals, not bound parameters, and deliberately: these expressions are pasted into three
# different queries (one of which is an `UPDATE ... WHERE ... IN (subquery)`), and threading a
# positionally-correct parameter list through all three -- while the same clause appears more than
# once inside the `CASE` -- is precisely the kind of bookkeeping that goes wrong silently. Every
# value interpolated below is either a module constant defined above or an `int()` of an id this
# process itself produced, so there is no injection surface.


def _sql_str_tuple(values: Iterable[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _age_s(column: str) -> str:
    """Seconds elapsed since a persisted `'%Y-%m-%dT%H:%M:%S.%fZ'` timestamp column. NULL for a
    NULL or unparseable value, which every caller below relies on reading as "not blocking."
    """
    return f"((JULIANDAY('now') - JULIANDAY({column})) * 86400.0)"


def _in_flight_ids_clause(in_flight_item_ids: Iterable[int]) -> str:
    ids = sorted({int(i) for i in in_flight_item_ids})
    if not ids:
        # `0` is SQLite's false literal -- `item.id IN ()` is not valid SQL, and the empty set is
        # the overwhelmingly common case (no post-processing worker running).
        return "0"
    return "item.id IN (" + ", ".join(str(i) for i in ids) + ")"


def _arr_clause() -> str:
    return (
        "(arr_instance.enabled = 1"
        f" AND item.arr_status IN {_sql_str_tuple(ARR_IN_FLIGHT_STATES)}"
        f" AND {_age_s('item.arr_status_at')} < {ARR_WAIT_MAX_S})"
    )


def _source_delete_clause() -> str:
    return (
        "(arr_instance.enabled = 1"
        " AND item.remote_delete_pending IS NOT NULL"
        f" AND item.arr_status IN {_sql_str_tuple(ARR_TERMINAL_IMPORT_STATES)}"
        " AND item.remote_deleted_at IS NULL"
        f" AND {_age_s('item.arr_status_at')} < {SOURCE_DELETE_WAIT_MAX_S})"
    )


# The job half of the predicate, kept separate because `dismiss_all_terminal`'s `UPDATE` has no
# `item`/`arr_instance` join to evaluate the other half against (and no need to: its own `WHERE`
# already restricts to terminal jobs, for which this is always false).
JOB_ACTIVE_CLAUSE = "job.state IN ('queued', 'running')"


def item_pipeline_busy_expr(in_flight_item_ids: Iterable[int]) -> str:
    """The **item** half of the predicate: is some part of this item's pipeline -- other than the
    lftp job itself -- still working on it? Requires `item` and `arr_instance` to be in scope (the
    latter via `path_queue.arr_instance_id`, `LEFT JOIN`ed, so an unbound queue reads NULL).

    Always evaluates to `0` or `1`, never NULL: see the module docstring on why unknown must read
    as "not blocking."
    """
    return (
        "COALESCE("
        "item.manual_outcome IS NULL AND ("
        f"{_in_flight_ids_clause(in_flight_item_ids)}"
        f" OR {_arr_clause()}"
        f" OR {_source_delete_clause()}"
        "), 0)"
    )


def in_flight_expr(in_flight_item_ids: Iterable[int]) -> str:
    """The whole predicate, for a query that has `job`, `item` and `arr_instance` in scope:
    **is this row still moving?** `1` = Active/pending box, `0` = Complete box. Never NULL.
    """
    return f"COALESCE({JOB_ACTIVE_CLAUSE}" f" OR {item_pipeline_busy_expr(in_flight_item_ids)}, 0)"


def waiting_reason_expr(in_flight_item_ids: Iterable[int]) -> str:
    """What the row says it is waiting on -- one of `WAITING_REASONS`, or NULL.

    Built from the *identical* clause strings `in_flight_expr` is, in the same order, so two
    invariants hold by construction and are asserted by test:

    - a non-NULL reason implies `in_flight_expr` is `1`;
    - `in_flight_expr` is `1` on a terminal job implies a non-NULL reason.

    A `queued`/`running` job deliberately gets NULL: the row's own state chip already says
    "QUEUED"/"DOWNLOADING," and repeating it as a waiting-reason badge would be noise.
    """
    busy = item_pipeline_busy_expr(in_flight_item_ids)
    postprocess = _in_flight_ids_clause(in_flight_item_ids)
    return (
        "CASE"
        f" WHEN {JOB_ACTIVE_CLAUSE} THEN NULL"
        f" WHEN NOT {busy} THEN NULL"
        f" WHEN ({postprocess}) AND item.state = 'EXTRACTING' THEN '{REASON_EXTRACTING}'"
        f" WHEN ({postprocess}) AND item.state = 'VERIFYING' THEN '{REASON_VERIFYING}'"
        f" WHEN ({postprocess}) THEN '{REASON_PROCESSING}'"
        f" WHEN {_arr_clause()} THEN '{REASON_AWAITING_IMPORT}'"
        f" WHEN {_source_delete_clause()} THEN '{REASON_DELETING_SOURCE}'"
        " ELSE NULL END"
    )


def item_pipeline_busy_subquery(in_flight_item_ids: Iterable[int]) -> str:
    """`SELECT item.id ...` for every item whose pipeline is still busy -- the same
    `item_pipeline_busy_expr` with the joins it needs supplied, for callers that can't join
    `item`/`arr_instance` into their own statement (`dismiss_all_terminal`'s `UPDATE job`).
    """
    return (
        "SELECT item.id FROM item "
        "LEFT JOIN path_queue ON path_queue.id = item.queue_id "
        "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
        f"WHERE {item_pipeline_busy_expr(in_flight_item_ids)}"
    )
