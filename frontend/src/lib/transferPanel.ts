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

import type { HistoryJobOut, HistoryQueueSummaryOut, JobOut } from '../api/types'
import { formatBytes, formatEta, formatPercent, formatRate, formatRelativeTimeIntl } from './format'
import { readLocalStorage, writeLocalStorage } from './storage'
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

/** The panel's terminal-job answer to "how much did this transfer actually move" (2026-08-16,
 * user feedback from live use: the panel showed elapsed time and average speed but never the
 * total bytes moved -- the natural reading they asked for is "14.8 GB in 6m 12s (40 MB/s avg)").
 * Composed from the exact figures `transferGroupFields` below already computes -- `bytesDone`,
 * `elapsed` (`elapsedSeconds`), `avgSpeed` (`averageSpeedBps`) -- through the same
 * `formatBytes`/`formatEta`/`formatRate` every other figure on this page already uses; no new
 * formatter. `null` whenever `elapsed` itself is `null` (the job never started, so there is no
 * duration to report a "bytes in duration" sentence against) -- callers fall back to the plain
 * `Elapsed` field for that case, same as before this task. The trailing "(Z avg)" clause is
 * omitted, not rendered as "(0 B/s avg)", whenever `avgSpeed` is `null` -- `averageSpeedBps`'s own
 * guard against a sub-one-second elapsed time (`MIN_ELAPSED_FOR_RATE_S`), so this function never
 * needs a second divide-by-zero check of its own.
 */
export function transferredSummary(bytesDone: number, elapsed: number | null, avgSpeed: number | null): string | null {
  if (elapsed == null) return null
  const base = `${formatBytes(bytesDone)} in ${formatEta(elapsed)}`
  return avgSpeed != null ? `${base} (${formatRate(avgSpeed)} avg)` : base
}

/** The panel's **Transfer** group -- bytes done/total, elapsed/transferred, average + current/
 * allocated speed, queued wait (only when notable), and file count: every figure the row used to
 * show inline before this task (`6e6b217`'s elapsed/average-speed/queued-wait addition, and the
 * file count `Row` always had). Failed-job error class + output tail are deliberately not part of
 * this list -- `output_tail` can be many lines of captured lftp output, which does not fit a
 * label/value row; the caller renders that block separately, exactly as the row used to.
 *
 * **A terminal job's `Elapsed` and `Average speed` fields collapse into one `Transferred` field**
 * (2026-08-16, `transferredSummary` above) -- once the job is done, "14.8 GB in 6m 12s (40 MB/s
 * avg)" says everything those two separate rows said, without making the reader do the
 * bytes-over-time division themselves. A **running** job keeps the two fields exactly as they
 * were: `Elapsed` is still ticking and `Average speed` is still a live, changing reading, not a
 * settled fact worth folding into one sentence yet (the task's own instruction: "active jobs'
 * fields stay as they are").
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
  const transferred = running ? null : transferredSummary(bytesDone, elapsed, avgSpeed)

  const fields: PanelField[] = [
    {
      label: 'Bytes',
      value: `${formatBytes(bytesDone)} / ${bytesTotal != null ? formatBytes(bytesTotal) : '?'} (${formatPercent(bytesDone, bytesTotal)})`,
    },
    { label: 'Files', value: `${opts.fileCount} file${opts.fileCount === 1 ? '' : 's'}` },
  ]
  if (transferred != null) {
    fields.push({
      label: 'Transferred',
      value: transferred,
      title:
        "This attempt's total bytes moved, over its own elapsed time, with the average speed folded in -- distinct from the live rate below, which is an EMA-smoothed instantaneous reading (core/progress.py)",
    })
  } else if (elapsed != null) {
    fields.push({
      label: 'Elapsed',
      value: formatEta(elapsed),
      title: 'Time this job spent running -- started_at to finished_at, or to now while still running',
    })
  }
  // 2026-08-16: the panel half of "exact timestamp on hover/in the panel" -- the collapsed
  // line's `completedTimeLabel` reading, repeated here since a terminal job's `finished_at`
  // itself was otherwise only *implied* by `Elapsed` (a duration, not a point in time), unlike
  // every other milestone this panel shows (`processingGroupFields`' Verified/Extracted/
  // Remote-deleted, all label/relative-value/exact-title triples).
  const completed = completedTimeLabel(job)
  if (completed != null) {
    fields.push({ label: 'Completed', value: completed.value, title: completed.title })
  }
  if (transferred == null && avgSpeed != null) {
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

/** The states `completedTimeLabel`/`sortTransferRows` below treat as "this job is done, not
 * still moving through the queue" -- the same three terminal states `isDismissable` above
 * already names, kept as its own set here since the two functions' callers (a row's collapsed
 * line, and the whole list's ordering) don't otherwise share an import.
 */
