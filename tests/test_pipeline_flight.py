"""**The one predicate that splits the Queue tab's two boxes** (2026-08-20,
docs/transfers-redesign-spec.md §3.2's pipeline-completion rule, `core/pipeline_flight.py`,
`prompts/done/2026-08-20-active-box-holds-inflight-pipeline.md`).

Three groups of tests, in the order the task's own risks run:

1. **The four blocking conditions**, each on its own, plus the manual override.
2. **The three exit traps** the task names explicitly -- a disabled *arr instance, `gone`/
   `dropped`, and a paused source delete whose `remote_delete_pending` deliberately stays set.
3. **The two properties**, which is where the real value is: *a row is in exactly one box* (the
   Active box is client-side over `list_jobs()`, the Complete box is a server-side paginated
   query with its own `total`, and they must never disagree), and *nothing blocks forever* --
   asserted over a matrix of every state combination rather than case by case, so a future
   blocking condition added without a bounded exit fails here rather than in production six
   weeks later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from lftpweb.api import jobs as jobs_api
from lftpweb.core import pipeline_flight
from lftpweb.core.events import EventBus
from lftpweb.core.queue import JobNotDismissableError, TransferQueue
from lftpweb.db import migrate
from lftpweb.models import ResolveItemRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


class _FakeRequest:
    """Same "call the route function directly with a minimal `Request` stand-in" shape
    `tests/test_item_events_api.py` established -- `resolve_item` reads `state.db` and, via
    `_publish_item_delta`, an optional `state.events` it degrades gracefully without.
    """

    def __init__(self, db):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db))


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


async def _make_arr_instance(db, *, enabled: bool) -> int:
    cursor = await db.execute(
        "INSERT INTO arr_instance (name, kind, base_url, api_key_enc, enabled, created_at, "
        "updated_at) VALUES ('Sonarr', 'sonarr', 'http://s', 'x', ?, '2026-01-01', '2026-01-01')",
        (1 if enabled else 0,),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queue(db, *, arr_instance_id: int | None = None, name: str = "q") -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'password', 'insecure')"
    )
    await db.commit()
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode, "
        "arr_instance_id) VALUES (?, ?, '/remote', '/local', 1, 'move', ?)",
        (host_id, name, arr_instance_id),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_item(
    db,
    queue_id: int,
    rel_path: str,
    *,
    state: str = "DOWNLOADED",
    arr_status: str | None = None,
    arr_status_at: str | None = None,
    remote_delete_pending: str | None = None,
    remote_deleted_at: str | None = None,
    manual_outcome: str | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state, "
        "arr_status, arr_status_at, remote_delete_pending, remote_deleted_at, manual_outcome, "
        "manual_outcome_at) VALUES (?, ?, 0, 100, 100, ?, ?, ?, ?, ?, ?, ?)",
        (
            queue_id,
            rel_path,
            state,
            arr_status,
            arr_status_at if arr_status_at is not None else (_ago(5) if arr_status else None),
            remote_delete_pending,
            remote_deleted_at,
            manual_outcome,
            _ago(1) if manual_outcome else None,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_job(db, item_id: int, *, state: str = "succeeded") -> int:
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt, finished_at) "
        "VALUES (?, 'pget', ?, 'main', 0, 1, ?)",
        (item_id, state, _ago(1) if state not in ("queued", "running") else None),
    )
    await db.commit()
    return cursor.lastrowid


def _queue(db, *, in_flight: set[int] | None = None) -> TransferQueue:
    q = TransferQueue(db, "/config", EventBus())
    if in_flight is not None:
        q.postprocess = SimpleNamespace(in_flight_item_ids=lambda: frozenset(in_flight))
    return q


async def _classify(q: TransferQueue, item_id: int) -> tuple[bool, str | None]:
    """This item's row as `list_jobs()` classifies it -- the exact pair the Active box reads."""
    rows = await q.list_jobs()
    row = next(r for r in rows if r["item_id"] == item_id)
    return bool(row["pipeline_in_flight"]), row["pipeline_waiting_reason"]


# --- The four blocking conditions -------------------------------------------------------------


