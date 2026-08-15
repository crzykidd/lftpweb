// How long a transfer took, and what speed it achieved -- derived, not carried on the wire
// (2026-08-14, prompts/2026-08-14-transfer-timing-and-throughput-display.md). Every job already
// has `queued_at`/`started_at`/`finished_at`/`bytes_done`/`bytes_total` (`api/jobs.py.JobOut`,
// `api/history.py.HistoryJobOut`), but nothing derived "49 seconds, ~34 MB/s" from them --
// a real debugging session had to reconstruct that by hand from two ISO timestamps. Pure
// functions, deliberately: `TransfersPage.test.ts`/`lib/*.test.ts` is this project's whole
// component-testing story (README.md's Known gaps -- no component rendering is tested), so
// anything worth covering has to be reachable without mounting anything.
//
// Deliberately does **not** use `Date` parsing tricks beyond `Date.parse`/`new Date(...)`; every
// timestamp here is one of this project's own ISO-8601 UTC strings (the same convention
// `api/history.py`'s `since`/`until` filters and `lib/format.ts`'s relative-time helpers already
// rely on).

/** Below this many seconds, a computed rate is discarded rather than shown (`averageSpeedBps`
 * below) -- the task's own guard: "a sub-second elapsed time must not produce a divide-by-zero
 * or an absurd rate." A handful of bytes over a few hundred milliseconds extrapolates to a
 * nominal rate nothing actually sustained; 1s is the same rounding granularity `formatEta`
 * already renders at (`lib/format.ts`), so nothing here claims more precision than the UI shows.
 */
const MIN_ELAPSED_FOR_RATE_S = 1

/** Below this many seconds, "time spent queued" is not surfaced at all -- the task's own
 * instruction: "Show it when it is non-trivial rather than always." Ordinary admission latency
 * (a scheduler tick, a just-freed slot) is well under this; a wait long enough to be worth
 * explaining is what `max_concurrent_transfers` admission control (DESIGN.md §4.5) actually
 * produces when it's genuinely holding a job back.
 */
const NOTABLE_QUEUE_WAIT_S = 5

/** Elapsed time for a job, in seconds -- `null` for a job that hasn't started yet (`startedAt ==
 * null`, i.e. still `queued`: there is nothing to measure elapsed *of*). For a job that has
 * started but not finished, pass the caller's own "now" (`nowMs`, defaulting to `Date.now()`)
 * so a running row ticks forward on every re-render rather than freezing at whatever instant this
 * module first ran -- `TransfersPage.tsx`'s `Row` already re-renders roughly once a second while
 * a job is running (the WS `progress` message drives `progressByJobId`), so no second timer is
 * needed here; this function just reads whatever "now" its caller supplies at render time.
 * Clamped to zero: a `finished_at` at or before `started_at` (possible at whole-second
 * granularity, or under minor clock skew) reads as "0s," never a negative duration.
 */
export function elapsedSeconds(
  startedAt: string | null,
  finishedAt: string | null,
  nowMs: number = Date.now(),
): number | null {
  if (startedAt == null) return null
  const startMs = Date.parse(startedAt)
  const endMs = finishedAt != null ? Date.parse(finishedAt) : nowMs
  return Math.max((endMs - startMs) / 1000, 0)
}

/** How long a job sat queued before it started, in seconds -- `startedAt - queuedAt`. `null`
 * while the job is still `queued` (`startedAt == null`): "time spent queued" is a completed
 * fact about a job that has since started, not a running total for one still waiting (which
 * would need its own, different sentence -- out of this task's scope). Clamped to zero for the
 * same reason `elapsedSeconds` is.
 */
export function queuedWaitSeconds(queuedAt: string, startedAt: string | null): number | null {
  if (startedAt == null) return null
  return Math.max((Date.parse(startedAt) - Date.parse(queuedAt)) / 1000, 0)
}

/** Whether a queued-wait reading is worth showing at all -- see `NOTABLE_QUEUE_WAIT_S`. `null`
 * (nothing to show yet, or the job never started) is never notable.
 */
export function isNotableQueuedWait(waitSeconds: number | null): boolean {
  return waitSeconds != null && waitSeconds >= NOTABLE_QUEUE_WAIT_S
}

/** The average throughput a job (or one attempt of one, in the item drawer's history list)
 * achieved over its own elapsed time -- **distinct from `speed_bps`**, the EMA-smoothed
 * instantaneous rate `core/progress.py` publishes over the WebSocket. Both numbers are useful
 * and they must never be presented as the same thing (the task's own bar) -- callers label this
 * one "average," never substitute it for the live reading.
 *
 * `bytesStart` matters: `job.bytes_done` is the *absolute* local footprint at last measurement,
 * not a per-job delta -- a resumed job's `bytes_done` already includes whatever an *earlier*
 * attempt left on disk before this job even started (`core/metrics.py`'s own module docstring,
 * "the non-monotonic trap," documents exactly this for the Dashboard's throughput sampler; the
 * same trap applies here). Passing `job.bytes_start` (only bytes *this* job actually moved:
 * `max(bytesDone - bytesStart, 0)`) avoids overstating the rate on a resumed transfer. Pass `0`
 * only when the caller genuinely has no `bytes_start` to give (see `ItemDrawer.tsx`'s history
 * rows, whose `HistoryJobOut` shape doesn't carry it -- documented at that call site, not silently
 * here).
 *
 * `null` whenever `elapsedS` is `null` or below `MIN_ELAPSED_FOR_RATE_S` -- guards both the
 * divide-by-zero at `elapsedS === 0` and the absurd-rate case just above it.
 */
export function averageSpeedBps(bytesDone: number, bytesStart: number, elapsedS: number | null): number | null {
  if (elapsedS == null || elapsedS < MIN_ELAPSED_FOR_RATE_S) return null
  const moved = Math.max(bytesDone - bytesStart, 0)
  return moved / elapsedS
}

/** The state label a `succeeded` job's row should show if the underlying item is still being
 * post-processed (2026-08-14, this task's item 3 -- a regression in perceived behaviour from
 * `prompts/done/2026-08-14-exit-zero-is-not-completion.md`: `list_jobs()` keeps a recently
 * `succeeded` job visible, but `core/postprocess.py`'s verify/extract steps can still be running
 * against the same item, which otherwise reads as a stalled 100%/0-B/s transfer). Gated on the
 * *job's* state being `succeeded`, not the item's: a `VERIFYING` item behind a still-`running`
 * job (verification of a partially-resumed local copy, e.g.) is a different situation this label
 * is not for. Deliberately just a short state word, never a fabricated progress bar -- the task's
 * own instruction -- because post-processing has no byte-level progress this codebase measures.
 */
export function postprocessNote(jobState: string, itemState: string | null | undefined): string | null {
  if (jobState !== 'succeeded') return null
  if (itemState === 'VERIFYING') return 'Verifying…'
  if (itemState === 'EXTRACTING') return 'Extracting…'
  return null
}
