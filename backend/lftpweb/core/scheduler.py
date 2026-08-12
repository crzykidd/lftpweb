"""Admission control (DESIGN.md §4.5) — a pure function, deliberately free of subprocesses
and I/O so the worked examples in §4.5/§14 are testable without spawning anything.

`core/queue.py` is the only caller: it gathers `(settings, running, queue)` from the database
and its own in-memory process table, calls `admit()`, and turns each `AdmitDecision` into a
spawned `lftp` process. This module never touches the database, the filesystem, or a
subprocess — see DESIGN.md §12 on why the scheduler/queue split exists at all.

**The invariant this whole module exists to protect:** a running job's allocation is fixed at
spawn and never re-shaped (§4.5's "the invariant"). `admit()` is stateless — it is *given* the
currently-running jobs' allocations as input on every call, and it never adjusts them; it only
ever decides what to hand out to newly-admitted jobs. Once a job appears in `running`, this
module keeps its `rate_limit_bps` in every headroom calculation it participates in for the rest
of its life, never revisits it.
"""

from __future__ import annotations

from dataclasses import dataclass

Lane = str  # 'main' | 'small'

LANE_MAIN = "main"
LANE_SMALL = "small"


@dataclass(frozen=True)
class SchedulerSettings:
    """Site-level transfer settings (DESIGN.md §4.5's table), persisted in `setting` and
    exposed via the settings API. All rates are bytes/sec; all sizes are bytes.
    """

    max_bandwidth_bps: int  # B
    max_concurrent_transfers: int  # N, main-lane slots
    small_item_threshold_bytes: int = 10_000_000  # 10 MB default (§4.5)
    small_lane_concurrency: int = 2
    small_lane_reserve_bps: int = 1_000_000  # "10% of B, min 1 MB/s" — computed by the caller
    min_share_floor_bps: int = 500_000


@dataclass(frozen=True)
class RunningJob:
    """A job currently occupying a slot. `rate_limit_bps` is the allocation it was admitted
    with — read-only from this module's point of view (the invariant, above).
    """

    id: int
    lane: Lane
    rate_limit_bps: int


@dataclass(frozen=True)
class QueuedJob:
    """A job waiting for admission. `rank`/`queued_at` implement DESIGN.md §4.5's ordering
    (`rank DESC, queued_at ASC` — default oldest-first, `queued_at` as the tiebreak, "Move to
    top" as a higher `rank`). `forced_full_rate` is the "Start now at max bandwidth" escape
    hatch (§4.5) — set per-item by a dedicated action, not by raising rank.
    """

    id: int
    lane: Lane
    rank: float
    queued_at: str  # sortable (ISO-8601), ties broken oldest-first
    forced_full_rate: bool = False


@dataclass(frozen=True)
class AdmitDecision:
    job_id: int
    lane: Lane
    rate_limit_bps: int
    forced_full_rate: bool = False


def _priority_key(q: QueuedJob) -> tuple[float, str]:
    # rank DESC, queued_at ASC — negate rank so a plain ascending sort gives rank-descending.
    return (-q.rank, q.queued_at)


def admit(
    settings: SchedulerSettings,
    running: list[RunningJob],
    queue: list[QueuedJob],
) -> list[AdmitDecision]:
    """One scheduling pass: `(N, B, running, queue, settings)` in, the admit list out.

    Three independent decisions, in this order, each below its own heading in DESIGN.md §4.5:

    1. **Start now at max bandwidth** — any queued main-lane item flagged
       `forced_full_rate` admits unconditionally, at the full ceiling `B`, regardless of slots
       or headroom. This is the deliberate-oversubscription escape hatch; it is *not* gated by
       anything below. Its allocation folds into `headroom` for step 2 exactly like any other
       running job would, which is what "freezes new [normal] admissions" without any special
       casing — headroom simply goes negative.
    2. **Main-lane admission** — the `slots`/`headroom`/floor-loop algorithm, over whatever
       main-lane queue entries step 1 didn't already take.
    3. **Fast lane** — a separate concurrency cap and its own reserve slice of `B`, entered by
       item size alone (the caller is responsible for lane assignment before calling this).
       Never touches main-lane `slots` or `headroom`, in either direction (§4.5: "consuming no
       main-lane slot and never entering the headroom calculation").
    """
    decisions: list[AdmitDecision] = []

    # --- 1. Start now at max bandwidth ------------------------------------------------
    forced = sorted(
        (q for q in queue if q.lane == LANE_MAIN and q.forced_full_rate),
        key=_priority_key,
    )
    for q in forced:
        decisions.append(
            AdmitDecision(
                job_id=q.id,
                lane=LANE_MAIN,
                rate_limit_bps=settings.max_bandwidth_bps,
                forced_full_rate=True,
            )
        )
    forced_ids = {d.job_id for d in decisions}

    # --- 2. Main-lane admission --------------------------------------------------------
    main_running = [r for r in running if r.lane == LANE_MAIN]
    main_queue = [q for q in queue if q.lane == LANE_MAIN and q.id not in forced_ids]

    running_count = len(main_running) + len(decisions)  # forced jobs occupy a slot too
    slots = max(settings.max_concurrent_transfers - running_count, 0)
    ready = min(slots, len(main_queue))

    allocated = sum(r.rate_limit_bps for r in main_running) + sum(
        d.rate_limit_bps for d in decisions
    )
    headroom = settings.max_bandwidth_bps - settings.small_lane_reserve_bps - allocated

    if ready > 0 and headroom > 0:
        share = headroom / ready
        while share < settings.min_share_floor_bps and ready > 1:
            ready -= 1
            share = headroom / ready
        ordered = sorted(main_queue, key=_priority_key)[:ready]
        for q in ordered:
            decisions.append(AdmitDecision(job_id=q.id, lane=LANE_MAIN, rate_limit_bps=int(share)))

    # --- 3. Fast lane --------------------------------------------------------------------
    small_running = [r for r in running if r.lane == LANE_SMALL]
    small_queue = [q for q in queue if q.lane == LANE_SMALL]

    small_slots = max(settings.small_lane_concurrency - len(small_running), 0)
    small_ready = min(small_slots, len(small_queue))
    if small_ready > 0:
        # The reserve is shared across every *active* (running + about-to-be-admitted) small
        # job, evenly — not handed out at the full reserve to each. Never re-shapes an
        # already-running small job's rate (same invariant as the main lane); only the newly
        # admitted ones get this computed share.
        total_active = len(small_running) + small_ready
        share = settings.small_lane_reserve_bps / total_active
        ordered = sorted(small_queue, key=_priority_key)[:small_ready]
        for q in ordered:
            decisions.append(AdmitDecision(job_id=q.id, lane=LANE_SMALL, rate_limit_bps=int(share)))

    return decisions
