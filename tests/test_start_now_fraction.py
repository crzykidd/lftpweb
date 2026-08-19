"""`TransferQueue.start_now` and `POST /api/jobs/{id}/start-now` (DESIGN.md §4.5's "Start now"
widened into a menu, 2026-08-19, prompts/done/2026-08-19-start-now-bandwidth-fractions.md).

No lftp process or fake seedbox needed here: `start_now` itself only validates and writes
`job.forced_full_rate`/`forced_rate_fraction`, then requests a tick -- the admission *math* for
a forced fraction is `tests/test_scheduler.py`'s own table, and the rate cap's flow from
`AdmitDecision.rate_limit_bps` into the spawned lftp rc (`net:limit-total-rate`) is unchanged
code already covered by `tests/test_lftp.py` regardless of how that number was computed. Same
call-the-route-function-directly harness `tests/test_delete_api.py`/`tests/test_history_api.py`
use for the API layer -- the thing under test is this route's own wiring (422/409/omitted-body
mapping), not FastAPI's routing layer.
"""

from __future__ import annotations

import aiosqlite
import pydantic
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs
from lftpweb.core.events import EventBus
from lftpweb.core.queue import (
    NoSiteLimitConfiguredError,
    TransferQueue,
    TransferSettings,
    save_transfer_settings,
)
from lftpweb.db import migrate
from lftpweb.models import StartNowRequest


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await migrate(conn)
    yield conn
    await conn.close()


