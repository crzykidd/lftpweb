import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useMemo, useRef, useState } from 'react'
import { getHistoryJobOutput, getHistoryJobs } from '../api/client'
import type { HistoryJobOut, HistoryJobsFilter, PathQueueOut } from '../api/types'
import { formatBytes, formatPercent } from '../lib/format'
import { StateChip } from './StateChip'

const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

/** DESIGN.md §9.2's three-word visible vocabulary doesn't apply here the way it does on the
 * Transfers page -- History's whole domain is terminal jobs, so the state chip renders the
 * outcome directly (mapped onto `StateChip`'s existing color vocabulary rather than inventing
 * a fourth palette for three states).
 */
function chipStateFor(state: HistoryJobOut['state']): string {
  switch (state) {
    case 'succeeded':
      return 'DOWNLOADED'
    case 'failed':
      return 'FAILED'
    case 'cancelled':
      return 'STOPPED'
    default:
      return state
  }
}

function itemName(relPath: string): string {
  const lastSlash = relPath.lastIndexOf('/')
  return lastSlash === -1 ? relPath : relPath.slice(lastSlash + 1)
}

function formatTs(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString()
}

type VirtualRow =
  | { kind: 'header'; queueId: number; queueName: string }
  | { kind: 'job'; job: HistoryJobOut }

/** Flattens the already-filtered/paginated page into queue-grouped sections (DESIGN.md §9.2:
 * "grouped by queue") as one array a single virtualizer can walk -- see the module's own
 * approach note in docs/decisions.md for why this (header rows interleaved into one flat,
 * virtualized list) was chosen over nested per-queue virtualizers.
 */
function groupByQueue(jobs: HistoryJobOut[]): VirtualRow[] {
  const rows: VirtualRow[] = []
  let currentQueueId: number | null = null
  for (const job of jobs) {
    if (job.queue_id !== currentQueueId) {
      rows.push({ kind: 'header', queueId: job.queue_id, queueName: job.queue_name })
      currentQueueId = job.queue_id
    }
    rows.push({ kind: 'job', job })
  }
  return rows
}

