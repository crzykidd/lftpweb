import { useMemo, useState } from 'react'
import { moveJobToTop, retryItem, startJobNow, stopJob } from '../api/client'
import type { FileNode, JobOut } from '../api/types'
import { ItemDrawer } from '../components/ItemDrawer'
import { StateChip } from '../components/StateChip'
import { useJobs } from '../hooks/useJobs'
import { useLiveModel } from '../hooks/useLiveModel'
import { formatEta, formatPercent, formatRate } from '../lib/format'

const START_NOW_EXPLAINED_KEY = 'lftpweb:startNowExplained'

/** DESIGN.md §9.2's three-word visible vocabulary, mapped onto the actual job/item states
 * that show up on this page (broader than just queued/running -- see
 * `core/queue.py.list_jobs`'s docstring and docs/decisions.md). `STOPPED`/`FAILED` surface
 * verbatim, exactly the "other internal states surface only on rows where they actually
 * apply" rule §9.2 states, rather than being folded into one of the three.
 */
function chipStateFor(job: JobOut): string {
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

function fileCountFor(nodes: FileNode[], job: JobOut): number {
  if (!job.is_dir) return 1
  const prefix = `${job.rel_path}/`
  return nodes.filter((n) => !n.is_dir && n.rel_path.startsWith(prefix)).length
}

function itemName(relPath: string): string {
  const lastSlash = relPath.lastIndexOf('/')
  return lastSlash === -1 ? relPath : relPath.slice(lastSlash + 1)
}

interface RowProps {
  job: JobOut
  nodes: FileNode[]
  live: { bytes_done: number; bytes_total: number | null; speed_bps: number; eta_s: number | null } | undefined
  onOpenDrawer: (job: JobOut) => void
  onMoveToTop: (job: JobOut) => void
  onStartNow: (job: JobOut) => void
  onStop: (job: JobOut) => void
  onRetry: (job: JobOut) => void
  busy: boolean
}

function Row({ job, nodes, live, onOpenDrawer, onMoveToTop, onStartNow, onStop, onRetry, busy }: RowProps) {
  const running = job.state === 'running'
  const bytesDone = running ? (live?.bytes_done ?? job.bytes_done) : job.bytes_done
  const bytesTotal = (running ? live?.bytes_total : job.bytes_total) ?? job.bytes_total
  const speed = running ? (live?.speed_bps ?? job.speed_bps ?? 0) : 0
  const eta = running ? (live?.eta_s ?? job.eta_s) : null

  return (
    <div className="flex flex-col gap-2 border-b border-zinc-200 px-3 py-2.5 text-sm last:border-b-0 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-3">
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

  const nodesByQueue = useMemo(() => {
    const map = new Map<number, FileNode[]>()
    for (const q of queues) map.set(q.queue_id, q.nodes)
    return map
  }, [queues])

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
  const handleStartNow = (job: JobOut) => {
    if (localStorage.getItem(START_NOW_EXPLAINED_KEY) !== '1') {
      setStartNowNotice(true)
      localStorage.setItem(START_NOW_EXPLAINED_KEY, '1')
    }
    return withBusy(job.id, () => startJobNow(job.id))
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

      {jobs.length > 0 && (
        <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
          {jobs.map((job) => (
            <Row
              key={job.id}
              job={job}
              nodes={nodesByQueue.get(job.queue_id) ?? []}
              live={progressByJobId[job.id]}
              onOpenDrawer={setDrawerJob}
              onMoveToTop={handleMoveToTop}
              onStartNow={handleStartNow}
              onStop={handleStop}
              onRetry={handleRetry}
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
