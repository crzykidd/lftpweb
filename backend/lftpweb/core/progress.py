"""Progress without parsing (DESIGN.md §4.4). Samples **only the active set** — the files
under currently-running jobs — at ~1 Hz, never a full tree walk of the queue. Computes
transferred bytes, speed, and ETA itself, EMA-smoothed so the UI doesn't jitter.

This is the module the whole project's central thesis (§1.3) cashes out in: nothing here reads
lftp's stdout. A job's progress is `local bytes on disk vs. its known remote size`, and "local
bytes on disk" is `core/local_scan.py`'s sidecar/temp-suffix math (§4.4a/b) — reused, not
reimplemented, via `local_scan.effective_file_size` for a `pget` job's one file and
`local_scan.scan_local` for a `mirror` job's subtree (itself already restricted to the active
job's own directory, not the whole queue — see the module docstring's "active set" framing).

**§15.9's fallback for pathologically large directories is not implemented in phase 3a.**
`scan_local` on a mirror job's subtree is still a real walk of that one job's files every tick;
what's *not* built yet is the "thousands of files in one job -> stop building the per-file
breakdown, sample the subtree's total only" degradation §15.9 describes. Recorded here rather
than silently skipped: the fake seedbox's directories (a handful of files) never exercise it,
and the phase 3 prompt's scope is the process/scheduler mechanics, not a synthetic
tens-of-thousands-of-files fixture. `JobProgress` and `ProgressSampler` are shaped so that
degradation is a change to `_bytes_done_for` alone when it's needed.

**The per-file breakdown that walk produces is no longer thrown away.** `JobProgress.children`
carries it (mirror jobs only) so `core/queue.py._publish_child_progress` can publish live
per-file progress for the files inside a mirroring directory without a second walk — see that
function's docstring for why this mattered: without it, a child file's `state`/`local_size`
only ever changed on the next full engine scan (`scan_interval_s`, default 30s), so a multi-file
release appeared to sit frozen and then flip to `DOWNLOADED` in one visible batch.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from lftpweb.core import local_scan

JobKind = Literal["mirror", "pget"]

DEFAULT_EMA_ALPHA = 0.3


@dataclass(frozen=True)
class ActiveJob:
    """One currently-running job, as the sampler needs it. `local_root` is the exact file
    path for a `pget` job, or the item's own local directory (queue_local_path/rel_path,
    **not** the mirror-command's parent-directory target — see `core/lftp.py`'s note on that
    distinction) for a `mirror` job. `bytes_total` is the remote size known at admission time;
    `None` means unknown (never seen a remote scan complete) — progress still reports bytes
    done and speed, just no percentage/ETA.
    """

    job_id: int
    kind: JobKind
    local_root: str
    bytes_total: int | None


@dataclass(frozen=True)
class JobProgress:
    job_id: int
    bytes_done: int
    bytes_total: int | None
    speed_bps: float  # EMA-smoothed instantaneous rate; 0 on a job's first sample
    eta_s: float | None  # None when speed is 0 or bytes_total is unknown
    # The per-file breakdown `_bytes_done_for` already walks to build `bytes_done` above, kept
    # alongside it rather than discarded -- see this module's docstring. `None` for a `pget`
    # job (a single file has no children); for a `mirror` job, every entry `scan_local` found
    # under `local_root`, keyed by its `.lftp`-suffix-stripped rel_path (relative to
    # `local_root`, i.e. relative to the *item's* own directory, not the queue root). Directory
    # entries are included too (size always 0, per `local_scan.LocalEntry`) -- it's
    # `core/queue.py`'s job to decide what to do with those, not this module's.
    children: Mapping[str, local_scan.LocalEntry] | None = None


def _bytes_done_for(job: ActiveJob) -> tuple[int, Mapping[str, local_scan.LocalEntry] | None]:
    if job.kind == "pget":
        return local_scan.effective_file_size(job.local_root), None
    entries = local_scan.scan_local(job.local_root)
    total = sum(e.size for e in entries.values() if not e.is_dir)
    return total, entries


class ProgressSampler:
    """Holds per-job EMA state across ticks. One instance lives for the process lifetime
    (owned by `core/queue.py`); `drop()` must be called when a job leaves the active set
    (finished, stopped, failed) so a future job id doesn't inherit stale speed history.
    """

    def __init__(self, alpha: float = DEFAULT_EMA_ALPHA) -> None:
        self._alpha = alpha
        self._prev_bytes: dict[int, int] = {}
        self._prev_time: dict[int, float] = {}
        self._speed: dict[int, float] = {}

    def drop(self, job_id: int) -> None:
        self._prev_bytes.pop(job_id, None)
        self._prev_time.pop(job_id, None)
        self._speed.pop(job_id, None)

    def sample(self, jobs: list[ActiveJob], now: float | None = None) -> dict[int, JobProgress]:
        """One tick over the given active jobs. `now` is injectable (monotonic seconds) so
        this is testable without `time.sleep`; defaults to `time.monotonic()`.
        """
        now = time.monotonic() if now is None else now
        live_ids = {j.job_id for j in jobs}
        for stale_id in set(self._prev_bytes) - live_ids:
            self.drop(stale_id)

        result: dict[int, JobProgress] = {}
        for job in jobs:
            bytes_done, children = _bytes_done_for(job)
            prev_bytes = self._prev_bytes.get(job.job_id)
            prev_time = self._prev_time.get(job.job_id)

            if prev_bytes is not None and prev_time is not None and now > prev_time:
                # Never negative: a resumed job's sidecar can momentarily read lower than the
                # last sample if lftp rewrote it mid-read; treat that as "no progress this
                # tick" rather than a negative speed.
                instantaneous = max(bytes_done - prev_bytes, 0) / (now - prev_time)
                prev_speed = self._speed.get(job.job_id, instantaneous)
                speed = self._alpha * instantaneous + (1 - self._alpha) * prev_speed
            else:
                speed = 0.0  # first sample for this job: no history to derive a rate from

            self._speed[job.job_id] = speed
            self._prev_bytes[job.job_id] = bytes_done
            self._prev_time[job.job_id] = now

            eta_s: float | None = None
            if job.bytes_total is not None and speed > 0:
                remaining = max(job.bytes_total - bytes_done, 0)
                eta_s = remaining / speed

            result[job.job_id] = JobProgress(
                job_id=job.job_id,
                bytes_done=bytes_done,
                bytes_total=job.bytes_total,
                speed_bps=speed,
                eta_s=eta_s,
                children=children,
            )
        return result
