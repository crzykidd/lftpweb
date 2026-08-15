import { useMemo, useState } from 'react'
import { dismissJob, moveJobToTop, retryItem, startJobNow, stopJob } from '../api/client'
import type { FileNode, JobOut } from '../api/types'
import { ItemDrawer } from '../components/ItemDrawer'
import { StateChip } from '../components/StateChip'
import { useJobs } from '../hooks/useJobs'
import { useLiveModel } from '../hooks/useLiveModel'
import { formatEta, formatPercent, formatRate } from '../lib/format'
import { averageSpeedBps, elapsedSeconds, isNotableQueuedWait, postprocessNote, queuedWaitSeconds } from '../lib/transferTiming'

const START_NOW_EXPLAINED_KEY = 'lftpweb:startNowExplained'

/** DESIGN.md §9.2's three-word visible vocabulary, mapped onto the actual job/item states
 * that show up on this page (broader than just queued/running -- see
 * `core/queue.py.list_jobs`'s docstring and docs/decisions.md). `STOPPED`/`FAILED` surface
 * verbatim, exactly the "other internal states surface only on rows where they actually
 * apply" rule §9.2 states, rather than being folded into one of the three.
 */
export function chipStateFor(job: JobOut): string {
  switch (job.state) {
    case 'queued':
      return 'QUEUED'
    case 'running':
      return 'DOWNLOADING'
    case 'succeeded':
      return 'DOWNLOADED'
    case 'cancelled':
      return 'STOPPED'
    case 'failed':
      return 'FAILED'
    default:
      return job.state
  }
}

/** Whether a job's Dismiss button (2026-08-13, `core/queue.py.dismiss_job`) should show for a
 * row in this state -- must match that endpoint's own guard (`JobNotDismissableError`) exactly,
 * or a click here would just surface a 409. `succeeded` joined 2026-08-14
 * (prompts/2026-08-14-exit-zero-is-not-completion.md) alongside `list_jobs()` starting to
 * surface a recently-succeeded job on this page at all -- a completed transfer needs the same
 * "stop showing this row" action a failed or stopped one already had. Exported as its own pure
 * function (rather than inlined in `Row`) so it can be unit-tested without mounting the
 * component -- this project doesn't test component rendering (README.md's Known gaps).
 */
export function isDismissable(state: JobOut['state']): boolean {
  return state === 'failed' || state === 'cancelled' || state === 'succeeded'
}

function fileCountFor(nodes: FileNode[], job: JobOut): number {
  if (!job.is_dir) return 1
  const prefix = `${job.rel_path}/`
  return nodes.filter((n) => !n.is_dir && n.rel_path.startsWith(prefix)).length
}