async def test_plain_non_arr_item_reaches_complete_once_postprocessing_finishes(db):
    """The consistency half of the decided rule: the split applies whether or not a queue is
    *arr-bound, and a plain queue's item must still actually *reach* Complete.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "plain.mkv", state="VERIFYING")
    await _make_job(db, item)
    q = _queue(db, in_flight={item})

    assert await _classify(q, item) == (True, pipeline_flight.REASON_VERIFYING)

    # The worker finishes: the item settles on its outcome and leaves the in-flight set.
    await db.execute("UPDATE item SET state = 'VERIFIED' WHERE id = ?", (item,))
    await db.commit()
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)
    _rows, total = await q.list_complete_jobs(limit=10, offset=0)
    assert total == 1


async def test_running_job_is_in_flight_with_no_waiting_reason(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="DOWNLOADING")
    await _make_job(db, item, state="running")
    q = _queue(db, in_flight=set())
    # No reason: the row's own state chip already says DOWNLOADING.
    assert await _classify(q, item) == (True, None)


async def test_postprocess_in_flight_reports_extracting(db):
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="EXTRACTING")
    await _make_job(db, item)
    q = _queue(db, in_flight={item})
    assert await _classify(q, item) == (True, pipeline_flight.REASON_EXTRACTING)


async def test_transient_state_left_by_a_crashed_worker_does_not_block(db):
    """`core/postprocess.py`'s own rule: a row still carrying `VERIFYING` after a restart means
    the worker *died*, not that work is in progress. Keying off `in_flight_item_ids()` -- which
    is in-memory and therefore empty in a fresh process -- is what makes a crashed worker unable
    to wedge a row in Active.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="VERIFYING")
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


async def test_postprocess_in_flight_before_the_state_write_lands_reads_as_processing(db):
    """`trigger()` adds the item to the in-flight set before any transient state is written; the
    row must still say *something* rather than falling through the `CASE` with no reason.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="DOWNLOADED")
    await _make_job(db, item)
    q = _queue(db, in_flight={item})
    assert await _classify(q, item) == (True, pipeline_flight.REASON_PROCESSING)


@pytest.mark.parametrize("arr_status", ["detected", "notified", "dropped"])
async def test_non_terminal_arr_status_on_an_enabled_instance_blocks(db, arr_status):
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(db, queue_id, "a.mkv", arr_status=arr_status)
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (True, pipeline_flight.REASON_AWAITING_IMPORT)


async def test_deferred_source_delete_blocks_and_says_so(db):
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db, queue_id, "a.mkv", arr_status="imported", remote_delete_pending="VERIFIED"
    )
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (True, pipeline_flight.REASON_DELETING_SOURCE)


async def test_source_delete_debt_stops_blocking_once_the_delete_lands(db):
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db,
        queue_id,
        "a.mkv",
        arr_status="imported",
        remote_delete_pending="VERIFIED",
        remote_deleted_at=_ago(1),
    )
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


# --- Exit trap 1: a disabled *arr instance ----------------------------------------------------


async def test_disabling_the_arr_instance_releases_every_item_waiting_on_it(db):
    """The test has to be "bound to a *currently enabled* instance", not merely "`arr_status` is
    set": nothing polls a disabled instance, so an item at `notified` would block permanently.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(db, queue_id, "a.mkv", arr_status="notified")
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert (await _classify(q, item))[0] is True

    await db.execute("UPDATE arr_instance SET enabled = 0 WHERE id = ?", (instance,))
    await db.commit()
    assert await _classify(q, item) == (False, None)


async def test_disabling_the_arr_instance_also_releases_a_stranded_source_delete(db):
    """Same reasoning applied to rung 4: `_sweep_stranded_source_deletes` runs per *polled*
    queue, so a disabled instance means nothing will ever clear the debt either.
    """
    instance = await _make_arr_instance(db, enabled=False)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db, queue_id, "a.mkv", arr_status="imported", remote_delete_pending="VERIFIED"
    )
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


async def test_an_unbound_queue_never_blocks_on_arr(db):
    queue_id = await _make_queue(db, arr_instance_id=None)
    item = await _make_item(db, queue_id, "a.mkv", arr_status="notified")
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


# --- Exit trap 2: `gone` and `dropped` --------------------------------------------------------


@pytest.mark.parametrize("arr_status", ["imported", "cleaned", "gone"])
async def test_terminal_arr_status_lands_in_complete(db, arr_status):
    """`gone` is terminal by design (`core/arrsync.py._REMATCHABLE_STATES`) and must land in
    Complete, not block -- it is the *end* of the `dropped` grace window, not a pause in it.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(db, queue_id, "a.mkv", arr_status=arr_status)
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


async def test_gone_row_still_owing_a_source_delete_does_not_block(db):
    """The trap inside the trap: rung 4 never fires on `gone`, and
    `_sweep_stranded_source_deletes` only ever retries `imported`/`cleaned` rows -- so a `gone`
    row's stranded `remote_delete_pending` has no actor at all and must never be read as "still
    in flight". `core/pipeline_flight.py` mirrors that sweep's own `WHERE` for exactly this
    reason.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db, queue_id, "a.mkv", arr_status="gone", remote_delete_pending="VERIFIED"
    )
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    assert await _classify(q, item) == (False, None)