function JobRow({ job }: { job: HistoryJobOut }) {
  const [expanded, setExpanded] = useState(false)
  const [output, setOutput] = useState<{ error_class: string | null; output_tail: string | null } | null>(
    null,
  )
  const [loading, setLoading] = useState(false)

  const handleToggle = async () => {
    if (!expanded && output === null && job.has_output_tail) {
      setLoading(true)
      try {
        const res = await getHistoryJobOutput(job.id)
        setOutput({ error_class: res.error_class, output_tail: res.output_tail })
      } finally {
        setLoading(false)
      }
    }
    setExpanded((v) => !v)
  }

  return (
    <div className="flex flex-col gap-1.5 border-b border-zinc-100 px-3 py-2 text-sm dark:border-zinc-900">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-0 flex-1 truncate font-medium text-zinc-900 dark:text-zinc-100" title={job.rel_path}>
          {itemName(job.rel_path)}
        </span>
        <span className="w-14 shrink-0 text-xs text-zinc-500 dark:text-zinc-400">{job.kind}</span>
        <StateChip state={chipStateFor(job.state)} />
        {job.attempt > 1 && (
          <span className="shrink-0 text-xs text-zinc-500 dark:text-zinc-400">attempt {job.attempt}</span>
        )}
        <span className="w-20 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
          {job.bytes_total != null ? formatBytes(job.bytes_total) : '—'}
        </span>
        <span className="w-12 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
          {formatPercent(job.bytes_done, job.bytes_total)}
        </span>
        <span className="w-40 shrink-0 text-right text-xs text-zinc-500 dark:text-zinc-400">
          {formatTs(job.finished_at ?? job.queued_at)}
        </span>
        {job.state === 'failed' && (
          <button
            type="button"
            onClick={handleToggle}
            className="shrink-0 rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
          >
            {job.error_class ?? 'UNKNOWN'} {expanded ? '▲' : '▼'}
          </button>
        )}
      </div>
      {expanded && job.state === 'failed' && (
        <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {loading && <p>Loading captured output…</p>}
          {!loading && output && (
            <>
              <p className="font-medium">{output.error_class ?? 'UNKNOWN'}</p>
              {output.output_tail ? (
                <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] opacity-90">
                  {output.output_tail}
                </pre>
              ) : (
                <p className="mt-1 opacity-75">No output was captured for this job.</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

interface HistoryJobsSectionProps {
  queues: PathQueueOut[]
}

const ROW_ESTIMATE_PX = 44
const HEADER_ESTIMATE_PX = 32

/** The `job` table half of the History page (DESIGN.md §9.2) -- completed, failed, and
 * cancelled transfers, grouped by queue, filterable by queue/state/error class/date range.
 * `succeeded` jobs live here and nowhere else on the live app (Transfers deliberately
 * excludes them -- docs/decisions.md, phase 3b). Server-capped/paginated with a "load more"
 * button; virtualized so a page of thousands of rows stays smooth.
 */
export function HistoryJobsSection({ queues }: HistoryJobsSectionProps) {
  const [queueId, setQueueId] = useState<string>('')
  const [state, setState] = useState<string>('')
  const [errorClass, setErrorClass] = useState<string>('')
  const [since, setSince] = useState<string>('')
  const [until, setUntil] = useState<string>('')

  const [jobs, setJobs] = useState<HistoryJobOut[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const filter: HistoryJobsFilter = useMemo(
    () => ({
      queue_id: queueId ? Number(queueId) : undefined,
      state: (state || undefined) as HistoryJobsFilter['state'],
      error_class: errorClass || undefined,
      since: since ? `${since}T00:00:00.000000Z` : undefined,
      until: until ? `${until}T23:59:59.999999Z` : undefined,
    }),
    [queueId, state, errorClass, since, until],
  )

  const load = async (offset: number, replace: boolean) => {
    setLoading(true)
    try {
      const res = await getHistoryJobs({ ...filter, offset, limit: 200 })
      setTotal(res.total)
      setJobs((prev) => (replace ? res.jobs : [...prev, ...res.jobs]))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(0, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  const rows = useMemo(() => groupByQueue(jobs), [jobs])

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => (rows[index]?.kind === 'header' ? HEADER_ESTIMATE_PX : ROW_ESTIMATE_PX),
    overscan: 10,
  })

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">Transfers</h2>
        <span className="text-xs text-zinc-500 dark:text-zinc-400">
          {jobs.length} of {total} shown
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select className={inputClasses} value={queueId} onChange={(e) => setQueueId(e.target.value)}>
          <option value="">All queues</option>
          {queues.map((q) => (
            <option key={q.id} value={q.id}>
              {q.name}
            </option>
          ))}
        </select>
        <select className={inputClasses} value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">Any state</option>
          <option value="succeeded">succeeded</option>
          <option value="failed">failed</option>
          <option value="cancelled">cancelled</option>
        </select>
        <input
          className={inputClasses}
          placeholder="Error class (e.g. AUTH_FAILED)"
          value={errorClass}
          onChange={(e) => setErrorClass(e.target.value)}
        />
        <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
          Since
          <input type="date" className={inputClasses} value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
          Until
          <input type="date" className={inputClasses} value={until} onChange={(e) => setUntil(e.target.value)} />
        </label>
        <button
          type="button"
          onClick={() => load(0, true)}
          disabled={loading}
          className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {jobs.length === 0 && !loading && (
        <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          No completed, failed, or cancelled transfers match these filters.
        </div>
      )}

      {jobs.length > 0 && (
        <div
          ref={scrollRef}
          className="max-h-[28rem] overflow-auto rounded-md border border-zinc-200 dark:border-zinc-800"
        >
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const row = rows[virtualRow.index]
              return (
                <div
                  key={virtualRow.key}
                  data-index={virtualRow.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                >
                  {row.kind === 'header' ? (
                    <div className="border-b border-zinc-200 bg-zinc-50 px-3 py-1.5 text-xs font-semibold text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-300">
                      {row.queueName}
                    </div>
                  ) : (
                    <JobRow job={row.job} />
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {jobs.length < total && (
        <button
          type="button"
          onClick={() => load(jobs.length, false)}
          disabled={loading}
          className="self-start rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          Load more ({total - jobs.length} remaining)
        </button>
      )}
    </section>
  )
}
