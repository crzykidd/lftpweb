import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  dismissAllJobs,
  dismissJob,
  getItemEvents,
  moveJobToTop,
  retryItem,
  startJobNow,
  stopJob,
} from '../api/client'
import type { FileNode, ItemEventOut, JobOut } from '../api/types'
import { ArrIcon, ArrRowChip } from '../components/LifecycleIcons'
import { ItemDrawer } from '../components/ItemDrawer'
import { StateChip } from '../components/StateChip'
import { useJobs } from '../hooks/useJobs'
import { useLiveModel } from '../hooks/useLiveModel'
import { arrHoverLabel } from '../lib/fileTree'
import { formatBytes, formatRate, formatRelativeTimeIntl } from '../lib/format'
import {
  type LiveProgress,
  type PanelField,
  type QueueGroup,
  completedTimeLabel,
  formatQueueGroupCounts,
  groupHasDismissable,
  groupJobsByQueue,
  hasArrGroup,
  isDismissable,
  isQueueCollapsed,
  processingGroupFields,
  queueGroupSummary,
  readCollapsedQueues,
  sortTransferRows,
  transferGroupFields,
  transferLineValue,
  withQueueCollapsed,
  writeCollapsedQueues,
} from '../lib/transferPanel'

// Re-exported so `TransfersPage.test.ts`'s pre-existing `import { isDismissable } from
// './TransfersPage'` keeps working unchanged -- the definition itself moved to
// `lib/transferPanel.ts` on 2026-08-17 (see that function's own docstring for why).
export { isDismissable }

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
  live: LiveProgress | undefined
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
  // 2026-08-15 (prompts/2026-08-15-transfers-single-line-rows-with-detail.md): everything this
  // row used to show inline -- file count, percent, rate, ETA, allocated, elapsed, average
  // speed, queued wait, post-processing note -- moves into the expand panel below. The row
  // itself keeps only what the task's own line-up names: queue position/tag (still needed for
  // the Move-to-top action right there on the same line), name, state chip, and one live
  // number (`transferLineValue`).
  const [expanded, setExpanded] = useState(false)
  const fileCount = fileCountFor(nodes, job)
  const completed = completedTimeLabel(job)

  return (
    <div className="flex flex-col gap-1.5 border-b border-zinc-200 px-3 py-2 text-sm last:border-b-0 dark:border-zinc-800">
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
        <button
          type="button"
          onClick={() => onOpenDrawer(job)}
          className="min-w-0 flex-1 truncate text-left font-medium text-zinc-900 hover:underline dark:text-zinc-100"
          title={job.rel_path}
        >
          {itemName(job.rel_path)}
        </button>
        <StateChip state={chipStateFor(job)} />
        {/* The *arr brand-logo chip (2026-08-16, prompts/2026-08-16-arr-chip-on-row-lines.md)
         * -- sits right beside the state chip, in the same compact cluster; renders nothing
         * when this item isn't *arr-tracked (`job.arr_status === null`). */}
        <ArrRowChip
          arrStatus={job.arr_status}
          arrStatusAt={job.arr_status_at}
          instanceName={job.arr_instance_name}
          instanceKind={job.arr_instance_kind}
        />
        {/* Completed time (2026-08-16, user report from live use): "each terminal row should
         * show when it completed" -- compact relative form, exact timestamp on hover, same
         * value/title split every other timestamp on this page uses. `null` (nothing rendered)
         * for an active row -- queued/running show what they show today, per the task's own
         * instruction. */}
        {completed && (
          <span
            className="w-20 shrink-0 text-right text-xs text-zinc-500 dark:text-zinc-400"
            title={completed.title}
          >
            {completed.value}
          </span>
        )}
        {/* The one live figure this line keeps -- percent + current rate while downloading (plus
         * a "<duration> left" ETA once one is known, 2026-08-19,
         * prompts/2026-08-19-transfers-row-shows-eta.md), final size otherwise. Everything else
         * that used to sit here lives in the panel. `w-44` (not the original `w-32`): the ETA
         * addition can make this the longest of the three figures ever shown here. */}
        <span className="w-44 shrink-0 whitespace-nowrap text-right text-zinc-500 dark:text-zinc-400">
          {transferLineValue(job, live)}
        </span>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
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
        {/* The expand control (2026-08-15) -- "chevron or ⓘ, matching existing idioms" per the
         * task's own instruction; a plain chevron toggle is `HistoryJobsSection.tsx.JobRow`'s
         * own precedent for an expandable row on this codebase. */}
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          title={expanded ? 'Hide details' : 'Show details -- transfer numbers, processing, and *arr status'}
          className="shrink-0 rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900"
        >
          {expanded ? '▲' : '▼'}
        </button>
      </div>

      {expanded && <RowDetailPanel job={job} live={live} fileCount={fileCount} />}
    </div>
  )
}