const TERMINAL_JOB_STATES: ReadonlySet<JobOut['state']> = new Set(['succeeded', 'failed', 'cancelled'])

/** A terminal row's completed-time reading for the collapsed line (2026-08-16, user report from
 * live use of the single-line rows this file already builds: "each terminal row should show when
 * it completed"). `value` is the compact relative form (`formatRelativeTimeIntl`, the same
 * reading `processingGroupFields`' Verified/Extracted/Remote-deleted fields already use), `title`
 * the exact timestamp for hover -- same value/title split as every other timestamp on this page.
 * `null` for an active job (queued/running show what they show today, per the task's own
 * instruction -- nothing to add here) and for a terminal job that somehow has no `finished_at`
 * yet (a job reaped without the column being set would be a backend bug, not something to paper
 * over with a fabricated time).
 */
export function completedTimeLabel(job: JobOut): { value: string; title: string } | null {
  if (!TERMINAL_JOB_STATES.has(job.state) || job.finished_at == null) return null
  return {
    value: formatRelativeTimeIntl(job.finished_at),
    title: new Date(job.finished_at).toLocaleString(),
  }
}

/** The Transfers page's row order (2026-08-16, same user report as `completedTimeLabel` above:
 * "the list should sort by that"). **Replaces the previous implicit order for terminal rows**:
 * before this, the whole list -- active and terminal alike -- came straight off `GET /api/jobs`
 * in `core/queue.py.list_jobs`'s own `ORDER BY job.rank DESC, job.queued_at ASC`, which is the
 * *scheduler's* run order and says nothing about when a terminal job actually finished; a job
 * that failed hours ago could sit above one that just succeeded, if the failed one happened to
 * have a higher `rank` or an earlier `queued_at`. This still trusts that same input order for
 * *active* rows (running, then queued in scheduler order) -- untouched, since that ordering is
 * exactly the run order the page's "Queued jobs run in the order shown" copy and Move-to-top
 * button promise -- but terminal rows now sort newest-completed-first instead.
 *
 * A plain `Array.prototype.sort` on the terminal partition is enough for the "stable for
 * ties/missing timestamps" requirement: engines have guaranteed a stable sort since ES2019, so
 * two terminal jobs with the same `finished_at` (or both missing one) keep their relative
 * position from the input array, i.e. `list_jobs`'s own `rank`/`queued_at` order -- a reasonable
 * fallback, and never a re-shuffle on every poll for jobs that didn't actually move.
 */
