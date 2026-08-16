"""Job lifecycle and process supervision (DESIGN.md §4.1–§4.6). `TransferQueue` owns
spawning, watching, and reaping — `core/scheduler.py` owns only the admission *decision*
(§4.5, §12). Every tick (`transfer_tick_s`, default ~1 Hz per §4.4):

1. reap any process that exited since the last tick, persist the outcome, and either retry
   (transient class, attempts remaining) or terminate the item's lifecycle (§4.3/§4.6);
2. sample progress for everything still running (`core/progress.py`) and publish it;
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from lftpweb.core import audit, download_prefix, lftp, local_scan, patterns, scheduler, settle
from lftpweb.core.events import EventBus
from lftpweb.core.itemview import ITEM_VIEW_COLUMNS, ItemView, item_view
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


class JobNotDismissableError(Exception):
    """Raised by `TransferQueue.dismiss_job` for a `queued`/`running` job (§4.6's active
    states) -- dismiss is a Transfers-page display action for a job that's already done,
    never a way to hide an active transfer. The UI never offers the button for these states
    (`dismiss_job`'s own docstring), but that's a courtesy, not the guard -- rejecting it here
    too is what makes it impossible, not merely unusual, matching the task's own instruction.
    """


# Live per-file progress inside a mirroring directory (see `_publish_child_progress`), tuned
# separately from the ~1 Hz `tick_s` cadence everything else in this module runs at:
#
# - `CHILD_PROGRESS_THROTTLE_TICKS`: publish/persist per-child progress only every Nth tick,
#   not every tick. Smooth feedback doesn't need 1 Hz precision on each `.rar`, and a 50-file
#   release changing every tick at 1 Hz is up to 50 `UPDATE`s a second -- steady write pressure
#   like that is exactly what turned the `VACUUM INTO` backup race from rare into routine (see
#   `docs/decisions.md`, `209928d`). 3 ticks (~3s at the default `tick_s`) keeps the writes
#   batched while still reading as "live" to someone watching the Files page.
# - `MAX_CHILD_PROGRESS_UPDATES_PER_TICK`: a safety cap, not the normal case. In practice the
#   changed set per throttled tick is bounded by lftp's own parallelism
#   (`mirror_parallel_transfer_count`, a handful of files at a time), never by how large the
#   release is -- but nothing here enforces that bound structurally, so a cap plus a logged
#   truncation (rather than a silent one) is cheap insurance against a future case where it
#   doesn't hold.
CHILD_PROGRESS_THROTTLE_TICKS = 3
MAX_CHILD_PROGRESS_UPDATES_PER_TICK = 100


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parent_rel(rel_path: str) -> str | None:
    if "/" not in rel_path:
        return None
    return rel_path.rsplit("/", 1)[0]


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
    forced_full_rate: bool
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
        # it's ticked once per `tick_s` by `_sample_and_publish_progress()` below, and a
        # second, out-of-cadence call would corrupt that state (a shorter-than-expected `dt`
        # skews the instantaneous rate it derives). This cache is what `list_jobs()` reads
        # instead: whatever the last real tick computed.
        self._last_progress: dict[int, Any] = {}  # job_id -> progress.JobProgress

        # Live per-file (child) progress inside a mirroring directory -- see
        # `_publish_child_progress`. `_progress_tick_count` counts calls to
        # `_sample_and_publish_progress` so child publishing can be throttled to every
        # `CHILD_PROGRESS_THROTTLE_TICKS`-th one. `_prev_child_sizes` is job_id -> {child
        # rel_path (relative to the job's own `local_root`, i.e. the item's directory, not the
        # queue root) -> the size last diffed for it} -- only the rel_paths this module has
        # actually persisted/published, so a child skipped by the cap on one tick still reads
        # as "changed" on the next rather than being silently marked seen. Reset per job_id in
        # `_reap_one` below so a future job id never inherits stale history (same shape as
        # `self.progress.drop`).
        self._progress_tick_count = 0
        self._prev_child_sizes: dict[int, dict[str, int]] = {}
        # Per-child speed (this task, 2026-08-14, "per-file speed inside a mirror"): a real
        # timestamp per (job_id, rel_path) -- `_prev_child_sizes` above only ever recorded the
        # *value* last diffed, never *when*, so there was nothing to divide a byte delta by
        # except the wrong assumption `tick_s * CHILD_PROGRESS_THROTTLE_TICKS`, which a slow
        # pass makes silently wrong (this project has already shipped one bug from exactly that
        # shape of wrong denominator, `6e6b217`). `_prev_child_times` is job_id -> {rel_path ->
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
        """Clear `running` rows left behind by a restart.

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
        """
        cursor = await self.db.execute("SELECT id, item_id FROM job WHERE state = 'running'")
        rows = await cursor.fetchall()
        if not rows:
            return

        logger.warning(
            "clearing %d job(s) left 'running' by a previous run: %s",
            len(rows),
            ", ".join(str(r["id"]) for r in rows),
        )
        await self.db.execute(
            "UPDATE job SET state = 'failed', pid = NULL, error_class = 'INTERRUPTED', "
            "finished_at = ? WHERE state = 'running'",
            (_now_iso(),),
        )
        await self.db.execute(
            "UPDATE item SET state = 'PARTIAL' WHERE state = 'DOWNLOADING' AND id IN "
            "(SELECT item_id FROM job WHERE error_class = 'INTERRUPTED')"
        )
        await self.db.commit()
        for row in rows:
            await self._publish_item_state(row["item_id"])

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

    async def enqueue_item(self, item_id: int, *, forced_full_rate: bool = False) -> int:
        """Manual queue (DESIGN.md §4.7): always wins, clears suppression, resets `attempt`.
        Returns the `job.id` -- a fresh one, or (see below) an existing active one.

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
            item_id, kind=kind, lane=lane, attempt=1, forced_full_rate=forced_full_rate
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
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(rank), 0) AS max_rank FROM job WHERE state = 'queued'"
        )
        row = await cursor.fetchone()
        new_rank = (row["max_rank"] or 0) + 1
        await self.db.execute(
            "UPDATE job SET rank = ? WHERE id = ? AND state = 'queued'", (new_rank, job_id)
        )
        await self.db.commit()
        self.request_tick()

    async def start_now(self, job_id: int) -> bool:
        """The "Start now at max bandwidth" action (DESIGN.md §4.5) — only meaningful for a
        still-queued job; a running job's allocation is fixed at spawn and never re-shaped
        (the invariant this whole scheduler exists to protect), so this is a no-op (returns
        `False`) once a job is already running rather than silently pretending to retune it.
        """
        row = await self._fetch_job(job_id)
        if row is None or row["state"] != "queued":
            return False
        await self.db.execute("UPDATE job SET forced_full_rate = 1 WHERE id = ?", (job_id,))
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
        """
        row = await self._fetch_job(job_id)
        if row is None:
            raise ValueError(f"job {job_id} not found")
        if row["state"] not in ("failed", "cancelled", "succeeded"):
            raise JobNotDismissableError(
                f"job {job_id} is {row['state']!r}; only a failed, cancelled, or succeeded "
                "job can be dismissed"
            )
        await self.db.execute("UPDATE job SET dismissed_at = ? WHERE id = ?", (_now_iso(), job_id))
        await self.db.commit()

    async def dismiss_all_terminal(self) -> int:
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
        this one plain `UPDATE ... WHERE` rather than a second copy of that subquery.

        Returns the actual row count affected (`cursor.rowcount`), the same "report the real
        number" convention `api/history.py`'s clear-history endpoints already use.
        """
        cursor = await self.db.execute(
            "UPDATE job SET dismissed_at = ? "
            "WHERE state IN ('failed','cancelled','succeeded') AND dismissed_at IS NULL",
            (_now_iso(),),
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
            # bytes_done otherwise retains whatever the ~1 Hz progress sampler last measured,
            # which can trail the true final size by a fraction of a tick (found comparing
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
            # downstream (post-processing's verify/delete/extract/move) can touch this job's
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
        every `CHILD_PROGRESS_THROTTLE_TICKS`-th tick, and only for files whose size changed
        since the *previous* throttled tick -- deliberately, so a 50-file release doesn't mean
        50 writes a second (that module's own docstring). But a job can finish *between* two
        throttled ticks, and once it does, nothing ever samples that job's children again --
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
        """The ~1 Hz tick the WS delta fix (DESIGN.md §2/§9) exists for. Two messages come
        out of here, both bounded by `len(self._running)` — the *active* set — never by the
        size of a queue's tree, however many thousand files it holds:

        - `progress` (job-centric, Transfers page): bytes/speed/ETA per running job.
          Unchanged from phase 3a — it was already shaped this way.
        - `item_delta` (item-centric, Files page): the same tick's local-size/state for the
          items those jobs belong to, batched per queue, so a downloading item's row updates
          live instead of waiting for the next full engine scan (up to `scan_interval_s`,
          default 30s) — the gap that made "stop it and see it go STOPPED without a page
          refresh" impossible before this phase.
        - `child_progress` (this task, 2026-08-14, "per-file speed inside a mirror"): a third,
          item-keyed message, `{item_id, speed_bps}` per changed child of a mirroring
          directory — deliberately not folded into either message above. It does not belong in
          `progress`: that message is job-centric and a child has no `job_id` of its own, so a
          pseudo-entry there would collide in the frontend's `progressByJobId` map and put a
          fictional row on the Transfers page. It does not belong in `item_delta` either: that
          message carries `item_view()` projections of persisted `item` columns only
          (DESIGN.md §2/§9's invariant), and a live rate is a sample, never a persisted one —
          see `_publish_child_progress`.

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

        Child (per-file) progress inside a mirroring directory is a separate, throttled pass
        -- see `_publish_child_progress`.
        """
        if not self._running:
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

        # Throttled, not every tick -- see CHILD_PROGRESS_THROTTLE_TICKS's comment. Appends
        # into `by_queue` in place so a child rides the same `item_delta` message as its
        # parent's row instead of a second WS round trip.
        self._progress_tick_count += 1
        if self._progress_tick_count % CHILD_PROGRESS_THROTTLE_TICKS == 0:
            # One real timestamp for every child diffed this pass -- not `tick_s *
            # CHILD_PROGRESS_THROTTLE_TICKS`, which a slow pass would make silently wrong (see
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
        `CHILD_PROGRESS_THROTTLE_TICKS`'s comment), then read back through `ITEM_VIEW_COLUMNS`/
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
        """Feeds `core/metrics.py`'s 30-tick throughput sampler from the exact same per-job
        byte accounting `_sample_and_publish_progress` just wrote above (`self._last_progress`)
        -- never a second measurement, and never lftp's own stdout (DESIGN.md §1.3). Called
        every tick, including ticks where nothing is running (`self._running` empty) --
        `ThroughputSampler.tick()` needs that to keep the heartbeat alive while lftpweb is
        idle, which is what tells an idle instance apart from a stopped one (see that module's
        docstring).
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
            "SELECT job.id, job.item_id, job.lane, job.rank, job.queued_at, job.forced_full_rate "
            "FROM job WHERE job.state = 'queued' ORDER BY job.rank DESC, job.queued_at ASC"
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
        # `rows` is already ordered `rank DESC, queued_at ASC`, so the row kept for admission is
        # the one that would have been served first anyway; the other stays `queued` and is
        # picked up on a later tick, once the running one is no longer active.
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
                    rank=row["rank"],
                    queued_at=row["queued_at"],
                    forced_full_rate=bool(row["forced_full_rate"]),
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
            forced_full_rate=decision.forced_full_rate,
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
            ", start-now" if decision.forced_full_rate else "",
        )

        started_at = _now_iso()
        await self.db.execute(
            "UPDATE job SET state = 'running', pid = ?, started_at = ?, rate_limit_bps = ?, "
            "forced_full_rate = ?, bytes_start = ?, bytes_total = ? WHERE id = ?",
            (
                spawned.pid,
                started_at,
                decision.rate_limit_bps,
                1 if decision.forced_full_rate else 0,
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
        self, item_id: int, *, kind: str, lane: str, attempt: int, forced_full_rate: bool = False
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO job (item_id, kind, state, lane, rank, attempt, forced_full_rate) "
            "VALUES (?, ?, 'queued', ?, 0, ?, ?)",
            (item_id, kind, lane, attempt, 1 if forced_full_rate else 0),
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
        """
        cursor = await self.db.execute(
            "SELECT job.*, item.rel_path, item.is_dir, item.queue_id, item.remote_size, "
            "       item.verified_at, item.extracted_at, item.remote_deleted_at, "
            "       item.arr_status, item.arr_status_at, "
            "       path_queue.name AS queue_name, arr_instance.name AS arr_instance_name "
            "FROM job "
            "JOIN item ON item.id = job.item_id "
            "JOIN path_queue ON path_queue.id = item.queue_id "
            "LEFT JOIN arr_instance ON arr_instance.id = path_queue.arr_instance_id "
            "WHERE job.state IN ('queued','running') "
            "   OR (job.state IN ('failed','cancelled','succeeded') "
            "       AND job.dismissed_at IS NULL "
            "       AND job.id = (SELECT MAX(j2.id) FROM job j2 WHERE j2.item_id = job.item_id)) "
            "ORDER BY job.rank DESC, job.queued_at ASC"
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

    def stats(self, settings: TransferSettings) -> dict:
        current_speed = sum(self._last_speeds.get(job_id, 0.0) for job_id in self._running)
        allocated = sum(p.rate_limit_bps for p in self._running.values())
        return {
            "current_speed_bps": int(current_speed),
            "allocated_bps": int(allocated),
            "ceiling_bps": settings.max_bandwidth_bps,
        }