/** A plain label/value block for one panel group (`PanelField[]`, `lib/transferPanel.ts`) --
 * shared by all three groups so Transfer/Processing/*arr render identically, and `emptyText`
 * says something honest rather than the group silently vanishing (a `queued` job's Processing
 * group, say, genuinely has nothing yet).
 */
function PanelFieldList({ fields, emptyText }: { fields: PanelField[]; emptyText: string }) {
  if (fields.length === 0) {
    return <p className="text-zinc-400 dark:text-zinc-600">{emptyText}</p>
  }
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      {fields.map((f) => (
        <Fragment key={f.label}>
          <dt className="text-zinc-500 dark:text-zinc-400">{f.label}</dt>
          <dd title={f.title} className="text-right text-zinc-800 dark:text-zinc-200">
            {f.value}
          </dd>
        </Fragment>
      ))}
    </dl>
  )
}

const ITEM_EVENTS_LIMIT = 20

/** The Transfers row's expand panel (2026-08-15) -- three groups, per the task's own line-up:
 * **Transfer** (bytes/elapsed/speed/queued-wait/file-count, plus a failed job's error class +
 * output tail), **Processing** (the item's verify/extract/remote-delete milestones, enriched by
 * the pipeline's own event messages -- fetched on demand, exactly once, when this panel first
 * opens), and ***arr** (hidden entirely when the job's queue has no bound instance).
 */