async def test_dropped_blocks_inside_its_grace_window_and_not_after_the_backstop(db):
    """`dropped`'s real exit is `core/arrsync.py._check_dropped_items` committing `gone` after
    `DROPPED_GONE_GRACE_S` (6h). `ARR_WAIT_MAX_S` is the backstop for the case that ladder can
    never run -- an enabled instance whose *arr is permanently unreachable -- and is deliberately
    longer than the grace window so it never pre-empts it.
    """
    assert pipeline_flight.ARR_WAIT_MAX_S > 6 * 3600.0
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    fresh = await _make_item(
        db, queue_id, "fresh.mkv", arr_status="dropped", arr_status_at=_ago(5 * 3600.0)
    )
    stale = await _make_item(
        db,
        queue_id,
        "stale.mkv",
        arr_status="dropped",
        arr_status_at=_ago(pipeline_flight.ARR_WAIT_MAX_S + 60),
    )
    await _make_job(db, fresh)
    await _make_job(db, stale)
    q = _queue(db, in_flight=set())
    assert (await _classify(q, fresh))[0] is True
    assert await _classify(q, stale) == (False, None)


# --- Exit trap 3: a PAUSED source delete ------------------------------------------------------


async def test_paused_source_delete_retries_stop_blocking_after_the_bounded_wait(db):
    """`_sweep_stranded_source_deletes` gives up after `MAX_SOURCE_DELETE_RETRY_ATTEMPTS` and
    writes one `remote_delete_retries_paused` event, but **leaves `remote_delete_pending` set** --
    deliberately, so a manual Files-page delete or a restart's clean in-memory slate can still
    act. The pause itself is therefore *not* readable from the item row, so a naive
    "`remote_delete_pending` non-null ⇒ still in flight" test blocks forever. The bounded wait is
    what closes that: past `SOURCE_DELETE_WAIT_MAX_S` from the confirmed import, nothing is still
    trying, and the row files as Complete with its debt intact and its audit trail unchanged.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db,
        queue_id,
        "a.mkv",
        arr_status="imported",
        arr_status_at=_ago(pipeline_flight.SOURCE_DELETE_WAIT_MAX_S + 60),
        remote_delete_pending="VERIFIED",
    )
    await _make_job(db, item)
    q = _queue(db, in_flight=set())

    assert await _classify(q, item) == (False, None)
    # The debt itself is untouched -- this is a *classification*, never a state change.
    cursor = await db.execute("SELECT remote_delete_pending FROM item WHERE id = ?", (item,))
    assert (await cursor.fetchone())["remote_delete_pending"] == "VERIFIED"


async def test_source_delete_wait_is_longer_than_the_retry_ladder_can_take(db):
    """Guards the constant against being tightened below the thing it is supposed to outlast:
    5 attempts at one per ~60s poll pass, with a 60s-doubling backoff, is ~20 minutes at the
    outside.
    """
    from lftpweb.core.arrsync import MAX_SOURCE_DELETE_RETRY_ATTEMPTS

    ladder_s = sum(60.0 * 2**n for n in range(MAX_SOURCE_DELETE_RETRY_ATTEMPTS))
    assert pipeline_flight.SOURCE_DELETE_WAIT_MAX_S > ladder_s


# --- The manual override ----------------------------------------------------------------------


async def test_manual_outcome_overrides_every_blocking_condition(db):
    """The override of last resort: it beats all of them at once, which is the whole point."""
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db,
        queue_id,
        "wedged.mkv",
        state="VERIFYING",
        arr_status="notified",
        remote_delete_pending="VERIFIED",
        manual_outcome="failed",
    )
    await _make_job(db, item)
    q = _queue(db, in_flight={item})
    assert await _classify(q, item) == (False, None)


async def test_manual_outcome_never_hides_a_genuinely_running_job(db):
    """A classification button does not get to hide a transfer that is actually running -- Stop
    is the control for that, and the API refuses the write besides.
    """
    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="DOWNLOADING", manual_outcome="complete")
    await _make_job(db, item, state="running")
    q = _queue(db, in_flight=set())
    assert (await _classify(q, item))[0] is True


async def test_resolve_endpoint_sets_clears_and_audits(db):
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(db, queue_id, "wedged.mkv", arr_status="notified")
    await _make_job(db, item)
    q = _queue(db, in_flight=set())
    request = _FakeRequest(db)

    resp = await jobs_api.resolve_item(item, request, ResolveItemRequest(outcome="complete"))
    assert resp.manual_outcome == "complete"
    assert resp.manual_outcome_at is not None
    assert await _classify(q, item) == (False, None)

    # Undo -- the row goes straight back through the normal predicate.
    resp = await jobs_api.resolve_item(item, request, ResolveItemRequest(outcome=None))
    assert resp.manual_outcome is None
    assert (await _classify(q, item))[0] is True

    cursor = await db.execute("SELECT kind FROM event WHERE item_id = ? ORDER BY id", (item,))
    assert [r["kind"] for r in await cursor.fetchall()] == [
        "manual_resolution_set",
        "manual_resolution_cleared",
    ]


async def test_resolve_endpoint_never_touches_the_pipelines_own_columns(db):
    """The safety constraint, asserted rather than trusted: a manual resolution writes exactly
    two columns. It must not be readable as a confirmed import, must not advance the delete
    ladder, and must not clear the debt that keeps the source inspectable.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(
        db, queue_id, "a.mkv", arr_status="notified", remote_delete_pending="VERIFIED"
    )
    await _make_job(db, item)
    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item,))
    before = dict(await cursor.fetchone())

    await jobs_api.resolve_item(item, _FakeRequest(db), ResolveItemRequest(outcome="complete"))

    cursor = await db.execute("SELECT * FROM item WHERE id = ?", (item,))
    after = dict(await cursor.fetchone())
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"manual_outcome", "manual_outcome_at"}


