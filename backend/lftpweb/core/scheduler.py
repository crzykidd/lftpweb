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

    **`max_bandwidth_bps` is whatever budget the caller hands in, and this module asks no
    further questions about where it came from.** Since 2026-08-21 the site has two bandwidth
    numbers -- a ceiling and a Queue-tab throttle within it (DESIGN.md §4.5's "The ceiling and
    the throttle") -- and `core/queue.py.TransferSettings.scheduler_settings` feeds the
    *effective* one here, at the call site. Nothing about this dataclass, `admit()`, or §4.5's
    worked examples changed for it: `B` was always "the budget", and it still is.
    """

    max_bandwidth_bps: int  # B -- the limit in force (see the note above)
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
    """A job waiting for admission. `queue_position` implements DESIGN.md §4.5's ordering —
    `queue_position ASC` (with `id` below as the final tiebreak) — a dense fractional total
    order that replaced `rank DESC, queued_at ASC` (2026-08-19,
    docs/transfers-redesign-spec.md §3.4, prompts/done/2026-08-19-queue-position-order-model.md).
    The old scheme was a two-zone boost (boosted zone by most-recently-boosted `rank`, natural
    zone by oldest `queued_at`) that could not support "move up one" — see the migration
    (`migrations/023_queue_position.sql`) and that prompt for why. `queued_at` still exists on
    `job` and still drives the Transfers page's queued-wait readout; it is simply no longer an
    ordering input to this module. `forced_rate_fraction` is the "Start now" escape hatch
    (§4.5) — set per-item by a dedicated action, not by position — `None` means not forced;
    otherwise it's the fraction of the site's `max_bandwidth_bps` this job admits at (2026-08-19,
    prompts/done/2026-08-19-start-now-bandwidth-fractions.md: the menu's 10%/25%/50%/75%
    options), with `1.0` reading as "Max" — byte-identical to the pre-fraction
    `forced_full_rate=True` this field replaces.
    """

    id: int
    lane: Lane
    queue_position: float
    forced_rate_fraction: float | None = None


@dataclass(frozen=True)
class AdmitDecision:
    job_id: int
    lane: Lane
    rate_limit_bps: int
    # Mirrors the admitted `QueuedJob.forced_rate_fraction`, if this decision came from step 1
    # below — `None` for every ordinary main-lane/fast-lane admission.
    forced_rate_fraction: float | None = None


def _priority_key(q: QueuedJob) -> tuple[float, int]:
    # queue_position ASC, id ASC — `id` is a stable final tiebreak for the (rare, but not
    # impossible) case of two rows sharing a position, e.g. an admission race.
    return (q.queue_position, q.id)


def admit(
    settings: SchedulerSettings,
    running: list[RunningJob],
    queue: list[QueuedJob],
) -> list[AdmitDecision]:
    """One scheduling pass: `(N, B, running, queue, settings)` in, the admit list out.

    Three independent decisions, in this order, each below its own heading in DESIGN.md §4.5:

    1. **Start now** — any queued main-lane item with a `forced_rate_fraction` set admits
       unconditionally, at `fraction × B` (rounded to the nearest whole byte/sec), regardless of
       slots or headroom (2026-08-19,
       prompts/done/2026-08-19-start-now-bandwidth-fractions.md — the "Start now at max
       bandwidth" escape hatch widened into a menu: 10%/25%/50%/75%/Max of the site total limit,
       computed once here, at admission, never re-shaped afterward like any other job's
       allocation). `fraction=1.0` (Max) computes `round(1.0 × B) == B` — the identical value,
       and the identical code path, the pre-fraction design always took. This is *not* gated by
       anything below. Its allocation folds into `headroom` for step 2 exactly like any other
       running job would, which is what "freezes new [normal] admissions" without any special
       casing — headroom simply goes negative (more so at Max than at a smaller fraction, but
       the mechanism is the same either way).
    2. **Main-lane admission** — the `slots`/`headroom`/floor-loop algorithm, over whatever
       main-lane queue entries step 1 didn't already take.
    3. **Fast lane** — a separate concurrency cap and its own reserve slice of `B`, entered by
       item size alone (the caller is responsible for lane assignment before calling this).
       Never touches main-lane `slots` or `headroom`, in either direction (§4.5: "consuming no
       main-lane slot and never entering the headroom calculation").
    """
    decisions: list[AdmitDecision] = []

    # --- 1. Start now (10%/25%/50%/75%/Max of the site total limit) -------------------
    forced = sorted(
        (q for q in queue if q.lane == LANE_MAIN and q.forced_rate_fraction is not None),
        key=_priority_key,
    )
    for q in forced:
        assert q.forced_rate_fraction is not None  # narrows for the type checker; filtered above
        decisions.append(
            AdmitDecision(
                job_id=q.id,
                lane=LANE_MAIN,
                rate_limit_bps=round(q.forced_rate_fraction * settings.max_bandwidth_bps),
                forced_rate_fraction=q.forced_rate_fraction,
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
