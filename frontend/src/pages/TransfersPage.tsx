import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  dismissAllJobs,
  dismissJob,
  getHistoryJobOutput,
  getItemChildren,
  getItemEvents,
  getTransferSettings,
  moveJob,
  retryItem,
  startJobNow,
  stopJob,
} from '../api/client'
import type { MoveDirection } from '../api/client'
import type { FileNode, ItemEventOut, JobOut } from '../api/types'
import { ArrIcon, ArrRowChip } from '../components/LifecycleIcons'
import { ItemDrawer } from '../components/ItemDrawer'
import { StartNowMenu } from '../components/StartNowMenu'
import { StateChip } from '../components/StateChip'
import { useCompleteJobs } from '../hooks/useCompleteJobs'
import { useJobs } from '../hooks/useJobs'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import { useLiveModel } from '../hooks/useLiveModel'
import { arrHoverLabel, nodeDisplaySize, stateProgressPercent } from '../lib/fileTree'
import { childSpeedLabel, formatBytes, formatRelativeTimeIntl } from '../lib/format'
import { ACTIVE_PAGE_SIZE, COMPLETE_PAGE_SIZE, pageCount, pageWindow, paginateClientSide } from '../lib/pagination'
import { queueDisplayName } from '../lib/queueDisplayName'
import type { StartNowRatePercent } from '../lib/startNow'
import {
  FAST_LANE_HINT,
  type LiveProgress,
  type PanelField,
  canMoveDown,
  canMoveUp,
  childDisplayName,
  completedTimeLabel,
  fileListCapNote,
  filterTransferJobs,
  hasArrGroup,
  isDismissable,
  isFastLane,
  mergeFileListChildren,
  processingGroupFields,
  showsFileList,
  sortTransferRows,
  transferFilterSummary,
  transferGroupFields,
  transferLineValue,
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
  // `child_progress` WS samples for this job's own queue, keyed by item id (2026-08-20,
  // docs/transfers-redesign-spec.md §3.3 stage 5) -- threaded down to the file-list expansion
  // the same way `nodes` already is, so N expanded rows read the one already-open socket
  // instead of polling. See `TransfersPage.tsx`'s own `useLiveModel()` call.
  childSpeedByItemId: Record<number, ChildSpeedSample>
  live: LiveProgress | undefined
  // Where this job sits in the actual run order (2026-08-13, prompts/2026-08-13-files-ux-pass.md
  // item 4) -- 1, 2, 3... counting only `state === 'queued'` rows, in the order `useJobs` already
  // returns them (`core/queue.py.list_jobs`'s own `ORDER BY queue_position ASC, id ASC`,
  // 2026-08-19 -- was `rank DESC, queued_at ASC` -- the real future run order). `undefined`
  // for a running/failed/cancelled row -- those aren't
  // "queued" in the sense a position means anything for.
  queuePosition: number | undefined
  // How many rows are currently `queued` (2026-08-19, `TransfersPage.tsx`'s own
  // `queuePositions.size`) -- `canMoveDown` (`lib/transferPanel.ts`) needs this alongside
  // `queuePosition` to know whether this row is already last in the global order.
  queuedCount: number
  // Settings -> Transfer's site total limit (2026-08-19,
  // prompts/done/2026-08-19-start-now-bandwidth-fractions.md) -- fed straight into
  // `StartNowMenu`, which decides (via `lib/startNow.ts`) whether the fraction options are
  // enabled. `undefined` while `GET /api/settings/transfer` is still in flight.
  maxBandwidthBps: number | undefined
  onOpenDrawer: (job: JobOut) => void
  // The chevron reorder controls (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2) --
  // one handler for ▲/▼/▲▲, replacing the previous single-purpose `onMoveToTop`.
  onMove: (job: JobOut, direction: MoveDirection) => void
  onStartNow: (job: JobOut, ratePercent: StartNowRatePercent | undefined) => void
  onStop: (job: JobOut) => void
  onRetry: (job: JobOut) => void
  onDismiss: (job: JobOut) => void
  busy: boolean
}