async def test_resolve_endpoint_refuses_while_a_transfer_is_active(db):
    from fastapi import HTTPException

    queue_id = await _make_queue(db)
    item = await _make_item(db, queue_id, "a.mkv", state="DOWNLOADING")
    await _make_job(db, item, state="running")
    with pytest.raises(HTTPException) as exc:
        await jobs_api.resolve_item(item, _FakeRequest(db), ResolveItemRequest(outcome="complete"))
    assert exc.value.status_code == 409


async def test_resolve_endpoint_404s_for_an_unknown_item(db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await jobs_api.resolve_item(9999, _FakeRequest(db), ResolveItemRequest(outcome="failed"))
    assert exc.value.status_code == 404


# --- Dismiss must agree with the split --------------------------------------------------------


async def test_an_in_flight_row_cannot_be_dismissed(db):
    """Dismissing something still being worked on makes no sense -- and it is also how a row
    would vanish from *both* boxes, since `list_jobs()` drops a dismissed job unconditionally.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    item = await _make_item(db, queue_id, "a.mkv", arr_status="notified")
    job_id = await _make_job(db, item)
    q = _queue(db, in_flight=set())
    with pytest.raises(JobNotDismissableError):
        await q.dismiss_job(job_id)


async def test_bulk_dismiss_skips_in_flight_rows_and_still_matches_the_complete_total(db):
    """The `total`/dismissed-count agreement property, extended to the new exclusion: whatever
    the Complete box reports as its total is exactly what "Dismiss" acts on.
    """
    instance = await _make_arr_instance(db, enabled=True)
    arr_queue = await _make_queue(db, arr_instance_id=instance, name="arr")
    plain_queue = await _make_queue(db, name="plain")
    waiting = await _make_item(db, arr_queue, "waiting.mkv", arr_status="notified")
    done = await _make_item(db, plain_queue, "done.mkv", state="VERIFIED")
    await _make_job(db, waiting)
    await _make_job(db, done)
    q = _queue(db, in_flight=set())

    _rows, total = await q.list_complete_jobs(limit=50, offset=0)
    assert total == 1
    assert await q.dismiss_all_terminal() == total

    # The in-flight row is untouched and still in the Active box.
    assert (await _classify(q, waiting))[0] is True
    assert {r["item_id"] for r in await q.list_jobs()} == {waiting}


# --- The properties ---------------------------------------------------------------------------
#
# A matrix of every interesting item shape, run through both boxes at once. This is where a
# future blocking condition without a bounded exit -- or one added to only one of the two
# queries -- is supposed to fail.


async def _build_matrix(db) -> tuple[int, int, list[int]]:
    """Every state combination the predicate distinguishes, one item each, on two queues (one
    *arr-bound and enabled, one plain). Returns `(enabled_instance_id, disabled_instance_id,
    item_ids)`.
    """
    enabled = await _make_arr_instance(db, enabled=True)
    disabled = await _make_arr_instance(db, enabled=False)
    arr_queue = await _make_queue(db, arr_instance_id=enabled, name="arr")
    off_queue = await _make_queue(db, arr_instance_id=disabled, name="off")
    plain_queue = await _make_queue(db, name="plain")

    items: list[int] = []
    n = 0
    for queue_id in (arr_queue, off_queue, plain_queue):
        for arr_status in (None, "detected", "notified", "dropped", "imported", "cleaned", "gone"):
            for pending in (None, "VERIFIED"):
                for item_state in ("DOWNLOADED", "VERIFYING", "EXTRACTING", "CORRUPT"):
                    n += 1
                    items.append(
                        await _make_item(
                            db,
                            queue_id,
                            f"m{n}.mkv",
                            state=item_state,
                            arr_status=arr_status,
                            remote_delete_pending=pending,
                        )
                    )
    for item in items:
        await _make_job(db, item)
    return enabled, disabled, items


async def test_property_every_row_is_in_exactly_one_box(db):
    """The correctness risk unique to this task. The Active box is client-side over
    `list_jobs()`; the Complete box is a server-side paginated query with its own `total`. If the
    two tests drifted, a row would appear in **both boxes or neither** -- so assert the partition
    directly, and assert `total` agrees with it.
    """
    _enabled, _disabled, items = await _build_matrix(db)
    # A couple of items also mid-post-processing, so that condition is represented too.
    in_flight = {items[1], items[7], items[30]}
    q = _queue(db, in_flight=in_flight)

    rows = await q.list_jobs()
    active = {r["item_id"] for r in rows if r["pipeline_in_flight"]}
    complete_rows, total = await q.list_complete_jobs(limit=1000, offset=0)
    complete = {r["item_id"] for r in complete_rows}

    assert active & complete == set()
    assert active | complete == set(items)
    assert total == len(complete)
    assert len(complete_rows) == total


async def test_property_a_waiting_reason_and_the_box_can_never_disagree(db):
    """The label is derived from the same clauses as the split, so: a reason implies the Active
    box, and an in-flight terminal-job row always has a reason (a `queued`/`running` row
    deliberately has none -- its state chip already says what it is doing).
    """
    _enabled, _disabled, items = await _build_matrix(db)
    q = _queue(db, in_flight={items[3], items[11]})
    for row in await q.list_jobs():
        reason = row["pipeline_waiting_reason"]
        if reason is not None:
            assert reason in pipeline_flight.WAITING_REASONS
            assert row["pipeline_in_flight"]
        if row["pipeline_in_flight"] and row["state"] not in ("queued", "running"):
            assert reason is not None


async def test_property_nothing_blocks_forever(db):
    """**No row can remain in Active once every pipeline actor has stopped working on it.**

    Every actor is stopped here: no post-processing worker (the in-flight set is empty, which is
    also what a crashed worker or a restart produces), and every persisted clock aged past both
    backstops. Whatever combination of `arr_status`/`remote_delete_pending`/`item.state` a row
    carries, it must file as Complete. A new blocking condition added without a bounded exit
    fails this test.
    """
    _enabled, _disabled, items = await _build_matrix(db)
    ancient = _ago(
        max(pipeline_flight.ARR_WAIT_MAX_S, pipeline_flight.SOURCE_DELETE_WAIT_MAX_S) * 2
    )
    await db.execute("UPDATE item SET arr_status_at = ? WHERE arr_status IS NOT NULL", (ancient,))
    await db.commit()

    q = _queue(db, in_flight=set())
    rows = await q.list_jobs()
    still_active = [r["rel_path"] for r in rows if r["pipeline_in_flight"]]
    assert still_active == []
    _complete_rows, total = await q.list_complete_jobs(limit=1000, offset=0)
    assert total == len(items)


async def test_property_unknown_never_blocks(db):
    """The fail-safe direction. A row whose `arr_status_at` is missing or unparseable cannot be
    bounded, so it must read as Complete rather than wedging in Active where nothing can clear
    it.
    """
    instance = await _make_arr_instance(db, enabled=True)
    queue_id = await _make_queue(db, arr_instance_id=instance)
    missing = await _make_item(db, queue_id, "missing.mkv", arr_status="notified")
    unparseable = await _make_item(db, queue_id, "bad.mkv", arr_status="notified")
    await db.execute("UPDATE item SET arr_status_at = NULL WHERE id = ?", (missing,))
    await db.execute("UPDATE item SET arr_status_at = 'not a date' WHERE id = ?", (unparseable,))
    await db.commit()
    await _make_job(db, missing)
    await _make_job(db, unparseable)

    q = _queue(db, in_flight=set())
    assert await _classify(q, missing) == (False, None)
    assert await _classify(q, unparseable) == (False, None)