export function sortTransferRows(jobs: JobOut[]): JobOut[] {
  const running: JobOut[] = []
  const queued: JobOut[] = []
  const terminal: JobOut[] = []
  for (const job of jobs) {
    if (job.state === 'running') running.push(job)
    else if (job.state === 'queued') queued.push(job)
    else terminal.push(job)
  }
  const newestFirst = [...terminal].sort((a, b) => {
    const aTime = a.finished_at != null ? new Date(a.finished_at).getTime() : null
    const bTime = b.finished_at != null ? new Date(b.finished_at).getTime() : null
    if (aTime == null && bTime == null) return 0
    if (aTime == null) return 1 // missing sorts last
    if (bTime == null) return -1
    return bTime - aTime // newest first
  })
  return [...running, ...queued, ...newestFirst]
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

// --- Group by queue (2026-08-16, prompts/2026-08-16-transfers-group-by-queue.md): "per-row
// queue labels make the page busy" -- the queue name/summary moves to a collapsible group
// header, one per queue, so individual rows can stop repeating it. ------------------------

/** One queue's rows, in the order the caller passed them in -- always call this on
 * `sortTransferRows`'s own output, so a group's `jobs` keep that exact within-group order
 * (active rows in scheduler order, terminal rows newest-completed-first) without this function
 * re-deriving or disturbing it. Only queues with at least one visible job produce a group, and
 * groups themselves are ordered by queue name -- both per the task's own instruction.
 */
export interface QueueGroup {
  queueId: number
  queueName: string
  jobs: JobOut[]
}

export function groupJobsByQueue(jobs: JobOut[]): QueueGroup[] {
  const groups = new Map<number, QueueGroup>()
  for (const job of jobs) {
    let group = groups.get(job.queue_id)
    if (!group) {
      group = { queueId: job.queue_id, queueName: job.queue_name, jobs: [] }
      groups.set(job.queue_id, group)
    }
    group.jobs.push(job)
  }
  return [...groups.values()].sort((a, b) => a.queueName.localeCompare(b.queueName))
}

/** A group header's job counts by outcome. The task's own instruction names four buckets --
 * active (running), queued, succeeded, failed -- but a `cancelled` (Stopped) row is a real fifth
 * outcome `isDismissable`/`chipStateFor` already both treat as first-class (a stopped job sits in
 * this same group until dismissed); dropping it from the header's counts would make them not sum
 * to the group's own row count, silently hiding rather than naming it (docs/decisions.md). It
 * follows `failed` in `formatQueueGroupCounts`'s enumeration order below and is omitted at zero
 * exactly like every other bucket.
 */
export interface QueueGroupCounts {
  active: number
  queued: number
  succeeded: number
  failed: number
  stopped: number
}

function outcomeCounts(jobs: JobOut[]): QueueGroupCounts {
  const counts: QueueGroupCounts = { active: 0, queued: 0, succeeded: 0, failed: 0, stopped: 0 }
  for (const job of jobs) {
    switch (job.state) {
      case 'running':
        counts.active += 1
        break
      case 'queued':
        counts.queued += 1
        break
      case 'succeeded':
        counts.succeeded += 1
        break
      case 'failed':
        counts.failed += 1
        break
      case 'cancelled':
        counts.stopped += 1
        break
    }
  }
  return counts
}

/** A group header's aggregate figures -- counts by outcome, the group's total size (sum of
 * `bytes_done`, per the task's own instruction), and its combined current rate while anything in
 * the group is downloading (`null` otherwise, so the header can omit the rate entirely rather
 * than show a stale/zero one). Reads live progress the same way `transferLineValue`/
 * `transferGroupFields` already do for a single row -- `liveByJobId[job.id]`'s reading takes
 * priority over the job's own last-polled figures while `running`, falling back to the job's own
 * figures for every other state (or if no live sample has arrived yet).
 */
export interface QueueGroupSummary {
  counts: QueueGroupCounts
  totalBytesDone: number
  combinedRateBps: number | null
}

export function queueGroupSummary(
  jobs: JobOut[],
  liveByJobId: Record<number, LiveProgress | undefined>,
): QueueGroupSummary {
  let totalBytesDone = 0
  let combinedRateBps = 0
  let anyRunning = false
  for (const job of jobs) {
    const live = liveByJobId[job.id]
    const running = job.state === 'running'
    totalBytesDone += running ? (live?.bytes_done ?? job.bytes_done) : job.bytes_done
    if (running) {
      anyRunning = true
      combinedRateBps += live?.speed_bps ?? job.speed_bps ?? 0
    }
  }
  return { counts: outcomeCounts(jobs), totalBytesDone, combinedRateBps: anyRunning ? combinedRateBps : null }
}

const COUNT_LABELS: ReadonlyArray<[keyof QueueGroupCounts, string]> = [
  ['active', 'active'],
  ['queued', 'queued'],
  ['succeeded', 'succeeded'],
  ['failed', 'failed'],
  ['stopped', 'stopped'],
]

/** The counts half of a group header's one line -- e.g. `"2 active, 3 queued"` -- omitting any
 * bucket that's zero (the task's own instruction, "to keep it quiet"). `''` when every bucket is
 * zero (a queue with no visible jobs never produces a group in the first place, per
 * `groupJobsByQueue`, so this is mostly a defensive empty string rather than a case that occurs
 * in practice).
 */
export function formatQueueGroupCounts(counts: QueueGroupCounts): string {
  return COUNT_LABELS.filter(([key]) => counts[key] > 0)
    .map(([key, label]) => `${counts[key]} ${label}`)
    .join(', ')
}

// --- Per-queue collapse persistence -- keyed by queue id, default expanded, survives reload
// and a queue's own temporary disappearance from the payload (the task's own instruction: a
// collapsed queue that scans to zero visible jobs and drops out keeps its stored preference for
// when it returns, since this map is never pruned against the currently-live queue list). ---
//
// 2026-08-16 (prompts/2026-08-16-history-jobs-group-collapse.md): the History page's jobs
// section reuses this exact map shape and read/write logic for its own per-queue collapse, but
// under a *different* storage key -- "a queue collapsed on Transfers is not implicitly collapsed
// on History" (the task's own instruction; the two pages' groups aren't even the same rows --
// Transfers is active+queued, History is terminal-only). `readCollapsedQueues`/
// `writeCollapsedQueues` below stay as the Transfers-specific names already wired into
// `TransfersPage.tsx`; `readCollapsedQueuesFor`/`writeCollapsedQueuesFor` below take the storage
// key explicitly so both pages share one implementation rather than two copies.

const TRANSFERS_COLLAPSED_QUEUES_KEY = 'transfers.collapsedQueues'
export const HISTORY_COLLAPSED_QUEUES_KEY = 'history.collapsedQueues'

/** JSON object keys are always strings on the wire/in storage (same fact `useLiveModel.ts`'s own
 * per-queue-bytes map comment notes) -- so this is keyed by `String(queueId)`, not `queueId`
 * itself. Presence with `true` means collapsed; anything else (absent key, or a stray `false`
 * left over from an older write) reads as expanded -- the task's own stated default.
 */
export type QueueCollapseMap = Record<string, boolean>

export function isQueueCollapseMap(value: unknown): value is QueueCollapseMap {
  if (typeof value !== 'object' || value == null) return false
  return Object.values(value as Record<string, unknown>).every((v) => typeof v === 'boolean')
}

export function isQueueCollapsed(map: QueueCollapseMap, queueId: number): boolean {
  return map[String(queueId)] === true
}

/** Never stores an explicit `false` -- an expanded (the default) queue simply has no entry, same
 * "default plus exceptions" shape `fileTree.ts`'s own `CollapsePreference` uses for the Files
 * page, just without that one's separate default flag (every queue defaults expanded here, so a
 * plain per-id exception set is enough).
 */
export function withQueueCollapsed(map: QueueCollapseMap, queueId: number, collapsed: boolean): QueueCollapseMap {
  const next = { ...map }
  if (collapsed) next[String(queueId)] = true
  else delete next[String(queueId)]
  return next
}

function readCollapsedQueuesFor(key: string): QueueCollapseMap {
  return readLocalStorage(key, isQueueCollapseMap) ?? {}
}

function writeCollapsedQueuesFor(key: string, map: QueueCollapseMap): void {
  writeLocalStorage(key, map)
}

export function readCollapsedQueues(): QueueCollapseMap {
  return readCollapsedQueuesFor(TRANSFERS_COLLAPSED_QUEUES_KEY)
}

export function writeCollapsedQueues(map: QueueCollapseMap): void {
  writeCollapsedQueuesFor(TRANSFERS_COLLAPSED_QUEUES_KEY, map)
}

export function readHistoryCollapsedQueues(): QueueCollapseMap {
  return readCollapsedQueuesFor(HISTORY_COLLAPSED_QUEUES_KEY)
}

export function writeHistoryCollapsedQueues(map: QueueCollapseMap): void {
  writeCollapsedQueuesFor(HISTORY_COLLAPSED_QUEUES_KEY, map)
}

// --- History jobs section's own group header aggregate (2026-08-16) -- the server-computed,
// filter-honest `HistoryQueueSummaryOut` (api/history.py._queue_summaries), formatted for the
// group header the same way `formatQueueGroupCounts` already formats Transfers' client-computed
// `QueueGroupCounts`. Reuses that exact function rather than a parallel formatter -- History's
// three outcome buckets (succeeded/failed/cancelled) are a subset of Transfers' five, so mapping
// onto `QueueGroupCounts` with `active`/`queued` pinned at 0 (never rendered, since
// `formatQueueGroupCounts` omits zero counts) gets identical output for identical meaning with no
// new formatting logic to maintain in parallel. -----------------------------------------------

/** `HistoryQueueSummaryOut`'s counts, reshaped onto `QueueGroupCounts` so
 * `formatQueueGroupCounts` can render them -- `cancelled` maps to the `stopped` bucket, matching
 * `HistoryJobsSection.tsx.chipStateFor`'s own `cancelled -> STOPPED` reading. `active`/`queued`
 * are always 0: History's whole domain is terminal jobs (`_TERMINAL_JOB_STATES`), so those two
 * buckets never apply here and never render (zero counts are omitted).
 */
export function historyQueueGroupCounts(summary: HistoryQueueSummaryOut): QueueGroupCounts {
  return {
    active: 0,
    queued: 0,
    succeeded: summary.succeeded,
    failed: summary.failed,
    stopped: summary.cancelled,
  }
}

// --- History jobs section: flattened-array grouping + collapse filtering, and the local
// summary update a single-row clear applies without a full reload (2026-08-16). Pulled out of
// `HistoryJobsSection.tsx` into this module -- the same "reuse them; extend a helper if History
// needs a variant, in place" instruction the task itself gives, and this project's whole
// component-testing story is pure functions in `lib/*.test.ts` (README.md's Known gaps: no
// component rendering is tested), so this logic has to be reachable without mounting anything,
// same as everything else in this file. ---------------------------------------------------

export type HistoryVirtualRow =
  | { kind: 'header'; queueId: number; queueName: string }
  | { kind: 'job'; job: HistoryJobOut }

/** Flattens the already-filtered/paginated History jobs page into queue-grouped sections
 * (DESIGN.md §9.2: "grouped by queue") as one array `HistoryJobsSection.tsx`'s virtualizer can
 * walk -- see docs/decisions.md for why header rows interleaved into one flat, virtualized list
 * was chosen over nested per-queue virtualizers. `collapsedQueues` filters a collapsed queue's
 * *job* rows out of the array -- the header row itself always stays (it's the only remaining way
 * to expand the group again), so the virtualizer never has to know collapse exists: it just sees
 * a shorter array, the same trick `TransfersPage.tsx`'s own (non-virtualized) `groups.map` uses.
 */
export function groupHistoryJobsByQueue(
  jobs: HistoryJobOut[],
  collapsedQueues: QueueCollapseMap,
): HistoryVirtualRow[] {
  const rows: HistoryVirtualRow[] = []
  let currentQueueId: number | null = null
  let currentCollapsed = false
  for (const job of jobs) {
    if (job.queue_id !== currentQueueId) {
      currentQueueId = job.queue_id
      currentCollapsed = isQueueCollapsed(collapsedQueues, job.queue_id)
      rows.push({ kind: 'header', queueId: job.queue_id, queueName: job.queue_name })
    }
    if (!currentCollapsed) rows.push({ kind: 'job', job })
  }
  return rows
}

/** The local-update counterpart to the `jobs`/`total` state trimming `HistoryJobsSection.tsx`'s
 * `confirmClear` already does for a single-row clear -- keeps a just-cleared job's queue summary
 * in sync without a full reload, decrementing exactly the one outcome bucket that job belonged to
 * and its `total_bytes_done`. `Math.max(0, ...)` guards against a summary that's already stale
 * (e.g. a second tab cleared the same row first) producing a negative count.
 */
export function decrementHistoryQueueSummary(
  summaries: HistoryQueueSummaryOut[],
  job: HistoryJobOut,
): HistoryQueueSummaryOut[] {
  return summaries.map((s) => {
    if (s.queue_id !== job.queue_id) return s
    const next = { ...s, total_bytes_done: Math.max(0, s.total_bytes_done - job.bytes_done) }
    if (job.state === 'succeeded') next.succeeded = Math.max(0, next.succeeded - 1)
    else if (job.state === 'failed') next.failed = Math.max(0, next.failed - 1)
    else if (job.state === 'cancelled') next.cancelled = Math.max(0, next.cancelled - 1)
    return next
  })
}
