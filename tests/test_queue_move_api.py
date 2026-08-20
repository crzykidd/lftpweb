"""`POST /api/jobs/{id}/move` (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
`prompts/2026-08-19-queue-reorder-chevrons.md`) -- the chevron reorder endpoint's own wiring:
`direction` validation, and how `core/queue.py.TransferQueue.move_job`'s two exceptions map onto
HTTP status codes. Same "call the route function directly against a recording fake queue" harness
`tests/test_start_now_fraction.py`'s own API-layer section uses -- `TransferQueue.move_job`'s
actual reorder/exhaustion/edge-case behavior is `tests/test_queue_position.py`'s job, against a
real database; this file only proves the route itself.
"""

from __future__ import annotations

import pydantic
import pytest
from fastapi import HTTPException

from lftpweb.api import jobs
from lftpweb.core.queue import JobNotQueuedError
from lftpweb.models import MoveJobRequest

# --- A minimal `Request` stand-in exposing only `app.state.queue`, same shape
# `tests/test_start_now_fraction.py._FakeRequest` uses -- this route only ever reads that one
# attribute. ------------------------------------------------------------------------------------


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
    """Stands in for `TransferQueue` at the API layer -- only `move_job` matters here, so
    nothing else is implemented. Lets the 404/409/422 wiring be tested without a real
    job/item/queue row chain, which `tests/test_queue_position.py` already exercises against a
    real database.
    """

    def __init__(self, *, raises: Exception | None = None):
        self._raises = raises
        self.calls: list[tuple[int, str]] = []

    async def move_job(self, job_id: int, direction: str) -> None:
        self.calls.append((job_id, direction))
        if self._raises is not None:
            raise self._raises


async def test_move_job_api_passes_direction_through():
    fake = _RecordingQueue()
    result = await jobs.move_job(7, MoveJobRequest(direction="up"), _FakeRequest(fake))
    assert result is None
    assert fake.calls == [(7, "up")]


async def test_move_job_api_top_direction_passes_through_too():
    fake = _RecordingQueue()
    await jobs.move_job(7, MoveJobRequest(direction="top"), _FakeRequest(fake))
    assert fake.calls == [(7, "top")]


async def test_move_job_api_maps_value_error_to_404():
    fake = _RecordingQueue(raises=ValueError("job 7 not found"))
    with pytest.raises(HTTPException) as exc_info:
        await jobs.move_job(7, MoveJobRequest(direction="down"), _FakeRequest(fake))
    assert exc_info.value.status_code == 404
    assert "job 7 not found" in exc_info.value.detail


async def test_move_job_api_maps_job_not_queued_to_409():
    fake = _RecordingQueue(raises=JobNotQueuedError("job 7 is not queued (state='running')"))
    with pytest.raises(HTTPException) as exc_info:
        await jobs.move_job(7, MoveJobRequest(direction="up"), _FakeRequest(fake))
    assert exc_info.value.status_code == 409
    assert "not queued" in exc_info.value.detail


def test_move_job_request_rejects_a_direction_outside_the_three_options():
    # `Literal["up", "down", "top"]` -- FastAPI turns this into a 422 before the route ever
    # runs; this is the same guarantee at the model layer, without needing a live ASGI request
    # to prove it (mirrors `test_start_now_fraction.py`'s identical check for its own menu).
    with pytest.raises(pydantic.ValidationError):
        MoveJobRequest(direction="sideways")
