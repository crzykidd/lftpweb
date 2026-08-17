import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useMemo, useRef, useState } from 'react'
import { clearHistoryJob, clearHistoryJobs, getHistoryJobOutput, getHistoryJobs } from '../api/client'
import type { HistoryJobOut, HistoryJobsFilter, HistoryQueueSummaryOut, PathQueueOut } from '../api/types'
import { formatBytes, formatPercent } from '../lib/format'
import {
  decrementHistoryQueueSummary,
  failedJobPanelContent,
  formatQueueGroupCounts,
  groupHistoryJobsByQueue,
  historyQueueGroupCounts,
  isQueueCollapsed,
  readHistoryCollapsedQueues,
  type QueueCollapseMap,
  withQueueCollapsed,
  writeHistoryCollapsedQueues,
} from '../lib/transferPanel'
import { ArrRowChip } from './LifecycleIcons'
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

/** A queue group's header line (2026-08-16) -- queue name, outcome counts, and total size, all
 * in one clickable line that toggles the group's collapse state. Mirrors
 * `TransfersPage.tsx.GroupHeader`'s own idiom (single-click-anywhere toggle, chevron + name +
 * counts + size), but reads the server-computed `HistoryQueueSummaryOut` instead of computing an
 * aggregate client-side over the loaded page -- see `groupHistoryJobsByQueue`'s and the `queueSummaries`
 * state's own comments for why. `summary` is only `undefined` for a queue whose rows just loaded
 * but whose summary response hasn't landed yet (the two arrive on the exact same response, so in
 * practice this is only a one-render gap) -- the header still renders, just without the counts/
 * size text until it does.
 */
function QueueGroupHeader({
  queueId,
  queueName,
  summary,
  collapsed,
  onToggle,
}: {
  queueId: number
  queueName: string
  summary: HistoryQueueSummaryOut | undefined
  collapsed: boolean
  onToggle: () => void
}) {
  const countsText = summary ? formatQueueGroupCounts(historyQueueGroupCounts(summary)) : ''
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!collapsed}
      title={collapsed ? 'Expand this queue' : 'Collapse this queue'}
      data-queue-id={queueId}
      className="flex w-full flex-wrap items-center gap-3 border-b border-zinc-200 bg-zinc-50 px-3 py-1.5 text-left text-xs font-semibold text-zinc-600 hover:bg-zinc-100 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-300 dark:hover:bg-zinc-900"
    >
      <span className="shrink-0 text-zinc-400 dark:text-zinc-600" aria-hidden="true">
        {collapsed ? '▸' : '▾'}
      </span>
      <span className="min-w-0 flex-1 truncate">{queueName}</span>
      {countsText && <span className="shrink-0 font-normal text-zinc-500 dark:text-zinc-400">{countsText}</span>}
      {summary && (
        <span className="shrink-0 font-normal text-zinc-500 dark:text-zinc-400">
          {formatBytes(summary.total_bytes_done)}
        </span>
      )}
    </button>
  )
}

