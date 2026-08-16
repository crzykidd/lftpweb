// The Transfers row's collapse-to-one-line-plus-expand-panel logic (2026-08-15,
// prompts/2026-08-15-transfers-single-line-rows-with-detail.md). Pure functions, deliberately --
// this project's whole component-testing story is `lib/*.test.ts` (README.md's Known gaps: no
// component rendering is tested), so the *decisions* a row's rendering makes have to be reachable
// without mounting anything, the same discipline `lib/transferTiming.ts` already established for
// elapsed/average-speed/queued-wait.
//
// Before this task, `TransfersPage.tsx`'s `Row` rendered every one of these figures inline --
// queue position, file count, percent, live rate, ETA, allocated rate, elapsed, average speed,
// queued wait, and a post-processing note -- which is what the user's own 2026-08-15 report
// ("crowding the row") is about. None of that information is dropped: it moves into the expand
// panel's three groups (Transfer / Processing / *arr), assembled here.

import type { JobOut } from '../api/types'
import { formatBytes, formatEta, formatPercent, formatRate, formatRelativeTimeIntl } from './format'
import { averageSpeedBps, elapsedSeconds, isNotableQueuedWait, queuedWaitSeconds } from './transferTiming'

/** The live progress reading `useLiveModel.ts`'s `progressByJobId` carries for a running job --
 * the same shape `TransfersPage.tsx`'s `Row` has always taken as a prop, pulled out here so
 * these pure functions don't need the whole hook's return type.
 */
export interface LiveProgress {
  bytes_done: number
  bytes_total: number | null
  speed_bps: number
  eta_s: number | null
}

/**
 * The row's one collapsed line -- the single number that still earns a place beside name/queue/
 * state-word (everything else moves into the panel, per the task's own line-up). While running:
 * percent complete plus the live rate, the two figures that answer "is this actually moving
 * right now." Otherwise (`queued`, or any terminal state): the transfer's own size, the "final
 * size" half of the task's "final size + outcome" instruction -- the *outcome* half is already
 * the state chip sitting right next to this text, so it is not repeated here. `'—'` only when
 * even the size isn't known yet (a `queued`/`failed` job whose remote size was never scanned).
 */
export function transferLineValue(job: JobOut, live?: LiveProgress): string {
  if (job.state === 'running') {
    const bytesDone = live?.bytes_done ?? job.bytes_done
    const bytesTotal = (live?.bytes_total ?? job.bytes_total) ?? job.bytes_total
    const speed = live?.speed_bps ?? job.speed_bps ?? 0
    return `${formatPercent(bytesDone, bytesTotal)} · ${formatRate(speed)}`
  }
  return job.bytes_total != null ? formatBytes(job.bytes_total) : '—'
}

/** One row of the expand panel -- a plain label/value pair, optionally with a longer `title` for
 * hover (the same "chip gets the short form, hover/drawer gets the long one" split
 * `ItemDrawer.tsx`'s own sections already use).
 */
export interface PanelField {
  label: string
  value: string
  title?: string
}

/** The panel's **Transfer** group -- bytes done/total, elapsed, average + current/allocated
 * speed, queued wait (only when notable), and file count: every figure the row used to show
 * inline before this task (`6e6b217`'s elapsed/average-speed/queued-wait addition, and the file
 * count `Row` always had). Failed-job error class + output tail are deliberately not part of
 * this list -- `output_tail` can be many lines of captured lftp output, which does not fit a
 * label/value row; the caller renders that block separately, exactly as the row used to.
 */