function Row({
  job,
  nodes,
  childSpeedByItemId,
  live,
  queuePosition,
  queuedCount,
  maxBandwidthBps,
  onOpenDrawer,
  onMove,
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
            title={`Queue position ${queuePosition} -- runs in this order (use the row's move controls to reorder)`}
          >
            #{queuePosition}
          </span>
        )}
        {/* Queue badge (2026-08-19, docs/transfers-redesign-spec.md §3.6, phase 1 stage 4a) --
         * a compact, muted locator, not a headline: dropping per-queue grouping (this task)
         * means each row now needs to say which queue it's from on its own. `queueDisplayName`
         * is the same `short_name || name` fallback `api/settings_queues.py.
         * resolve_queue_display_name` uses server-side, so this always agrees with Settings ->
         * Queues. The full name sits in the `title` for hover -- the same "chip short, hover
         * long" split the name button beside it already uses for `rel_path`. */}
        <span
          className="max-w-[8rem] shrink-0 truncate rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
          title={job.queue_name}
        >
          {queueDisplayName(job.queue_short_name, job.queue_name)}
        </span>
        {/* Fast-lane marker (2026-08-19, spec §3.5) -- small items (under
         * `small_item_threshold_bytes`, 10 MB default, DESIGN.md §4.5) admit from a separate
         * lane with its own concurrency cap and reserved bandwidth, so a job at #9 can genuinely
         * start before the main-lane job at #2. Decided: keep one `1..N` numbering and mark the
         * row rather than give the fast lane its own numbering or its own box -- the tooltip
         * (`FAST_LANE_HINT`) says why, not just that. */}
        {isFastLane(job) && (
          <span
            className="shrink-0 rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
            title={FAST_LANE_HINT}
          >
            fast lane
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
              {/* Chevron reorder controls (2026-08-19, docs/transfers-redesign-spec.md §3.4
               * stage 2) -- ▲▲ to top, ▲ up one, ▼ down one, replacing the previous single
               * "Move to top" button. Global scope, matching the global scheduler and the
               * global numbering (`queuePositions` below). Stage 2 shipped this while the page
               * still grouped rows by queue, so a move could appear to swap a job with something
               * in a *different* group -- a known intermediate state at the time, not a bug.
               * Stage 4a (phase 1, this task) drops grouping for one flat, globally-ordered
               * list, which resolves it: the row directly above in the global order is now also
               * the row directly above on screen, so ▲ always trades with what's visually right
               * there. Disabled at the edges of the *global* queued order (`canMoveUp`/
               * `canMoveDown`, `lib/transferPanel.ts`) -- the backend's own `move_job` treats an
               * out-of-turn request as a silent no-op regardless, so this is the UI affordance on
               * top of that guard, not the only one. */}
              <button
                type="button"
                disabled={busy || !canMoveUp(queuePosition)}
                onClick={() => onMove(job, 'top')}
                aria-label="Move to top of queue"
                title="Move to top of queue"
                className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
              >
                ▲▲
              </button>
              <button
                type="button"
                disabled={busy || !canMoveUp(queuePosition)}
                onClick={() => onMove(job, 'up')}
                aria-label="Move up one"
                title="Move up one"
                className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
              >
                ▲
              </button>
              <button
                type="button"
                disabled={busy || !canMoveDown(queuePosition, queuedCount)}
                onClick={() => onMove(job, 'down')}
                aria-label="Move down one"
                title="Move down one"
                className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
              >
                ▼
              </button>
              {job.forced_rate_fraction == null && (
                <StartNowMenu
                  disabled={busy}
                  maxBandwidthBps={maxBandwidthBps}
                  onSelect={(ratePercent) => onStartNow(job, ratePercent)}
                />
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

      {expanded && (
        <RowDetailPanel job={job} live={live} fileCount={fileCount} nodes={nodes} childSpeedByItemId={childSpeedByItemId} />
      )}
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

/** A failed row's captured lftp output (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase
 * 1 stage 4b) -- inline when `job.output_tail` already carries it (the Active/pending box's own
 * rows, from the bounded `GET /api/jobs`, unchanged), fetched on demand via the existing `GET
 * /api/history/jobs/{id}/output` (`getHistoryJobOutput`) when it doesn't (a Complete-box row,
 * from the paginated `GET /api/jobs/complete`, which never inlines this ~4KB blob -- see
 * `JobOut.has_output_tail`'s own comment). One component instead of branching inline in
 * `RowDetailPanel` below, and the same "fetch once, on expand" shape this panel's own item-
 * events `useEffect` already establishes a few lines down.
 */
function FailedOutputPanel({ job }: { job: JobOut }) {
  const needsFetch = job.output_tail == null && job.has_output_tail
  const [output, setOutput] = useState<string | null>(job.output_tail)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!needsFetch) {
      setOutput(job.output_tail)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    getHistoryJobOutput(job.id)
      .then((res) => {
        if (!cancelled) setOutput(res.output_tail)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id, needsFetch])

  return (
    <div className="mt-2 rounded-md border border-red-200 bg-red-50 p-2 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
      <p className="font-medium">{job.error_class ?? 'UNKNOWN'}</p>
      {loading && <p className="mt-1 opacity-75">Loading captured output…</p>}
      {error && <p className="mt-1 text-xs">Couldn't load captured output: {error}</p>}
      {output && (
        <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] opacity-90">
          {output}
        </pre>
      )}
    </div>
  )
}

const ITEM_EVENTS_LIMIT = 20

/** One row of the file-list group below -- a leaner sibling of `FileTree.tsx`'s own `Row`, not
 * that component reused: this list is a handful of label/value cells inside an expand panel, not
 * a virtualized tree with its own selection/hover-card/column-resize machinery, so reusing `Row`
 * wholesale would drag all of that in for no benefit. What *is* shared is the actual
 * presentation logic (`stateProgressPercent`/`nodeDisplaySize`/`childSpeedLabel`/`formatBytes`,
 * all from `lib/fileTree.ts`/`lib/format.ts`) -- see this file's own module comment.
 */
function FileListRow({ row, jobRelPath }: { row: ReturnType<typeof mergeFileListChildren>[number]; jobRelPath: string }) {
  const percent = stateProgressPercent(row.state, row.local_size, row.remote_size)
  const size = nodeDisplaySize({ is_dir: false, local_size: row.local_size, remote_size: row.remote_size })
  const speedLabel = childSpeedLabel(row.speed_bps)
  return (
    <li className="flex items-center gap-2 py-0.5">
      <span className="min-w-0 flex-1 truncate text-zinc-700 dark:text-zinc-300" title={row.rel_path}>
        {childDisplayName(row.rel_path, jobRelPath)}
      </span>
      <StateChip state={row.state} percent={percent} />
      <span className="w-16 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
        {size != null ? formatBytes(size) : '—'}
      </span>
      <span className="w-20 shrink-0 text-right text-zinc-500 dark:text-zinc-400">{speedLabel}</span>
    </li>
  )
}

/** The Transfers row's **Files** group (2026-08-20, docs/transfers-redesign-spec.md §3.3, phase
 * 1 stage 5) -- "the thing Files is currently used for, moved to where the ordering lives."
 * Only rendered for a directory (`mirror`) job (`showsFileList`, `RowDetailPanel` below); a
 * `pget` job's single file already has its own progress on the collapsed line, so there is
 * nothing for this group to add for it.
 *
 * **Fetched once, on mount** (`GET /api/items/{id}/children`, capped server-side) -- the same
 * "fetch once, on expand" shape `FailedOutputPanel`/this panel's own Processing group already
 * establish. **Never re-fetched for live updates**: once the initial, bounded row set has
 * landed, `mergeFileListChildren` overlays the freshest `state`/`local_size`/`speed_bps` from
 * `nodes`/`childSpeedByItemId` -- the same `item_delta`/`child_progress` WebSocket messages
 * `TransfersPage.tsx`'s single `useLiveModel()` call already receives for the Files page. That is
 * the whole point of threading `nodes`/`childSpeedByItemId` down from there rather than polling
 * this endpoint: ten expanded rows read the one already-open socket, not ten independent pollers.
 */
function FileListGroup({
  job,
  nodes,
  childSpeedByItemId,
}: {
  job: JobOut
  nodes: FileNode[]
  childSpeedByItemId: Record<number, ChildSpeedSample>
}) {
  const [fetched, setFetched] = useState<FileNode[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getItemChildren(job.item_id)
      .then((res) => {
        if (cancelled) return
        setFetched(res.children)
        setTotal(res.total)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [job.item_id])

  const rows = useMemo(
    () => (fetched != null ? mergeFileListChildren(fetched, nodes, childSpeedByItemId) : []),
    [fetched, nodes, childSpeedByItemId],
  )
  const capNote = fetched != null ? fileListCapNote(fetched.length, total) : null

  return (
    <div>
      <h4 className="mb-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
        Files
      </h4>
      {loading && fetched == null && <p className="text-zinc-400 dark:text-zinc-600">Loading files…</p>}
      {error && <p className="text-red-600 dark:text-red-400">Couldn't load files: {error}</p>}
      {fetched != null && fetched.length === 0 && !loading && (
        <p className="text-zinc-400 dark:text-zinc-600">No files tracked yet.</p>
      )}
      {rows.length > 0 && (
        <ul className="flex flex-col divide-y divide-zinc-100 dark:divide-zinc-800">
          {rows.map((row) => (
            <FileListRow key={row.rel_path} row={row} jobRelPath={job.rel_path} />
          ))}
        </ul>
      )}
      {capNote && <p className="mt-1 text-zinc-400 dark:text-zinc-600">{capNote}</p>}
    </div>
  )
}

/** The Transfers row's expand panel (2026-08-15) -- four groups now (2026-08-20, stage 5 added
 * **Files**): **Transfer** (bytes/elapsed/speed/queued-wait/file-count, plus a failed job's
 * error class + output tail), **Files** (per-file progress, directory jobs only --
 * `showsFileList`), **Processing** (the item's verify/extract/remote-delete milestones, enriched
 * by the pipeline's own event messages -- fetched on demand, exactly once, when this panel first
 * opens), and ***arr** (hidden entirely when the job's queue has no bound instance).
 *
 * **One expand affordance, one panel, multiple sections** -- not a second chevron. The Complete
 * box's failed-row output (`FailedOutputPanel`, unchanged, still nested inside the Transfer
 * group below) already established that this panel is where every "more detail on this same
 * row" surface lives; Files joins that list rather than getting its own toggle, so a
 * failed-and-directory row can show its captured output *and* its per-file breakdown from the
 * one click that already opens everything else.
 */
function RowDetailPanel({
  job,
  live,
  fileCount,
  nodes,
  childSpeedByItemId,
}: {
  job: JobOut
  live: LiveProgress | undefined
  fileCount: number
  nodes: FileNode[]
  childSpeedByItemId: Record<number, ChildSpeedSample>
}) {
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
        {job.state === 'failed' && <FailedOutputPanel job={job} />}
      </div>

      {showsFileList(job) && <FileListGroup job={job} nodes={nodes} childSpeedByItemId={childSpeedByItemId} />}

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

/** Numbered pages, SAB-style (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
 * 4b) -- `1 2 3 4 ›`, the task's own example. All the boundary arithmetic (the visible window,
 * whether ‹/› are enabled) lives in `lib/pagination.ts` and is unit-tested there; this component
 * is pure layout over that. Renders nothing at all for a single-page box -- a pager with one,
 * disabled page number is clutter, not a control.
 */
function Pager({ current, count, onChange }: { current: number; count: number; onChange: (page: number) => void }) {
  if (count <= 1) return null
  const visible = pageWindow(current, count)
  return (
    <div className="flex items-center gap-1">
      <button
        type="button"
        disabled={current <= 1}
        onClick={() => onChange(current - 1)}
        aria-label="Previous page"
        className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
      >
        ‹
      </button>
      {visible.map((p) => (
        <button
          key={p}
          type="button"
          aria-current={p === current ? 'page' : undefined}
          onClick={() => onChange(p)}
          className={
            p === current
              ? 'rounded-md border border-indigo-400 bg-indigo-50 px-2 py-1 text-xs font-semibold text-indigo-800 dark:border-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300'
              : 'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'
          }
        >
          {p}
        </button>
      ))}
      <button
        type="button"
        disabled={current >= count}
        onClick={() => onChange(current + 1)}
        aria-label="Next page"
        className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
      >
        ›
      </button>
    </div>
  )
}

/** DESIGN.md §9.2 Transfers page -- the job queue. Rows stay deliberately plain (queued /
 * downloading / downloaded, with STOPPED/FAILED surfacing only where they apply); the item
 * drawer opens per row for the full per-file breakdown.
 */
export function TransfersPage() {
  const { jobs, refresh } = useJobs()
  // `childSpeedByItemId` (2026-08-20, docs/transfers-redesign-spec.md §3.3 stage 5) -- the same
  // `useLiveModel()` call this page already made for `queues`/`progressByJobId`, just reading one
  // more field off its return value. Not a second WebSocket: `useLiveModel` opens one connection
  // per call, and this page already has exactly one call, so widening what it reads costs nothing
  // in request volume no matter how many rows a user expands.
  const { queues, progressByJobId, childSpeedByItemId } = useLiveModel()
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set())
  const [drawerJob, setDrawerJob] = useState<JobOut | null>(null)
  const [startNowNotice, setStartNowNotice] = useState(false)
  // Settings -> Transfer's site total limit (2026-08-19,
  // prompts/done/2026-08-19-start-now-bandwidth-fractions.md) -- fetched once on mount, purely
  // to decide whether the "Start now" menu's fraction options are enabled
  // (`lib/startNow.ts.isSiteLimitConfigured`). `undefined` until the request resolves; every
  // row's menu reads disabled in the meantime, same as "not configured" (`StartNowMenu`'s own
  // fallback).
  const [maxBandwidthBps, setMaxBandwidthBps] = useState<number | undefined>(undefined)
  const [clearingAll, setClearingAll] = useState(false)
  const [dismissOutcome, setDismissOutcome] = useState<DismissOutcome | null>(null)
  const [dismissingAll, setDismissingAll] = useState(false)
  const [dismissAllError, setDismissAllError] = useState<string | null>(null)
  const [dismissAllCount, setDismissAllCount] = useState<number | null>(null)
  // The name filter (2026-08-19, prompts/2026-08-19-transfers-name-filter.md) -- plain
  // `useState`, deliberately not persisted (no localStorage, no URL param): it clears on reload
  // and on navigating away, matching the Files page's own text filter and the Logs filter. A
  // stale filter hiding active transfers after a reload would be its own confusion. Drives the
  // Active/pending box (below) directly and instantly -- that box is client-side over an
  // already-loaded, bounded set, so there's nothing to debounce.
  const [search, setSearch] = useState('')
  // The Complete box's own copy of `search`, debounced (2026-08-19, docs/transfers-redesign-
  // spec.md §3.2, phase 1 stage 4b) -- that box is now server-paginated (`useCompleteJobs`
  // below), so every keystroke would otherwise fire its own request. "Dismiss list" also reads
  // this, not `search` directly, so the count it shows and the filter text it sends to
  // `dismiss_all_terminal` always match what the Complete box is actually displaying.
  const [debouncedSearch, setDebouncedSearch] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search), 250)
    return () => clearTimeout(id)
  }, [search])
  // "Dismiss list" -- its own busy/error/outcome trio, same shape "Dismiss all"'s
  // dismissingAll/dismissAllError/dismissAllCount above already use (the page's existing
  // notification convention), kept separate rather than reusing those three: "Dismiss list" is
  // a distinct control the user can click independently of (and possibly around the same time
  // as) "Dismiss all", which stays completely unchanged by this task. It also supersedes the
  // per-queue "Dismiss Queue" control this task removes (2026-08-19, docs/transfers-redesign-
  // spec.md §3.1, phase 1 stage 4a) -- filter to a queue, then "Dismiss list" does the same job.
  const [dismissingList, setDismissingList] = useState(false)
  const [dismissListError, setDismissListError] = useState<string | null>(null)
  const [dismissListCount, setDismissListCount] = useState<number | null>(null)
  // Two paginated boxes (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b) --
  // 1-based, independent page state per box. `lib/pagination.ts` owns every bit of the
  // boundary/window arithmetic; this page only ever stores "which page" and hands it there.
  const [activePage, setActivePage] = useState(1)
  const [completePage, setCompletePage] = useState(1)

  const nodesByQueue = useMemo(() => {
    const map = new Map<number, FileNode[]>()
    for (const q of queues) map.set(q.queue_id, q.nodes)
    return map
  }, [queues])

  // Fetched once, not polled -- the "Start now" menu only needs to know whether a site limit is
  // configured at all, not track it live; a page reload after a Settings -> Transfer change
  // picks up the new value, the same freshness every other one-shot settings read on this page
  // already has. A failed fetch leaves `maxBandwidthBps` `undefined`, which every fraction
  // option already treats as "not configured" (`lib/startNow.ts.isSiteLimitConfigured`) --
  // Max stays available regardless, so there is nothing to show the user beyond that.
  useEffect(() => {
    let cancelled = false
    getTransferSettings()
      .then((settings) => {
        if (!cancelled) setMaxBandwidthBps(settings.max_bandwidth_bps)
      })
      .catch(() => {
        // Deliberately silent -- see the comment above.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // --- Active / pending box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
  // 4b) -- client-side paginated, 20/page, over `jobs` narrowed to `queued`/`running` only.
  // `jobs` itself (`useJobs`/`GET /api/jobs`, `core/queue.py.list_jobs`) is deliberately
  // unchanged by this task (see docs/decisions.md for why keeping it was chosen over narrowing
  // it) -- it still carries each item's most recent terminal job too; this box simply doesn't
  // render those any more, since the Complete box below owns them now. -------------------------

  const activeJobs = useMemo(
    () => jobs.filter((j) => j.state === 'queued' || j.state === 'running'),
    [jobs],
  )
  // Row order (2026-08-16, `lib/transferPanel.ts.sortTransferRows`'s own docstring): running
  // before queued, in `jobs`' own scheduler order -- unaffected by this task, since `activeJobs`
  // never contains a terminal row for `sortTransferRows`'s own newest-first branch to act on.
  const sortedActiveJobs = useMemo(() => sortTransferRows(activeJobs), [activeJobs])
  // The name filter, applied instantly (client-side, bounded set) -- `filterTransferJobs`
  // returns `sortedActiveJobs` itself, by identity, whenever `search` is empty/whitespace-only,
  // so this is a no-op `useMemo` recompute rather than a fresh array on every render while the
  // filter isn't in use.
  const filteredActiveJobs = useMemo(() => filterTransferJobs(sortedActiveJobs, search), [sortedActiveJobs, search])
  const filterActive = search.trim() !== ''
  const activeFilterSummary = transferFilterSummary(filteredActiveJobs.length, sortedActiveJobs.length, search)
  const activePageCount = pageCount(filteredActiveJobs.length, ACTIVE_PAGE_SIZE)
  const activePageJobs = useMemo(
    () => paginateClientSide(filteredActiveJobs, activePage, ACTIVE_PAGE_SIZE),
    [filteredActiveJobs, activePage],
  )
  // Reset to page 1 when the filter text changes (task's own instruction) -- keyed on `search`
  // alone, never on `jobs` (which changes every ~2s poll tick regardless of the filter), so
  // typing resets the page but a background refresh never does.
  useEffect(() => {
    setActivePage(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])
  // Clamp rather than render an empty box (task's own instruction) -- the filter narrows (or
  // jobs finish and leave the active set) while on a page that no longer exists.
  useEffect(() => {
    if (activePage > activePageCount) setActivePage(activePageCount)
  }, [activePage, activePageCount])

  // --- Complete box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b) --
  // server-side paginated and filtered, 50/page, newest-finished first
  // (`useCompleteJobs`/`GET /api/jobs/complete`). ------------------------------------------

  const {
    jobs: completeJobs,
    total: completeTotal,
    loading: completeLoading,
    error: completeError,
    refresh: refreshComplete,
  } = useCompleteJobs(completePage, debouncedSearch)
  const completeFilterActive = debouncedSearch.trim() !== ''
  const completePageCount = pageCount(completeTotal, COMPLETE_PAGE_SIZE)
  // Same reset-on-filter-change/clamp-on-narrow pair as the Active box above, keyed on the
  // debounced filter text and the server-reported total respectively.
  useEffect(() => {
    setCompletePage(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch])
  useEffect(() => {
    if (completePage > completePageCount) setCompletePage(completePageCount)
  }, [completePage, completePageCount])

  // Queue position (2026-08-13, item 4): `jobs` (`useJobs`/`GET /api/jobs`) already comes back
  // in the real run order (`core/queue.py.list_jobs`'s `ORDER BY queue_position ASC, id ASC`,
  // 2026-08-19 -- was `rank DESC, queued_at ASC`) -- no new endpoint, just counting the
  // `queued` rows in the order already returned. Read off `jobs` (not `activeJobs`/`sortedActive
  // Jobs`), same as before this task -- a queue position is about the real future run order and
  // must stay correct regardless of how either box happens to be filtered/paginated for display.
  // A Complete-box row's id is never a key in this map (it's never `queued`), so `Row`'s own
  // `queuePosition` prop -- and therefore its chevrons -- naturally never render there, with no
  // extra branching needed at either call site below.
  const queuePositions = useMemo(() => {
    const positions = new Map<number, number>()
    let n = 0
    for (const job of jobs) {
      if (job.state === 'queued') positions.set(job.id, ++n)
    }
    return positions
  }, [jobs])
  const queuedCount = queuePositions.size

  // Every row action refreshes both boxes, not just `jobs` (2026-08-19, phase 1 stage 4b) --
  // a Retry on a Complete-box row, for instance, supersedes that item's old terminal job (so it
  // must disappear from the Complete box) and creates a fresh `queued` one (so it must appear in
  // the Active box); a Stop on an Active-box row lands it in `cancelled`, which the Complete box
  // must then pick up. Cheaper than reasoning per-action about which one box could possibly be
  // affected -- both fetches are already cheap, polled ones.
  const refreshAll = () => {
    refresh()
    refreshComplete()
  }

  const withBusy = async (jobId: number, action: () => Promise<unknown>) => {
    setBusyIds((prev) => new Set(prev).add(jobId))
    try {
      await action()
      refreshAll()
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev)
        next.delete(jobId)
        return next
      })
    }
  }

  const handleMove = (job: JobOut, direction: MoveDirection) =>
    withBusy(job.id, () => moveJob(job.id, direction))
  const handleStop = (job: JobOut) => withBusy(job.id, () => stopJob(job.id))
  const handleRetry = (job: JobOut) => withBusy(job.id, () => retryItem(job.item_id))
  const handleDismiss = (job: JobOut) => withBusy(job.id, () => dismissJob(job.id))
  const handleStartNow = (job: JobOut, ratePercent: StartNowRatePercent | undefined) => {
    if (localStorage.getItem(START_NOW_EXPLAINED_KEY) !== '1') {
      setStartNowNotice(true)
      localStorage.setItem(START_NOW_EXPLAINED_KEY, '1')
    }
    return withBusy(job.id, () => startJobNow(job.id, ratePercent))
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
      refreshAll()
    } catch (err) {
      setDismissAllError(errorMessage(err))
    } finally {
      setDismissingAll(false)
    }
  }

  /** "Dismiss list" (2026-08-19, prompts/2026-08-19-transfers-name-filter.md) -- the name
   * filter's own dedicated control. A new, separate control -- not a re-scoping of "Clear all
   * failed"/"Dismiss all" above, which keep their existing whole-list meaning untouched by this
   * task. Supersedes the per-queue "Dismiss Queue" control (v0.2.3, `278e10f`), removed
   * 2026-08-19 alongside grouping (docs/transfers-redesign-spec.md §3.1) -- filter to a queue,
   * then this does the same job. No confirmation dialog: dismiss only ever sets `dismissed_at`
   * on an already-terminal job row, never touches `item.state`, never deletes bytes, and never
   * touches the remote.
   *
   * **Re-scoped from an explicit id list to the filter text itself, 2026-08-19** (docs/
   * transfers-redesign-spec.md §3.2, phase 1 stage 4b), the moment the Complete box became
   * server-paginated: `filteredDismissableIds` (the old scope) only ever named the ids on the
   * page currently loaded, which is not what this button promises once a filter can match more
   * than one page's worth. `dismissAllJobs(undefined, undefined, debouncedSearch)` ->
   * `core/queue.py.dismiss_all_terminal(name_filter=...)` dismisses every matching row
   * server-side in one request, still never a client-side loop over each row's own `/dismiss`
   * call. Uses `debouncedSearch`, not `search`, so this always dismisses exactly what the
   * Complete box is currently showing (`completeTotal` is that same query's own matching
   * count) -- never a half-typed filter the box hasn't caught up to yet.
   */
  const handleDismissList = async () => {
    if (!completeFilterActive || completeTotal === 0) return
    setDismissingList(true)
    setDismissListError(null)
    setDismissListCount(null)
    try {
      // Trimmed, matching `useCompleteJobs`'s own trim before it reaches `getCompleteJobs` --
      // otherwise a filter with incidental leading/trailing whitespace could dismiss a
      // different set of rows than the Complete box is actually displaying.
      const res = await dismissAllJobs(undefined, undefined, debouncedSearch.trim())
      setDismissListCount(res.dismissed)
      refreshAll()
    } catch (err) {
      setDismissListError(errorMessage(err))
    } finally {
      setDismissingList(false)
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
      refreshAll()
    } finally {
      setClearingAll(false)
    }
  }

  // "Dismiss list"'s disabled/tooltip logic (2026-08-19) -- disabled while the (debounced)
  // filter is empty (nothing to scope it to), and disabled again once it's non-empty but the
  // Complete box's own query matched zero rows -- a live tooltip says which of the two it is, so
  // a greyed-out button never reads as simply broken. Reads `completeTotal`, the exact count
  // `core/queue.py.dismiss_all_terminal(name_filter=...)` would act on for this same text
  // (`DismissAllRequest.name_filter`'s own docstring), not a client-side id count.
  const dismissListDisabled = !completeFilterActive || completeTotal === 0 || dismissingList
  const dismissListTitle = !completeFilterActive
    ? 'Type in the filter above to enable -- dismisses only the terminal rows it matches'
    : completeTotal === 0
      ? "No dismissable rows match this filter -- everything matching is still queued or downloading"
      : `Dismiss the ${completeTotal} matching row${completeTotal === 1 ? '' : 's'} that ${completeTotal === 1 ? 'is' : 'are'} finished -- records stay on the History page`

  const drawerNodes = drawerJob ? (nodesByQueue.get(drawerJob.queue_id) ?? []) : []

  return (
    <div className="flex flex-col gap-3">
      {startNowNotice && (
        <div className="flex items-start justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <p>
            <strong>Start now</strong> admits this job immediately at your chosen share of the
            site bandwidth limit — 10%/25%/50%/75%, or Max for the full ceiling — deliberately
            <em> oversubscribing</em> past what other running jobs are allocated (DESIGN.md
            §4.5). It's the "I want this one now" escape hatch: new admissions pause until enough
            jobs finish to bring the total back under the ceiling, rather than throttling what's
            already running. The percent options need a site bandwidth limit configured
            (Settings → Transfer) — Max always works.
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

      {/* Name filter + "Dismiss list" (2026-08-19, prompts/2026-08-19-transfers-name-filter.md)
       * -- start typing and only rows whose `rel_path` contains the text stay visible: instantly
       * in the Active/pending box below (client-side, bounded), after a short debounce in the
       * Complete box (server-side, paginated -- `useCompleteJobs`). "Dismiss list" is a separate
       * control scoped to exactly what the filter currently matches, server-side, across every
       * page (2026-08-19, phase 1 stage 4b -- `handleDismissList`'s own docstring) -- "Clear all
       * failed"/"Dismiss all" below keep their existing whole-list meaning, untouched by this
       * task, including while a filter is active. */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by name…"
          className="min-w-0 flex-1 rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400 focus:outline-none dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:placeholder:text-zinc-600"
        />
        {activeFilterSummary && (
          <span className="shrink-0 text-xs text-zinc-500 dark:text-zinc-400">{activeFilterSummary}</span>
        )}
        <button
          type="button"
          disabled={dismissListDisabled}
          onClick={handleDismissList}
          title={dismissListTitle}
          className="shrink-0 rounded-md border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {dismissingList ? 'Dismissing…' : `Dismiss list${completeTotal > 0 ? ` (${completeTotal})` : ''}`}
        </button>
      </div>

      {dismissListCount != null && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
          <span className="font-medium">
            Dismissed {dismissListCount} job{dismissListCount === 1 ? '' : 's'}
          </span>
          <button
            type="button"
            onClick={() => setDismissListCount(null)}
            className="shrink-0 text-xs underline decoration-dotted"
          >
            Dismiss
          </button>
        </div>
      )}

      {dismissListError && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span>Couldn't dismiss list: {dismissListError}</span>
          <button
            type="button"
            onClick={() => setDismissListError(null)}
            className="shrink-0 text-xs underline decoration-dotted"
          >
            Dismiss
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

      {/* "Nothing queued or downloading" now reads off `activeJobs` (2026-08-19, phase 1 stage
       * 4b), not `jobs` -- `jobs` still carries each item's most recent terminal job too
       * (unchanged, docs/decisions.md), which would otherwise suppress this message the moment
       * anything had ever finished, even with nothing currently active. */}
      {activeJobs.length === 0 && (
        <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          Nothing queued or downloading — queue an item from Files.
        </div>
      )}

      {/* The filter's own empty state for the Active/pending box (2026-08-19) -- distinct from
       * the "nothing queued or downloading" state above: there *are* active transfers, none of
       * them just happen to match the filter text. The Complete box's own empty/no-match states
       * render separately, inside that box below. */}
      {activeJobs.length > 0 && filterActive && filteredActiveJobs.length === 0 && (
        <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          <span>No active transfers match "{search.trim()}".</span>
          <button
            type="button"
            onClick={() => setSearch('')}
            className="text-xs text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
          >
            Clear filter
          </button>
        </div>
      )}

      {/* Makes the row order legible as an order (2026-08-13, item 4) -- "what is the proper
       * way to see the priority of the download queue" was the ask; the ordering already
       * existed (`rank DESC, queued_at ASC`) but nothing said so. Only shown once there's an
       * actual order to explain (2+ queued jobs) -- a single queued job's position is not
       * interesting on its own. Unaffected by the Active box's own client-side pagination
       * (2026-08-19, phase 1 stage 4b) -- the chevrons still trade a job with its true neighbour
       * in the global order even when that neighbour is a page away and not currently on screen.
       */}
      {queuedCount > 1 && (
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Queued jobs run in the order shown, top to bottom — use each row's{' '}
          <strong>▲ / ▼ / ▲▲</strong> controls to reorder.
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

      {/* Two paginated boxes, no per-queue grouping (2026-08-19, docs/transfers-redesign-
       * spec.md §3.1/§3.2, phase 1 stages 4a/4b). `core/scheduler.py` has zero references to
       * `queue_id` -- admission is one global line -- so grouping by queue visually implied each
       * queue had its own line and its own ordering, which was false. `Row`'s own queue badge
       * (`queueDisplayName`) and fast-lane marker (`isFastLane`) replace the group header's
       * queue name -- the name filter is what makes that safe to drop (docs/decisions.md).
       *
       * **Active / pending** -- client-side paginated (20/page): the ▲/▼ chevrons still always
       * move a job in the *global* order, matching the caption above; within one page the row
       * directly above is the one being traded with, same as the flat stage-4a list, though a
       * move across a page boundary trades with a neighbour not currently on screen (an accepted
       * consequence of paginating this box, not a bug).
       *
       * **Complete** -- server-side paginated (50/page), newest-finished first
       * (`useCompleteJobs`/`GET /api/jobs/complete`). Rows shifting between pages as more work
       * finishes is accepted and explicitly not a problem to solve (the spec's own words, same
       * as SAB). `Row` itself needed no changes for this box: a terminal job's id is never a key
       * in `queuePositions`, so its chevrons/Start-now/Stop simply don't render, the same
       * `job.state === 'queued'`/`'running'` guards `Row` already had. */}
      {activePageJobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Active / pending
          </h2>
          <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
            {activePageJobs.map((job) => (
              <Row
                key={job.id}
                job={job}
                nodes={nodesByQueue.get(job.queue_id) ?? []}
                childSpeedByItemId={childSpeedByItemId}
                live={progressByJobId[job.id]}
                queuePosition={queuePositions.get(job.id)}
                queuedCount={queuedCount}
                maxBandwidthBps={maxBandwidthBps}
                onOpenDrawer={setDrawerJob}
                onMove={handleMove}
                onStartNow={handleStartNow}
                onStop={handleStop}
                onRetry={handleRetry}
                onDismiss={handleDismiss}
                busy={busyIds.has(job.id)}
              />
            ))}
          </div>
          <div className="flex items-center justify-end">
            <Pager current={activePage} count={activePageCount} onChange={setActivePage} />
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Complete
        </h2>

        {completeError && (
          <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <span>Couldn't load completed transfers: {completeError}</span>
          </div>
        )}

        {!completeLoading && completeJobs.length === 0 && (
          <div className="flex h-40 flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
            {completeFilterActive ? (
              <>
                <span>No completed transfers match "{debouncedSearch.trim()}".</span>
                <button
                  type="button"
                  onClick={() => setSearch('')}
                  className="text-xs text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
                >
                  Clear filter
                </button>
              </>
            ) : (
              <span>Nothing finished yet.</span>
            )}
          </div>
        )}

        {completeJobs.length > 0 && (
          <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
            {completeJobs.map((job) => (
              <Row
                key={job.id}
                job={job}
                nodes={nodesByQueue.get(job.queue_id) ?? []}
                childSpeedByItemId={childSpeedByItemId}
                live={progressByJobId[job.id]}
                queuePosition={queuePositions.get(job.id)}
                queuedCount={queuedCount}
                maxBandwidthBps={maxBandwidthBps}
                onOpenDrawer={setDrawerJob}
                onMove={handleMove}
                onStartNow={handleStartNow}
                onStop={handleStop}
                onRetry={handleRetry}
                onDismiss={handleDismiss}
                busy={busyIds.has(job.id)}
              />
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {completeTotal > 0 &&
              `Page ${completePage} of ${completePageCount} (${completeTotal} total)`}
          </span>
          <Pager current={completePage} count={completePageCount} onChange={setCompletePage} />
        </div>
      </div>

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
