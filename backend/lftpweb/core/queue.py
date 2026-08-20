"""Job lifecycle and process supervision (DESIGN.md §4.1–§4.6). `TransferQueue` owns
spawning, watching, and reaping — `core/scheduler.py` owns only the admission *decision*
(§4.5, §12). Every tick (`transfer_tick_s`, default 1 s per §4.4):

1. reap any process that exited since the last tick, persist the outcome, and either retry
   (transient class, attempts remaining) or terminate the item's lifecycle (§4.3/§4.6);
2. sample progress for everything still running (`core/progress.py`) and publish it -- but only
   every `PROGRESS_SAMPLE_TICKS`-th tick (~5s), not every tick; reap/admit/stop stay on the 1s
   loop so a Stop click still takes effect in ~1s (§4.4);
3. gather `(running, queue)` from the database, call `scheduler.admit()`, and spawn whatever
   it admits (`core/lftp.py`).

Nothing here parses lftp's stdout for progress — see `core/progress.py`'s module docstring.
Output is captured only to classify a non-zero exit (`core/lftp.py.classify_output`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import (
    audit,
    download_prefix,
    lftp,
    local_scan,
    mount_sentinel,
    patterns,
    scheduler,
    settle,
)
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import ITEM_VIEW_COLUMNS, ItemView, item_view
from lftpweb.core.pipeline_flight import (
    in_flight_expr,
    item_pipeline_busy_subquery,
    waiting_reason_expr,
)
from lftpweb.core.local_delete import _physical_local_root
from lftpweb.core.metrics import RunningJobBytes, ThroughputSampler
from lftpweb.core.progress import ActiveJob, JobProgress, ProgressSampler, child_speed_bps
from lftpweb.core.remote import HostConfig, parse_connection_limit

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_BASE_S = 30.0
DEFAULT_RETRY_BACKOFF_MAX_S = 15 * 60.0


def compute_retry_backoff(base_s: float, attempt: int) -> float:
    """Seconds to wait before retry number `attempt` (1-based -- the attempt that just failed).
    Exponential from `base_s`, doubling per attempt, clamped to `DEFAULT_RETRY_BACKOFF_MAX_S`.

    A pure function purely so it can be unit-tested without driving a real job through
    `_reap_one` (the same shape `core/scheduler.py.admit` and `core/settle.py` already use). It
    exists because this arithmetic silently ignored `TransferSettings.retry_backoff_base_s` from
    phase 3a until 2026-08-14 -- the setting round-tripped through the API and the Settings form
    the whole time while `_reap_one` read the module constant instead, so the field was a live
    control that did nothing. Nothing tested it, which is exactly how it survived; the fix is
    paired with the test that would have caught it.
    """
    return min(base_s * (2 ** (attempt - 1)), DEFAULT_RETRY_BACKOFF_MAX_S)


# Transient vs. permanent (DESIGN.md §4.3) — the same whitelist `core/lftp.py` uses for the
# same reason (see its module docstring): only retry what we can positively identify as
# transient, never "everything that isn't in the permanent list."
PERMANENT_ERROR_CLASSES = frozenset(
    {"AUTH_FAILED", "PERMISSION_DENIED", "REMOTE_GONE", "DISK_FULL"}
)


# 2026-08-17 (prompts/2026-08-17-interrupted-job-popout-explains-itself.md): the explanation
# `_reconcile_orphaned_jobs` below writes into `output_tail` for a job it marks INTERRUPTED. Real
# lftp output has actual command transcripts in it; this is prose instead, but it lands in the
# same column and flows through the same `has_output_tail`/`GET .../output` path the reap-path
# writes already use (`api/history.py`), so the History popout has something to show instead of
# the blank panel a NULL `output_tail` produced. Kept as a module constant, not inlined at the
# call site, so the test asserting its exact wording and this comment's account of *why* it exists
# stay next to each other only once.
INTERRUPTED_OUTPUT_TAIL = (
    "Transfer interrupted by an application restart or crash -- the process did not exit on its "
    "own. Partial bytes on disk are retained; the next attempt for this item resumes from them."
)


class JobNotDismissableError(Exception):
    """Raised by `TransferQueue.dismiss_job` for a `queued`/`running` job (§4.6's active
    states) -- dismiss is a Transfers-page display action for a job that's already done,
    never a way to hide an active transfer. The UI never offers the button for these states
    (`dismiss_job`'s own docstring), but that's a courtesy, not the guard -- rejecting it here
    too is what makes it impossible, not merely unusual, matching the task's own instruction.
    """


class NoSiteLimitConfiguredError(Exception):
    """Raised by `TransferQueue.start_now` for a *fractional* request (`rate_percent < 100`)
    when no site bandwidth limit is configured (2026-08-19,
    prompts/done/2026-08-19-start-now-bandwidth-fractions.md). A percentage of nothing is
    meaningless -- `api/jobs.py.start_now` turns this into a 409 naming the reason, rather than
    silently substituting Max, which is the one outcome the task's own settled decisions rule
    out explicitly. Max itself (`rate_percent` omitted or `100`) never raises this: it reuses
    whatever `max_bandwidth_bps` already is, unconditionally, exactly as the pre-fraction
    "Start now at max bandwidth" path always did.
    """


class JobNotQueuedError(Exception):
    """Raised by `TransferQueue.move_job` when `job_id` is no longer `queued` -- it started
    running, or reached a terminal state, sometime between the Transfers page rendering its
    chevrons and the click landing (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
    `prompts/2026-08-19-queue-reorder-chevrons.md`). Reordering a running job is meaningless:
    its allocation is fixed at spawn and never re-shaped (DESIGN.md §4.5's invariant), and a
    terminal job isn't in line at all. `api/jobs.py.move_job` turns this into a 409, matching
    `JobNotDismissableError`'s own "the job exists, the request just isn't valid for its current
    state" convention above -- never a 404, which would wrongly suggest the job itself is gone.
    """


# Progress sampling cadence -- deliberately *not* the ~1 Hz `tick_s` everything else in this
# module runs at (reap/admit/stop stay at `tick_s`, §4.4/DESIGN.md §4.4):
#
# - `PROGRESS_SAMPLE_TICKS`: `_sample_and_publish_progress` -- job-level `ProgressSampler.sample`,
#   the per-tick `item_delta` publish for the parent item, *and* `_publish_child_progress` --
#   all gate on this same counter, so job and child speeds are measured over the identical
#   interval. Unified 2026-08-16 (user decision, watching a live transfer): job speed used to
#   sample every tick (~1 Hz) while per-file speed sampled every 3rd tick, each with its own EMA
#   lag, so a one-file directory showed two speeds that never agreed. One shared 5-tick (~5s)
#   cadence fixes that (same instants, same smoothing) and gives the underlying rate a longer
#   delta window to average over -- a side benefit, not the primary motivation. Was
#   `CHILD_PROGRESS_THROTTLE_TICKS = 3`, child-progress-only; still keeps the write-pressure
#   rationale that constant was for (`docs/decisions.md`, `209928d`): a 50-file release
#   recomputing every tick at 1 Hz is up to 50 `UPDATE`s a second, exactly what turned the
#   `VACUUM INTO` backup race from rare into routine. 5 ticks batches the writes further, not
#   less, while still reading as "live" on the Files/Transfers pages. A fresh job's speed now
#   reads 0 until its second sample, ~5-10s in -- longer than before, and accepted as-is (see
#   `docs/decisions.md`).
# - `MAX_CHILD_PROGRESS_UPDATES_PER_TICK`: a safety cap, not the normal case. In practice the
#   changed set per sampled tick is bounded by lftp's own parallelism
#   (`mirror_parallel_transfer_count`, a handful of files at a time), never by how large the
#   release is -- but nothing here enforces that bound structurally, so a cap plus a logged
#   truncation (rather than a silent one) is cheap insurance against a future case where it
#   doesn't hold.
PROGRESS_SAMPLE_TICKS = 5
MAX_CHILD_PROGRESS_UPDATES_PER_TICK = 100


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parent_rel(rel_path: str) -> str | None:
    if "/" not in rel_path:
        return None
    return rel_path.rsplit("/", 1)[0]


def _like_escape(value: str) -> str:
    """Escapes `%`/`_`/`\\` so a `LIKE ... ESCAPE '\\'` pattern built from user text matches a
    **literal** substring, never a glob -- the same "no glob/regex parsing" contract
    `lib/transferPanel.ts.filterTransferJobs`'s own docstring states for its client-side
    counterpart. Callers build the pattern as `f"%{_like_escape(needle.lower())}%"` and compare
    against `LOWER(rel_path)`, mirroring that function's `toLowerCase()` case-insensitivity
    rather than relying on SQLite's own `LIKE`, which only case-folds ASCII by default.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_forced_rate_fraction(row: Any) -> float | None:
    """The one place that reads both `job.forced_rate_fraction` (migration 022) and
    `job.forced_full_rate` (migration 001) off a `job` row, so every reader -- `_admit` below,
    `api/jobs.py._job_out` -- agrees on what a row means (2026-08-19,
    prompts/done/2026-08-19-start-now-bandwidth-fractions.md). Prefers the fraction column;
    falls back to `1.0` for a legacy row that predates it (`forced_full_rate=1`,
    `forced_rate_fraction` NULL) -- migration 022 backfills every such row at upgrade time, so
    this fallback is a belt-and-suspenders read, not the normal path. `row` is a plain `dict`
    (every caller passes one -- `list_jobs()`'s own `dict(row)`, or a test fixture built the
    same shape) or a `sqlite3.Row`-like mapping; `.get` covers a dict missing the key entirely
    (an older test fixture, say) the same way it already covers `speed_bps`/`eta_s` elsewhere in
    this module's own row shaping.
    """
    fraction = (
        row.get("forced_rate_fraction") if hasattr(row, "get") else row["forced_rate_fraction"]
    )
    if fraction is not None:
        return float(fraction)
    return 1.0 if row["forced_full_rate"] else None


def position_between(lower: float | None, upper: float | None) -> float:
    """A fresh `job.queue_position` for a spot in the queue's dense ordering
    (`queue_position ASC`, `migrations/023_queue_position.sql`) -- the one primitive every
    writer of the column uses, so the arithmetic exists in exactly one place. `lower` is the
    position of the row immediately before the new one belongs (`None` = insert before
    everything); `upper` is the row immediately after (`None` = insert after everything). Both
    `None` means an empty ordering.

    - `_insert_job`'s default (append at the back): `position_between(MAX(queue_position), None)`.
    - `move_to_top`: `position_between(None, MIN(queue_position))`.
    - `_rescue_position` (the v0.2.6 startup-rescue re-derivation, below): the midpoint between
      the natural-zone neighbours the rescued job's original `queued_at` falls between.
    - **Not yet called by any chevron** -- this is stage 1 of
      docs/transfers-redesign-spec.md §3.4; stage 2's "move up one / down one" will call this
      directly with the row's immediate neighbours. Exercised today only via the three callers
      above and this module's own tests, per the spec's "prove the primitive now" instruction.

    No rebalancing: repeated midpoint bisection between the same two neighbours converges on
    float precision after ~50 successive inserts at the same spot, which is an existing,
    accepted property of the fractional-key design (DESIGN.md §4.5's "occasional rebalance"),
    not a new limitation this function introduces.
    """
    if lower is None and upper is None:
        return 1.0
    if lower is None:
        return upper - 1.0
    if upper is None:
        return lower + 1.0
    return (lower + upper) / 2.0


def _parent_rel_path(full_path: str) -> str:
    """The directory portion of a full filesystem path, POSIX-style — used only to compute
    the directory a `pget` target file must already live in (see `_spawn_decision`), not for
    any remote-path logic.
    """
    return full_path.rsplit("/", 1)[0] if "/" in full_path else "."


@dataclass(frozen=True)
class TransferSettings:
    """Site-level settings (DESIGN.md §4.5's table + §9.3's parallelism knobs), persisted as
    JSON in `setting`. One instance = one site (DESIGN.md §4.5: "one container serves one
    site... not per-queue").
    """

    max_bandwidth_bps: int = 10_000_000
    max_concurrent_transfers: int = 2
    small_item_threshold_bytes: int = 10_000_000
    small_lane_concurrency: int = 2
    small_lane_reserve_bps: int | None = None  # None -> derived: 10% of B, min 1 MB/s
    min_share_floor_bps: int = 500_000
    mirror_parallel_transfer_count: int = 4
    mirror_use_pget_n: int = 4
    pget_default_n: int = 4
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_base_s: float = DEFAULT_RETRY_BACKOFF_BASE_S
    extra_lftp_settings: str = ""

    def effective_small_lane_reserve_bps(self) -> int:
        """The fast lane's slice of the ceiling, never more than half of it.

        The half-of-B clamp is load-bearing, not defensive. DESIGN.md §4.5 originally
        specified "10% of B, min 1 MB/s" — but that floor is unconditional, so any ceiling at
        or below 1 MB/s produced a reserve >= B, hence headroom <= 0, hence the main lane
        admitted nothing, ever. Jobs queued and sat there with no error and no log line. The
        fast lane exists to stop small items being blocked; it must never be able to block
        everything else instead.
        """
        raw = (
            self.small_lane_reserve_bps
            if self.small_lane_reserve_bps is not None
            else max(round(self.max_bandwidth_bps * 0.10), 1_000_000)
        )
        return min(raw, self.max_bandwidth_bps // 2)

    def scheduler_settings(self) -> scheduler.SchedulerSettings:
        return scheduler.SchedulerSettings(
            max_bandwidth_bps=self.max_bandwidth_bps,
            max_concurrent_transfers=self.max_concurrent_transfers,
            small_item_threshold_bytes=self.small_item_threshold_bytes,
            small_lane_concurrency=self.small_lane_concurrency,
            small_lane_reserve_bps=self.effective_small_lane_reserve_bps(),
            min_share_floor_bps=self.min_share_floor_bps,
        )


SETTING_KEY = "transfer_settings"


async def load_transfer_settings(db: aiosqlite.Connection) -> TransferSettings:
    cursor = await db.execute("SELECT value FROM setting WHERE key = ?", (SETTING_KEY,))
    row = await cursor.fetchone()
    if row is None:
        return TransferSettings()
    try:
        data = json.loads(row["value"])
    except (ValueError, TypeError):
        return TransferSettings()
    known = {f: data[f] for f in TransferSettings.__dataclass_fields__ if f in data}
    return TransferSettings(**known)


async def save_transfer_settings(db: aiosqlite.Connection, settings: TransferSettings) -> None:
    payload = json.dumps({f: getattr(settings, f) for f in TransferSettings.__dataclass_fields__})
    await db.execute(
        "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (SETTING_KEY, payload),
    )
    await db.commit()


@dataclass
class _RunningProcess:
    job_id: int
    item_id: int
    queue_id: int
    rel_path: str  # for item_delta WS messages (Files page) -- not used for any lftp command
    is_dir: bool
    kind: str
    lane: str
    rate_limit_bps: int
    # 2026-08-19 (prompts/done/2026-08-19-start-now-bandwidth-fractions.md): the fraction of the
    # site limit this job was force-started at (`None` for a normal admission, `1.0` for Max) --
    # mirrors `scheduler.AdmitDecision.forced_rate_fraction`, the field this replaces.
    forced_rate_fraction: float | None
    local_root: str  # what progress.py should sample (file path for pget, item dir for mirror)
    bytes_total: int | None
    remote_mtime: float | None
    spawned: lftp.SpawnedJob
    wait_task: asyncio.Task
    # Mirrors `job.bytes_start` (the local size already on disk when this job was spawned --
    # a resume/retry does not start over, `-c`). Carried here so `_sample_metrics` never needs
    # a second DB read to feed core/metrics.py's non-monotonic-safe per-job delta; see that
    # module's docstring for why `bytes_done` alone can't be differenced by job id.
    bytes_start: int = 0
    stop_requested: bool = False


class TransferQueue:
    """One instance lives on `app.state.queue` for the process lifetime, alongside
    `core/engine.py`'s `Engine` (DESIGN.md §2). Shares the same `db` connection and
    `EventBus`; does not scan — it only ever acts on `item`/`job` rows the engine already
    populated.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        config_dir: str,
        events: EventBus,
        *,
        run_dir: str = lftp.DEFAULT_RUN_DIR,
        tick_s: float = 1.0,
        host_provider: Any = None,
    ) -> None:
        self.db = db
        self.config_dir = config_dir
        self.events = events
        self.run_dir = run_dir
        self.tick_s = tick_s
        # Callable[[], Awaitable[HostConfig | None]] — reuses core/engine.py's
        # load_host_config so credential decryption has exactly one implementation. Injected
        # rather than imported directly to keep this module testable without the engine.
        self._host_provider = host_provider
        # Phase 5 (DESIGN.md §6): set as a plain attribute after construction, not a
        # constructor parameter — `main.py`'s lifespan builds `TransferQueue` before `Engine`
        # (phase 4's ordering, needed so `AutoQueue` can use `enqueue_item`), and the
        # postprocessing pipeline needs `Engine.pool` (the one pooled asyncssh connection,
        # DESIGN.md §5/§7.4), which doesn't exist until `Engine` is constructed. `None` (every
        # existing test's default) means postprocessing simply never triggers — not a crash,
        # not a silent no-op that looks like a bug, just "this capability isn't wired up."
        self.postprocess: Any = None

        self._running: dict[int, _RunningProcess] = {}  # job_id -> process
        self._backoff_until: dict[int, float] = {}  # item_id -> monotonic time
        self.progress = ProgressSampler()
        # Throughput metrics (this task, DESIGN.md — new section proposed): same
        # process-lifetime, tick-driven shape as `self.progress` above -- see
        # `core/metrics.py`'s module docstring and `_sample_metrics` below.
        self.metrics = ThroughputSampler(self.db)
        self._last_speeds: dict[int, float] = {}  # job_id -> most recent EMA speed, for stats()
        # `list_jobs()` (an API read path, polled on whatever cadence the frontend chooses)
        # must never call `self.progress.sample()` itself — the sampler's EMA math assumes
        # it's ticked once every `PROGRESS_SAMPLE_TICKS`-th `tick_s` by
        # `_sample_and_publish_progress()` below, and a second, out-of-cadence call would
        # corrupt that state (a shorter-than-expected `dt` skews the instantaneous rate it
        # derives). This cache is what `list_jobs()` reads instead: whatever the last real
        # sampled tick computed.
        self._last_progress: dict[int, Any] = {}  # job_id -> progress.JobProgress

        # Live per-file (child) progress inside a mirroring directory -- see
        # `_publish_child_progress`. `_progress_tick_count` counts calls to
        # `_sample_and_publish_progress` (every `tick_s`, ~1s) and gates the *entire* body of
        # that method -- job-level sampling, the parent's `item_delta`, and child publishing
        # alike -- to run only every `PROGRESS_SAMPLE_TICKS`-th one (2026-08-16: unified onto
        # one counter so job and child speeds are measured over the identical interval; see
        # that constant's own comment). `_prev_child_sizes` is job_id -> {child rel_path
        # (relative to the job's own `local_root`, i.e. the item's directory, not the queue
        # root) -> the size last diffed for it} -- only the rel_paths this module has actually
        # persisted/published, so a child skipped by the cap on one sampled tick still reads as
        # "changed" on the next rather than being silently marked seen. Reset per job_id in
        # `_reap_one` below so a future job id never inherits stale history (same shape as
        # `self.progress.drop`).
        self._progress_tick_count = 0
        self._prev_child_sizes: dict[int, dict[str, int]] = {}
        # Per-child speed (this task, 2026-08-14, "per-file speed inside a mirror"): a real
        # timestamp per (job_id, rel_path) -- `_prev_child_sizes` above only ever recorded the
        # *value* last diffed, never *when*, so there was nothing to divide a byte delta by
        # except the wrong assumption `tick_s * PROGRESS_SAMPLE_TICKS`, which a slow pass makes
        # silently wrong (this project has already shipped one bug from exactly that shape of
        # wrong denominator, `6e6b217`). `_prev_child_times` is job_id -> {rel_path ->
        # `time.monotonic()` at the last tick this method actually diffed that child};
        # `_child_speed` is job_id -> {rel_path -> its last EMA-smoothed rate}, the per-child
        # analogue of `ProgressSampler._speed` above, smoothed with the exact same
        # `core/progress.py.ema_step` formula rather than a second scheme (a raw per-tick delta
        # on one file is burstier than the job aggregate, per that module's docstring). Both
        # pruned in `_reap_one` alongside `_prev_child_sizes` so a future job id can never
        # inherit stale rate history.
        self._prev_child_times: dict[int, dict[str, float]] = {}
        self._child_speed: dict[int, dict[str, float]] = {}

        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

        # Phase 8 (DESIGN.md §8): "hold all transfers for a host whose credentials need
        # re-entry, instead of spawning jobs that fail." `_admit` checks this every tick; the
        # flag only exists so the WARNING log line fires once per transition into the held
        # state instead of once per second for as long as it lasts (see `_admit` below).
        self.credentials_need_reentry = False

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        await self._reconcile_orphaned_jobs()
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="lftpweb-transfer-queue-loop")

    async def _reconcile_orphaned_jobs(self) -> None:
        """Clear `running` rows left behind by a restart, then re-queue everything this sweep
        (or an earlier one) left interrupted (2026-08-18, production incident diagnosed live +
        support bundle `lftpweb-support-0.2.4-20260818T192004Z`).

        A job is only `running` while this process supervises its lftp child, so any such row
        found at startup is orphaned by definition — the container went away and took the
        process with it. Left alone the row says `running` forever: it shows up in
        `list_jobs()` as a phantom transfer that never progresses, and because scans
        deliberately don't overwrite job-lifecycle states (`_protected_rel_paths`), the item
        stays `DOWNLOADING` forever too. Observed on a real deployment after an image pull.

        Deliberately *not* suppressed for auto-queue: an interrupted transfer is not a user
        decision (§4.6), so the item stays eligible to be picked up again. Partial bytes on
        disk are kept and `-c` resumes from them; the next scan recomputes the item's state
        from what is actually there.

        **`output_tail` gets `INTERRUPTED_OUTPUT_TAIL`, not left NULL** (2026-08-17, a live find
        on the test instance: a container restart mid-mirror produced exactly this row, and its
        History popout rendered completely blank -- the frontend fix makes the panel reachable at
        all, but a fixed panel with nothing to say is still an empty panel). `COALESCE(NULLIF(
        output_tail, ''), ?)` only fills a NULL or empty-string column, never overwrites a
        genuinely captured tail: a `running` row's `output_tail` is never written anywhere else in
        this module (only reap/dismiss/auth-failure paths touch that column, and none of them can
        run on a job this sweep is about to mark `failed` before the tick loop even starts), so the
        guard is defensive, not load-bearing today -- but "never overwrite real captured lftp
        output with prose" is cheap enough to state unconditionally rather than assume it forever.

        **The 2026-08-18 incident, and why "stays eligible" wasn't actually true.** The docstring
        above has always claimed an interrupted item "stays eligible to be picked up again" — but
        that only ever held for `PARTIAL`. A job that had *actually finished* (every byte on disk,
        lftp already exited 0) when the supervisor itself froze in D-state disk-wait during an NFS
        half-outage got the exact same `failed`/`INTERRUPTED` marking as a genuinely-partial one,
        and the item correctly re-derives `DOWNLOADED` from the filesystem (§1.3) on the next scan
        — but `DOWNLOADED` is not an auto-queue-eligible state, so nothing ever re-queues it, no
        post-processing runs, and the `.downloading-` prefix stays on forever. Its `PARTIAL`
        sibling self-healed within seconds because `PARTIAL` *is* auto-queue eligible; the
        asymmetry was the bug. The fix below re-queues what this sweep just interrupted (rule 1)
        and, since production already had a row stranded by an *earlier* restart before this fix
        shipped, also rescues that shape directly (rule 2) — see `_requeue_stranded_downloaded`.
        A re-queued complete item's `mirror -c` finds nothing to transfer, exits 0 almost
        immediately, and that *observed* job success is what triggers the post-processing
        pipeline (`_reap_one` below) — the exact recovery the user performed by hand (one Queue
        click) for the live incident item. Reusing observed-job-success as the single pipeline
        trigger, rather than adding a second entry point that fires straight from this sweep, is
        the deliberate choice recorded in `docs/decisions.md` — this sweep only ever calls the
        same `enqueue_item` every other caller uses, never a hand-rolled pipeline kickoff.
        """
        cursor = await self.db.execute(
            "SELECT id, item_id, queued_at FROM job WHERE state = 'running'"
        )
        rows = await cursor.fetchall()
        if rows:
            logger.warning(
                "clearing %d job(s) left 'running' by a previous run: %s",
                len(rows),
                ", ".join(str(r["id"]) for r in rows),
            )
            await self.db.execute(
                "UPDATE job SET state = 'failed', pid = NULL, error_class = 'INTERRUPTED', "
                "finished_at = ?, output_tail = COALESCE(NULLIF(output_tail, ''), ?) "
                "WHERE state = 'running'",
                (_now_iso(), INTERRUPTED_OUTPUT_TAIL),
            )
            await self.db.execute(
                "UPDATE item SET state = 'PARTIAL' WHERE state = 'DOWNLOADING' AND id IN "
                "(SELECT item_id FROM job WHERE error_class = 'INTERRUPTED')"
            )
            await self.db.commit()
            for row in rows:
                await self._publish_item_state(row["item_id"])

        # Rule 1: re-queue every item this pass just marked INTERRUPTED -- mount-gated per
        # queue below. `handled_item_ids` is threaded into rule 2 so an item this pass already
        # handled (queued or gated) is never double-processed by the stranded-row query.
        # `mount_cache`/`gated_logged` are shared across both rules so a queue holding several
        # affected items is only stat'd once and only ever earns one gate event for this whole
        # pass, not one per item (matching `core/autoqueue.py.on_scan`'s own per-queue debounce).
        mount_cache: dict[int, tuple[bool, str]] = {}
        gated_logged: set[int] = set()
        handled_item_ids: set[int] = set()
        for row in rows:
            handled_item_ids.add(row["item_id"])
            await self._requeue_interrupted_item(
                row["item_id"],
                mount_cache=mount_cache,
                gated_logged=gated_logged,
                freshly_interrupted=True,
                queued_at=row["queued_at"],
            )

        # Rule 2: rescue rows stranded by *earlier* restarts (before this fix shipped, or from
        # a previous pass that found its queue mount-gated). See `_requeue_stranded_downloaded`.
        await self._requeue_stranded_downloaded(
            mount_cache=mount_cache, gated_logged=gated_logged, exclude_item_ids=handled_item_ids
        )

    async def _requeue_interrupted_item(
        self,
        item_id: int,
        *,
        mount_cache: dict[int, tuple[bool, str]],
        gated_logged: set[int],
        freshly_interrupted: bool,
        queued_at: str,
    ) -> None:
        """Re-queue one item whose most recent job is `failed`/`INTERRUPTED` -- `enqueue_item`
        is idempotent (never a hand-rolled INSERT: the duplicate-process guards it carries exist
        for exactly this kind of caller, prompts/2026-08-13-lftp-timestamped-temp-files.md), so
        this is safe to call even if auto-queue or a human races it. A re-queued `DOWNLOADED`
        item's `mirror -c` no-ops straight into the post-processing pipeline on its observed
        success; a re-queued `PARTIAL` one resumes from its bytes, same as auto-queue would have
        done, but without depending on auto-queue being enabled for this queue.

        **Mount-gated** (2026-08-18): auto-queue refuses to act for a queue whose mount sentinel
        fails, precisely so nothing writes into an unmounted directory
        (`core/mount_sentinel.py`, `core/autoqueue.py.on_scan`) -- a restart with a broken mount
        (the exact incident this exists for) must not have this sweep spawn lftp processes into
        the void. A gated item is left exactly as today: marked INTERRUPTED, not re-queued. The
        next healthy scan's auto-queue still picks up a `PARTIAL` one on its own; a `DOWNLOADED`
        one is covered by `_requeue_stranded_downloaded` on the *next* startup (auto-queue itself
        structurally cannot see a `DOWNLOADED` item -- it isn't in `ELIGIBLE_STATES`).

        **`queued_at` preserves the queued-wait readout** (2026-08-19,
        prompts/2026-08-19-rescue-requeue-keeps-queue-position.md): the caller passes the
        interrupted job's own original `queued_at` -- for rule 1 that's the row this sweep just
        marked INTERRUPTED, for rule 2 (`_requeue_stranded_downloaded`) it's the most recent
        interrupted job it already keyed off. It is honest besides -- the item genuinely has been
        waiting since then, so the Transfers page's queued-wait readout tells the truth.

        **`_rescue_position` re-derives the actual queue *place*** (2026-08-19, this task --
        under the position model, `queued_at` alone no longer places anything; see that method's
        own docstring for how the neighbours are found and why boosted jobs are excluded from the
        search). `rank` is never touched here, so this still can't outrank an explicit
        "Move to top".
        """
        item = await self._fetch_item(item_id)
        if item is None:
            return
        ok, reason = await self._mount_ok(item["queue_id"], mount_cache)
        if not ok:
            if item["queue_id"] not in gated_logged:
                gated_logged.add(item["queue_id"])
                await audit.record_event(
                    self.db,
                    level="warning",
                    kind="interrupted_requeue_gated",
                    message=(
                        f"queue {item['queue_id']}: startup re-queue of interrupted items "
                        f"skipped -- {reason}. Marking left as INTERRUPTED; a PARTIAL item will "
                        "still be picked up by the next healthy scan's auto-queue, a DOWNLOADED "
                        "one on the next startup once the mount is healthy."
                    ),
                )
            return
        try:
            position = await self._rescue_position(queued_at)
            job_id = await self.enqueue_item(item_id, queued_at=queued_at, queue_position=position)
        except Exception:
            logger.exception("startup re-queue of item %d after an interrupted job failed", item_id)
            return
        await audit.record_event(
            self.db,
            level="info",
            item_id=item_id,
            job_id=job_id,
            kind="interrupted_requeued",
            message=(
                f"item {item_id} ({item['rel_path']!r}): job interrupted by a restart/crash -- "
                "re-queued; a completed transfer no-ops straight into post-processing, a "
                "partial one resumes from its bytes -- re-queued at its original position, not "
                "the back of the line"
                + ("" if freshly_interrupted else " (stranded by an earlier restart)")
            ),
        )

    async def _rescue_position(self, original_queued_at: str) -> float:
        """Re-derive the `queue_position` the v0.2.6 startup rescue needs, now that
        `queued_at` is no longer an ordering input (2026-08-19, this task -- the acceptance
        criterion for `docs/transfers-redesign-spec.md`'s stage 1, and the one thing in this
        task that already shipped to production and must not regress).

        **The old fix, and why it stopped placing anything.** Before this task, the rescue
        carried the interrupted job's own `queued_at` forward and left `rank` at its default
        (0) -- under `rank DESC, queued_at ASC` that was enough: rank-0 rows sort purely by
        `queued_at`, so backdating it put the row back among the natural-zone jobs exactly
        where it belonged, and rank 0 could never outrank a `rank > 0` "Move to top". Under
        `queue_position ASC`, `queued_at` carries no positional weight at all -- this method is
        what replaces it: find the two *natural-zone* jobs the original `queued_at` falls
        between, and take their `queue_position` midpoint (`position_between`, above).

        **Restricted to `rank = 0` jobs -- this is load-bearing, not incidental.** `rank`
        is otherwise dead for ordering as of this task (migration 023's own comment), but it is
        still written by `move_to_top` and stays the one reliable "was this job ever explicitly
        boosted" marker. Natural-zone (`rank = 0`) jobs are the only ones whose `queue_position`
        is guaranteed ordered the same as their `queued_at` -- they only ever get a position by
        appending at the back (`_insert_job`'s default) or by this very method's own midpoint
        insert, both of which preserve that correlation by construction. A boosted job's
        position carries no such relationship to its `queued_at` -- it could have been queued
        long ago and boosted just now, or boosted long ago and left with an old timestamp -- so
        comparing the rescued job's `queued_at` against a boosted job's can pick the wrong
        neighbour and land the rescued job ahead of an explicit "Move to top". Concretely: a job
        boosted to `queue_position = -3` with `queued_at` *after* the rescued job's original
        timestamp would, under a naive comparison across all queued jobs, look like a valid
        "right neighbour" -- and the midpoint would land the rescued job in front of it. Excluding
        boosted jobs from the search entirely is what rules that out; see
        tests/test_queue_orphans.py's rescue-position tests for the worked counterexample.

        The natural-zone neighbours bracket the position; `MAX(queue_position)` over boosted
        (`rank != 0`) jobs is the floor a rescued job must never cross, folded in via
        `position_between` exactly like every other caller of that primitive.
        """
        cursor = await self.db.execute(
            "SELECT queue_position FROM job WHERE state = 'queued' AND rank = 0 "
            "AND queued_at <= ? ORDER BY queue_position DESC LIMIT 1",
            (original_queued_at,),
        )
        left = await cursor.fetchone()
        cursor = await self.db.execute(
            "SELECT queue_position FROM job WHERE state = 'queued' AND rank = 0 "
            "AND queued_at > ? ORDER BY queue_position ASC LIMIT 1",
            (original_queued_at,),
        )
        right = await cursor.fetchone()
        cursor = await self.db.execute(
            "SELECT MAX(queue_position) AS m FROM job WHERE state = 'queued' AND rank != 0"
        )
        boosted_max = (await cursor.fetchone())["m"]

        if left is not None and right is not None:
            return position_between(left["queue_position"], right["queue_position"])
        if right is not None:
            # No natural-zone job has an older `queued_at` -- the rescued job belongs at the
            # very front of the natural zone, immediately after the last boosted job (if any).
            return position_between(boosted_max, right["queue_position"])
        if left is not None:
            # No natural-zone job has a newer `queued_at` -- the rescued job belongs at the
            # back of the natural zone.
            return position_between(left["queue_position"], None)
        # No natural-zone jobs currently queued at all -- park it right after any boosted ones.
        return position_between(boosted_max, None)

    async def _requeue_stranded_downloaded(
        self,
        *,
        mount_cache: dict[int, tuple[bool, str]],
        gated_logged: set[int],
        exclude_item_ids: set[int],
    ) -> None:
        """Rescue rows stranded by an *earlier* restart -- the shape this fix's own production
        incident item was already in before this code shipped: rule 1 above only re-queues jobs
        *this* startup just marked INTERRUPTED, so a row an earlier, unfixed restart already
        wedged would otherwise sit stranded forever, exactly as it did in production.

        Deliberately narrow -- only the complete-but-unwitnessed shape auto-queue structurally
        cannot see (`DOWNLOADED` is not in its `ELIGIBLE_STATES`). A stranded `PARTIAL` row is
        already auto-queue's job on the next healthy scan; this clause does not touch it.

        The shape: item `state = 'DOWNLOADED'`, its **most recent** job is `failed`/
        `INTERRUPTED`, no active job remains for it, and its physical directory (the one true
        resolver, `core/local_delete.py._physical_local_root` -- never a second one) still
        carries the download prefix, i.e. the *arr-visible name never got restored. `state =
        'DOWNLOADED'` alone already rules out every postprocess outcome state
        (`core/postprocess.py.OWNED_STATES` -- `VERIFYING`/`VERIFIED`/`CORRUPT`/`EXTRACTING`/
        `EXTRACTED`/`EXTRACT_FAILED`), so there is nothing further to check there.
        """
        cursor = await self.db.execute(
            "SELECT i.id AS item_id, i.rel_path, i.queue_id, i.pending_download_prefix, "
            "(SELECT j.queued_at FROM job j WHERE j.item_id = i.id ORDER BY j.id DESC LIMIT 1) "
            "AS interrupted_queued_at "
            "FROM item i WHERE i.state = 'DOWNLOADED' AND i.pending_download_prefix IS NOT NULL "
            "AND instr(i.rel_path, '/') = 0 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM job j WHERE j.item_id = i.id AND j.state IN ('queued', 'running')"
            ") "
            "AND (SELECT j.error_class FROM job j WHERE j.item_id = i.id ORDER BY j.id DESC "
            "     LIMIT 1) = 'INTERRUPTED'"
        )
        candidates = await cursor.fetchall()
        for row in candidates:
            if row["item_id"] in exclude_item_ids:
                continue
            queue = await self._fetch_queue(row["queue_id"])
            if queue is None:
                continue
            root = Path(queue["local_path"].rstrip("/"))
            physical = await _physical_local_root(
                self.db, queue_id=row["queue_id"], root=root, rel_path=row["rel_path"]
            )
            if physical.name != f"{row['pending_download_prefix']}{row['rel_path']}":
                # The prefixed directory is no longer on disk (an earlier pass already renamed
                # it, or something else changed it out from under this column) -- nothing
                # stranded here after all.
                continue
            await self._requeue_interrupted_item(
                row["item_id"],
                mount_cache=mount_cache,
                gated_logged=gated_logged,
                freshly_interrupted=False,
                queued_at=row["interrupted_queued_at"],
            )

    async def _mount_ok(
        self, queue_id: int, mount_cache: dict[int, tuple[bool, str]]
    ) -> tuple[bool, str]:
        """`(is_ok, reason)` for `queue_id`'s mount sentinel, cached per call to
        `_reconcile_orphaned_jobs` so a queue with several affected items is only stat'd once
        (`core/mount_sentinel.py.check`). The caller pairs this with `gated_logged` to also emit
        only a single gate event per queue for the whole pass, matching
        `core/autoqueue.py.on_scan`'s own once-per-queue debounce rather than one event per item.
        """
        if queue_id in mount_cache:
            return mount_cache[queue_id]
        queue = await self._fetch_queue(queue_id)
        if queue is None:
            result = (False, f"queue {queue_id} no longer exists")
        elif not mount_sentinel.check(queue["local_path"]):
            result = (
                False,
                f"local root {queue['local_path']!r} is missing, unreadable, or has not yet "
                "completed a scan with the mount sentinel present",
            )
        else:
            result = (True, "")
        mount_cache[queue_id] = result
        return result

    async def stop(self) -> None:
        """Graceful shutdown (DESIGN.md §10.3): SIGTERM every in-flight lftp child so its
        `-c` resume state is clean, rather than SIGKILLing them via process-group teardown.

        **Children are terminated concurrently, not one after another** (2026-08-14). Sequential
        termination cost `len(self._running) * grace_s` in the worst case -- with the main lane
        and the fast lane both full that is ~40s of SIGTERM grace before this method even
        returns, and the retention/metrics/backup schedulers still have to stop after it. Docker
        sends SIGKILL when `stop_grace_period` expires, so an oversized shutdown does not degrade
        gracefully: it gets cut off, and the clean-resume path this method exists to provide
        never actually runs. Concurrently, total shutdown is bounded at roughly one `grace_s`
        regardless of how many transfers are in flight, which is what makes
        `docker-compose.yml`'s `stop_grace_period: 60s` comfortable rather than marginal.

        Each child is independent -- separate processes, separate `/run` rc files, no shared
        state between them -- so there is nothing to serialize for correctness. `gather` with
        `return_exceptions=True` because one child failing to die must not prevent the others
        being signalled, or leave their per-job rc files (`spawned.cleanup()`) behind.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        async def _terminate_one(proc: _RunningProcess) -> None:
            try:
                await lftp.terminate(proc.spawned, grace_s=10.0)
            finally:
                # Always -- an rc file left in the `/run` tmpfs holds this job's seedbox
                # password (§4.2/§11.1), so cleanup must not be skipped just because
                # termination raised.
                proc.spawned.cleanup()

        running = list(self._running.values())
        if not running:
            return
        results = await asyncio.gather(
            *(_terminate_one(proc) for proc in running), return_exceptions=True
        )
        for proc, result in zip(running, results, strict=True):
            if isinstance(result, Exception):
                logger.warning(
                    "job %s: error terminating lftp child during shutdown: %r",
                    proc.job_id,
                    result,
                )

    def request_tick(self) -> None:
        self._wake.set()

    @property
    def is_alive(self) -> bool:
        """DESIGN.md §10.3: `/api/health`'s "whether the scheduler loop is alive" -- this is
        that loop (the admission-control scheduler, §4.5, lives in `core/scheduler.py` as a
        pure function, but it only ever runs from here).
        """
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                logger.exception("transfer queue tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_s)
            except TimeoutError:
                pass
            self._wake.clear()

    # --- public actions (called by api/jobs.py) -----------------------------------------

    async def enqueue_item(
        self,
        item_id: int,
        *,
        forced_full_rate: bool = False,
        queued_at: str | None = None,
        queue_position: float | None = None,
    ) -> int:
        """Manual queue (DESIGN.md §4.7): always wins, clears suppression, resets `attempt`.
        Returns the `job.id` -- a fresh one, or (see below) an existing active one.

        `forced_full_rate` stays a plain boolean here -- the `POST /api/jobs` request body's
        own `start_now` field (`QueueItemRequest`), unrelated to and unextended by the "Start
        now" *menu* (2026-08-19, prompts/done/2026-08-19-start-now-bandwidth-fractions.md),
        which only widened the already-queued job's own `POST /api/jobs/{id}/start-now` action.
        `True` here maps onto `forced_rate_fraction=1.0` (Max) below -- byte-identical to what
        this boolean already did before that task.

        **`queued_at` override** (2026-08-19,
        prompts/2026-08-19-rescue-requeue-keeps-queue-position.md): `None` (every caller before
        this task, and every caller today except the startup rescue below) stamps today's
        now, byte-for-byte the pre-existing behavior -- same opt-in-parameter pattern as
        `core/postprocess.py.perform_remote_delete`'s `caller`. The startup rescue passes the
        *interrupted* job's own original `queued_at` so the Transfers page's queued-wait
        readout stays honest -- see `_requeue_interrupted_item` for why.

        **`queue_position` override** (2026-08-19, this task -- replaces "carrying `queued_at`
        forward" as what actually places a rescued job in line, now that position rather than
        `queued_at` is the ordering key): `None` (every caller except the startup rescue) appends
        at the back via `_insert_job`'s own default. The rescue passes `_rescue_position`'s
        result instead, so the re-queued row lands where its original `queued_at` would have put
        it -- between the same neighbours -- while never landing ahead of an explicit "Move to
        top". `rank` is untouched either way (still defaults to 0 via `_insert_job`), which is
        what makes that guarantee hold -- see `_rescue_position`'s own docstring.

        **Idempotent, not rejecting** (2026-08-13,
        prompts/2026-08-13-lftp-timestamped-temp-files.md's root cause). This used to insert a
        new `job` row and set the item `QUEUED` unconditionally, with no check for a
        `queued`/`running` job already on this item -- so a double-click, or clicking Queue on
        an item auto-queue had just picked up, spawned a **second concurrent lftp process
        against the same remote and local paths**. Observed for real: two duplicate jobs, four
        lftp processes where there should have been two, and orphaned
        `foo.mkv.lftp~<timestamp>~` temp files (lftp itself avoiding the first process's `.lftp`
        file -- a symptom, not the disease; see docs/decisions.md for what reproducing that
        against the fake seedbox actually showed). Returning the existing job's id rather than
        raising is the kinder reading of a double-click or a race between two callers wanting
        the same thing -- neither is a mistake worth surfacing as an error, and every existing
        caller (`api/jobs.py`'s `POST /api/jobs`, `retry_item` below, `core/autoqueue.py`) is
        already written to accept whatever id comes back without caring whether it's new.

        This check alone is **not** the whole fix -- two `enqueue_item` calls racing each other
        (interleaved across the `await`s between the check and the insert, which asyncio's
        cooperative scheduling genuinely allows) could still both observe "no active job" before
        either commits. `_admit` below is the second, independent layer: it refuses to let two
        processes run concurrently against the same item regardless of how many `queued` job
        rows exist for it, which is what actually closes that race -- this check just means the
        common case (a human clicking twice) never creates the extra row in the first place.

        **Deliberately does not consult the settle gate** (`core/settle.py`,
        prompts/open-issues.md #2) -- that gate's *eligibility* half only lives in
        `core/autoqueue.py`'s own query, which this method has no part of. An explicit user
        click beats a heuristic; the gate's *completion* half (`_reap_one` below) still
        applies regardless of how the job was queued, so the worst case of clicking Queue on
        a settling item is a wasted partial transfer that resumes, never a bad import or a
        bad delete.
        """
        item = await self._fetch_item(item_id)
        if item is None:
            raise ValueError(f"item {item_id} not found")

        existing_job_id = await self._active_job_id(item_id)
        if existing_job_id is not None:
            return existing_job_id

        kind = "mirror" if item["is_dir"] else "pget"
        lane = await self._lane_for(item)
        job_id = await self._insert_job(
            item_id,
            kind=kind,
            lane=lane,
            attempt=1,
            forced_rate_fraction=1.0 if forced_full_rate else None,
            queued_at=queued_at,
            queue_position=queue_position,
        )
        await self.db.execute(
            "UPDATE item SET state = 'QUEUED', auto_queue_suppressed = 0, suppressed_reason = NULL, "
            "error_class = NULL, error_detail = NULL WHERE id = ?",
            (item_id,),
        )
        await self.db.commit()
        self._backoff_until.pop(item_id, None)
        await self._publish_item_state(item_id)
        self.request_tick()
        return job_id

    async def _active_job_id(self, item_id: int) -> int | None:
        """The item's current `queued`/`running` job id, if any -- the same active-job presence
        test `stop_item`/`delete_local`'s guard already use, reused here (2026-08-13) as
        `enqueue_item`'s duplicate-prevention check. `ORDER BY id DESC LIMIT 1` matters only if
        more than one such row somehow already exists (the exact scenario this task fixes);
        the most recent one is the one actually worth returning.
        """
        cursor = await self.db.execute(
            "SELECT id FROM job WHERE item_id = ? AND state IN ('queued', 'running') "
            "ORDER BY id DESC LIMIT 1",
            (item_id,),
        )
        row = await cursor.fetchone()
        return row["id"] if row is not None else None

    async def stop_job(self, job_id: int) -> None:
        """Stop semantics (DESIGN.md §4.6) — exact, because this is the one section the phase
        3 prompt calls out by name:

        - **Running**: SIGTERM to that one PID (not SIGKILL — lets lftp flush its
          `.lftp-pget-status` sidecar so the partial stays resumable); SIGKILL only after a
          ~10s grace (`core/lftp.py.terminate`).
        - **Queued, not started**: just marked cancelled — there's no process to signal.
        - Either way: item -> `STOPPED`, partial data untouched, `auto_queue_suppressed` set.
          Phase 4's auto-queue doesn't exist yet, but the flag is set anyway (DESIGN.md §4.6:
          without it, phase 4 would resurrect a stopped job on its next pass, forever).
        """
        row = await self._fetch_job(job_id)
        if row is None:
            raise ValueError(f"job {job_id} not found")

        proc = self._running.get(job_id)
        if proc is not None:
            proc.stop_requested = True
            await lftp.terminate(proc.spawned, grace_s=10.0)
            # The reaper (next tick, or right now) sees the process has exited and finalizes
            # the row; we don't duplicate that bookkeeping here.
            await self._reap_one(proc)
            return

        if row["state"] == "queued":
            await self.db.execute(
                "UPDATE job SET state = 'cancelled', finished_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (job_id,),
            )
            await self._suppress_item(row["item_id"], reason="user_stopped", state="STOPPED")
            await self.db.commit()
            await self._publish_item_state(row["item_id"])
            self.request_tick()

    async def move_to_top(self, job_id: int) -> None:
        """`position_between(None, MIN(queue_position))` -- one `UPDATE`, no renumbering of any
        other row (2026-08-19, docs/transfers-redesign-spec.md §3.4). Behaviorally identical to
        the pre-position-model implementation from the user's point of view: this job now sorts
        before every other queued job.

        **`rank` is still bumped too**, even though it is otherwise dead for ordering as of this
        task (migration 023's own comment) -- it is the one durable "was this job ever explicitly
        boosted" marker, and `TransferQueue._rescue_position` reads it (only it, nothing else
        does) to keep the startup rescue from ever landing a re-queued job ahead of a job moved
        to top here. Not read back by this method itself; kept in lockstep purely for that
        reader.
        """
        cursor = await self.db.execute(
            "SELECT MIN(queue_position) AS min_pos, COALESCE(MAX(rank), 0) AS max_rank "
            "FROM job WHERE state = 'queued'"
        )
        row = await cursor.fetchone()
        new_position = position_between(None, row["min_pos"])
        new_rank = (row["max_rank"] or 0) + 1
        await self.db.execute(
            "UPDATE job SET queue_position = ?, rank = ? WHERE id = ? AND state = 'queued'",
            (new_position, new_rank, job_id),
        )
        await self.db.commit()
        self.request_tick()

    async def move_job(self, job_id: int, direction: str) -> None:
        """The chevron reorder actions (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage
        2, `prompts/2026-08-19-queue-reorder-chevrons.md`): `direction` is `'up'`, `'down'`, or
        `'top'`. One method, not three near-identical ones -- `api/jobs.py` exposes it behind a
        single `POST /api/jobs/{id}/move` endpoint.

        **`'top'` reuses `move_to_top` verbatim** -- no second implementation of "front of the
        line" exists. It only adds the same not-queued guard `'up'`/`'down'` need below (
        `move_to_top` itself has no such guard -- its own `UPDATE ... WHERE id = ? AND state =
        'queued'` just silently no-ops on a non-queued row, which was fine when its only caller
        was a UI button that already hid itself for a non-queued row; this method's callers need
        an explicit signal instead, per the task's own "reject cleanly" instruction).

        **`'up'`/`'down'` resolve the target's adjacent neighbour in the same lane-agnostic
        global position order the scheduler uses** (`queue_position ASC, id ASC` over `state =
        'queued'`, identical to `_admit`'s own ordering query), then set the moved job's position
        to the midpoint (`position_between`) between that neighbour and the neighbour's own
        next-outward neighbour -- a standard swap-by-midpoint reorder. Concretely, for `'up'`
        moving a job from index `i` to `i-1`: the new position lands between index `i-2`'s
        position (or unbounded, if `i-1` is already the front) and index `i-1`'s own position,
        which is exactly what makes the two rows trade places without touching either neighbour's
        stored value.

        **Edge cases, each covered by its own test in `tests/test_queue_position.py`:**

        - Already at the front (`'up'`) or back (`'down'`) of the *global* queued order: a no-op
          that returns normally, not an error. The UI disables the control here, but this method
          cannot rely on that -- a second tab, or a stale render, can still send the request.
        - Only one job queued: both directions are trivially the "already at the edge" case
          above.
        - `job_id` not found at all: `ValueError` (matches every other not-found guard in this
          class -- `api/jobs.py` maps it to 404).
        - `job_id` exists but is not `queued` (running, or already terminal -- started or
          finished between the page rendering its position and the click landing):
          `JobNotQueuedError` (`api/jobs.py` maps it to 409). Reordering a running job is
          meaningless: DESIGN.md §4.5's invariant is that its allocation is fixed at spawn and
          never re-shaped, so there is nothing here for a position change to affect.
        - **Concurrent moves.** Two `move_job` calls racing each other can each read the same
          neighbour positions before either commits, in principle landing two jobs on the exact
          same `queue_position`. This is survivable, not corrupting: every reader of
          `queue_position` (`_admit`, `list_jobs`, this method's own neighbour query) orders
          `queue_position ASC, id ASC`, so `id` is the deterministic final tiebreak regardless of
          how many rows share a position -- see `tests/test_queue_position.py`'s duplicate-
          position test. No lock is taken here for the same reason `_insert_job`/`move_to_top`
          never have: SQLite's own single-writer serialization already makes each individual
          `UPDATE` atomic, and a stale-neighbour race merely produces "close enough" ordering for
          one tick, never two rows silently colliding into an unreadable state.

        **Position exhaustion.** Repeated midpoint bisection between the same two neighbours
        halves the gap between them every time, and eventually produces a value float precision
        cannot distinguish from one of its own bounds (`position_between`'s own docstring calls
        this out as an accepted property of the fractional-key design, "occasional rebalance").
        Detected here by comparing the computed midpoint back against the two bounds that
        produced it -- if it lands on either one, the *entire* queued set is renormalized to
        1.0, 2.0, 3.0... in current order (`_renormalize_queue_positions`, below) and this method
        retries once against the now evenly-spaced positions, which cannot exhaust on the very
        next bisection.
        """
        if direction == "top":
            row = await self._fetch_job(job_id)
            if row is None:
                raise ValueError(f"job {job_id} not found")
            if row["state"] != "queued":
                raise JobNotQueuedError(f"job {job_id} is not queued (state={row['state']!r})")
            await self.move_to_top(job_id)
            return

        if direction not in ("up", "down"):
            raise ValueError(f"unknown move direction {direction!r}")

        cursor = await self.db.execute(
            "SELECT id, queue_position FROM job WHERE state = 'queued' "
            "ORDER BY queue_position ASC, id ASC"
        )
        rows = await cursor.fetchall()
        ids = [r["id"] for r in rows]
        positions = [r["queue_position"] for r in rows]

        try:
            index = ids.index(job_id)
        except ValueError:
            row = await self._fetch_job(job_id)
            if row is None:
                raise ValueError(f"job {job_id} not found") from None
            raise JobNotQueuedError(
                f"job {job_id} is not queued (state={row['state']!r})"
            ) from None

        if direction == "up":
            if index == 0:
                return  # already at the front of the global order -- no-op
            neighbour_idx, outward_idx = index - 1, index - 2
        else:
            if index == len(ids) - 1:
                return  # already at the back of the global order -- no-op
            neighbour_idx, outward_idx = index + 1, index + 2

        neighbour_pos = positions[neighbour_idx]
        outward_pos = positions[outward_idx] if 0 <= outward_idx < len(positions) else None

        if direction == "up":
            lower, upper = outward_pos, neighbour_pos
        else:
            lower, upper = neighbour_pos, outward_pos
        new_position = position_between(lower, upper)

        # Exhaustion only ever arises from a genuine two-bound midpoint -- an unbounded insert
        # (`lower`/`upper` None) always lands 1.0 away from its one real bound, never adjacent to
        # it in float terms.
        exhausted = (
            lower is not None
            and upper is not None
            and (new_position <= lower or new_position >= upper)
        )
        if exhausted:
            await self._renormalize_queue_positions()
            await self.move_job(job_id, direction)
            return

        await self.db.execute(
            "UPDATE job SET queue_position = ? WHERE id = ? AND state = 'queued'",
            (new_position, job_id),
        )
        await self.db.commit()
        self.request_tick()

    async def _renormalize_queue_positions(self) -> None:
        """Collapse whatever float precision `move_job`'s repeated midpoint bisection has
        exhausted: rewrite every `queued` job's `queue_position` as 1.0, 2.0, 3.0... in its
        current `queue_position ASC, id ASC` order. Purely a precision reset, not a reordering --
        every row keeps its relative place, so a caller that re-derives its own neighbours
        immediately afterward (`move_job`'s own retry) computes the identical move it was already
        trying to make, just against evenly-spaced inputs that cannot be adjacent in float terms.
        `rank`/`queued_at` are untouched; this only ever rewrites `queue_position`, the one column
        bisection can exhaust.
        """
        cursor = await self.db.execute(
            "SELECT id FROM job WHERE state = 'queued' ORDER BY queue_position ASC, id ASC"
        )
        rows = await cursor.fetchall()
        for i, row in enumerate(rows, start=1):
            await self.db.execute(
                "UPDATE job SET queue_position = ? WHERE id = ?", (float(i), row["id"])
            )
        await self.db.commit()

    async def start_now(self, job_id: int, *, rate_percent: int | None = None) -> bool:
        """The "Start now" action (DESIGN.md §4.5), now a menu (2026-08-19,
        prompts/done/2026-08-19-start-now-bandwidth-fractions.md): `rate_percent` is one of
        `10`/`25`/`50`/`75`/`100` (validated by `api/jobs.py.StartNowRequest`'s `Literal` before
        this is ever called), or `None` -- both `None` and `100` mean Max, byte-for-byte the
        only behavior this action had before this task.

        Only meaningful for a still-queued job; a running job's allocation is fixed at spawn
        and never re-shaped (the invariant this whole scheduler exists to protect), so this is
        a no-op (returns `False`) once a job is already running rather than silently pretending
        to retune it.

        **A fraction requires a configured site bandwidth limit.** `10 MB/s at 25%` is a real
        number; `25% of nothing` is not -- raises `NoSiteLimitConfiguredError` rather than
        silently admitting at Max instead, which the task's settled decisions rule out by name.
        "No site limit configured" reads as `max_bandwidth_bps <= 0` (docs/decisions.md has the
        call): the Settings -> Transfer field already treats 0 as the degenerate "admits
        nothing, ever" ceiling (`TransferTab.tsx`'s own reserve-clamp warning), so 0 doubling as
        "not really configured" needs no new sentinel or migration to the settings row itself.
        Max (`fraction == 1.0`) is exempt from this check -- it reuses whatever
        `max_bandwidth_bps` already is, unconditionally, exactly as the pre-fraction path did.
        """
        row = await self._fetch_job(job_id)
        if row is None or row["state"] != "queued":
            return False
        fraction = (rate_percent or 100) / 100.0
        if fraction != 1.0:
            settings = await load_transfer_settings(self.db)
            if settings.max_bandwidth_bps <= 0:
                raise NoSiteLimitConfiguredError(
                    "start-now at a fraction requires a configured site bandwidth limit "
                    "(Settings -> Transfer -> Max bandwidth) -- none is set"
                )
        await self.db.execute(
            "UPDATE job SET forced_full_rate = 1, forced_rate_fraction = ? WHERE id = ?",
            (fraction, job_id),
        )
        await self.db.commit()
        self.request_tick()
        return True

    async def stop_item(self, item_id: int) -> bool:
        """Stop-by-item (DESIGN.md §9.2's Files-page Stop action). The Files page only knows
        the *item* it's showing a row for, never the job id -- `GET /api/files` deliberately
        doesn't expose one, since an item can outlive several job attempts (§4.3's retries).
        Resolves to the item's current active (`queued`/`running`) job, if any, and applies
        the exact same stop semantics as `stop_job` (§4.6). Returns `False` -- a no-op, not
        an error -- when nothing is active for the item, the same "no-op rather than pretend
        it did something" shape `start_now` already uses for its own inapplicable case.
        """
        cursor = await self.db.execute(
            "SELECT id FROM job WHERE item_id = ? AND state IN ('queued','running') ORDER BY id DESC LIMIT 1",
            (item_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        await self.stop_job(row["id"])
        return True

    async def retry_item(self, item_id: int) -> int:
        """Manual re-queue (DESIGN.md §4.6): clears suppression, resets `attempt` — distinct
        from the *automatic* retry in `_reap_one`, which increments `attempt` instead.
        """
        return await self.enqueue_item(item_id)

    async def dismiss_job(self, job_id: int) -> None:
        """Dismiss a terminal job from the Transfers page (2026-08-13,
        prompts/done/2026-08-13-dismiss-terminal-jobs.md) — user report: they deleted files on
        the seedbox mid-transfer, the job failed `REMOTE_GONE`, and Retry was the *only*
        available action, which is exactly wrong when the remote files are genuinely gone.

        **Dismiss, not delete.** This only ever sets `job.dismissed_at`, which `list_jobs()`
        checks — the row itself is untouched, so `api/history.py` (reading the same table)
        keeps showing it. Deleting the row would erase the record of what happened, the
        opposite of what History exists for.

        **Only a terminal job** (`failed`/`cancelled`/`succeeded`) can be dismissed —
        `queued`/`running` raises `JobNotDismissableError` rather than silently no-opping, so a
        client that races a dismiss against a job starting gets a real error instead of an
        inconsistent-looking success. The Transfers page only ever offers the button on a
        terminal row, but that's a courtesy; this is the guard. `succeeded` joined this set
        2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md) alongside `list_jobs()`
        starting to surface a recently-succeeded job at all — see that method's own docstring.

        **Deliberately does not touch `item.state` or `item.auto_queue_suppressed`/
        `suppressed_reason`.** This is a display action about the *job* row, not a decision
        about the *item* — the item's suppression (§4.6) is correct and load-bearing exactly
        as it stands: a `REMOTE_GONE` item is suppressed with `suppressed_reason =
        'permanent_error'` precisely so auto-queue never picks it back up, and dismissing the
        dead job from view must not silently undo that. The obvious next "improvement" is to
        have dismiss also clear suppression — don't; that path already exists and is called
        Retry (`retry_item` above, "always wins, clears suppression, resets `attempt`"), which
        is the deliberate, visible way to say "actually, try again."

        **Nor an item whose pipeline is still in flight** (2026-08-20, docs/transfers-redesign-
        spec.md §3.2's pipeline-completion rule) — a job can exit 0 while verify/extract, the
        *arr's confirmed import, or the deferred source delete are all still outstanding, and that
        row now lives in the Active/pending box. Dismissing something still being worked on makes
        no sense, and it is also how a row would vanish from *both* boxes (`list_jobs()` excludes
        a dismissed job unconditionally). Same `JobNotDismissableError` → 409 shape as the active
        states above, and the same "the UI not offering the button is a courtesy, this is the
        guard" reasoning. `core/pipeline_flight.py` owns the test itself.
        """
        row = await self._fetch_job(job_id)
        if row is None:
            raise ValueError(f"job {job_id} not found")
        if row["state"] not in ("failed", "cancelled", "succeeded"):
            raise JobNotDismissableError(
                f"job {job_id} is {row['state']!r}; only a failed, cancelled, or succeeded "
                "job can be dismissed"
            )
        if await self.item_pipeline_busy(row["item_id"]):
            raise JobNotDismissableError(
                f"job {job_id} has finished but its item's pipeline is still in flight "
                "(post-processing, an *arr import, or a deferred source delete) — it is still "
                "shown under Active/pending and can be dismissed once that finishes, or "
                "resolved manually"
            )
        await self.db.execute("UPDATE job SET dismissed_at = ? WHERE id = ?", (_now_iso(), job_id))
        await self.db.commit()

    async def item_pipeline_busy(self, item_id: int) -> bool:
        """Whether one item is still in flight by the shared predicate
        (`core/pipeline_flight.item_pipeline_busy_subquery`) — the single-item read `dismiss_job`
        above and `api/jobs.py.resolve_item` both need, expressed against the same SQL the two
        listing queries use rather than as a fourth hand-written version of the rule.

        The *item* half only: neither caller has a job row in scope, and both already know
        whatever they need about job state separately.
        """
        cursor = await self.db.execute(
            "SELECT 1 FROM ("
            f"{item_pipeline_busy_subquery(self._postprocess_in_flight_ids())}"
            ") AS busy WHERE busy.id = ?",
            (item_id,),
        )
        return await cursor.fetchone() is not None

    async def dismiss_all_terminal(
        self,
        queue_id: int | None = None,
        job_ids: Sequence[int] | None = None,
        name_filter: str | None = None,
        outcome: str | None = None,
    ) -> int:
        """The bulk counterpart to `dismiss_job` above (2026-08-15, "Dismiss all" at the top of
        the Transfers page) -- one `UPDATE`, not a per-job loop, per the task's own preference
        for a bulk endpoint over a client-side `Promise.allSettled` fan-out.

        Same guard as `dismiss_job`, just applied to every row at once: only `failed`/
        `cancelled`/`succeeded` rows with `dismissed_at IS NULL` are touched -- a `queued`/
        `running` job is never matched by this `WHERE`, so there is no active-job case to reject
        the way `dismiss_job` raises `JobNotDismissableError` for one. Unlike that method, this
        doesn't restrict itself to each item's *most recent* job (`list_jobs()`'s `MAX(id)`
        superseding rule) -- an older, already-superseded terminal row is never shown on
        Transfers regardless of its `dismissed_at`, so dismissing it too is harmless and keeps
        this one plain `UPDATE ... WHERE` rather than a second copy of that subquery. This one
        rule has exactly one exception: `name_filter` below, which re-adds the restriction on
        purpose, for a different reason.

        `queue_id` (2026-08-17, the group-header "Dismiss Queue" control,
        `prompts/2026-08-17-transfers-dismiss-per-queue.md`) restricts the same `UPDATE` to one
        queue's own terminal jobs, `None` (the default, and every call before this task) meaning
        every queue -- byte-for-byte the original behavior. `job` has no `queue_id` column of its
        own (only `item_id`, migration 001), so the restriction is a subquery over `item`, the
        same join `list_jobs()` already does via SQL rather than a second Python-side filter. A
        `queue_id` naming no queue that exists simply matches zero rows -- the same "nothing to
        do, not an error" answer an empty/all-dismissed queue already gives; this never 404s.

        `job_ids` (2026-08-19, the Transfers page's name filter and its own "Dismiss list"
        button, `prompts/2026-08-19-transfers-name-filter.md`) restricts the same `UPDATE` to an
        explicit set of job ids -- `None` (the default) means no id restriction at all, exactly
        today's pre-existing behavior. `job_ids` is a *narrowing* of the same terminal-state
        `WHERE`, never an override of it: an id naming a `queued`/`running` job simply matches
        zero rows for that id, the identical "the client's list can only ask for a subset of
        what the guard already allows" contract `DismissAllRequest`'s own docstring states.

        **An empty `job_ids` list (`[]`, as opposed to `None`) must dismiss nothing and return
        `0` -- it must never degrade into "no filter, so dismiss everything."** This is the
        dangerous edge of this whole change: `[]` is a real, deliberate "the current filter
        matches zero dismissable rows" input, not "no restriction was given." Handled with an
        early return before the `UPDATE` is even built, both because it is the correct answer
        and because `... AND id IN ()` is not valid SQL to begin with.

        `name_filter` (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b)
        supersedes `job_ids` for "Dismiss list" once the Complete box is server-paginated: a
        filter can match more rows than are loaded on any one page, so an explicit id list can
        only ever express "dismiss this page", not "dismiss everything the filter matches" --
        `DismissAllRequest.name_filter`'s own docstring has the full reasoning. Case-insensitive
        substring match against `item.rel_path` (`_like_escape`, above -- the same literal-
        substring, no-glob contract `lib/transferPanel.ts.filterTransferJobs` states for its
        client-side counterpart), an empty string matching every row (same as an empty
        client-side filter). Unlike every other scope on this method, this one **does** add the
        `list_jobs()`-style `MAX(id)`-per-item restriction back in -- deliberately, so this
        `UPDATE`'s own `WHERE` is built from *exactly* the same predicate
        `TransferQueue.list_complete_jobs` filters its listing on. That is what lets
        `CompleteJobsResponse.total` for a given filter always equal the count this dismisses:
        without it, "Dismiss list" could silently sweep up an already-superseded terminal row
        the user was never shown (a stale `failed` attempt on an item that has since been
        retried and is queued/running again) -- harmless in the `job_ids`/`queue_id` cases above
        (their inputs are already the exact set the frontend loaded, so a superseded row was
        never a candidate to begin with), but a real drift here since `name_filter` is
        recomputed server-side against the *whole* table, not a client-supplied id list.

        `outcome` (2026-08-20, follow-up to phase 1 stage 4b,
        `prompts/2026-08-20-transfers-dismiss-menu-and-counts.md`) restricts the same `UPDATE`
        to one terminal state -- the Complete box's own "Dismiss" menu ("all, downloaded, failed
        (or whatever the completed status are)", the user's own words). `None` (the default)
        means no state restriction beyond the base `state IN (...)` guard already applies --
        unchanged behavior for every caller that predates this task.

        **`outcome` composes with `name_filter` -- decided by the user, 2026-08-20** (recorded
        in `docs/decisions.md`): both narrow the same set, so a request naming both dismisses
        their intersection (`AND`ed into the same `WHERE`, not two separate queries reconciled
        after the fact). `DismissAllRequest`'s own validator is what keeps `job_ids`/`queue_id`
        out of this composition -- this method trusts its caller to have already enforced that,
        the same way it trusts the `job_ids == []` guard above to have already been checked by
        the time any of these branches run.

        `outcome` deliberately does **not** add the `name_filter` branch's `MAX(id)`-per-item
        restriction on its own (only `name_filter` does, whether or not `outcome` is also given)
        -- an `outcome`-only dismiss is a narrowing of this method's own pre-existing "dismiss
        every terminal row, superseded or not" behavior (see the module docstring above for why
        that's harmless with no filter at all), not a promise to match `list_complete_jobs`'s
        listing predicate the way `name_filter` alone is. Once `name_filter` is also given, the
        combined `WHERE` picks up the restriction the normal way, which is what keeps
        `list_complete_jobs(name_filter=..., outcome=...)`'s `total` and this method's
        `(name_filter=..., outcome=...)` dismissed count in agreement -- the same property
        `name_filter` alone already guarantees, now extended to the composed case (see
        `docs/decisions.md` and the paired test,
        `test_dismiss_all_terminal_name_filter_count_matches_list_complete_jobs_total`).

        **An item whose pipeline is still in flight is never dismissed** (2026-08-20,
        docs/transfers-redesign-spec.md §3.2's pipeline-completion rule) -- dismissing something
        still being worked on makes no sense, and this `UPDATE`'s count has to keep matching
        `list_complete_jobs`'s `total`, which now excludes those rows too. Same one expression
        (`core/pipeline_flight.item_pipeline_busy_subquery`), reached through a subquery because
        an `UPDATE job` has no `item`/`arr_instance` join of its own. Only the *item* half is
        needed here: this `WHERE` already restricts to terminal jobs, for which the job half is
        false by construction. This applies to **every** scope, `job_ids` included -- an explicit
        id naming an in-flight row simply matches nothing, the same "a narrowing can only ask for
        a subset of what the guard already allows" contract every other scope has.

        Returns the actual row count affected (`cursor.rowcount`), the same "report the real
        number" convention `api/history.py`'s clear-history endpoints already use.
        """
        if job_ids is not None and len(job_ids) == 0:
            return 0

        where = [
            "state IN ('failed','cancelled','succeeded')",
            "dismissed_at IS NULL",
            f"item_id NOT IN ({item_pipeline_busy_subquery(self._postprocess_in_flight_ids())})",
        ]
        params: list[Any] = [_now_iso()]
        if queue_id is not None:
            where.append("item_id IN (SELECT id FROM item WHERE queue_id = ?)")
            params.append(queue_id)
        if job_ids is not None:
            placeholders = ",".join("?" for _ in job_ids)
            where.append(f"id IN ({placeholders})")
            params.extend(job_ids)
        if outcome is not None:
            where.append("state = ?")
            params.append(outcome)
        if name_filter is not None:
            where.append("id = (SELECT MAX(j2.id) FROM job j2 WHERE j2.item_id = job.item_id)")
            where.append(
                "item_id IN (SELECT id FROM item WHERE LOWER(rel_path) LIKE ? ESCAPE '\\')"
            )
            params.append(f"%{_like_escape(name_filter.lower())}%")

        cursor = await self.db.execute(
            f"UPDATE job SET dismissed_at = ? WHERE {' AND '.join(where)}", params
        )
        await self.db.commit()
        return cursor.rowcount

    async def _item_is_settled(self, queue_id: int, rel_path: str) -> bool:
        """The completion half of the settle gate (prompts/open-issues.md #2,
        `core/settle.py`), consulted from `_reap_one` on job success. Returns `True`
        unconditionally when `settle.SettleSettings.enabled` is off -- this project's "every
        new capability defaults off" rule, and this is the completion half, so the setting
        being off must restore exactly the pre-gate behaviour rather than a softened version
        of it.

        Reflects the fingerprint as of the most recent scan (`core/engine.py._persist` is the
        only writer of `item_settle`) -- a job can finish between two scan passes, so this is
        the best information available, not a live recomputation.
        """
        settings = await settle.load_settle_settings(self.db)
        if not settings.enabled:
            return True
        return await settle.is_settled_in_db(self.db, queue_id, rel_path)

    # --- one scheduling tick -------------------------------------------------------------

    async def tick(self) -> None:
        await self._reap_finished()
        await self._sample_and_publish_progress()
        await self._sample_metrics()
        await self._admit()

    async def _reap_finished(self) -> None:
        for proc in [p for p in self._running.values() if p.wait_task.done()]:
            await self._reap_one(proc)

    async def _reap_one(self, proc: _RunningProcess) -> None:
        if proc.job_id not in self._running:
            return  # already reaped (stop_job() and the tick loop can race on the same proc)
        del self._running[proc.job_id]
        self.progress.drop(proc.job_id)
        self.metrics.drop(proc.job_id)
        self._last_speeds.pop(proc.job_id, None)
        self._last_progress.pop(proc.job_id, None)
        self._prev_child_sizes.pop(proc.job_id, None)
        self._prev_child_times.pop(proc.job_id, None)
        self._child_speed.pop(proc.job_id, None)

        if not proc.wait_task.done():
            # stop_job() reaps immediately after terminate() returns, before the task's own
            # `await proc.wait()` inside wait_and_capture has necessarily unblocked — awaiting
            # it here is instant since the process has already exited.
            await proc.wait_task
        exit_code, tail = proc.wait_task.result()
        finished_at = _now_iso()

        stopped = proc.stop_requested

        if stopped:
            await self.db.execute(
                "UPDATE job SET state = 'cancelled', pid = NULL, exit_code = ?, output_tail = ?, "
                "finished_at = ? WHERE id = ?",
                (exit_code, tail[-lftp.OUTPUT_TAIL_BYTES :], finished_at, proc.job_id),
            )
            await self._suppress_item(proc.item_id, reason="user_stopped", state="STOPPED")
            await self.db.commit()
            await self._publish_item_state(proc.item_id)
            proc.spawned.cleanup()
            return

        if exit_code == 0:
            # bytes_done otherwise retains whatever the progress sampler last measured (as of
            # 2026-08-16, up to `PROGRESS_SAMPLE_TICKS`-1 ticks stale, not "the last tick"),
            # which can trail the true final size by up to a sample window (found comparing
            # /api/stats's 24h-transferred total against a real completed job's actual file
            # size). `cmd:fail-exit true` guarantees exit 0 only on a complete transfer
            # (§4.3), so the true byte count here is simply the job's own `bytes_total`
            # (already known from the remote scan at admission time), not a re-measurement.
            #
            # **This is `bytes_done`, not a completion verdict.** DESIGN.md §4.3's "no
            # inference" rule is about *how we learn the outcome* -- trust lftp's own exit
            # code rather than parse `jobs -v` output (§1.2) or guess from partial progress
            # samples. It was never license to conclude exit 0 proves every byte arrived: a
            # live incident (2026-08-13/14, queue `ar-tv`, job 43,
            # prompts/2026-08-14-exit-zero-is-not-completion.md) had `cmd:fail-exit true`
            # exit 0 with one file 500 MB short, still sitting on disk as a `.lftp` temp file
            # -- lftp reported "no error," which is all exit 0 ever actually promises. Whether
            # the transfer is *complete* is answered below, from the filesystem (§1.3's own
            # principle: progress and completion are derived from what's on disk, not
            # inferred from the process). See docs/decisions.md for the proposed §4.3 wording.
            logger.info(
                "job %s succeeded: %s (%s bytes)", proc.job_id, proc.rel_path, proc.bytes_total
            )
            # `output_tail` is retained on every success now, not just a failure -- the one
            # job whose success was in doubt (job 43 above) had its own lftp output captured
            # and then unconditionally thrown away by this same UPDATE, so the one thing that
            # would have explained the gap at a glance was gone before anyone knew to look.
            # 4 KB per job (`lftp.OUTPUT_TAIL_BYTES`) against a `job` table `list_jobs()`
            # already bounds by construction (its own docstring) and History already
            # paginates -- retaining it unconditionally is simpler than a second write path
            # that only fires for the incomplete branch below, and correct for exactly the
            # same reason that branch needs it.
            await self.db.execute(
                "UPDATE job SET state = 'succeeded', pid = NULL, exit_code = 0, output_tail = ?, "
                "bytes_done = COALESCE((SELECT remote_size FROM item WHERE item.id = job.item_id), bytes_done), "
                "finished_at = ? WHERE id = ?",
                (tail[-lftp.OUTPUT_TAIL_BYTES :], finished_at, proc.job_id),
            )

            # 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md, defect 3's "real
            # fix"): a final, accurate, un-throttled reading of every child row before anything
            # downstream (post-processing's verify/extract/move/delete) can touch this job's
            # files -- see `_flush_child_progress_final`'s own docstring for the bug this
            # closes. A no-op for a `pget` job (single file, no children) and cheap for a
            # `mirror` job either way (bounded by the release's own file count, the same walk
            # `_publish_child_progress` already does every throttled tick).
            await self._flush_child_progress_final(proc)

            # The settle gate's completion half (prompts/open-issues.md #2, `core/settle.py`),
            # for a top-level item only -- the same eligibility shape core/autoqueue.py uses,
            # since a queued job is always for a top-level item. `_item_is_settled` reads
            # `settle.SettleSettings` itself and returns `True` unconditionally when the gate
            # is off, so this whole branch is a no-op change of behavior for every install that
            # hasn't opted in.
            top_level = "/" not in proc.rel_path
            settled = (not top_level) or await self._item_is_settled(proc.queue_id, proc.rel_path)

            # The completeness check (this task, 2026-08-14): a filesystem-only read, exactly
            # what §1.3 already mandates as the source of truth for progress and completion --
            # never `jobs -v` (§1.2), never trusting the exit code alone. Distinct question
            # from `settled` above: that one asks "has the remote stopped changing?"; this one
            # asks "did we actually get it all?" Both must hold before DOWNLOADED.
            complete, local_bytes, evidence = await self._completeness_on_disk(proc)

            if settled and not complete:
                # Exit 0, but the disk disagrees: either a leftover lftp temp file/sidecar
                # (`local_scan.TEMP_FILE_RE`/`find_orphan_sidecars`) or local bytes short of
                # the remote total known at admission time. Treated as an ordinary incomplete
                # transfer, not a success -- `PARTIAL` is re-queueable (§3.2), and auto-queue's
                # existing eligibility (`ELIGIBLE_STATES`) picks it back up exactly the way
                # job 44 did for real, resuming from what's already on disk (`-c`). Never
                # DOWNLOADED, never post-processing -- see the `if settled and complete`
                # trigger guard below.
                await self.db.execute(
                    "UPDATE item SET state = 'PARTIAL', substate = NULL, local_size = ?, "
                    "auto_queue_suppressed = 0, suppressed_reason = NULL, error_class = NULL, error_detail = NULL "
                    "WHERE id = ?",
                    (local_bytes, proc.item_id),
                )
                await audit.record_event(
                    self.db,
                    level="warning",
                    item_id=proc.item_id,
                    job_id=proc.job_id,
                    kind="incomplete_on_exit_zero",
                    message=(
                        f"job {proc.job_id} for {proc.rel_path!r} exited 0 but the filesystem "
                        f"shows only {local_bytes} of {proc.bytes_total} expected bytes"
                        + (
                            f"; leftover on disk: {', '.join(evidence)}"
                            if evidence
                            else " (no leftover temp file/sidecar found -- short on bytes alone)"
                        )
                        + " -- held at PARTIAL instead of DOWNLOADED; post-processing was not"
                        " triggered (prompts/2026-08-14-exit-zero-is-not-completion.md)"
                    ),
                )
            elif settled:
                # No inference beyond what lftp actually told us (DESIGN.md §4.3, as amended):
                # `cmd:fail-exit true` plus a clean filesystem read together mean the whole
                # transfer succeeded, so the item is DOWNLOADED now, not "probably, pending the
                # next scan." The engine's own reconcile pass will confirm sizes on its next
                # cycle; this is what makes the UI update immediately instead of waiting for it.
                #
                # `pending_download_prefix` is deliberately left untouched here (2026-08-14,
                # prompts/done/2026-08-14-rename-after-postprocessing-not-before.md, reversing
                # part of the same day's earlier "folder prefix during transfer" entry in
                # docs/decisions.md). That entry renamed the directory back to its real name
                # right here, before DOWNLOADED, on the reasoning that the transfer -- the thing
                # the setting's name says it protects -- was over by this point. It still is, but
                # "over" isn't "safe to publish": verify/extract haven't run yet, and a `.sfv`
                # mismatch or a bad archive can only be discovered by post-processing, which
                # hasn't started. Renaming here published an unverified release under its real
                # name for however long verify+extract take -- measured at 7.7s for 1.7 GB, so a
                # 21 GB release sat exposed for roughly a minute and a half, the exact window an
                # importer needs to grab something that then turns out corrupt. The rename now
                # happens in `core/postprocess.py`, as the pipeline's last step on a release nothing
                # along the way flagged bad -- see that module's `_finalize_download_prefix`.
                await self.db.execute(
                    "UPDATE item SET state = 'DOWNLOADED', downloaded_at = ?, substate = NULL, "
                    "auto_queue_suppressed = 0, suppressed_reason = NULL, error_class = NULL, error_detail = NULL "
                    "WHERE id = ?",
                    (finished_at, proc.item_id),
                )
            else:
                # This job genuinely succeeded -- everything visible on the remote at
                # admission time is now on disk -- but the remote subtree has not held still
                # for `settle.REQUIRED_SETTLE_SCANS` consecutive scans, so this may not be the
                # *whole* item (the directory-upload race the settle gate exists for: a
                # release mid-upload, mirrored, exits 0 because every file it was asked for
                # arrived). Held at REMOTE_ONLY/settling rather than DOWNLOADED so
                # post-processing never runs on a partial release -- auto-queue or a manual
                # Queue click can pick this item back up once it does settle, and lftp resumes
                # rather than re-fetching what's already there. "A wasted partial transfer
                # that resumes," never a bad import or a bad delete.
                await self.db.execute(
                    "UPDATE item SET state = 'REMOTE_ONLY', substate = 'settling', "
                    "auto_queue_suppressed = 0, suppressed_reason = NULL, error_class = NULL, error_detail = NULL "
                    "WHERE id = ?",
                    (proc.item_id,),
                )
                await audit.record_event(
                    self.db,
                    level="info",
                    item_id=proc.item_id,
                    kind="settle_gate_held",
                    message=(
                        f"job {proc.job_id} for {proc.rel_path!r} succeeded but the item has "
                        "not settled (prompts/open-issues.md #2) -- held at REMOTE_ONLY/"
                        "settling instead of DOWNLOADED; post-processing was not triggered"
                    ),
                )

            await self.db.commit()
            await self._publish_item_state(proc.item_id)
            proc.spawned.cleanup()
            # Phase 5 (DESIGN.md §6): "triggered on transition to DOWNLOADED." Only when this
            # job's item actually reached DOWNLOADED above -- an item held back by either the
            # settle branch or the incomplete-on-exit-zero branch above must not trigger
            # post-processing (that's the whole point of both gates); it re-enters this same
            # path via a fresh job once it settles/completes.
            if settled and complete and self.postprocess is not None and top_level:
                self.postprocess.trigger(proc.item_id)
            return

        error_class = lftp.classify_output(tail)
        logger.warning(
            "job %s failed: %s (exit %s, %s)", proc.job_id, proc.rel_path, exit_code, error_class
        )
        await self.db.execute(
            "UPDATE job SET state = 'failed', pid = NULL, exit_code = ?, error_class = ?, "
            "output_tail = ?, finished_at = ? WHERE id = ?",
            (exit_code, error_class, tail[-lftp.OUTPUT_TAIL_BYTES :], finished_at, proc.job_id),
        )

        transfer_settings = await load_transfer_settings(self.db)
        max_attempts = transfer_settings.max_attempts
        current_attempt = await self._current_attempt(proc.job_id)
        can_retry = error_class in lftp.TRANSIENT_ERROR_CLASSES and current_attempt < max_attempts
        if can_retry:
            # `transfer_settings.retry_backoff_base_s`, not `DEFAULT_RETRY_BACKOFF_BASE_S`
            # (2026-08-14): the setting round-tripped through `TransferSettings` and Settings ->
            # Transfer's own form since phase 3a, but this line read the module constant instead,
            # so changing the field did nothing at all -- a control that looks live and is not.
            # Found during the FieldHelp sweep while verifying that field's help text against the
            # code, which is exactly the check that sweep exists to force
            # (prompts/done/2026-08-13-field-help-sweep.md). The constant remains the dataclass
            # default, so an install that never touched the setting sees no behavior change.
            backoff = compute_retry_backoff(transfer_settings.retry_backoff_base_s, current_attempt)
            self._backoff_until[proc.item_id] = time.monotonic() + backoff
            await self._insert_job(
                proc.item_id, kind=proc.kind, lane=proc.lane, attempt=current_attempt + 1
            )
            await self.db.execute(
                "UPDATE item SET state = 'QUEUED', error_class = ?, error_detail = ? WHERE id = ?",
                (error_class, tail[-2000:], proc.item_id),
            )
            await self.db.commit()
        else:
            reason = (
                "retries_exhausted"
                if error_class not in PERMANENT_ERROR_CLASSES
                else "permanent_error"
            )
            await self.db.execute(
                "UPDATE item SET error_class = ?, error_detail = ? WHERE id = ?",
                (error_class, tail[-2000:], proc.item_id),
            )
            await self._suppress_item(proc.item_id, reason=reason, state="FAILED")
            await self.db.commit()

        await self._publish_item_state(proc.item_id)
        proc.spawned.cleanup()

    async def _completeness_on_disk(self, proc: _RunningProcess) -> tuple[bool, int, list[str]]:
        """Whether `proc`'s target is actually, fully on disk -- the filesystem-only check
        `_reap_one` runs on exit 0, before an item can reach `DOWNLOADED` (2026-08-14,
        prompts/2026-08-14-exit-zero-is-not-completion.md). `cmd:fail-exit true`'s exit 0
        means lftp reported no error; it does not mean every byte arrived (DESIGN.md §4.3, as
        amended -- see docs/decisions.md for the proposed wording), so this reads the disk the
        same way `core/local_scan.py`/§4.4 already do for live progress, rather than trusting
        the exit code alone. This is exactly the "filesystem check" DESIGN.md §1.3 already
        mandates as the source of truth -- never `jobs -v` (§1.2).

        Two independent things count as incomplete, matching the live incident this closes
        (job 43: `S.W.A.T.S06E22….mkv.lftp` + its `.lftp-pget-status` sidecar left on disk,
        the item still marked `DOWNLOADED` anyway):

        - **Any lftp temp file remains anywhere under the item.** Both `.lftp` and lftp's
          `.lftp~<timestamp>~` fallback (`local_scan.TEMP_FILE_RE`/`find_temp_files`,
          `find_temp_variants`) -- a temp file's reported size can lie in a way a real,
          already-renamed file's cannot (`LocalEntry.is_temp`'s own docstring), so its mere
          presence is disqualifying regardless of what the byte totals below say.
        - **An orphaned `.lftp-pget-status` sidecar** (`local_scan.find_orphan_sidecars`) --
          one whose carrier file was renamed away or removed without the sidecar being
          cleaned up alongside it, invisible to `scan_local`'s own output but real evidence
          lftp's bookkeeping and the disk disagree.
        - **Local bytes short of the *relevant* remote total** (`_relevant_remote_total`
          below) -- measured with the same sidecar-corrected accounting `scan_local`/
          `effective_file_size` already use, never reimplemented here.

        **Why not just `proc.bytes_total` (`item.remote_size` at spawn) for the byte
        comparison.** That column is deliberately a raw rollup -- "every remote byte under a
        directory, irrespective of the completeness predicate" (`core/reconcile.py`'s own
        docstring) -- so it includes a `file_exclude`d file's bytes even though lftp was
        handed `--exclude-glob` for exactly that pattern and never fetched it (DESIGN.md
        §3.2 rule 1's own exclusion clause, §4.7). Comparing local bytes against the raw total
        would hold a perfectly clean, correctly-excluding transfer at `PARTIAL` forever --
        the exact infinite-loop failure mode §6's archive-cleanup accounting was written to
        avoid, reintroduced here if this check used the wrong total. `_relevant_remote_total`
        is this method's async part for exactly that reason: an `EXCLUDED` descendant's own
        bytes are subtracted out first, reusing the state a real scan already assigned it
        rather than re-deriving pattern matches here.

        Returns `(complete, local_bytes, evidence)`. `evidence` lists every leftover temp
        file/sidecar path found -- exactly what the `incomplete_on_exit_zero` audit event
        needs to explain the gap at a glance, the row this incident never got.

        `proc.local_root` is the file's own path for a `pget` job, or the item's directory for
        a `mirror` job (`_RunningProcess.local_root`'s own docstring) -- the same distinction
        `_flush_child_progress_final` and `core/progress.py` already key off of.
        """
        root = Path(proc.local_root)
        evidence: list[str] = []
        if proc.kind == "pget":
            evidence += [str(v) for v in local_scan.find_temp_variants(root.parent, root.name)]
            sidecar = root.parent / f"{root.name}{local_scan.PGET_STATUS_SUFFIX}"
            if root.exists() and sidecar.exists():
                evidence.append(str(sidecar))
            local_bytes = local_scan.effective_file_size(root)
            # A `pget` job's target is never itself `EXCLUDED` (an excluded item is never
            # queued in the first place, manually or by auto-queue), so the raw admission-time
            # total is already the relevant one -- no child rows to reconcile against.
            remote_total = proc.bytes_total or 0
        else:
            entries = local_scan.scan_local(root)
            local_bytes = sum(e.size for e in entries.values() if not e.is_dir)
            # `find_temp_files`, not `entries`' own `is_temp` flag: `scan_local` reports a
            # still-temp file under its *final*, stripped name (so it can be matched against
            # its remote counterpart), which is the wrong name for an audit message naming
            # exactly what's still on disk -- `find_temp_files` returns the real path.
            evidence += [str(p) for p in local_scan.find_temp_files(root)]
            evidence += [str(p) for p in local_scan.find_orphan_sidecars(root)]
            remote_total = await self._relevant_remote_total(proc)

        complete = not evidence and local_bytes >= remote_total
        return complete, local_bytes, evidence

    async def _relevant_remote_total(self, proc: _RunningProcess) -> int:
        """The byte total that actually counts toward `proc`'s completeness -- `proc.bytes_total`
        (the raw `item.remote_size` rollup at spawn) minus every tracked descendant file
        currently `EXCLUDED` (DESIGN.md §3.2 rule 1's own exclusion clause), the identical
        accounting a real scan's `core/reconcile.py` rule 1 already uses to decide
        DOWNLOADED-vs-PARTIAL. Reuses the state a scan already assigned rather than
        re-deriving pattern matches here -- the same "reuse the existing completeness seam"
        choice §6's archive-cleanup accounting made for the identical reason.

        Falls back to the raw `proc.bytes_total` when no descendant file rows are tracked yet
        for this item -- the best information available before this item's first real scan
        populated them (every production job is preceded by at least one, since auto-queue
        eligibility and the settle/mount gates all depend on a prior scan's own output; a
        fallback exists only for the theoretical gap, not the common case).
        """
        prefix = f"{proc.rel_path}/"
        # SQLite LIKE's `%`/`_` are wildcards -- escaped so a literal `%`/`_` in a real
        # release name can't widen or narrow the match.
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cursor = await self.db.execute(
            "SELECT state, remote_size FROM item WHERE queue_id = ? AND is_dir = 0 "
            "AND rel_path LIKE ? ESCAPE '\\'",
            (proc.queue_id, f"{escaped}%"),
        )
        rows = await cursor.fetchall()
        if not rows:
            return proc.bytes_total or 0
        return sum(row["remote_size"] or 0 for row in rows if row["state"] != "EXCLUDED")

    async def _flush_child_progress_final(self, proc: _RunningProcess) -> None:
        """A final, accurate, un-throttled reading of a `mirror` job's per-child rows, run once
        at reap time on job success -- the "real fix" half of
        `prompts/2026-08-13-delete-state-truthfulness.md`'s defect 3 (`core/mount_sentinel.py.
        resolve_vanished` is the safety net for whenever this doesn't run, or a stale reading
        forms anyway).

        **The bug.** `_publish_child_progress` persists a child's `local_size`/`state` only
        every `PROGRESS_SAMPLE_TICKS`-th tick, and only for files whose size changed since the
        *previous* sampled tick -- deliberately, so a 50-file release doesn't mean 50 writes a
        second (that module's own docstring). But a job can finish *between* two sampled
        ticks, and once it does, nothing ever samples that job's children again --
        `_reap_one` only ever wrote the *parent* item's row. So the last thing a small file's
        row says can be a mid-transfer `PARTIAL`, true for a fraction of a second, frozen there
        indefinitely. On a `copy`-mode queue a later engine scan corrects it (the file is still
        sitting in both trees). On a `move` queue, post-processing can relocate the whole
        release out of both trees -- remote deleted after verify, local moved to
        `staging_path` -- before any scan gets the chance, and the stale `PARTIAL` becomes
        permanent (`resolve_vanished`'s own docstring has the full mechanics).

        **The fix.** The job has just exited 0 -- `cmd:fail-exit true` guarantees every file it
        was asked to transfer is now on disk under its final name, no more `.lftp` temp
        suffixes in flight (DESIGN.md §4.3) -- so one more walk of `proc.local_root` at this
        exact moment, right here, gives the true final state, and this runs it unthrottled and
        unconditionally rather than waiting for a tick that might not come. Same walk
        `core/progress.py._bytes_done_for` already does for a live `mirror` job
        (`local_scan.scan_local`), same per-child state rule `_publish_child_progress` already
        uses (`local >= remote_size -> DOWNLOADED, else PARTIAL`, left alone when `remote_size`
        is `NULL`) -- not reimplemented, just run one more time, synchronously, not wrapped in
        `asyncio.to_thread`: bounded by one release's own file count (the same bound that
        module's docstring already argues from), and `core/engine.py.scan_queue` already calls
        `local_scan.scan_local` this same way for its own (much larger) tree walk.

        A no-op for a `pget` job (single file, `item.id == proc.item_id` already got the
        parent's own final write above; there are no children to flush) and for any job whose
        `local_root` no longer exists by the time this runs (`scan_local` returns `{}` for a
        missing root -- nothing to flush, nothing to crash on).
        """
        if proc.kind != "mirror":
            return
        entries = local_scan.scan_local(proc.local_root)
        full_paths: list[str] = []
        for rel, entry in entries.items():
            if entry.is_dir:
                continue
            full_path = f"{proc.rel_path}/{rel}"
            await self.db.execute(
                "UPDATE item SET local_size = ?, state = CASE "
                "WHEN remote_size IS NULL THEN state "
                "WHEN ? >= remote_size THEN 'DOWNLOADED' "
                "ELSE 'PARTIAL' END "
                "WHERE queue_id = ? AND rel_path = ?",
                (entry.size, entry.size, proc.queue_id, full_path),
            )
            full_paths.append(full_path)
        if not full_paths:
            return
        await self.db.commit()
        placeholders = ",".join("?" * len(full_paths))
        cursor = await self.db.execute(
            # ITEM_VIEW_COLUMNS is a module constant and `placeholders` is just `?` repeated
            # once per path; the only bound values are `queue_id`/`full_paths`.
            f"SELECT {ITEM_VIEW_COLUMNS} FROM item WHERE queue_id = ? AND rel_path IN ({placeholders})",  # noqa: S608
            (proc.queue_id, *full_paths),
        )
        nodes = [item_view(row) for row in await cursor.fetchall()]
        if nodes:
            self.events.publish({"type": "item_delta", "queue_id": proc.queue_id, "nodes": nodes})

    async def _sample_and_publish_progress(self) -> None:
        """Called every `tick_s` (~1s) from `tick()`, but the whole body below only actually
        runs every `PROGRESS_SAMPLE_TICKS`-th call (~5s) -- 2026-08-16, unifying job- and
        child-level progress onto one cadence (`PROGRESS_SAMPLE_TICKS`'s own comment has the
        why: two independently-throttled EMAs never agreed on a one-file directory's speed).
        Reap/admit/stop stay on the 1s `tick()` loop untouched -- a Stop click still takes
        effect in ~1s regardless of where in this method's 5-tick window it lands, because
        stopping signals the process directly (`stop_job`) rather than waiting for a sample.

        On a sampled tick, three messages come out of here, all bounded by `len(self._running)`
        — the *active* set — never by the size of a queue's tree, however many thousand files
        it holds:

        - `progress` (job-centric, Transfers page): bytes/speed/ETA per running job, from
          `core/progress.py.ProgressSampler.sample`. Unchanged from phase 3a in shape; the
          *cadence* it's called at is what moved.
        - `item_delta` (item-centric, Files page): the same sampled tick's local-size/state for
          the items those jobs belong to, batched per queue, so a downloading item's row
          updates live instead of waiting for the next full engine scan (up to
          `scan_interval_s`, default 30s) — the gap that made "stop it and see it go STOPPED
          without a page refresh" impossible before this phase.
        - `child_progress` (2026-08-14, "per-file speed inside a mirror"): a third, item-keyed
          message, `{item_id, speed_bps}` per changed child of a mirroring directory —
          deliberately not folded into either message above. It does not belong in `progress`:
          that message is job-centric and a child has no `job_id` of its own, so a pseudo-entry
          there would collide in the frontend's `progressByJobId` map and put a fictional row
          on the Transfers page. It does not belong in `item_delta` either: that message
          carries `item_view()` projections of persisted `item` columns only (DESIGN.md
          §2/§9's invariant), and a live rate is a sample, never a persisted one — see
          `_publish_child_progress`.

        The parent item's row is *persisted then read back* through `core/itemview.py` rather
        than hand-built, matching `core/engine.py.scan_queue`'s invariant (DESIGN.md §2/§9):
        nothing goes on the wire that wasn't read back out of `item`. This used to hand-build
        the dict with `"state": "DOWNLOADING"` hardcoded instead -- true in practice, because
        `_spawn_decision` is the only writer of a running job's item state and scans never
        overwrite a job-lifecycle state, but asserted rather than read, which is exactly the
        shape that let a `REMOVED_LOCAL` item publish as `REMOTE_ONLY` before this file's WS
        paths were unified onto `item_view` (`docs/decisions.md`, 2026-08-12). The extra
        `SELECT` is one indexed lookup per running job -- bounded by `len(self._running)`, a
        handful of concurrent top-level transfers, never queue size.

        Child (per-file) progress inside a mirroring directory is published unconditionally
        whenever this method's body runs at all -- see `_publish_child_progress` -- since the
        gate above already puts job and child sampling on the same tick.
        """
        if not self._running:
            return
        # The shared gate (see `PROGRESS_SAMPLE_TICKS`'s comment): only every Nth call to this
        # method -- itself invoked every `tick_s` by `tick()` -- does any sampling, DB writes,
        # or publishing at all. Only counts while something is running, matching the pre-2026-
        # 08-16 child-only throttle's own behavior (a quiet queue never advances the counter).
        self._progress_tick_count += 1
        if self._progress_tick_count % PROGRESS_SAMPLE_TICKS != 0:
            return
        active = [
            ActiveJob(
                job_id=p.job_id, kind=p.kind, local_root=p.local_root, bytes_total=p.bytes_total
            )
            for p in self._running.values()
        ]
        results = self.progress.sample(active)
        for job_id, prog in results.items():
            await self.db.execute(
                "UPDATE job SET bytes_done = ? WHERE id = ?", (prog.bytes_done, job_id)
            )

        for p in self._running.values():
            prog = results.get(p.job_id)
            if prog is None:
                continue
            await self.db.execute(
                "UPDATE item SET local_size = ? WHERE id = ?", (prog.bytes_done, p.item_id)
            )
        await self.db.commit()
        self._last_speeds = {job_id: prog.speed_bps for job_id, prog in results.items()}
        self._last_progress = results

        parent_ids = [p.item_id for p in self._running.values() if p.job_id in results]
        parent_views: dict[int, ItemView] = {}
        if parent_ids:
            placeholders = ",".join("?" * len(parent_ids))
            cursor = await self.db.execute(
                # ITEM_VIEW_COLUMNS is a module constant and `placeholders` is just `?`
                # repeated once per id; the only bound values are `parent_ids` itself.
                f"SELECT {ITEM_VIEW_COLUMNS} FROM item WHERE id IN ({placeholders})",  # noqa: S608
                parent_ids,
            )
            parent_views = {row["id"]: item_view(row) for row in await cursor.fetchall()}

        by_queue: dict[int, list[dict[str, Any]]] = {}
        for p in self._running.values():
            view = parent_views.get(p.item_id)
            if view is not None:
                by_queue.setdefault(p.queue_id, []).append(view)

        # Unconditional now -- the gate at the top of this method already means we only get
        # here on a `PROGRESS_SAMPLE_TICKS`-th tick, so job and child sampling share the exact
        # same instant; there is no separate child-only throttle left to apply. Appends into
        # `by_queue` in place so a child rides the same `item_delta` message as its parent's
        # row instead of a second WS round trip.
        #
        # One real timestamp for every child diffed this pass -- not `tick_s *
        # PROGRESS_SAMPLE_TICKS`, which a slow pass would make silently wrong (see
        # `child_speed_bps`'s own docstring). `time.monotonic()` matches what
        # `core/progress.py.ProgressSampler` already keys its own EMA history on.
        await self._publish_child_progress(results, by_queue, now=time.monotonic())

        for queue_id, nodes in by_queue.items():
            self.events.publish({"type": "item_delta", "queue_id": queue_id, "nodes": nodes})

        self.events.publish(
            {
                "type": "progress",
                "jobs": [
                    {
                        "job_id": p.job_id,
                        "item_id": p.item_id,
                        "bytes_done": results[p.job_id].bytes_done,
                        "bytes_total": results[p.job_id].bytes_total,
                        "speed_bps": results[p.job_id].speed_bps,
                        "eta_s": results[p.job_id].eta_s,
                    }
                    for p in self._running.values()
                ],
            }
        )

    async def _publish_child_progress(
        self,
        results: dict[int, JobProgress],
        by_queue: dict[int, list[dict[str, Any]]],
        *,
        now: float | None = None,
    ) -> None:
        """Live per-file progress for the files inside a mirroring directory -- the fix this
        task exists for.

        The defect: a `mirror` job is one row in `self._running` no matter how many files it
        contains, so the loop above only ever updates and publishes the *parent* item. Every
        child file's `local_size`/`state` was previously only ever recomputed by the next full
        engine scan (`scan_interval_s`, default 30s) -- so a multi-file release sat visibly
        frozen, then flipped a whole batch of rows to `DOWNLOADED` at once when the scan caught
        up. Two things compound that: lftp writes `foo.rar.lftp` while transferring and renames
        it on completion (so a child doesn't exist under its final name until it's done -- real
        quantization, not just a perceived one), and the per-file sizes were already being
        computed by `core/progress.py`'s walk and thrown away, keeping only the sum. Neither
        needs new I/O; `JobProgress.children` (from that same walk, already `.lftp`-suffix
        stripped by `local_scan.scan_local`) is the fix.

        Diffs each mirror job's children against `self._prev_child_sizes` (last tick *this
        method actually ran*, not every `tick_s`) so only files whose effective size changed
        do any work -- naturally small, since `mirror` only transfers a handful of files
        concurrently (bounded by `mirror_parallel_transfer_count`), never by release size.
        Directories are excluded from the diff: `local_scan.LocalEntry.size` is always 0 for a
        directory, so a real directory is never "changed" by this rule, and excluding it
        explicitly (rather than relying on that always being true) keeps the child-state rule
        below scoped to files, which is the only shape `core/reconcile.py` defines it for.

        Child state uses the exact rule `core/reconcile.py` uses for a leaf file: `local >=
        remote -> DOWNLOADED, else PARTIAL` -- against the child's own persisted
        `item.remote_size`, left alone (not overwritten) when that's `NULL` (remote size not
        yet known), since "unknown vs. 0" is not this method's call to make. This does not
        change how a *top-level* item's completeness is computed (out of scope here; that's
        `core/reconcile.py`/the settle-gate work) -- only leaf files nested under a currently
        downloading mirror item.

        Persist -> read back -> publish, same invariant as the parent above and
        `core/engine.py.scan_queue`: a child's `UPDATE` is issued for every changed file (up
        to the cap), all committed together (batching the writes is the point -- see
        `PROGRESS_SAMPLE_TICKS`'s comment), then read back through `ITEM_VIEW_COLUMNS`/
        `item_view` and only *that* goes on the wire. A child with no matching `item` row (not
        yet persisted by an engine scan) is silently skipped, not invented here -- `_persist`
        in `core/engine.py` is the only writer of new `item` rows.

        **The per-child rate** (this task, 2026-08-14): alongside the `local_size`/`state`
        write above, each changed child's byte delta is divided by the *real* elapsed time
        since it was last diffed (`self._prev_child_times`, `now - prev_time`, both
        `time.monotonic()`) and EMA-smoothed with `core/progress.py.child_speed_bps` --
        deliberately the same smoothing `ProgressSampler` uses for a job's own aggregate rate,
        reused rather than reinvented. A child seen for the first time (no `prev_time` yet, the
        same shape as `ProgressSampler`'s "first sample" branch) reports `0.0`, not a rate
        derived from zero history. The result is collected into `child_progress_by_key`, keyed
        by `(queue_id, full_path)` since `full_path` alone is not unique across queues, and
        turned into the wire's `{item_id, speed_bps}` shape once the read-back below has each
        child's `item.id` -- `_prev_child_sizes` only ever carried a rel_path string, never the
        id `item_view` produces.

        `now` is injectable (`time.monotonic()` seconds) purely for testability, matching
        `ProgressSampler.sample`'s own `now` parameter; `_sample_and_publish_progress` always
        passes a real one.
        """
        now = time.monotonic() if now is None else now
        parent_by_job = {p.job_id: p for p in self._running.values()}
        updates_remaining = MAX_CHILD_PROGRESS_UPDATES_PER_TICK
        truncated = False
        to_read_back: dict[int, list[str]] = {}  # queue_id -> full rel_paths to read back
        # (queue_id, full rel_path) -> this tick's EMA speed for that child, filled in below and
        # matched up with each child's `item.id` once the read-back has it.
        speed_by_key: dict[tuple[int, str], float] = {}

        for job_id, prog in results.items():
            if prog.children is None:  # pget job: no children to report
                continue
            parent = parent_by_job.get(job_id)
            if parent is None:
                continue

            prev = self._prev_child_sizes.setdefault(job_id, {})
            prev_times = self._prev_child_times.setdefault(job_id, {})
            prev_speeds = self._child_speed.setdefault(job_id, {})
            current = {rel: entry.size for rel, entry in prog.children.items() if not entry.is_dir}
            changed = [rel for rel, size in current.items() if prev.get(rel) != size]
            if not changed:
                continue

            if len(changed) > updates_remaining:
                changed = changed[:updates_remaining]
                truncated = True
            updates_remaining -= len(changed)

            full_paths = []
            for rel in changed:
                size = current[rel]
                full_path = f"{parent.rel_path}/{rel}"
                full_paths.append(full_path)

                # The rate, from this child's own previous size/time (both absent on a first
                # sighting -- see the docstring's "first sample" note) -- computed *before*
                # `prev`/`prev_times`/`prev_speeds` are overwritten below for this rel.
                prev_size = prev.get(rel)
                prev_time = prev_times.get(rel)
                if prev_size is not None and prev_time is not None and now > prev_time:
                    speed = child_speed_bps(
                        max(size - prev_size, 0),
                        now - prev_time,
                        prev_speeds.get(rel),
                        self.progress.alpha,
                    )
                else:
                    speed = 0.0
                speed_by_key[(parent.queue_id, full_path)] = speed
                prev_speeds[rel] = speed
                prev_times[rel] = now

                await self.db.execute(
                    "UPDATE item SET local_size = ?, state = CASE "
                    "WHEN remote_size IS NULL THEN state "
                    "WHEN ? >= remote_size THEN 'DOWNLOADED' "
                    "ELSE 'PARTIAL' END "
                    "WHERE queue_id = ? AND rel_path = ?",
                    (size, size, parent.queue_id, full_path),
                )
                # Only mark a rel_path "seen" once it's actually been persisted -- a child the
                # cap skipped this tick must still read as "changed" on the next one, not be
                # silently dropped from the diff.
                prev[rel] = size

            if full_paths:
                to_read_back.setdefault(parent.queue_id, []).extend(full_paths)

            if updates_remaining <= 0:
                break

        child_progress_items: list[dict[str, Any]] = []
        if to_read_back:
            await self.db.commit()
            for queue_id, full_paths in to_read_back.items():
                placeholders = ",".join("?" * len(full_paths))
                cursor = await self.db.execute(
                    # ITEM_VIEW_COLUMNS is a module constant and `placeholders` is just `?`
                    # repeated once per path; the only bound values are
                    # `queue_id`/`full_paths`.
                    f"SELECT {ITEM_VIEW_COLUMNS} FROM item WHERE queue_id = ? AND rel_path IN ({placeholders})",  # noqa: S608
                    (queue_id, *full_paths),
                )
                rows = await cursor.fetchall()
                nodes = [item_view(row) for row in rows]
                by_queue.setdefault(queue_id, []).extend(nodes)
                for node in nodes:
                    speed = speed_by_key.get((queue_id, node["rel_path"]))
                    if speed is not None:
                        child_progress_items.append({"item_id": node["id"], "speed_bps": speed})

        if truncated:
            # A silent cap reads as "we published everything this tick" when we did not.
            logger.warning(
                "child progress publish capped at %d update(s) this tick; remaining children "
                "will show on a later tick instead",
                MAX_CHILD_PROGRESS_UPDATES_PER_TICK,
            )

        # A third, item-keyed message (docstring above) -- never folded into `progress` or
        # `item_delta`. Bounded the same way the rest of this method is: at most
        # `len(child_progress_items) <= MAX_CHILD_PROGRESS_UPDATES_PER_TICK` entries, never
        # proportional to tree size. Omitted entirely on a tick with nothing to report, same as
        # `item_delta`'s own per-queue publish above only fires for queues that actually changed.
        if child_progress_items:
            self.events.publish({"type": "child_progress", "items": child_progress_items})

    async def _sample_metrics(self) -> None:
        """Feeds `core/metrics.py`'s 30-tick throughput sampler from the same per-job byte
        accounting `_sample_and_publish_progress` writes -- never a second measurement, and
        never lftp's own stdout (DESIGN.md §1.3). Called every `tick_s` (~1s), including ticks
        where nothing is running (`self._running` empty) -- `ThroughputSampler.tick()` needs
        that to keep the heartbeat alive while lftpweb is idle, which is what tells an idle
        instance apart from a stopped one (see that module's docstring).

        `self._last_progress` is only refreshed every `PROGRESS_SAMPLE_TICKS`-th tick now
        (2026-08-16) -- on the ticks between, this reuses the same cached bytes_done rather
        than re-measuring, so a job's contribution here is a step function updated at the same
        cadence as the progress sampler, not a fresh read every second. `ThroughputSampler`'s
        own 30-tick averaging window already smooths over that.
        """
        running = [
            RunningJobBytes(
                job_id=p.job_id,
                queue_id=p.queue_id,
                # `_last_progress` was just populated, for every currently-running job, by
                # `_sample_and_publish_progress` above in this same tick -- the fallback to
                # `p.bytes_start` (contribution 0) only matters for a job that somehow never
                # got a progress sample yet, which shouldn't happen in practice given the call
                # order but keeps this from raising on a `KeyError` if it ever did.
                bytes_done=self._last_progress[p.job_id].bytes_done
                if p.job_id in self._last_progress
                else p.bytes_start,
                bytes_start=p.bytes_start,
            )
            for p in self._running.values()
        ]
        await self.metrics.tick(running)

    async def _admit(self) -> None:
        settings = await load_transfer_settings(self.db)
        sched_settings = settings.scheduler_settings()

        running = [
            scheduler.RunningJob(id=p.job_id, lane=p.lane, rate_limit_bps=p.rate_limit_bps)
            for p in self._running.values()
        ]
        now = time.monotonic()
        cursor = await self.db.execute(
            "SELECT job.id, job.item_id, job.lane, "
            "       COALESCE(job.queue_position, 1e18) AS queue_position, "
            "       job.forced_full_rate, job.forced_rate_fraction "
            "FROM job WHERE job.state = 'queued' "
            "ORDER BY COALESCE(job.queue_position, 1e18) ASC, job.id ASC"
        )
        rows = await cursor.fetchall()
        queue: list[scheduler.QueuedJob] = []
        # 2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md, this task's root
        # cause): never hand the scheduler two queued jobs for the same item in one pass, and
        # never admit a job for an item that already has a process running. This is what
        # actually prevents two concurrent lftp processes against the same remote/local paths
        # -- `enqueue_item`'s own check (above) only stops the *common* case (a double-click)
        # from creating a second `queued` row in the first place; a race between two
        # `enqueue_item` calls, or a row inserted directly (as a test does, or as some future
        # caller might), still lands here as two `queued` rows for one `item_id`, and this is
        # the layer that refuses to let both become processes regardless of how they got there.
        # `rows` is already ordered `queue_position ASC, id ASC`
        # (2026-08-19, docs/transfers-redesign-spec.md §3.4 -- replaces `rank DESC, queued_at
        # ASC`), so the row kept for admission is the one that would have been served first
        # anyway; the other stays `queued` and is picked up on a later tick, once the running
        # one is no longer active. `COALESCE(..., 1e18)` is defensive, not load-bearing in
        # production -- `_insert_job` never leaves the column NULL -- but it keeps a
        # hand-crafted row (a test building one directly) sorting *last* rather than first,
        # which is what SQLite's own NULL-sorts-first default would otherwise do.
        active_item_ids = {p.item_id for p in self._running.values()}
        for row in rows:
            if row["id"] in self._running:
                continue
            if now < self._backoff_until.get(row["item_id"], 0.0):
                continue
            if row["item_id"] in active_item_ids:
                continue
            active_item_ids.add(row["item_id"])
            queue.append(
                scheduler.QueuedJob(
                    id=row["id"],
                    lane=row["lane"],
                    queue_position=row["queue_position"],
                    forced_rate_fraction=resolve_forced_rate_fraction(row),
                )
            )

        decisions = scheduler.admit(sched_settings, running, queue)
        if not decisions:
            # Work is waiting and nothing was admitted. That is usually correct (no headroom,
            # no slots), but it is indistinguishable from a wedged queue unless we say why —
            # a silently deadlocked scheduler is how the reserve-exceeds-ceiling bug hid.
            if queue:
                allocated = sum(r.rate_limit_bps or 0 for r in running)
                logger.debug(
                    "admitted none: %d queued, %d running, ceiling=%d reserve=%d allocated=%d "
                    "headroom=%d slots=%d",
                    len(queue),
                    len(running),
                    sched_settings.max_bandwidth_bps,
                    sched_settings.small_lane_reserve_bps,
                    allocated,
                    sched_settings.max_bandwidth_bps
                    - sched_settings.small_lane_reserve_bps
                    - allocated,
                    sched_settings.max_concurrent_transfers - len(running),
                )
            return

        host = await self._host_provider() if self._host_provider else None
        if host is None:
            logger.warning("scheduler admitted %d job(s) but no host is configured", len(decisions))
            return

        # DESIGN.md §8: "hold all transfers for that host instead of spawning jobs that
        # fail." Without this check, `_spawn_decision` would spawn lftp with
        # `host.password is None`, and every one of those processes would fail
        # `AUTH_FAILED` a few seconds later -- exactly the "wave of AUTH_FAILED jobs and no
        # explanation" DESIGN.md §8 names as the failure mode to prevent. Checked here, not
        # inside `_spawn_decision`, so it holds admission for *every* decision this tick, not
        # just the first one to reach spawn.
        if host.credentials_need_reentry:
            if not self.credentials_need_reentry:
                logger.warning(
                    "holding %d job(s): host %s credentials need re-entry "
                    "(Settings -> Connection)",
                    len(decisions),
                    host.id,
                )
            self.credentials_need_reentry = True
            return
        self.credentials_need_reentry = False

        for decision in decisions:
            try:
                await self._spawn_decision(decision, host, settings)
            except Exception as exc:  # noqa: BLE001
                # A spawn failure is an environment problem (unwritable run_dir, missing lftp
                # binary), not a transfer problem — it will recur identically next tick. Left
                # to propagate it would be caught by _loop's blanket handler, leaving the job
                # `queued` forever while the tick hot-loops once a second and the UI shows
                # nothing wrong. Fail the job visibly instead, and keep the other decisions.
                logger.exception("failed to spawn job %d", decision.job_id)
                await self._fail_job(
                    decision.job_id,
                    error_class="SPAWN_FAILED",
                    detail=f"{type(exc).__name__}: {exc}",
                )

    async def _spawn_decision(
        self, decision: scheduler.AdmitDecision, host: HostConfig, settings: TransferSettings
    ) -> None:
        job_row = await self._fetch_job(decision.job_id)
        if job_row is None:
            return
        item = await self._fetch_item(job_row["item_id"])
        if item is None:
            return

        # Final guard, independent of `_admit`'s own dedup above (2026-08-13,
        # prompts/2026-08-13-lftp-timestamped-temp-files.md): never actually exec a second lftp
        # process for an item that already has one in `self._running`, regardless of how this
        # decision came to exist. `_admit`'s per-tick dedup is what normally prevents the
        # scheduler from ever producing such a decision in the first place, but a guard that
        # lives only where the decision is *built* is one refactor of that method away from
        # being no guard at all where the process actually gets exec'd -- this is the one place
        # that can never be bypassed short of removing this check itself. Leaves the job
        # `queued` (not failed): the moment the other job for this item finishes, this one is
        # legitimately admissible again on a later tick.
        if any(p.item_id == item["id"] for p in self._running.values()):
            logger.warning(
                "job %s: refusing to spawn a second process for item %s (%s) -- one is already "
                "running",
                decision.job_id,
                item["id"],
                item["rel_path"],
            )
            return

        queue_row = await self._fetch_queue(item["queue_id"])
        if queue_row is None:
            return

        remote_full = queue_row["remote_path"].rstrip("/") + "/" + item["rel_path"]
        resolved_download_prefix: str | None = None
        mirror_rename_target = False
        if job_row["kind"] == "pget":
            local_full = queue_row["local_path"].rstrip("/") + "/" + item["rel_path"]
            local_root_for_progress = local_full
            mkdir_target = _parent_rel_path(local_full)
        else:
            parent = _parent_rel(item["rel_path"])
            local_parent = queue_row["local_path"].rstrip("/") + (f"/{parent}" if parent else "")
            # "Folder prefix during transfer" (2026-08-14, `core/download_prefix.py`) --
            # directory items only (§ the task's own scope limit; a `pget` job never reaches
            # this branch at all). `_resolve_download_prefix_for_spawn` below is what makes a
            # *stale* prefix safe: it prefers whatever is already recorded on the item over
            # today's settings, so a resume always targets the directory its own partial bytes
            # are physically sitting in.
            resolved_download_prefix = await self._resolve_download_prefix_for_spawn(
                item, queue_row
            )
            if resolved_download_prefix is not None:
                name = item["rel_path"].rsplit("/", 1)[-1]
                local_full = (
                    f"{local_parent}/"
                    f"{download_prefix.prefixed_name(resolved_download_prefix, name)}"
                )
                mirror_rename_target = True
            else:
                local_full = local_parent  # core/lftp.py: mirror's target is the *parent* dir
            local_root_for_progress = (
                local_full
                if resolved_download_prefix is not None
                else queue_row["local_path"].rstrip("/") + "/" + item["rel_path"]
            )
            mkdir_target = local_parent

        # lftp creates directories *within* what it's given (mirror builds the item's own
        # subdirectory tree; pget's sparse-file preallocation happens inside its target's
        # parent) but not the parent chain leading up to it. Found running this against the
        # fake seedbox with a nested item: `pget -o <path>` failed "No such file or
        # directory" because `<queue.local_path>/<parent-of-rel_path>/` didn't exist yet — for
        # a genuinely top-level item (DESIGN.md §4.7) this is a no-op (the parent is just the
        # queue root, which the operator already provisioned), but nothing here should assume
        # every queued item is top-level (phase 2 decided `item` rows exist per node).
        Path(mkdir_target).mkdir(parents=True, exist_ok=True)

        creds = lftp.HostCreds(
            address=host.address,
            port=host.port,
            username=host.username,
            auth_method=host.auth_method,
            key_path=host.key_path,
            password=host.password,
            ssh_key=host.ssh_key,
            known_hosts_policy=host.known_hosts_policy,
            pinned_host_key=await self._pinned_host_key(host),
        )
        connection_limit = await self._connection_limit(host)

        # DESIGN.md §4.7/§3.2 rule 8: the identical compiled file_exclude set the reconciler
        # uses (core/engine.py.scan_queue) also builds lftp's --exclude-glob arguments here --
        # one evaluator, two consumers, never two copies to drift apart (core/patterns.py).
        # Only meaningful for `mirror` (lftp.build_transfer_command only emits --exclude-glob
        # on that branch); harmless to compute unconditionally for `pget`.
        compiled = await patterns.compiled_for_queue(self.db, item["queue_id"])

        spec = lftp.JobSpec(
            job_id=decision.job_id,
            kind=job_row["kind"],
            creds=creds,
            remote_path=remote_full,
            local_path=local_full,
            rate_limit_bps=decision.rate_limit_bps,
            connection_limit=connection_limit,
            parallel=settings.mirror_parallel_transfer_count,
            pget_n=settings.mirror_use_pget_n
            if job_row["kind"] == "mirror"
            else settings.pget_default_n,
            exclude_globs=compiled.exclude_globs(),
            extra_settings=settings.extra_lftp_settings,
            run_dir=self.run_dir,
            mirror_rename_target=mirror_rename_target,
        )

        try:
            spawned = await lftp.spawn(spec)
        except lftp.NoHostKeyPinError as exc:
            logger.warning("job %s: %s", decision.job_id, exc)
            await self._suppress_item(item["id"], reason="permanent_error", state="FAILED")
            await self.db.execute(
                "UPDATE job SET state = 'failed', error_class = 'AUTH_FAILED', output_tail = ? WHERE id = ?",
                (str(exc), decision.job_id),
            )
            await self.db.commit()
            return

        wait_task = asyncio.create_task(
            lftp.wait_and_capture(spawned), name=f"lftpweb-job-{decision.job_id}-wait"
        )
        proc = _RunningProcess(
            job_id=decision.job_id,
            item_id=item["id"],
            queue_id=item["queue_id"],
            rel_path=item["rel_path"],
            is_dir=bool(item["is_dir"]),
            kind=job_row["kind"],
            lane=decision.lane,
            rate_limit_bps=decision.rate_limit_bps,
            forced_rate_fraction=decision.forced_rate_fraction,
            local_root=local_root_for_progress,
            bytes_total=item["remote_size"],
            remote_mtime=float(item["remote_mtime"]) if item["remote_mtime"] is not None else None,
            spawned=spawned,
            wait_task=wait_task,
            bytes_start=item["local_size"] or 0,  # same value written to job.bytes_start below
        )
        self._running[decision.job_id] = proc

        # A transfer starting/finishing is the single thing an operator most wants in the
        # log (DESIGN.md §10.1). Until this existed, `POST /api/jobs` was followed by total
        # silence: no way to tell whether lftp had spawned, succeeded, or was still going.
        logger.info(
            "job %s spawned: %s %s -> %s (pid %s, %s B/s cap%s)",
            decision.job_id,
            job_row["kind"],
            remote_full,
            local_full,
            spawned.pid,
            decision.rate_limit_bps,
            f", start-now @ {round(decision.forced_rate_fraction * 100)}%"
            if decision.forced_rate_fraction is not None
            else "",
        )

        started_at = _now_iso()
        await self.db.execute(
            "UPDATE job SET state = 'running', pid = ?, started_at = ?, rate_limit_bps = ?, "
            "forced_full_rate = ?, forced_rate_fraction = ?, bytes_start = ?, bytes_total = ? "
            "WHERE id = ?",
            (
                spawned.pid,
                started_at,
                decision.rate_limit_bps,
                1 if decision.forced_rate_fraction is not None else 0,
                decision.forced_rate_fraction,
                item["local_size"] or 0,
                # 2026-08-14 (prompts/2026-08-14-exit-zero-is-not-completion.md, defect 4):
                # `job.bytes_total` was never written here, so it stayed NULL forever and
                # every API response computed it fresh from the *live* `item.remote_size`
                # instead (`api/jobs.py`/`api/history.py`) -- a value that can change after
                # this job spawned (a later scan, a pattern edit) while `job.bytes_done` stays
                # fixed at whatever `remote_size` was when `_reap_one` wrote it. The two ended
                # up using different denominators for the same job, so `bytes_done` could
                # exceed `bytes_total` in the API response even though neither number was
                # individually wrong -- fixed by freezing this value here, at spawn, the same
                # "fixed at admission and never re-shaped" invariant DESIGN.md §4.5 already
                # uses for a job's bandwidth allocation. Matches `proc.bytes_total` exactly
                # (`_RunningProcess` below), which already carried this same value in memory
                # for progress sampling -- this is the persisted counterpart.
                item["remote_size"],
                decision.job_id,
            ),
        )
        # `pending_download_prefix` is written unconditionally here, `NULL` included -- it is
        # this row's *current* physical-location bookkeeping ("folder prefix during transfer"),
        # so a plain (non-prefixed) mirror or a `pget` job must clear whatever a stale value
        # might otherwise still say, exactly as a genuinely fresh or feature-off spawn should.
        await self.db.execute(
            "UPDATE item SET state = 'DOWNLOADING', pending_download_prefix = ? WHERE id = ?",
            (resolved_download_prefix, item["id"]),
        )
        await self.db.commit()
        await self._publish_item_state(item["id"])

    # --- small DB helpers ------------------------------------------------------------------

    async def _fetch_item(self, item_id: int):
        cursor = await self.db.execute("SELECT * FROM item WHERE id = ?", (item_id,))
        return await cursor.fetchone()

    async def _fetch_job(self, job_id: int):
        cursor = await self.db.execute("SELECT * FROM job WHERE id = ?", (job_id,))
        return await cursor.fetchone()

    async def _fetch_queue(self, queue_id: int):
        cursor = await self.db.execute("SELECT * FROM path_queue WHERE id = ?", (queue_id,))
        return await cursor.fetchone()

    async def _current_attempt(self, job_id: int) -> int:
        cursor = await self.db.execute("SELECT attempt FROM job WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return row["attempt"] if row is not None else 1

    async def _lane_for(self, item) -> str:
        settings = await load_transfer_settings(self.db)
        size = item["remote_size"] or 0
        return "small" if size and size < settings.small_item_threshold_bytes else "main"

    async def _resolve_download_prefix_for_spawn(self, item, queue_row) -> str | None:
        """The exact prefix string this `mirror` spawn should write its local root under, or
        `None` for no prefix at all -- "folder prefix during transfer"
        (`core/download_prefix.py`). Only ever called from `_spawn_decision`'s `mirror` branch
        (a `pget` job never reaches this).

        **Reuses `item.pending_download_prefix` whenever it is already set, and never
        recomputes from today's settings in that case.** This is what keeps a *stale* prefix
        (the site or queue setting edited, or turned off, since this item was last spawned or
        left `STOPPED` mid-transfer) from orphaning anything: a resume must target the exact
        directory its own partial bytes are physically sitting in, not start over under a
        different name because Settings -> Transfer changed in the meantime. Only when nothing
        is recorded yet -- a genuinely fresh spawn, or a previous transfer that already
        completed and had this column cleared (`_reap_one`) -- does this consult the resolved
        site/queue setting at all.
        """
        existing = item["pending_download_prefix"]
        if existing is not None:
            return existing
        site = await download_prefix.load_download_prefix_settings(self.db)
        queue_enabled = queue_row["download_prefix_enabled"]
        enabled, prefix = download_prefix.resolve_for_queue(
            None if queue_enabled is None else bool(queue_enabled),
            queue_row["download_prefix"],
            site,
        )
        return prefix if enabled else None

    async def _insert_job(
        self,
        item_id: int,
        *,
        kind: str,
        lane: str,
        attempt: int,
        forced_rate_fraction: float | None = None,
        queued_at: str | None = None,
        queue_position: float | None = None,
    ) -> int:
        """`queued_at=None` (every caller before 2026-08-19) lets the column's own
        `DEFAULT (STRFTIME(...))` stamp now, unchanged. A caller may pass an explicit value to
        backdate the row instead -- see `enqueue_item`'s own docstring for why.

        `forced_rate_fraction` (2026-08-19,
        prompts/done/2026-08-19-start-now-bandwidth-fractions.md) writes both columns in
        lockstep -- `forced_full_rate = 1` iff `forced_rate_fraction is not None` -- migration
        022's own contract for keeping the two in agreement.

        `queue_position=None` (every caller except the startup rescue, this task) appends at the
        back -- `position_between(MAX(queue_position over queued jobs), None)`, the position
        model's replacement for "new jobs sort last under `queued_at ASC`"
        (`migrations/023_queue_position.sql`). The rescue passes an explicit, pre-computed
        position instead (`_rescue_position`) so the re-queued row lands where its original
        `queued_at` would have placed it, not at the back.
        """
        forced_full_rate = 1 if forced_rate_fraction is not None else 0
        if queue_position is None:
            cursor = await self.db.execute(
                "SELECT MAX(queue_position) AS m FROM job WHERE state = 'queued'"
            )
            row = await cursor.fetchone()
            queue_position = position_between(row["m"], None)
        if queued_at is None:
            cursor = await self.db.execute(
                "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
                "forced_rate_fraction, queue_position) VALUES (?, ?, 'queued', ?, 0, ?, ?, ?, ?)",
                (
                    item_id,
                    kind,
                    lane,
                    attempt,
                    forced_full_rate,
                    forced_rate_fraction,
                    queue_position,
                ),
            )
        else:
            cursor = await self.db.execute(
                "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate, "
                "forced_rate_fraction, queued_at, queue_position) VALUES (?, ?, 'queued', ?, 0, ?, "
                "?, ?, ?, ?)",
                (
                    item_id,
                    kind,
                    lane,
                    attempt,
                    forced_full_rate,
                    forced_rate_fraction,
                    queued_at,
                    queue_position,
                ),
            )
        await self.db.commit()
        return cursor.lastrowid

    async def _fail_job(self, job_id: int, *, error_class: str, detail: str) -> None:
        """Mark a job failed without retrying it (DESIGN.md §4.3, §4.6).

        Used for failures that are not the transfer's fault and will recur identically —
        currently only a spawn failure. Suppresses the item like any other permanent error,
        so nothing re-queues it automatically.
        """
        row = await self._fetch_job(job_id)
        detail = detail[:4096]
        await self.db.execute(
            "UPDATE job SET state = 'failed', finished_at = ?, error_class = ?, output_tail = ? "
            "WHERE id = ?",
            (_now_iso(), error_class, detail, job_id),
        )
        if row is not None:
            await self._suppress_item(row["item_id"], reason="permanent_error", state="FAILED")
            await self.db.execute(
                "UPDATE item SET error_class = ?, error_detail = ? WHERE id = ?",
                (error_class, detail, row["item_id"]),
            )
        await self.db.commit()

    async def _publish_item_state(self, item_id: int) -> None:
        """Push a one-row Files-tree update the moment this module changes an item's
        lifecycle state (queued, spawned, stopped, failed, succeeded, or requeued after a
        transient failure) — part of the WS delta fix (DESIGN.md §2/§9). Without this the
        Files page only learns about a lifecycle change on the *next* full engine scan (up
        to `scan_interval_s`, default 30s), which is far too slow for "stop it and see it go
        STOPPED without a page refresh." Proportional by construction: exactly one row,
        never the tree — see `core/engine.py.diff_nodes` for the scan-driven counterpart.
        """
        row = await self._fetch_item(item_id)
        if row is None:
            return
        self.events.publish(
            {
                "type": "item_delta",
                "queue_id": row["queue_id"],
                "nodes": [item_view(row)],
            }
        )

    async def _suppress_item(self, item_id: int, *, reason: str, state: str) -> None:
        await self.db.execute(
            "UPDATE item SET state = ?, auto_queue_suppressed = 1, suppressed_reason = ? WHERE id = ?",
            (state, reason, item_id),
        )

    async def _pinned_host_key(self, host: HostConfig) -> str | None:
        if host.known_hosts_policy == "insecure":
            return None
        from lftpweb.core.remote import KnownHostsStore

        store = KnownHostsStore(Path(self.config_dir) / "known_hosts.json")
        return store.get(host.address, host.port)

    async def _connection_limit(self, host: HostConfig) -> int | None:
        cursor = await self.db.execute(
            "SELECT connection_overrides FROM host WHERE id = ?", (host.id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return parse_connection_limit(row["connection_overrides"])

    # --- read models for api/jobs.py --------------------------------------------------------

    def _postprocess_in_flight_ids(self) -> frozenset[int]:
        """`PostprocessPipeline.in_flight_item_ids()`, read straight off the pipeline this queue
        already holds a reference to (`self.postprocess`, set after construction by `app.py`;
        `None` in most tests and in a process where post-processing was never wired).

        Read *here*, in the read models themselves, rather than plumbed in from `api/jobs.py` --
        `dismiss_all_terminal` needs the same set and has no request context to take it from, and
        one lookup site is one fewer place for the three callers to disagree. `api/jobs.py`'s own
        `_busy_context` stays as it is: that one feeds `core/local_delete.py`'s guards, an
        unrelated consumer of the same set.
        """
        if self.postprocess is None:
            return frozenset()
        ids = self.postprocess.in_flight_item_ids()
        return frozenset(ids) if ids else frozenset()

    def _in_flight_select(self) -> str:
        """The two computed columns both listing queries project, built once so their aliases
        (and the expressions behind them) can't drift apart -- `core/pipeline_flight.py`.
        """
        ids = self._postprocess_in_flight_ids()
        return (
            f"{in_flight_expr(ids)} AS pipeline_in_flight, "
            f"{waiting_reason_expr(ids)} AS pipeline_waiting_reason"
        )

    async def list_jobs(self) -> list[dict]:
        """The Transfers page's row set (DESIGN.md §9.2). Not just `queued`/`running`:
        §9.2 explicitly requires "failed rows show the error class and the captured lftp
        output tail", and a stopped job must be visible as `STOPPED` rather than vanishing
        the instant it's reaped — DESIGN.md names the Transfers page as where both surface,
        distinct from the History page's full `job`/`event` audit trail. So this also
        includes a `failed`/`cancelled`/`succeeded` job when it is that item's *most recent*
        job (the `MAX(id)` subquery) — a manual retry inserts a new `queued` row for the same
        item, which is already covered by the first clause and makes the old terminal row
        irrelevant (superseded), so this doesn't need to filter it out separately.

        **`succeeded` joined this set 2026-08-14**
        (prompts/2026-08-14-exit-zero-is-not-completion.md) — before this, a job that finished
        cleanly simply vanished from Transfers the instant it was reaped, which is what made a
        real incident so hard to see live: seven minutes of an actual transfer looked, from
        the UI, like nothing running and 0 B/s in the header. Same `MAX(id)`/`dismissed_at`
        shape as `failed`/`cancelled` already had — one terminal row per item, dismissible via
        `dismiss_job` exactly like the other two, so the row set stays bounded by construction
        (the same boundedness `api/history.py`'s own docstring contrasts against its own
        unbounded, paginated shape, which is why this method can inline `output_tail` at all).

        A terminal job whose `dismissed_at` is set (2026-08-13, `dismiss_job` above) is
        excluded here too — dismissal is exactly "stop counting this as the item's visible
        row," the same effect a fresh retry already has, just without creating one. Nothing
        else reads `dismissed_at`: `api/history.py` has no such filter, by design, so a
        dismissed job stays visible there.

        Also joins `path_queue` for `queue_name` (DESIGN.md §9.2: with multiple active
        queues, a Transfers-page row has to say which one it's from). This row set is
        bounded by construction (the docstring above), unlike `api/history.py`'s unbounded,
        paginated endpoint — that's why this can inline the join instead of shipping
        `queue_id` alone and making the client resolve it.

        **2026-08-15** (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): also
        joins `item.verified_at`/`item.extracted_at`/`item.remote_deleted_at`/`item.arr_status`/
        `item.arr_status_at`, plus `arr_instance.name` via `path_queue.arr_instance_id` (`LEFT
        JOIN` — most queues have no bound instance, migration 018's "everything OFF by
        default"), for the Transfers row's new expand panel (`api/jobs.py._job_out`). Same
        bounded-by-construction reasoning as `queue_name` above — this is not the phase-6
        unbounded-list trap `api/history.py`'s own docstring warns against.

        **2026-08-16** (prompts/2026-08-16-arr-chip-on-row-lines.md): also joins
        `arr_instance.kind` (`'sonarr'`/`'radarr'`) — the collapsed row's new brand-logo chip
        needs to know *which* logo to draw; `arr_instance_name` alone is free-text the user can
        rename to anything, so it can't drive that choice on its own.

        **2026-08-19** (docs/transfers-redesign-spec.md §3.6, phase 1 stage 4a): also joins
        `path_queue.short_name AS queue_short_name` — the ungrouped Transfers row's queue badge
        (`api/jobs.py._job_out`, `lib/queueDisplayName.ts`) needs a queue's short name per row
        now that grouping (and its once-per-queue header) is gone.

        **2026-08-20** (docs/transfers-redesign-spec.md §3.2's pipeline-completion rule): also
        projects `pipeline_in_flight`/`pipeline_waiting_reason` from
        `core/pipeline_flight.py` — the *one* definition of "still moving," shared verbatim with
        `list_complete_jobs`'s own `NOT (...)` and `dismiss_all_terminal`'s exclusion, so the two
        boxes can never disagree about which one a row belongs in. Deliberately computed here and
        shipped as a field rather than re-derived on the client: a second encoding of this rule is
        exactly the drift the split cannot survive. `item.manual_outcome`/`manual_outcome_at`
        (migration 025) ride along so the row can *show* it was manually resolved rather than
        looking like a normal completion, and `item.state` is joined only because the reason
        expression reads it (never projected — see `models.py.JobOut`).
        """
        cursor = await self.db.execute(
            "SELECT job.*, item.rel_path, item.is_dir, item.queue_id, item.remote_size, "
            "       item.verified_at, item.extracted_at, item.remote_deleted_at, "
            "       item.arr_status, item.arr_status_at, "
            "       item.manual_outcome, item.manual_outcome_at, "
            f"       {self._in_flight_select()}, "
            "       path_queue.name AS queue_name, path_queue.short_name AS queue_short_name, "
            "       arr_instance.name AS arr_instance_name, "
            "       arr_instance.kind AS arr_instance_kind "
            "FROM job "
            "JOIN item ON item.id = job.item_id "
            "JOIN path_queue ON path_queue.id = item.queue_id "
            "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
            "WHERE job.state IN ('queued','running') "
            "   OR (job.state IN ('failed','cancelled','succeeded') "
            "       AND job.dismissed_at IS NULL "
            "       AND job.id = (SELECT MAX(j2.id) FROM job j2 WHERE j2.item_id = job.item_id)) "
            "ORDER BY COALESCE(job.queue_position, 1e18) ASC, job.id ASC"
        )
        rows = await cursor.fetchall()
        out = []
        for row in rows:
            d = dict(row)
            p = self._last_progress.get(row["id"])
            if p is not None:
                d["bytes_done"] = p.bytes_done
                d["speed_bps"] = p.speed_bps
                d["eta_s"] = p.eta_s
            out.append(d)
        return out

    async def list_complete_jobs(
        self,
        *,
        limit: int,
        offset: int,
        name_filter: str | None = None,
        outcome: str | None = None,
    ) -> tuple[list[dict], int]:
        """The Queue tab's **Complete** box (2026-08-19, docs/transfers-redesign-spec.md §3.2,
        phase 1 stage 4b) -- `list_jobs()`'s terminal half, split out, server-side paginated,
        and newest-finished-first, rather than inlined into that method's already-bounded row
        set. Same join shape and the identical `MAX(j2.id)`-per-item "one row per item, most
        recent job wins" rule `list_jobs()` uses for its own terminal rows (that method's own
        docstring) -- an item that has been retried since its last terminal job stops appearing
        here the moment the new job exists, the same "superseded" reasoning, so a row never
        shows in both the Active/pending box and this one at once.

        Unlike `list_jobs()`, this is genuinely unbounded in total row count (a busy install
        accumulates thousands of terminal jobs over time) -- the same shape `api/history.py`'s
        own endpoints are built around, which is why the return type mirrors theirs: the
        matching page of rows *and* `total`, the full filtered count ignoring the page, so the
        frontend can render numbered pages (`lib/pagination.ts`) without a second unbounded
        query. `api/jobs.py`'s caller strips `output_tail` back out of each row before building
        `JobOut` -- the identical `has_output_tail`-only convention `api/history.py` already
        uses, for the identical reason (never inline a ~4KB blob on every row of an unbounded
        list).

        `name_filter` (optional) is a case-insensitive substring match against `item.rel_path`
        (`_like_escape`, module-level above) -- the server-side twin of the client-side
        `lib/transferPanel.ts.filterTransferJobs`'s own semantics, now that this box's rows are
        no longer all loaded at once for a client-side filter to run over. Built from the exact
        same predicate `dismiss_all_terminal`'s own `name_filter` branch uses, so "Dismiss
        list"'s dismissed count always matches what this method reports as `total` for the same
        filter text -- see that method's own docstring for why that agreement matters.

        `outcome` (2026-08-20, follow-up to phase 1 stage 4b,
        `prompts/2026-08-20-transfers-dismiss-menu-and-counts.md`) restricts the listing to one
        terminal state, the same vocabulary `dismiss_all_terminal`'s own `outcome` narrows to.
        **Not currently reachable from `GET /api/jobs/complete`** -- the Complete box's "Dismiss"
        menu deliberately does not fetch a live per-outcome count (the task's own instruction:
        "if per-outcome counts are not already available, do not add a query to get them"), so
        no frontend caller passes this. It exists here purely so `dismiss_all_terminal`'s
        `(name_filter=..., outcome=...)` composed case can be tested against the identical
        predicate this method would use, the same "count and dismissed set built from one
        predicate" property `name_filter` alone already has a test for
        (`test_dismiss_all_terminal_name_filter_count_matches_list_complete_jobs_total`,
        extended rather than duplicated for this case).

        **2026-08-20: terminal is no longer the same thing as complete** (docs/transfers-redesign-
        spec.md §3.2's pipeline-completion rule). An item whose lftp job exited 0 but whose
        verify/extract, *arr import, or deferred source delete is still outstanding belongs in the
        *Active* box, so it is excluded from both this listing **and its `total`** by
        `NOT (core/pipeline_flight.in_flight_expr(...))` -- the identical expression `list_jobs`
        projects as `pipeline_in_flight`, so a row is in exactly one box by construction rather
        than by two rules that happen to agree today. The count query grew the same
        `path_queue`/`arr_instance` joins the page query already had, purely so that one
        expression can be evaluated identically in both.
        """
        in_flight = self._postprocess_in_flight_ids()
        where = [
            "job.state IN ('failed','cancelled','succeeded')",
            "job.dismissed_at IS NULL",
            "job.id = (SELECT MAX(j2.id) FROM job j2 WHERE j2.item_id = job.item_id)",
            f"NOT {in_flight_expr(in_flight)}",
        ]
        params: list[Any] = []
        if name_filter is not None:
            where.append("LOWER(item.rel_path) LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(name_filter.lower())}%")
        if outcome is not None:
            where.append("job.state = ?")
            params.append(outcome)
        where_sql = " AND ".join(where)

        count_cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM job "
            "JOIN item ON item.id = job.item_id "
            "JOIN path_queue ON path_queue.id = item.queue_id "
            "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
            f"WHERE {where_sql}",
            params,
        )
        count_row = await count_cursor.fetchone()
        total = count_row["c"] if count_row is not None else 0

        cursor = await self.db.execute(
            "SELECT job.*, item.rel_path, item.is_dir, item.queue_id, item.remote_size, "
            "       item.verified_at, item.extracted_at, item.remote_deleted_at, "
            "       item.arr_status, item.arr_status_at, "
            "       item.manual_outcome, item.manual_outcome_at, "
            f"       {self._in_flight_select()}, "
            "       path_queue.name AS queue_name, path_queue.short_name AS queue_short_name, "
            "       arr_instance.name AS arr_instance_name, "
            "       arr_instance.kind AS arr_instance_kind "
            "FROM job "
            "JOIN item ON item.id = job.item_id "
            "JOIN path_queue ON path_queue.id = item.queue_id "
            "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
            f"WHERE {where_sql} "
            "ORDER BY COALESCE(job.finished_at, job.queued_at) DESC, job.id DESC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows], total

    def stats(self, settings: TransferSettings) -> dict:
        current_speed = sum(self._last_speeds.get(job_id, 0.0) for job_id in self._running)
        allocated = sum(p.rate_limit_bps for p in self._running.values())
        return {
            "current_speed_bps": int(current_speed),
            "allocated_bps": int(allocated),
            "ceiling_bps": settings.max_bandwidth_bps,
        }