function RowDetailPanel({ job, live, fileCount }: { job: JobOut; live: LiveProgress | undefined; fileCount: number }) {
  const transferFields = useMemo(() => transferGroupFields(job, { live, fileCount }), [job, live, fileCount])
  const processingFields = useMemo(() => processingGroupFields(job), [job])
  const showArr = hasArrGroup(job)
  const arrLabel = arrHoverLabel({ arr_status: job.arr_status, arr_status_at: job.arr_status_at }, job.arr_instance_name)

  // The pipeline's own event messages (2026-08-15) -- "the carefully-worded event messages ARE
  // the UI," the same History §7.3 philosophy this task's own instruction points at. Fetched
  // once, when this panel mounts (which only happens while `expanded`, `TransfersPage.tsx`'s
  // `Row` above) or if `job.item_id` itself changes -- the same on-open fetch shape
  // `ItemDrawer.tsx`'s `HistoryPanel` already establishes.
  const [events, setEvents] = useState<ItemEventOut[] | null>(null)
  const [eventsLoading, setEventsLoading] = useState(false)
  const [eventsError, setEventsError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setEventsLoading(true)
    setEventsError(null)
    getItemEvents(job.item_id, ITEM_EVENTS_LIMIT)
      .then((res) => {
        if (!cancelled) setEvents(res.events)
      })
      .catch((err: unknown) => {
        if (!cancelled) setEventsError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setEventsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [job.item_id])

  return (
    <div className="flex flex-col gap-3 rounded-md border border-zinc-200 bg-zinc-50 p-3 text-xs dark:border-zinc-800 dark:bg-zinc-900/40">
      <div>
        <h4 className="mb-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Transfer
        </h4>
        <PanelFieldList fields={transferFields} emptyText="Nothing to show yet." />
        {job.state === 'failed' && (
          <div className="mt-2 rounded-md border border-red-200 bg-red-50 p-2 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            <p className="font-medium">{job.error_class ?? 'UNKNOWN'}</p>
            {job.output_tail && (
              <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] opacity-90">
                {job.output_tail}
              </pre>
            )}
          </div>
        )}
      </div>

      <div>
        <h4 className="mb-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Processing
        </h4>
        <PanelFieldList fields={processingFields} emptyText="No verify/extract/delete milestones recorded yet." />
        {eventsLoading && <p className="mt-1 text-zinc-400 dark:text-zinc-600">Loading processing events…</p>}
        {eventsError && <p className="mt-1 text-red-600 dark:text-red-400">Couldn't load processing events: {eventsError}</p>}
        {events != null && events.length > 0 && (
          <ul className="mt-2 flex flex-col gap-1 border-t border-zinc-200 pt-2 dark:border-zinc-800">
            {events.map((e) => (
              <li key={e.id} className="flex flex-col gap-0.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span
                    className={
                      e.level === 'error' || e.level === 'warning'
                        ? 'font-medium text-amber-700 dark:text-amber-400'
                        : 'font-medium text-zinc-700 dark:text-zinc-300'
                    }
                  >
                    {e.kind}
                  </span>
                  <span className="text-zinc-500 dark:text-zinc-400" title={new Date(e.ts).toLocaleString()}>
                    {formatRelativeTimeIntl(e.ts)}
                  </span>
                </div>
                {/* The pipeline's own event message, verbatim -- "the carefully-worded event
                 * messages ARE the UI" (History §7.3's own precedent, this task's instruction). */}
                <p className="text-zinc-600 dark:text-zinc-400">{e.message}</p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {showArr && (
        <div>
          <h4 className="mb-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            *arr
          </h4>
          <div className="flex items-center gap-2">
            <ArrIcon arrStatus={job.arr_status} arrStatusAt={job.arr_status_at} instanceName={job.arr_instance_name} />
            <span className="text-zinc-700 dark:text-zinc-300">
              {arrLabel ?? `${job.arr_instance_name}: not yet matched`}
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

/** A queue group's header line (2026-08-16) -- queue name, outcome counts, and total/combined-
 * rate, all in one clickable line that toggles the group's collapse state. The queue name used to
 * repeat on every row (`Row` above, before this task); it lives here exactly once per group now.
 *
 * 2026-08-17 (prompts/2026-08-17-transfers-dismiss-per-queue.md): the header line used to be a
 * single full-width `<button>` -- adding a second, independently-clickable "Dismiss Queue"
 * control inside it means that can no longer be a real `<button>` (a `<button>` nested inside a
 * `<button>` is invalid HTML and makes click handling ambiguous). It's a `<div role="button">`
 * instead, carrying the same `onClick`/keyboard toggle behavior by hand (`onKeyDown` below), so
 * the whole row is still one big click target for collapse/expand -- only the inner Dismiss
 * Queue button, an actual sibling `<button>`, stops that click from also propagating up to the
 * row's own toggle.
 */
function GroupHeader({
  group,
  collapsed,
  onToggle,
  liveByJobId,
  dismissing,
  onDismissQueue,
}: {
  group: QueueGroup
  collapsed: boolean
  onToggle: () => void
  liveByJobId: Record<number, LiveProgress>
  dismissing: boolean
  onDismissQueue: () => void
}) {
  const summary = useMemo(() => queueGroupSummary(group.jobs, liveByJobId), [group.jobs, liveByJobId])
  const countsText = formatQueueGroupCounts(summary.counts)
  const showDismiss = groupHasDismissable(group.jobs)
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onToggle()
        }
      }}
      aria-expanded={!collapsed}
      title={collapsed ? 'Expand this queue' : 'Collapse this queue'}
      className="flex w-full flex-wrap items-center gap-3 border-b border-zinc-200 bg-zinc-50 px-3 py-2 text-left text-sm hover:bg-zinc-100 dark:border-zinc-800 dark:bg-zinc-900/60 dark:hover:bg-zinc-900"
    >
      <span className="shrink-0 text-zinc-400 dark:text-zinc-600" aria-hidden="true">
        {collapsed ? '▸' : '▾'}
      </span>
      <span className="min-w-0 flex-1 truncate font-semibold text-zinc-900 dark:text-zinc-100">
        {group.queueName}
      </span>
      {countsText && <span className="shrink-0 text-xs text-zinc-500 dark:text-zinc-400">{countsText}</span>}
      <span className="shrink-0 text-right text-xs text-zinc-500 dark:text-zinc-400">
        {formatBytes(summary.totalBytesDone)}
        {summary.combinedRateBps != null && ` · ${formatRate(summary.combinedRateBps)}`}
      </span>
      {showDismiss && (
        <button
          type="button"
          disabled={dismissing}
          onClick={(e) => {
            // Doesn't toggle the group's own collapse -- the header line above is a collapse
            // toggle (this task's own instruction).
            e.stopPropagation()
            onDismissQueue()
          }}
          title="Dismiss this queue's finished rows"
          className="shrink-0 rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
        >
          {dismissing ? 'Dismissing…' : 'Dismiss Queue'}
        </button>
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
  const [dismissingAll, setDismissingAll] = useState(false)
  const [dismissAllError, setDismissAllError] = useState<string | null>(null)
  const [dismissAllCount, setDismissAllCount] = useState<number | null>(null)
  // Per-queue "Dismiss Queue" (2026-08-17, prompts/2026-08-17-transfers-dismiss-per-queue.md) --
  // keyed by `queueId`, not a single flag/string/number the way the page-wide Dismiss-all state
  // above is, so two different groups' controls never lock or clobber each other.
  const [dismissingQueueIds, setDismissingQueueIds] = useState<Set<number>>(new Set())
  const [dismissQueueError, setDismissQueueError] = useState<Record<number, string>>({})
  const [dismissQueueCount, setDismissQueueCount] = useState<Record<number, number>>({})

  const nodesByQueue = useMemo(() => {
    const map = new Map<number, FileNode[]>()
    for (const q of queues) map.set(q.queue_id, q.nodes)
    return map
  }, [queues])

  // Row order (2026-08-16, `lib/transferPanel.ts.sortTransferRows`'s own docstring): active rows
  // keep `jobs`' own scheduler order untouched, terminal rows sort newest-completed-first --
  // **replaces** the previous implicit order for terminal rows, which was the same `rank DESC,
  // queued_at ASC` scheduler order active rows still use (meaningless for a row that's already
  // finished). `queuePositions` below deliberately keeps reading `jobs` itself, not this sorted
  // view -- a queue position is about the real future run order, unaffected by how terminal rows
  // happen to be displayed.
  const sortedJobs = useMemo(() => sortTransferRows(jobs), [jobs])

  // Group by queue (2026-08-16, prompts/2026-08-16-transfers-group-by-queue.md): "per-row queue
  // labels make the page busy" -- one collapsible group per queue, ordered by queue name, each
  // row's within-group order untouched from `sortedJobs` above (`groupJobsByQueue`'s own
  // docstring). Collapse state is per-queue, persisted, and read once on mount -- a queue that
  // temporarily drops out of `jobs` (no visible rows right now) simply produces no group, but its
  // stored preference is never pruned, so it's there again when the queue returns.
  const groups = useMemo(() => groupJobsByQueue(sortedJobs), [sortedJobs])
  const [collapsedQueues, setCollapsedQueues] = useState(readCollapsedQueues)
  const toggleQueueCollapsed = (queueId: number) => {
    setCollapsedQueues((prev) => {
      const next = withQueueCollapsed(prev, queueId, !isQueueCollapsed(prev, queueId))
      writeCollapsedQueues(next)
      return next
    })
  }

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
  const dismissableCount = useMemo(() => jobs.filter((j) => isDismissable(j.state)).length, [jobs])

  /** "Dismiss all" at the top of the page (2026-08-15, user addition to
   * prompts/2026-08-15-transfers-single-line-rows-with-detail.md) -- every currently-
   * dismissable (terminal, not-yet-dismissed) job, not just `failed` (see `handleClearAllFailed`
   * above for why that one stays scoped to `failed`). A single server-side bulk call
   * (`core/queue.py.dismiss_all_terminal`), not a client-side `Promise.allSettled` fan-out --
   * the task's own stated preference. Since it's one request, "partial failure" can only mean
   * the request itself failed (network/HTTP) -- reported honestly via `dismissAllError`, same
   * as any other failed mutation on this page, rather than a per-row breakdown there is no
   * per-row result to report.
   */
  const handleDismissAll = async () => {
    setDismissingAll(true)
    setDismissAllError(null)
    setDismissAllCount(null)
    try {
      const res = await dismissAllJobs()
      setDismissAllCount(res.dismissed)
      refresh()
    } catch (err) {
      setDismissAllError(errorMessage(err))
    } finally {
      setDismissingAll(false)
    }
  }

  /** A `GroupHeader`'s own "Dismiss Queue" control (2026-08-17,
   * prompts/2026-08-17-transfers-dismiss-per-queue.md) -- the per-queue-scoped counterpart to
   * `handleDismissAll` above, same shape: one bulk request (`dismissAllJobs(queueId)` ->
   * `core/queue.py.dismiss_all_terminal(queue_id=...)`), and the same honest "the request itself
   * either worked or it didn't" error reporting, just keyed by `queueId` throughout so a second
   * group's button stays live (and its own outcome/error stay separate) while this one is still
   * in flight.
   */
  const handleDismissQueue = async (queueId: number) => {
    setDismissingQueueIds((prev) => new Set(prev).add(queueId))
    setDismissQueueError((prev) => {
      const next = { ...prev }
      delete next[queueId]
      return next
    })
    setDismissQueueCount((prev) => {
      const next = { ...prev }
      delete next[queueId]
      return next
    })
    try {
      const res = await dismissAllJobs(queueId)
      setDismissQueueCount((prev) => ({ ...prev, [queueId]: res.dismissed }))
      refresh()
    } catch (err) {
      setDismissQueueError((prev) => ({ ...prev, [queueId]: errorMessage(err) }))
    } finally {
      setDismissingQueueIds((prev) => {
        const next = new Set(prev)
        next.delete(queueId)
        return next
      })
    }
  }

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

      {/* "Dismiss all" (2026-08-15, user addition) -- see `handleDismissAll`'s docstring.
       * Hidden entirely once there is nothing dismissable, same "don't show a control with
       * nothing to do" rule "Clear all failed" below already follows. */}
      {dismissableCount > 0 && (
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {dismissableCount} dismissable job{dismissableCount === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            disabled={dismissingAll}
            onClick={handleDismissAll}
            title="Dismiss every terminal (not active) job from this page -- records stay on the History page"
            className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            {dismissingAll ? 'Dismissing…' : 'Dismiss all'}
          </button>
        </div>
      )}

      {dismissAllCount != null && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          <span className="font-medium">
            Dismissed {dismissAllCount} job{dismissAllCount === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            onClick={() => setDismissAllCount(null)}
            className="shrink-0 text-xs underline decoration-dotted"
          >
            Dismiss
          </button>
        </div>
      )}

      {dismissAllError && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span>Couldn't dismiss all: {dismissAllError}</span>
          <button
            type="button"
            onClick={() => setDismissAllError(null)}
            className="shrink-0 text-xs underline decoration-dotted"
          >
            Dismiss
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
        <div className="flex flex-col gap-3">
          {groups.map((group) => {
            const collapsed = isQueueCollapsed(collapsedQueues, group.queueId)
            return (
              <div
                key={group.queueId}
                className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800"
              >
                <GroupHeader
                  group={group}
                  collapsed={collapsed}
                  onToggle={() => toggleQueueCollapsed(group.queueId)}
                  liveByJobId={progressByJobId}
                  dismissing={dismissingQueueIds.has(group.queueId)}
                  onDismissQueue={() => handleDismissQueue(group.queueId)}
                />
                {dismissQueueCount[group.queueId] != null && (
                  <div className="flex items-center justify-between gap-3 border-b border-zinc-200 bg-emerald-50 px-3 py-1.5 text-xs text-emerald-900 dark:border-zinc-800 dark:bg-emerald-950/40 dark:text-emerald-200">
                    <span className="font-medium">
                      Dismissed {dismissQueueCount[group.queueId]} job
                      {dismissQueueCount[group.queueId] === 1 ? '' : 's'}
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        setDismissQueueCount((prev) => {
                          const next = { ...prev }
                          delete next[group.queueId]
                          return next
                        })
                      }
                      className="shrink-0 underline decoration-dotted"
                    >
                      Dismiss
                    </button>
                  </div>
                )}
                {dismissQueueError[group.queueId] && (
                  <div className="flex items-center justify-between gap-3 border-b border-zinc-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-900 dark:border-zinc-800 dark:bg-amber-950/40 dark:text-amber-200">
                    <span>Couldn't dismiss this queue: {dismissQueueError[group.queueId]}</span>
                    <button
                      type="button"
                      onClick={() =>
                        setDismissQueueError((prev) => {
                          const next = { ...prev }
                          delete next[group.queueId]
                          return next
                        })
                      }
                      className="shrink-0 underline decoration-dotted"
                    >
                      Dismiss
                    </button>
                  </div>
                )}
                {!collapsed &&
                  group.jobs.map((job) => (
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
            )
          })}
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
