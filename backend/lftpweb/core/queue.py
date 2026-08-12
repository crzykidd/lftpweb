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

from lftpweb.core import lftp, patterns, scheduler
from lftpweb.core.events import EventBus
from lftpweb.core.progress import ActiveJob, ProgressSampler
from lftpweb.core.remote import HostConfig

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_BASE_S = 30.0
DEFAULT_RETRY_BACKOFF_MAX_S = 15 * 60.0

# Transient vs. permanent (DESIGN.md §4.3) — the same whitelist `core/lftp.py` uses for the
# same reason (see its module docstring): only retry what we can positively identify as
# transient, never "everything that isn't in the permanent list."
PERMANENT_ERROR_CLASSES = frozenset(
    {"AUTH_FAILED", "PERMISSION_DENIED", "REMOTE_GONE", "DISK_FULL"}
)


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
        self._last_speeds: dict[int, float] = {}  # job_id -> most recent EMA speed, for stats()
        # `list_jobs()` (an API read path, polled on whatever cadence the frontend chooses)
        # must never call `self.progress.sample()` itself — the sampler's EMA math assumes
        # it's ticked once per `tick_s` by `_sample_and_publish_progress()` below, and a
        # second, out-of-cadence call would corrupt that state (a shorter-than-expected `dt`
        # skews the instantaneous rate it derives). This cache is what `list_jobs()` reads
        # instead: whatever the last real tick computed.
        self._last_progress: dict[int, Any] = {}  # job_id -> progress.JobProgress

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
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for proc in list(self._running.values()):
            await lftp.terminate(proc.spawned, grace_s=10.0)
            proc.spawned.cleanup()

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
        Returns the new `job.id`.
        """
        item = await self._fetch_item(item_id)
        if item is None:
            raise ValueError(f"item {item_id} not found")
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

    # --- one scheduling tick -------------------------------------------------------------

    async def tick(self) -> None:
        await self._reap_finished()
        await self._sample_and_publish_progress()
        await self._admit()

    async def _reap_finished(self) -> None:
        for proc in [p for p in self._running.values() if p.wait_task.done()]:
            await self._reap_one(proc)

    async def _reap_one(self, proc: _RunningProcess) -> None:
        if proc.job_id not in self._running:
            return  # already reaped (stop_job() and the tick loop can race on the same proc)
        del self._running[proc.job_id]
        self.progress.drop(proc.job_id)
        self._last_speeds.pop(proc.job_id, None)
        self._last_progress.pop(proc.job_id, None)

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
            logger.info(
                "job %s succeeded: %s (%s bytes)", proc.job_id, proc.rel_path, proc.bytes_total
            )
            await self.db.execute(
                "UPDATE job SET state = 'succeeded', pid = NULL, exit_code = 0, output_tail = NULL, "
                "bytes_done = COALESCE((SELECT remote_size FROM item WHERE item.id = job.item_id), bytes_done), "
                "finished_at = ? WHERE id = ?",
                (finished_at, proc.job_id),
            )
            # No inference (DESIGN.md §4.3): `cmd:fail-exit true` makes exit 0 mean the whole
            # transfer succeeded, so the item is DOWNLOADED now, not "probably, pending the
            # next scan." The engine's own reconcile pass will confirm sizes on its next
            # cycle; this is what makes the UI update immediately instead of waiting for it.
            await self.db.execute(
                "UPDATE item SET state = 'DOWNLOADED', downloaded_at = ?, "
                "auto_queue_suppressed = 0, suppressed_reason = NULL, error_class = NULL, error_detail = NULL "
                "WHERE id = ?",
                (finished_at, proc.item_id),
            )
            await self.db.commit()
            await self._publish_item_state(proc.item_id)
            proc.spawned.cleanup()
            # Phase 5 (DESIGN.md §6): "triggered on transition to DOWNLOADED." Only for a
            # top-level item — the same eligibility shape core/autoqueue.py uses — since a
            # queued job is always for a top-level item (a whole release via `mirror`, or a
            # loose top-level file via `pget`) and postprocessing operates on the release as
            # a whole, not once per nested file/subdirectory item.
            if self.postprocess is not None and "/" not in proc.rel_path:
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

        max_attempts = (await load_transfer_settings(self.db)).max_attempts
        current_attempt = await self._current_attempt(proc.job_id)
        can_retry = error_class in lftp.TRANSIENT_ERROR_CLASSES and current_attempt < max_attempts
        if can_retry:
            backoff = min(
                DEFAULT_RETRY_BACKOFF_BASE_S * (2 ** (current_attempt - 1)),
                DEFAULT_RETRY_BACKOFF_MAX_S,
            )
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

        by_queue: dict[int, list[dict[str, Any]]] = {}
        for p in self._running.values():
            prog = results.get(p.job_id)
            if prog is None:
                continue
            await self.db.execute(
                "UPDATE item SET local_size = ? WHERE id = ?", (prog.bytes_done, p.item_id)
            )
            by_queue.setdefault(p.queue_id, []).append(
                {
                    "id": p.item_id,
                    "rel_path": p.rel_path,
                    "is_dir": p.is_dir,
                    "state": "DOWNLOADING",
                    "remote_size": p.bytes_total,
                    "local_size": prog.bytes_done,
                    "remote_mtime": p.remote_mtime,
                }
            )
        await self.db.commit()
        self._last_speeds = {job_id: prog.speed_bps for job_id, prog in results.items()}
        self._last_progress = results

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
        for row in rows:
            if row["id"] in self._running:
                continue
            if now < self._backoff_until.get(row["item_id"], 0.0):
                continue
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
        queue_row = await self._fetch_queue(item["queue_id"])
        if queue_row is None:
            return

        remote_full = queue_row["remote_path"].rstrip("/") + "/" + item["rel_path"]
        if job_row["kind"] == "pget":
            local_full = queue_row["local_path"].rstrip("/") + "/" + item["rel_path"]
            local_root_for_progress = local_full
            mkdir_target = _parent_rel_path(local_full)
        else:
            parent = _parent_rel(item["rel_path"])
            local_parent = queue_row["local_path"].rstrip("/") + (f"/{parent}" if parent else "")
            local_full = local_parent  # core/lftp.py: mirror's target is the *parent* dir
            local_root_for_progress = queue_row["local_path"].rstrip("/") + "/" + item["rel_path"]
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
            "forced_full_rate = ?, bytes_start = ? WHERE id = ?",
            (
                spawned.pid,
                started_at,
                decision.rate_limit_bps,
                1 if decision.forced_full_rate else 0,
                item["local_size"] or 0,
                decision.job_id,
            ),
        )
        await self.db.execute("UPDATE item SET state = 'DOWNLOADING' WHERE id = ?", (item["id"],))
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
                "nodes": [
                    {
                        "id": row["id"],
                        "rel_path": row["rel_path"],
                        "is_dir": bool(row["is_dir"]),
                        "state": row["state"],
                        "remote_size": row["remote_size"],
                        "local_size": row["local_size"],
                        "remote_mtime": float(row["remote_mtime"])
                        if row["remote_mtime"] is not None
                        else None,
                    }
                ],
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
        if row is None or not row["connection_overrides"]:
            return None
        try:
            overrides = json.loads(row["connection_overrides"])
        except (ValueError, TypeError):
            return None
        value = overrides.get("net:connection-limit") or overrides.get("connection_limit")
        return int(value) if value else None

    # --- read models for api/jobs.py --------------------------------------------------------

    async def list_jobs(self) -> list[dict]:
        """The Transfers page's row set (DESIGN.md §9.2). Not just `queued`/`running`:
        §9.2 explicitly requires "failed rows show the error class and the captured lftp
        output tail", and a stopped job must be visible as `STOPPED` rather than vanishing
        the instant it's reaped — DESIGN.md names the Transfers page as where both surface,
        distinct from the History page's full `job`/`event` audit trail. So this also
        includes a `failed`/`cancelled` job when it is that item's *most recent* job (the
        `MAX(id)` subquery) — a manual retry inserts a new `queued` row for the same item,
        which is already covered by the first clause and makes the old failed/cancelled row
        irrelevant (superseded), so this doesn't need to filter it out separately.
        """
        cursor = await self.db.execute(
            "SELECT job.*, item.rel_path, item.is_dir, item.queue_id, item.remote_size "
            "FROM job JOIN item ON item.id = job.item_id "
            "WHERE job.state IN ('queued','running') "
            "   OR (job.state IN ('failed','cancelled') "
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