function itemName(relPath: string): string {
  const lastSlash = relPath.lastIndexOf('/')
  return lastSlash === -1 ? relPath : relPath.slice(lastSlash + 1)
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

/** "Clear all failed" (2026-08-13, prompts/done/2026-08-13-dismiss-terminal-jobs.md) --
 * `Promise.allSettled`, not `Promise.all`, same reasoning `FileTree.tsx`'s bulk actions
 * already use: one job's dismiss failing (e.g. it started running between page load and the
 * click) must not hide whether the other N-1 succeeded.
 */
interface DismissFailure {
  rel_path: string
  name: string
  error: string
}

interface DismissOutcome {
  total: number
  succeeded: number
  failures: DismissFailure[]
}

interface RowProps {
  job: JobOut
  nodes: FileNode[]
  live: { bytes_done: number; bytes_total: number | null; speed_bps: number; eta_s: number | null } | undefined
  // Where this job sits in the actual run order (2026-08-13, prompts/2026-08-13-files-ux-pass.md
  // item 4) -- 1, 2, 3... counting only `state === 'queued'` rows, in the order `useJobs` already
  // returns them (`core/queue.py.list_jobs`'s own `ORDER BY job.rank DESC, job.queued_at ASC`,
  // the real future run order). `undefined` for a running/failed/cancelled row -- those aren't
  // "queued" in the sense a position means anything for.
  queuePosition: number | undefined
  onOpenDrawer: (job: JobOut) => void
  onMoveToTop: (job: JobOut) => void
  onStartNow: (job: JobOut) => void
  onStop: (job: JobOut) => void
  onRetry: (job: JobOut) => void
  onDismiss: (job: JobOut) => void
  busy: boolean
}

function Row({
  job,
  nodes,
  live,
  queuePosition,
  onOpenDrawer,
  onMoveToTop,
  onStartNow,
  onStop,
  onRetry,
  onDismiss,
  busy,
}: RowProps) {
  const running = job.state === 'running'
  const bytesDone = running ? (live?.bytes_done ?? job.bytes_done) : job.bytes_done
  const bytesTotal = (running ? live?.bytes_total : job.bytes_total) ?? job.bytes_total
  const speed = running ? (live?.speed_bps ?? job.speed_bps ?? 0) : 0
  const eta = running ? (live?.eta_s ?? job.eta_s) : null

  // Elapsed / average speed / queued wait (2026-08-14,
  // prompts/2026-08-14-transfer-timing-and-throughput-display.md) -- "how long it took" and
  // "what speed that works out to," derived from the job's own timestamps rather than left for
  // a reader to reconstruct by hand from two ISO strings. For a running job, `elapsedSeconds`
  // measures against `Date.now()` at render time -- no new timer: this row already re-renders
  // roughly once a second while running, driven by the WS `progress` message that updates
  // `live` (`useLiveModel.ts`).
  const elapsed = elapsedSeconds(job.started_at, job.finished_at)
  // `bytesDone` here (not `job.bytes_done`) so a running job's average uses the same freshest
  // reading its percentage/ETA already do; `job.bytes_start` has no live counterpart -- it's
  // fixed at spawn (`core/queue.py`'s admission), so the job's own value is always current.
  const avgSpeed = averageSpeedBps(bytesDone, job.bytes_start, elapsed)
  const queuedWait = queuedWaitSeconds(job.queued_at, job.started_at)
  // The item's own state (`core/postprocess.py`'s VERIFYING/EXTRACTING, published via the same
  // `item_delta`/`snapshot` WS messages this page already merges into `nodes` -- no new fetch,
  // no new plumbing) is the honest source for "what is a finished-but-still-working row doing."
  const itemNode = nodes.find((n) => n.id === job.item_id)
  const postprocess = postprocessNote(job.state, itemNode?.state)
  const showTimingRow = elapsed != null || avgSpeed != null || isNotableQueuedWait(queuedWait) || postprocess != null

  return (
    <div className="flex flex-col gap-2 border-b border-zinc-200 px-3 py-2.5 text-sm last:border-b-0 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-3">
        {/* Queue position (2026-08-13, item 4): "what is the proper way to see the priority of
         * the download queue" -- the capability (`rank DESC, queued_at ASC`, Move to top) already
         * existed and was invisible; this makes the row order legible as an order rather than
         * something to infer. Only for a still-`queued` row -- a running job isn't waiting on
         * anything anymore, so a position wouldn't mean the same thing for it. */}
        {queuePosition != null && (
          <span
            className="shrink-0 rounded-full bg-indigo-100 px-1.5 py-0.5 text-xs font-semibold tabular-nums text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300"
            title={`Queue position ${queuePosition} -- runs in this order (Move to top to reorder)`}
          >
            #{queuePosition}
          </span>
        )}
        {/* Which queue this row belongs to (DESIGN.md §9.2) -- deliberately a plain muted
         * tag, not a StateChip, so it never competes with the state color for attention;
         * with more than one active queue this is the only thing that tells rows apart. */}
        <span
          className="shrink-0 rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-medium whitespace-nowrap text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
          title={`Queue: ${job.queue_name}`}
        >
          {job.queue_name}
        </span>
        <button
          type="button"
          onClick={() => onOpenDrawer(job)}
          className="min-w-0 flex-1 truncate text-left font-medium text-zinc-900 hover:underline dark:text-zinc-100"
          title={job.rel_path}
        >
          {itemName(job.rel_path)}
        </button>
        <StateChip state={chipStateFor(job)} />
        <span className="w-20 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
          {fileCountFor(nodes, job)} file{fileCountFor(nodes, job) === 1 ? '' : 's'}
        </span>
        <span className="w-14 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
          {formatPercent(bytesDone, bytesTotal)}
        </span>
        <span className="w-24 shrink-0 text-right text-zinc-500 dark:text-zinc-400">{formatRate(speed)}</span>
        <span className="w-20 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
          {running ? `ETA ${formatEta(eta)}` : '—'}
        </span>
        {/* Allocated vs. current (DESIGN.md §9.1): under admission control a job holds its
         * allocation even while pulling less of it -- without this number the scheduler
         * looks broken at exactly the moments it's working correctly. */}
        <span
          className="w-32 shrink-0 text-right text-zinc-500 dark:text-zinc-400"
          title="Allocated at admission (DESIGN.md §4.5) -- held for this job's lifetime regardless of current speed"
        >
          {job.rate_limit_bps != null ? `${formatRate(job.rate_limit_bps)} alloc.` : '—'}
        </span>
      </div>

      {/* Elapsed / average speed / queued wait / post-processing note (2026-08-14) -- see the
       * derivations above for what each figure means and why it's guarded the way it is. Only
       * rendered once there's at least one figure worth showing, e.g. a still-`queued` job with
       * a trivial wait and no `started_at` yet shows nothing here. */}
      {showTimingRow && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400">
          {postprocess && (
            <span
              className="font-medium text-amber-700 dark:text-amber-400"
              title="This job finished transferring; verify/extract is still running against the same item -- not a stalled transfer"
            >
              {postprocess}
            </span>
          )}
          {elapsed != null && (
            <span title="Time this job spent running -- started_at to finished_at, or to now while still running">
              Elapsed {formatEta(elapsed)}
            </span>
          )}
          {avgSpeed != null && (
            <span title="This attempt's bytes moved, averaged over its own elapsed time -- distinct from the live rate above, which is an EMA-smoothed instantaneous reading (core/progress.py)">
              avg {formatRate(avgSpeed)}
            </span>
          )}
          {isNotableQueuedWait(queuedWait) && queuedWait != null && (
            <span title="Time this job waited in the queue before it started running -- queued_at to started_at, often a sign max_concurrent_transfers (DESIGN.md §4.5) was holding it back">
              queued {formatEta(queuedWait)}
            </span>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {job.state === 'queued' && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => onMoveToTop(job)}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Move to top
            </button>
            {!job.forced_full_rate && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onStartNow(job)}
                className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
              >
                Start now at max bandwidth
              </button>
            )}
          </>
        )}
        {(job.state === 'queued' || job.state === 'running') && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onStop(job)}
            className="rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
          >
            Stop
          </button>
        )}
        {job.state === 'failed' && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onRetry(job)}
            className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Retry
          </button>
        )}
        {/* Dismiss (2026-08-13): terminal rows only -- a failed job whose remote is actually
         * gone (REMOTE_GONE, permanently suppressed) had no action but Retry, which is
         * precisely wrong for it. Purely a display action on this job row -- see
         * `core/queue.py.dismiss_job`'s docstring for why it never touches the item's own
         * state or suppression. No confirmation dialog: nothing is destroyed, the record
         * stays on the History page. `succeeded` joined this set 2026-08-14
         * (prompts/2026-08-14-exit-zero-is-not-completion.md) alongside `list_jobs()` starting
         * to surface a recently-succeeded job at all -- see `isDismissable`. */}
        {isDismissable(job.state) && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onDismiss(job)}
            className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900"
            title="Remove this row from Transfers -- it stays visible on the History page"
          >
            Dismiss
          </button>
        )}
      </div>

      {job.state === 'failed' && (
        <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          <p className="font-medium">{job.error_class ?? 'UNKNOWN'}</p>
          {job.output_tail && (
            <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] opacity-90">
              {job.output_tail}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

/** DESIGN.md §9.2 Transfers page -- the job queue. Rows stay deliberately plain (queued /
 * downloading / downloaded, with STOPPED/FAILED surfacing only where they apply); the item
 * drawer opens per row for the full per-file breakdown.
 */
export function TransfersPage() {
  const { jobs, refresh } = useJobs()
  const { queues, progressByJobId } = useLiveModel()
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set())
  const [drawerJob, setDrawerJob] = useState<JobOut | null>(null)
  const [startNowNotice, setStartNowNotice] = useState(false)
  const [clearingAll, setClearingAll] = useState(false)
  const [dismissOutcome, setDismissOutcome] = useState<DismissOutcome | null>(null)

  const nodesByQueue = useMemo(() => {
    const map = new Map<number, FileNode[]>()
    for (const q of queues) map.set(q.queue_id, q.nodes)
    return map
  }, [queues])

  // Queue position (2026-08-13, item 4): `jobs` (`useJobs`/`GET /api/jobs`) already comes back
  // in the real run order (`core/queue.py.list_jobs`'s `ORDER BY job.rank DESC, job.queued_at
  // ASC`) -- no new endpoint, just counting the `queued` rows in the order already returned.
  // Running/failed/cancelled rows are in the same list (`list_jobs`'s own docstring) but never
  // get a position: a running job isn't waiting, and a failed/cancelled one isn't in line at
  // all.
  const queuePositions = useMemo(() => {
    const positions = new Map<number, number>()
    let n = 0
    for (const job of jobs) {
      if (job.state === 'queued') positions.set(job.id, ++n)
    }
    return positions
  }, [jobs])
  const queuedCount = queuePositions.size

  const withBusy = async (jobId: number, action: () => Promise<unknown>) => {
    setBusyIds((prev) => new Set(prev).add(jobId))
    try {
      await action()
      refresh()
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev)
        next.delete(jobId)
        return next
      })
    }
  }

  const handleMoveToTop = (job: JobOut) => withBusy(job.id, () => moveJobToTop(job.id))
  const handleStop = (job: JobOut) => withBusy(job.id, () => stopJob(job.id))
  const handleRetry = (job: JobOut) => withBusy(job.id, () => retryItem(job.item_id))
  const handleDismiss = (job: JobOut) => withBusy(job.id, () => dismissJob(job.id))
  const handleStartNow = (job: JobOut) => {
    if (localStorage.getItem(START_NOW_EXPLAINED_KEY) !== '1') {
      setStartNowNotice(true)
      localStorage.setItem(START_NOW_EXPLAINED_KEY, '1')
    }
    return withBusy(job.id, () => startJobNow(job.id))
  }

  const failedJobs = useMemo(() => jobs.filter((j) => j.state === 'failed'), [jobs])

  /** "Clear all failed" (2026-08-13) -- the bulk counterpart to the per-row Dismiss button,
   * for the "I should have a clear or delete button" half of the user's report: one-at-a-time
   * dismissal of a stack of dead rows is its own annoyance. Scoped to `failed` only, not
   * `cancelled` too -- a stopped job is the result of a deliberate Stop click, not the kind of
   * unattended pile-up `failed` rows (auto-retries exhausted, or a permanent class like
   * REMOTE_GONE) can become; a cancelled row is still one Dismiss click away individually.
   * `Promise.allSettled` (not `Promise.all`) so one job racing to `running` between page load
   * and the click doesn't hide whether the rest actually cleared.
   */
  const handleClearAllFailed = async () => {
    const targets = failedJobs
    if (targets.length === 0) return
    setClearingAll(true)
    setDismissOutcome(null)
    try {
      const results = await Promise.allSettled(targets.map((job) => dismissJob(job.id)))
      const failures: DismissFailure[] = []
      let succeeded = 0
      results.forEach((result, i) => {
        const job = targets[i]
        if (result.status === 'fulfilled') succeeded += 1
        else failures.push({ rel_path: job.rel_path, name: itemName(job.rel_path), error: errorMessage(result.reason) })
      })
      setDismissOutcome({ total: targets.length, succeeded, failures })
      refresh()
    } finally {
      setClearingAll(false)
    }
  }

  const drawerNodes = drawerJob ? (nodesByQueue.get(drawerJob.queue_id) ?? []) : []

  return (
    <div className="flex flex-col gap-3">
      {startNowNotice && (
        <div className="flex items-start justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <p>
            <strong>Start now</strong> admits this job immediately at the full bandwidth ceiling —
            deliberately <em>oversubscribing</em> past what other running jobs are allocated
            (DESIGN.md §4.5). It's the "I want this one now" escape hatch: new admissions pause
            until enough jobs finish to bring the total back under the ceiling, rather than
            throttling what's already running.
          </p>
          <button
            type="button"
            onClick={() => setStartNowNotice(false)}
            className="shrink-0 rounded-md border border-amber-300 px-2 py-1 text-xs font-medium hover:bg-amber-100 dark:border-amber-800 dark:hover:bg-amber-900"
          >
            Got it
          </button>
        </div>
      )}

      {jobs.length === 0 && (
        <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          Nothing queued or downloading — queue an item from Files.
        </div>
      )}

      {/* Makes the row order legible as an order (2026-08-13, item 4) -- "what is the proper
       * way to see the priority of the download queue" was the ask; the ordering already
       * existed (`rank DESC, queued_at ASC`) but nothing said so. Only shown once there's an
       * actual order to explain (2+ queued jobs) -- a single queued job's position is not
       * interesting on its own. */}
      {queuedCount > 1 && (
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Queued jobs run in the order shown, top to bottom — use <strong>Move to top</strong> to
          reorder.
        </p>
      )}

      {/* "Clear all failed" (2026-08-13) -- the bulk counterpart to each row's own Dismiss
       * button; see `handleClearAllFailed`'s docstring for why this is scoped to `failed`
       * only. Only shown once there's something to clear. */}
      {failedJobs.length > 0 && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {failedJobs.length} failed job{failedJobs.length === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            disabled={clearingAll}
            onClick={handleClearAllFailed}
            className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            {clearingAll ? 'Clearing…' : 'Clear all failed'}
          </button>
        </div>
      )}

      {dismissOutcome && (
        <div
          className={`rounded-md border px-3 py-2 text-sm ${
            dismissOutcome.failures.length === 0
              ? 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200'
              : 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200'
          }`}
        >
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">
              Cleared {dismissOutcome.succeeded} of {dismissOutcome.total}
              {dismissOutcome.failures.length > 0 && `, ${dismissOutcome.failures.length} failed`}
            </span>
            <button
              type="button"
              onClick={() => setDismissOutcome(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
          {dismissOutcome.failures.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-xs">
              {dismissOutcome.failures.map((f) => (
                <li key={f.rel_path}>
                  <span className="font-mono">{f.name}</span> — {f.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {jobs.length > 0 && (
        <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
          {jobs.map((job) => (
            <Row
              key={job.id}
              job={job}
              nodes={nodesByQueue.get(job.queue_id) ?? []}
              live={progressByJobId[job.id]}
              queuePosition={queuePositions.get(job.id)}
              onOpenDrawer={setDrawerJob}
              onMoveToTop={handleMoveToTop}
              onStartNow={handleStartNow}
              onStop={handleStop}
              onRetry={handleRetry}
              onDismiss={handleDismiss}
              busy={busyIds.has(job.id)}
            />
          ))}
        </div>
      )}

      {drawerJob && (
        <ItemDrawer
          title={itemName(drawerJob.rel_path)}
          rootRelPath={drawerJob.rel_path}
          itemId={drawerJob.item_id}
          nodes={drawerNodes}
          onClose={() => setDrawerJob(null)}
        />
      )}
    </div>
  )
}