function JobRow({ job, onClearRequest }: { job: HistoryJobOut; onClearRequest: (job: HistoryJobOut) => void }) {
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

  // The fetch-vs-static-empty-state decision for the panel below, computed once per render
  // rather than at each of its two call sites -- see `failedJobPanelContent`'s own docstring.
  const panelContent = job.state === 'failed' ? failedJobPanelContent(job) : null

  return (
    <div className="flex flex-col gap-1.5 border-b border-zinc-100 px-3 py-2 text-sm dark:border-zinc-900">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-0 flex-1 truncate font-medium text-zinc-900 dark:text-zinc-100" title={job.rel_path}>
          {itemName(job.rel_path)}
        </span>
        <span className="w-14 shrink-0 text-xs text-zinc-500 dark:text-zinc-400">{job.kind}</span>
        <StateChip state={chipStateFor(job.state)} />
        {/* The *arr brand-logo chip (2026-08-16, prompts/2026-08-16-arr-chip-on-row-lines.md)
         * -- same shared component and status vocabulary as the Transfers row's own chip
         * (`TransfersPage.tsx`); renders nothing when this job isn't *arr-tracked. */}
        <ArrRowChip
          arrStatus={job.arr_status}
          arrStatusAt={job.arr_status_at}
          instanceName={job.arr_instance_name}
          instanceKind={job.arr_instance_kind}
        />
        {job.attempt > 1 && (
          <span className="shrink-0 text-xs text-zinc-500 dark:text-zinc-400">attempt {job.attempt}</span>
        )}
        {/* Dismissed from Transfers (2026-08-13, prompts/done/2026-08-13-dismiss-terminal-jobs.md)
         * -- this row is still here (dismissal never touches this table), it just no longer
         * shows on the Transfers page. Worth a quiet marker: without it, "why did this
         * disappear from Transfers" has no answer on the one page that could give it one. */}
        {job.dismissed_at && (
          <span
            className="shrink-0 rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
            title={`Dismissed from Transfers ${formatTs(job.dismissed_at)}`}
          >
            dismissed
          </span>
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
        {/* Clear (2026-08-13, prompts/2026-08-13-clear-history.md) -- deletes this row from
         * History outright, unlike Dismiss above (which only hides it from Transfers). Every
         * job in this list is already terminal (the endpoint's whole domain), so there's
         * nothing to reject here -- the confirm panel this opens is what makes it deliberate. */}
        <button
          type="button"
          onClick={() => onClearRequest(job)}
          title="Clear this record from History -- cannot be undone"
          className="shrink-0 rounded-md px-1.5 py-1 text-xs font-medium text-zinc-400 hover:bg-red-50 hover:text-red-700 dark:text-zinc-600 dark:hover:bg-red-950 dark:hover:text-red-300"
        >
          Clear
        </button>
      </div>
      {expanded && job.state === 'failed' && (
        <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {loading && <p>Loading captured output…</p>}
          {/* 2026-08-17 (prompts/2026-08-17-interrupted-job-popout-explains-itself.md): a
           * `has_output_tail: false` row (e.g. an INTERRUPTED job recovered before
           * `core/queue.py` started writing a reason) never gets `output` fetched by
           * `handleToggle` above -- `output` would stay `null` forever, and the fetch-path
           * branch below would never render. This branch renders the same "no output" copy
           * straight from `job.error_class`, which is already on the list row, no fetch
           * needed. `failedJobPanelContent` is the single source of that fetch-vs-static
           * decision, shared with `lib/transferPanel.test.ts`. */}
          {!loading && panelContent?.kind === 'static' && (
            <>
              <p className="font-medium">{job.error_class ?? 'UNKNOWN'}</p>
              <p className="mt-1 opacity-75">No output was captured for this job.</p>
            </>
          )}
          {!loading && panelContent?.kind === 'fetch' && output && (
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
  // The server-computed, filter-honest per-queue aggregate (2026-08-16,
  // prompts/2026-08-16-history-jobs-group-collapse.md) -- `GET /api/history/jobs`'s
  // `queue_summaries`, riding alongside `jobs` on the same response. Deliberately not derived
  // from `jobs` client-side: this list is `LIMIT`/`OFFSET` paginated, so a client-side sum would
  // be wrong the instant a queue has more matching rows than are currently loaded (the task's
  // own "key difference from Transfers"; see api/history.py's module docstring).
  const [queueSummaries, setQueueSummaries] = useState<HistoryQueueSummaryOut[]>([])
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
      setQueueSummaries(res.queue_summaries)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(0, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  // --- Clearing (2026-08-13, prompts/2026-08-13-clear-history.md) -----------------------
  //
  // Irreversible, so every path goes through this confirm panel first -- one row or the
  // whole filtered set, never a silent delete. Bulk clears run as a single server-side
  // `DELETE` (api/history.py) against the *filter*, not the loaded page, so it also removes
  // rows beyond what's currently fetched; a full reload afterward is what picks that up. A
  // single-row clear only ever removes the one row already in hand, so it's applied locally
  // instead of a full reload -- no unbounded refetch just to drop one item.
  const hasActiveFilter = Boolean(queueId || state || errorClass || since || until)
  const [pendingClear, setPendingClear] = useState<{ kind: 'row'; job: HistoryJobOut } | { kind: 'bulk' } | null>(
    null,
  )
  const [clearing, setClearing] = useState(false)
  const [clearError, setClearError] = useState<string | null>(null)

  const confirmClear = async () => {
    const pending = pendingClear
    if (!pending) return
    setClearing(true)
    setClearError(null)
    try {
      if (pending.kind === 'row') {
        await clearHistoryJob(pending.job.id)
        setJobs((prev) => prev.filter((j) => j.id !== pending.job.id))
        setTotal((t) => Math.max(0, t - 1))
        // Applied locally for the same reason `jobs`/`total` are above -- one row's server
        // truth is already known (it's the row just cleared), no need for a full reload just to
        // pick up a single-bucket, single-queue change to `queue_summaries`.
        setQueueSummaries((prev) => decrementHistoryQueueSummary(prev, pending.job))
      } else {
        await clearHistoryJobs(filter)
        await load(0, true)
      }
      setPendingClear(null)
    } catch (err) {
      setClearError(err instanceof Error ? err.message : 'Clear failed')
    } finally {
      setClearing(false)
    }
  }

  // Per-queue collapse (2026-08-16, prompts/2026-08-16-history-jobs-group-collapse.md) -- same
  // map shape and persistence as Transfers' own queue groups (`TransfersPage.tsx`), but under
  // `history.*`'s own storage key (`readHistoryCollapsedQueues`/`writeHistoryCollapsedQueues` in
  // `lib/transferPanel.ts`) so collapsing a queue here never implicitly collapses it there, or
  // vice versa -- the task's own instruction. Default expanded, read once on mount.
  const [collapsedQueues, setCollapsedQueues] = useState<QueueCollapseMap>(readHistoryCollapsedQueues)
  const toggleQueueCollapsed = (queueId: number) => {
    setCollapsedQueues((prev) => {
      const next = withQueueCollapsed(prev, queueId, !isQueueCollapsed(prev, queueId))
      writeHistoryCollapsedQueues(next)
      return next
    })
  }

  const summaryByQueueId = useMemo(() => {
    const map = new Map<number, HistoryQueueSummaryOut>()
    for (const s of queueSummaries) map.set(s.queue_id, s)
    return map
  }, [queueSummaries])

  const rows = useMemo(() => groupHistoryJobsByQueue(jobs, collapsedQueues), [jobs, collapsedQueues])

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
        <button
          type="button"
          disabled={total === 0}
          onClick={() => setPendingClear({ kind: 'bulk' })}
          className="ml-auto rounded-md border border-red-300 px-2.5 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
        >
          {hasActiveFilter ? `Clear filtered (${total})` : `Clear all (${total})`}
        </button>
      </div>

      {/* Clearing is irreversible -- one confirm panel shared by the per-row "Clear" button
       * and the toolbar's bulk clear above, distinguished only by its message and count. */}
      {pendingClear && (
        <div className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="text-red-900 dark:text-red-200">
            {pendingClear.kind === 'row' ? (
              <>
                Clear the History record for <strong>{itemName(pendingClear.job.rel_path)}</strong>? This
                cannot be undone.
              </>
            ) : (
              <>
                Clear <strong>{total}</strong> job {total === 1 ? 'record' : 'records'}
                {hasActiveFilter ? ' matching the current filters' : ''}? This cannot be undone.
              </>
            )}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmClear}
              disabled={clearing}
              className="rounded-md bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-800 disabled:opacity-50 dark:bg-red-800 dark:hover:bg-red-700"
            >
              {clearing ? 'Clearing…' : 'Clear'}
            </button>
            <button
              type="button"
              onClick={() => setPendingClear(null)}
              disabled={clearing}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {clearError && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span>{clearError}</span>
          <button type="button" onClick={() => setClearError(null)} className="shrink-0 text-xs underline decoration-dotted">
            Dismiss
          </button>
        </div>
      )}

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
                    <QueueGroupHeader
                      queueId={row.queueId}
                      queueName={row.queueName}
                      summary={summaryByQueueId.get(row.queueId)}
                      collapsed={isQueueCollapsed(collapsedQueues, row.queueId)}
                      onToggle={() => toggleQueueCollapsed(row.queueId)}
                    />
                  ) : (
                    <JobRow job={row.job} onClearRequest={(job) => setPendingClear({ kind: 'row', job })} />
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