async def _make_queue(db) -> int:
    cursor = await db.execute(
        "INSERT INTO host (name, address, port, username, auth_method, known_hosts_policy) "
        "VALUES ('h', '127.0.0.1', 22, 'u', 'key', 'strict')"
    )
    host_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO path_queue (host_id, name, remote_path, local_path, enabled, sync_mode) "
        "VALUES (?, 'q', '/remote', '/local', 1, 'copy')",
        (host_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _make_queued_job(db, queue_id: int, rel_path: str = "item") -> int:
    cursor = await db.execute(
        "INSERT INTO item (queue_id, rel_path, is_dir, remote_size, local_size, state) "
        "VALUES (?, ?, 0, 1000, 0, 'REMOTE_ONLY')",
        (queue_id, rel_path),
    )
    item_id = cursor.lastrowid
    cursor = await db.execute(
        "INSERT INTO job (item_id, kind, state, lane, rank, attempt) "
        "VALUES (?, 'pget', 'queued', 'main', 0, 1)",
        (item_id,),
    )
    await db.commit()
    return cursor.lastrowid


async def _forced_columns(db, job_id: int) -> tuple[int, float | None]:
    cursor = await db.execute(
        "SELECT forced_full_rate, forced_rate_fraction FROM job WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    return row["forced_full_rate"], row["forced_rate_fraction"]


def _queue_obj(db) -> TransferQueue:
    return TransferQueue(db, "/config", EventBus())


# --- TransferQueue.start_now: DB-level behavior, no admission/spawn involved ----------------


async def test_start_now_omitted_percent_means_max(db):
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    applied = await _queue_obj(db).start_now(job_id)
    assert applied is True
    assert await _forced_columns(db, job_id) == (1, 1.0)


async def test_start_now_percent_100_is_byte_identical_to_omitted(db):
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    applied = await _queue_obj(db).start_now(job_id, rate_percent=100)
    assert applied is True
    assert await _forced_columns(db, job_id) == (1, 1.0)


async def test_start_now_fraction_with_configured_site_limit_persists_the_fraction(db):
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    await save_transfer_settings(
        db, TransferSettings(max_bandwidth_bps=10_000_000, max_concurrent_transfers=2)
    )
    applied = await _queue_obj(db).start_now(job_id, rate_percent=25)
    assert applied is True
    assert await _forced_columns(db, job_id) == (1, 0.25)


async def test_start_now_fraction_with_no_site_limit_configured_raises_and_withholds(db):
    # "No site limit configured" reads as max_bandwidth_bps <= 0 (docs/decisions.md) -- the
    # Settings -> Transfer field's own pre-existing degenerate-ceiling case.
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    await save_transfer_settings(
        db, TransferSettings(max_bandwidth_bps=0, max_concurrent_transfers=2)
    )
    with pytest.raises(NoSiteLimitConfiguredError):
        await _queue_obj(db).start_now(job_id, rate_percent=25)
    # Withheld, not partially applied: the job's own forced columns are untouched.
    assert await _forced_columns(db, job_id) == (0, None)


async def test_start_now_max_ignores_a_missing_site_limit(db):
    # Max is exempt from the "no site limit configured" guard -- it reuses whatever
    # max_bandwidth_bps already is, unconditionally, exactly as the pre-fraction path did.
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    await save_transfer_settings(
        db, TransferSettings(max_bandwidth_bps=0, max_concurrent_transfers=2)
    )
    applied = await _queue_obj(db).start_now(job_id)  # omitted = Max
    assert applied is True
    assert await _forced_columns(db, job_id) == (1, 1.0)


async def test_start_now_on_a_running_job_is_a_no_op(db):
    queue_id = await _make_queue(db)
    job_id = await _make_queued_job(db, queue_id)
    await db.execute("UPDATE job SET state = 'running' WHERE id = ?", (job_id,))
    await db.commit()
    applied = await _queue_obj(db).start_now(job_id, rate_percent=25)
    assert applied is False
    assert await _forced_columns(db, job_id) == (0, None)


# --- API layer: POST /api/jobs/{id}/start-now -----------------------------------------------
#
# A minimal `Request` stand-in exposing only `app.state.queue`, same shape
# `tests/test_delete_api.py._FakeRequest` uses -- this route only ever reads that one attribute.


class _FakeState:
    def __init__(self, queue):
        self.queue = queue


class _FakeApp:
    def __init__(self, queue):
        self.state = _FakeState(queue)


class _FakeRequest:
    def __init__(self, queue):
        self.app = _FakeApp(queue)


class _RecordingQueue:
    """Stands in for `core/queue.py.TransferQueue` at the API layer -- only `start_now` matters
    here, so nothing else is implemented. Lets the 422/409/omitted-body wiring be tested without
    a real job/item/queue row chain, which `TransferQueue.start_now`'s own tests above already
    exercise against a real database.
    """

    def __init__(self, *, raises: Exception | None = None, applied: bool = True):
        self._raises = raises
        self._applied = applied
        self.calls: list[tuple[int, int | None]] = []

    async def start_now(self, job_id: int, *, rate_percent: int | None = None) -> bool:
        self.calls.append((job_id, rate_percent))
        if self._raises is not None:
            raise self._raises
        return self._applied


async def test_start_now_api_no_body_means_max():
    fake = _RecordingQueue()
    result = await jobs.start_now(1, _FakeRequest(fake), body=None)
    assert result == {"applied": True}
    assert fake.calls == [(1, None)]  # `TransferQueue.start_now`'s own `None` = Max default


async def test_start_now_api_passes_rate_percent_through():
    fake = _RecordingQueue()
    result = await jobs.start_now(1, _FakeRequest(fake), body=StartNowRequest(rate_percent=25))
    assert result == {"applied": True}
    assert fake.calls == [(1, 25)]


async def test_start_now_api_maps_no_site_limit_to_409():
    fake = _RecordingQueue(raises=NoSiteLimitConfiguredError("no site limit configured"))
    with pytest.raises(HTTPException) as exc_info:
        await jobs.start_now(1, _FakeRequest(fake), body=StartNowRequest(rate_percent=25))
    assert exc_info.value.status_code == 409
    assert "no site limit configured" in exc_info.value.detail


def test_start_now_request_rejects_a_rate_percent_outside_the_menu():
    # `Literal[10, 25, 50, 75, 100] | None` -- FastAPI turns this into a 422 before the route
    # ever runs (item 1's own "server-side validation rejects anything outside that set"); this
    # is the same guarantee at the model layer, without needing a live ASGI request to prove it.
    with pytest.raises(pydantic.ValidationError):
        StartNowRequest(rate_percent=33)


def test_start_now_request_accepts_every_menu_option_and_none():
    for value in (10, 25, 50, 75, 100, None):
        assert StartNowRequest(rate_percent=value).rate_percent == value
