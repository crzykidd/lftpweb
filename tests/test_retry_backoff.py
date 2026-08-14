"""`core/queue.py.compute_retry_backoff` -- the retry delay arithmetic.

Exists because `TransferSettings.retry_backoff_base_s` was a dead setting from phase 3a until
2026-08-14: it loaded, saved, and round-tripped through `PUT /api/settings/transfer` and the
Settings -> Transfer form, while `TransferQueue._reap_one` computed the actual delay from the
`DEFAULT_RETRY_BACKOFF_BASE_S` module constant and never read the saved value. Changing the
field did nothing at all.

No test covered it, which is precisely why it survived nine phases plus two live-testing
sessions. These assert the two properties that were silently broken: the delay is driven by the
*passed* base rather than the constant, and the cap still applies.
"""

import asyncio

from lftpweb.core.queue import (
    DEFAULT_RETRY_BACKOFF_BASE_S,
    DEFAULT_RETRY_BACKOFF_MAX_S,
    compute_retry_backoff,
)


def test_first_attempt_waits_exactly_the_base():
    assert compute_retry_backoff(30.0, 1) == 30.0
    assert compute_retry_backoff(5.0, 1) == 5.0


def test_delay_doubles_per_attempt():
    assert compute_retry_backoff(30.0, 2) == 60.0
    assert compute_retry_backoff(30.0, 3) == 120.0


def test_a_custom_base_actually_changes_the_delay():
    """The regression this file exists for. A base other than the module default must produce a
    different delay -- when `_reap_one` read the constant, every one of these returned the
    30s-derived value regardless of what the user had saved.
    """
    assert compute_retry_backoff(5.0, 1) != compute_retry_backoff(DEFAULT_RETRY_BACKOFF_BASE_S, 1)
    assert compute_retry_backoff(5.0, 3) == 20.0
    assert compute_retry_backoff(120.0, 2) == 240.0


def test_clamped_to_the_maximum():
    assert compute_retry_backoff(30.0, 99) == DEFAULT_RETRY_BACKOFF_MAX_S
    # A base already above the cap is clamped on its very first retry.
    assert compute_retry_backoff(DEFAULT_RETRY_BACKOFF_MAX_S * 2, 1) == DEFAULT_RETRY_BACKOFF_MAX_S


def test_zero_base_means_retry_immediately():
    """Not rejected here -- `api/settings.py` owns input validation. This only pins that the
    arithmetic itself stays well-defined rather than producing a negative or a surprise.
    """
    assert compute_retry_backoff(0.0, 1) == 0.0
    assert compute_retry_backoff(0.0, 5) == 0.0


# --- Shutdown terminates children concurrently (2026-08-14) -----------------------------------
#
# `TransferQueue.stop()` used to await `lftp.terminate(..., grace_s=10.0)` once per in-flight
# child, in sequence -- so worst-case shutdown was `len(running) * grace_s`. With the main lane
# and the fast lane both full that is ~40s before `stop()` even returns, and the retention/
# metrics/backup schedulers stop after it. Docker SIGKILLs at `stop_grace_period`, so an
# oversized shutdown does not degrade gracefully: it is cut off, and the clean-resume path
# `stop()` exists to provide never runs. Concurrent termination bounds it at ~one grace period
# however many transfers are in flight.


class _FakeSpawned:
    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


class _FakeProc:
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.spawned = _FakeSpawned()


async def test_stop_terminates_every_child_concurrently(monkeypatch):
    """Overlap is the assertion: N children whose termination each take `delay` must finish in
    about `delay`, not `N * delay`. Timing-based, so the bar is deliberately loose -- it fails
    hard on sequential behaviour (0.5s vs 0.05s) without being flaky on a slow machine.
    """
    import time as _time

    from lftpweb.core import queue as queue_module

    n_children = 10
    delay = 0.05

    async def _slow_terminate(spawned, *, grace_s):  # noqa: ARG001
        await asyncio.sleep(delay)

    monkeypatch.setattr(queue_module.lftp, "terminate", _slow_terminate)

    q = queue_module.TransferQueue.__new__(queue_module.TransferQueue)
    q._task = None
    procs = {i: _FakeProc(i) for i in range(n_children)}
    q._running = procs

    started = _time.monotonic()
    await q.stop()
    elapsed = _time.monotonic() - started

    assert elapsed < delay * n_children / 2, (
        f"stop() took {elapsed:.3f}s for {n_children} children at {delay}s each -- "
        "that is sequential termination, not concurrent"
    )
    assert all(p.spawned.cleaned for p in procs.values()), "every rc file must be cleaned up"


async def test_stop_cleans_up_even_when_a_child_fails_to_terminate(monkeypatch):
    """One child raising must not strand the others' rc files -- each holds that job's seedbox
    password in the `/run` tmpfs (DESIGN.md §4.2/§11.1), so cleanup is not optional.
    """
    from lftpweb.core import queue as queue_module

    async def _explode(spawned, *, grace_s):  # noqa: ARG001
        raise RuntimeError("child would not die")

    monkeypatch.setattr(queue_module.lftp, "terminate", _explode)

    q = queue_module.TransferQueue.__new__(queue_module.TransferQueue)
    q._task = None
    procs = {i: _FakeProc(i) for i in range(3)}
    q._running = procs

    await q.stop()  # must not raise

    assert all(p.spawned.cleaned for p in procs.values())