export function transferGroupFields(
  job: JobOut,
  opts: { live?: LiveProgress; fileCount: number; nowMs?: number },
): PanelField[] {
  const running = job.state === 'running'
  const bytesDone = running ? (opts.live?.bytes_done ?? job.bytes_done) : job.bytes_done
  const bytesTotal = (running ? opts.live?.bytes_total : job.bytes_total) ?? job.bytes_total
  const currentSpeed = running ? (opts.live?.speed_bps ?? job.speed_bps ?? 0) : null
  const eta = running ? (opts.live?.eta_s ?? job.eta_s) : null

  const elapsed = elapsedSeconds(job.started_at, job.finished_at, opts.nowMs)
  const avgSpeed = averageSpeedBps(bytesDone, job.bytes_start, elapsed)
  const queuedWait = queuedWaitSeconds(job.queued_at, job.started_at)

  const fields: PanelField[] = [
    {
      label: 'Bytes',
      value: `${formatBytes(bytesDone)} / ${bytesTotal != null ? formatBytes(bytesTotal) : '?'} (${formatPercent(bytesDone, bytesTotal)})`,
    },
    { label: 'Files', value: `${opts.fileCount} file${opts.fileCount === 1 ? '' : 's'}` },
  ]
  if (elapsed != null) {
    fields.push({
      label: 'Elapsed',
      value: formatEta(elapsed),
      title: 'Time this job spent running -- started_at to finished_at, or to now while still running',
    })
  }
  if (avgSpeed != null) {
    fields.push({
      label: 'Average speed',
      value: formatRate(avgSpeed),
      title:
        "This attempt's bytes moved, averaged over its own elapsed time -- distinct from the live rate below, which is an EMA-smoothed instantaneous reading (core/progress.py)",
    })
  }
  if (currentSpeed != null) {
    fields.push({
      label: 'Current speed',
      value: eta != null ? `${formatRate(currentSpeed)} (ETA ${formatEta(eta)})` : formatRate(currentSpeed),
    })
  }
  if (job.rate_limit_bps != null) {
    fields.push({
      label: 'Allocated',
      value: formatRate(job.rate_limit_bps),
      title:
        "Allocated at admission (DESIGN.md §4.5) -- held for this job's lifetime regardless of current speed",
    })
  }
  if (isNotableQueuedWait(queuedWait) && queuedWait != null) {
    fields.push({
      label: 'Queued wait',
      value: formatEta(queuedWait),
      title:
        'Time this job waited in the queue before it started running -- often a sign max_concurrent_transfers (DESIGN.md §4.5) was holding it back',
    })
  }
  return fields
}

/** The panel's **Processing** group -- the item-level milestones `JobOut` now carries
 * (2026-08-15: `verified_at`/`extracted_at`/`remote_deleted_at`, the same three
 * `ItemDrawer.tsx.lifecycleChronology` already reads off `FileNode`), each shown only once it
 * has actually happened -- "more post-processing detail than today's single state word" is the
 * task's own bar, and a milestone that hasn't happened yet says nothing useful here (the item's
 * *current* state, already visible as this row's state chip, already covers "still working").
 * The pipeline's own event messages (fetched on demand, on expand -- `GET /api/items/{id}/
 * events`) are threaded in separately by the caller, never here: this function only knows the
 * bounded `JobOut` fields and issues no fetch of its own.
 */
export function processingGroupFields(job: JobOut): PanelField[] {
  const fields: PanelField[] = []
  if (job.verified_at != null) {
    fields.push({
      label: 'Verified',
      value: formatRelativeTimeIntl(job.verified_at),
      title: new Date(job.verified_at).toLocaleString(),
    })
  }
  if (job.extracted_at != null) {
    fields.push({
      label: 'Extracted',
      value: formatRelativeTimeIntl(job.extracted_at),
      title: new Date(job.extracted_at).toLocaleString(),
    })
  }
  if (job.remote_deleted_at != null) {
    fields.push({
      label: 'Remote deleted',
      value: formatRelativeTimeIntl(job.remote_deleted_at),
      title: new Date(job.remote_deleted_at).toLocaleString(),
    })
  }
  return fields
}

/** Whether the panel's ***arr** group should render at all -- "hidden entirely when the queue
 * has no bound instance" (the task's own instruction). `JobOut.arr_instance_name` is the one
 * signal for that: it is `null` exactly when this job's queue has no bound *arr instance
 * (`api/jobs.py._job_out`'s `LEFT JOIN arr_instance`). An instance *can* be bound with
 * `arr_status` still `null` (the poller hasn't matched this item yet) -- that case still shows
 * the group, just with nothing detected yet, since it is the *instance binding*, not whether it
 * has said anything, that gates visibility.
 */
export function hasArrGroup(job: JobOut): boolean {
  return job.arr_instance_name != null
}
