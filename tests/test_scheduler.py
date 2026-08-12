"""The admission scheduler table test (DESIGN.md §4.5, §14) — every worked example, plus the
floor loop and the two escape hatches (fast lane, start-now). Pure function, no subprocess, no
I/O: `(N, B, running, queue, settings)` in, an admit list out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lftpweb.core.scheduler import (
    LANE_MAIN,
    LANE_SMALL,
    AdmitDecision,
    QueuedJob,
    RunningJob,
    SchedulerSettings,
    admit,
)


def _settings(**overrides) -> SchedulerSettings:
    base = dict(
        max_bandwidth_bps=10_000_000,
        max_concurrent_transfers=2,
        small_item_threshold_bytes=10_000_000,
        small_lane_concurrency=2,
        small_lane_reserve_bps=0,  # DESIGN.md §4.5's worked table uses clean numbers with no reserve
        min_share_floor_bps=0,
    )
    base.update(overrides)
    return SchedulerSettings(**base)


def _q(
    id_: int, queued_at: str, rank: float = 0.0, lane: str = LANE_MAIN, forced: bool = False
) -> QueuedJob:
    return QueuedJob(id=id_, lane=lane, rank=rank, queued_at=queued_at, forced_full_rate=forced)


# --- §4.5's worked table, N=2 B=10MB/s, reserve=0 ------------------------------------------


def test_five_queued_nothing_running_admits_two_at_half():
    settings = _settings()
    queue = [_q(i, f"t{i}") for i in range(1, 6)]
    decisions = admit(settings, running=[], queue=queue)
    assert decisions == [
        AdmitDecision(job_id=1, lane=LANE_MAIN, rate_limit_bps=5_000_000),
        AdmitDecision(job_id=2, lane=LANE_MAIN, rate_limit_bps=5_000_000),
    ]


def test_one_item_alone_admits_at_full_bandwidth():
    settings = _settings()
    decisions = admit(settings, running=[], queue=[_q(1, "t1")])
    assert decisions == [AdmitDecision(job_id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000)]


def test_second_item_blocked_while_first_holds_full_bandwidth():
    settings = _settings()
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000)]
    decisions = admit(settings, running=running, queue=[_q(2, "t2")])
    assert decisions == []  # headroom is 0 -> the third/blocked item waits


def test_refill_at_half_share_when_partner_already_finished():
    # One job running at 5 (its partner finished), a new item arrives -> starts at 5; the
    # running job is untouched (still 5, never re-shaped).
    settings = _settings()
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=5_000_000)]
    decisions = admit(settings, running=running, queue=[_q(2, "t2")])
    assert decisions == [AdmitDecision(job_id=2, lane=LANE_MAIN, rate_limit_bps=5_000_000)]


def test_job_finishes_three_still_queued_refills_at_headroom_over_one():
    # A job finishes (drops out of `running`), 3 still queued, one slot free (N=2, one still
    # running at 5) -> headroom 5, ready 1 -> next by priority starts at 5.
    settings = _settings()
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=5_000_000)]
    queue = [_q(2, "t2"), _q(3, "t3"), _q(4, "t4")]
    decisions = admit(settings, running=running, queue=queue)
    assert decisions == [AdmitDecision(job_id=2, lane=LANE_MAIN, rate_limit_bps=5_000_000)]


def test_default_order_is_oldest_first():
    settings = _settings(max_concurrent_transfers=1)
    queue = [_q(3, "t3"), _q(1, "t1"), _q(2, "t2")]  # inserted out of order
    decisions = admit(settings, running=[], queue=queue)
    assert [d.job_id for d in decisions] == [1]


def test_move_to_top_uses_higher_rank_over_queued_at():
    settings = _settings(max_concurrent_transfers=1)
    queue = [_q(1, "t1", rank=0.0), _q(2, "t2", rank=100.0)]  # 2 was "moved to top"
    decisions = admit(settings, running=[], queue=queue)
    assert [d.job_id for d in decisions] == [2]


# --- The min_share_floor loop ("run fewer, faster") ------------------------------------------


def test_floor_loop_reduces_ready_until_share_clears_the_floor():
    # headroom 1,000,000 over 5 ready -> 200,000 each, below the 300,000 floor.
    # ready=4 -> 250,000, still below. ready=3 -> 333,333, clears it. Admit exactly 3.
    settings = _settings(
        max_bandwidth_bps=1_000_000,
        max_concurrent_transfers=5,
        min_share_floor_bps=300_000,
    )
    queue = [_q(i, f"t{i}") for i in range(1, 6)]
    decisions = admit(settings, running=[], queue=queue)
    assert [d.job_id for d in decisions] == [1, 2, 3]
    assert all(d.rate_limit_bps == 333_333 for d in decisions)


def test_floor_loop_stops_at_ready_one_even_below_the_floor():
    # A single item's share can't be reduced further by shrinking `ready` (already 1) — it is
    # admitted anyway rather than being refused outright.
    settings = _settings(
        max_bandwidth_bps=100_000, max_concurrent_transfers=5, min_share_floor_bps=300_000
    )
    decisions = admit(settings, running=[], queue=[_q(1, "t1")])
    assert decisions == [AdmitDecision(job_id=1, lane=LANE_MAIN, rate_limit_bps=100_000)]


def test_zero_or_negative_headroom_admits_nothing_on_the_main_lane():
    settings = _settings(max_bandwidth_bps=10_000_000, max_concurrent_transfers=5)
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000)]
    decisions = admit(settings, running=running, queue=[_q(2, "t2")])
    assert decisions == []


# --- Fast lane: bypasses headroom and the main-lane slot count entirely ----------------------


def test_fast_lane_item_admits_even_when_main_lane_headroom_is_negative():
    settings = _settings(small_lane_reserve_bps=1_000_000, small_lane_concurrency=2)
    # Main lane fully saturated: headroom deeply negative.
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000)]
    queue = [_q(2, "small-1", lane=LANE_SMALL)]
    decisions = admit(settings, running=running, queue=queue)
    assert decisions == [AdmitDecision(job_id=2, lane=LANE_SMALL, rate_limit_bps=1_000_000)]


def test_fast_lane_shares_its_reserve_across_concurrent_small_jobs():
    settings = _settings(small_lane_reserve_bps=1_000_000, small_lane_concurrency=2)
    queue = [
        _q(1, "small-1", lane=LANE_SMALL),
        _q(2, "small-2", lane=LANE_SMALL),
        _q(3, "small-3", lane=LANE_SMALL),
    ]
    decisions = admit(settings, running=[], queue=queue)
    # Concurrency cap is 2 -> only two admitted, third waits; the reserve splits evenly.
    assert [d.job_id for d in decisions] == [1, 2]
    assert all(d.rate_limit_bps == 500_000 for d in decisions)


def test_fast_lane_running_jobs_are_never_re_shaped_by_a_new_arrival():
    settings = _settings(small_lane_reserve_bps=1_000_000, small_lane_concurrency=2)
    running = [RunningJob(id=1, lane=LANE_SMALL, rate_limit_bps=1_000_000)]
    decisions = admit(settings, running=running, queue=[_q(2, "small-2", lane=LANE_SMALL)])
    # Job 1 keeps its 1,000,000 (not present in `decisions` — it's not re-admitted); job 2
    # gets a share computed over both active small jobs, not the full reserve to itself.
    assert decisions == [AdmitDecision(job_id=2, lane=LANE_SMALL, rate_limit_bps=500_000)]


def test_fast_lane_never_consumes_a_main_lane_slot():
    settings = _settings(max_concurrent_transfers=1, small_lane_reserve_bps=1_000_000)
    queue = [_q(1, "main-1", lane=LANE_MAIN), _q(2, "small-1", lane=LANE_SMALL)]
    decisions = admit(settings, running=[], queue=queue)
    ids = {d.job_id for d in decisions}
    assert ids == {1, 2}  # both admitted despite N=1 -- the small item didn't compete for it


# --- Start now at max bandwidth: oversubscribes, then freezes normal admission --------------


def test_start_now_admits_unconditionally_at_full_bandwidth():
    settings = _settings(max_concurrent_transfers=1)
    running = [RunningJob(id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000)]  # N already full
    queue = [_q(2, "t2", forced=True)]
    decisions = admit(settings, running=running, queue=queue)
    assert decisions == [
        AdmitDecision(job_id=2, lane=LANE_MAIN, rate_limit_bps=10_000_000, forced_full_rate=True)
    ]


def test_start_now_oversubscription_freezes_further_normal_admission():
    settings = _settings(max_concurrent_transfers=5)
    queue = [_q(1, "t1", forced=True), _q(2, "t2")]
    decisions = admit(settings, running=[], queue=queue)
    # Item 1 is force-admitted at the full 10,000,000; that alone drives headroom negative
    # (10,000,000 - 0 - 10,000,000 = 0), so item 2 gets nothing this pass.
    assert decisions == [
        AdmitDecision(job_id=1, lane=LANE_MAIN, rate_limit_bps=10_000_000, forced_full_rate=True)
    ]


def test_start_now_admission_resumes_once_the_forced_job_finishes():
    settings = _settings(max_concurrent_transfers=5)
    # The forced job has already left `queue` (it's running now) and finished, so it's not in
    # `running` either -> headroom is back to normal.
    decisions = admit(settings, running=[], queue=[_q(2, "t2")])
    assert decisions == [AdmitDecision(job_id=2, lane=LANE_MAIN, rate_limit_bps=10_000_000)]


# --- regression: the fast-lane reserve must never exceed the ceiling -----------------------


@pytest.mark.parametrize("ceiling", [100_000, 400_000, 1_000_000, 2_000_000, 10_000_000])
def test_low_ceiling_still_admits_work(ceiling):
    """A small global cap must not deadlock the queue (DESIGN.md §4.5).

    "10% of B, min 1 MB/s" is an unconditional floor, so before the B/2 cap any ceiling at or
    below 1 MB/s produced a reserve >= B, headroom <= 0, and a main lane that admitted nothing
    forever — silently. Found in build phase 3a with a 400 KB/s cap.
    """
    from lftpweb.core.queue import TransferSettings

    settings = TransferSettings(
        max_bandwidth_bps=ceiling, max_concurrent_transfers=2, min_share_floor_bps=1
    ).scheduler_settings()

    assert (
        settings.small_lane_reserve_bps <= ceiling // 2
    ), "reserve must never exceed half the ceiling"

    queued = [
        QueuedJob(
            id=1,
            lane="main",
            rank=0.0,
            queued_at=datetime(2026, 8, 11, tzinfo=UTC),
            forced_full_rate=False,
        )
    ]
    decisions = admit(settings, [], queued)
    assert len(decisions) == 1, f"nothing admitted at ceiling={ceiling} — queue is deadlocked"
    assert decisions[0].rate_limit_bps > 0
