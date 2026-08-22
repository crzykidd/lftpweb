import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import {
  dismissAllJobs,
  dismissJob,
  getHealth,
  getHistoryJobOutput,
  getItemChildren,
  getItemEvents,
  getTransferSettings,
  moveJob,
  pauseQueue,
  resolveItem,
  retryItem,
  startJobNow,
  stopJob,
  unpauseQueue,
} from '../api/client'
import type { MoveDirection } from '../api/client'
import type { FileNode, ItemEventOut, JobOut } from '../api/types'
import { ArrIcon, ArrRowChip } from '../components/LifecycleIcons'
import { BandwidthControl } from '../components/BandwidthControl'
import { DismissMenu } from '../components/DismissMenu'
import { ItemDrawer } from '../components/ItemDrawer'
import { Pager } from '../components/Pager'
import { PageSizeSelect } from '../components/PageSizeSelect'
import { PauseMenu } from '../components/PauseMenu'
import { PreflightBox } from '../components/PreflightBox'
import { ResolveMenu } from '../components/ResolveMenu'
import { StartNowMenu } from '../components/StartNowMenu'
import { StateChip } from '../components/StateChip'
import { useCompleteJobs } from '../hooks/useCompleteJobs'
import { useJobs } from '../hooks/useJobs'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import { useLiveModel } from '../hooks/useLiveModel'
import { usePoll } from '../hooks/usePoll'
import { usePreflight } from '../hooks/usePreflight'
import { useRescan } from '../hooks/useRescan'
import { arrHoverLabel, childDisplayState, nodeDisplaySize, stateProgressPercent } from '../lib/fileTree'
import { childSpeedLabel, formatBytes, formatRelativeTimeIntl, pauseResumeLabel } from '../lib/format'
import {
  ACTIVE_PAGE_SIZE,
  COMPLETE_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  isPageSize,
  pageCount,
  pageReadout,
  paginateClientSide,
} from '../lib/pagination'
import type { PageSize } from '../lib/pagination'
import { queueDisplayName } from '../lib/queueDisplayName'
import type { StartNowRatePercent } from '../lib/startNow'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import {
  FAST_LANE_HINT,
  type LiveProgress,
  type PanelField,
  canDismiss,
  canMoveDown,
  canMoveUp,
  canResolveManually,
  childDisplayName,
  completedTimeLabel,
  dismissMenuOptions,
  fileListCapNote,
  filterTransferJobs,
  hasArrGroup,
  isDismissable,
  isFastLane,
  isPipelineInFlight,
  manualOutcomeLabel,
  mergeFileListChildren,
  processingGroupFields,
  queueRowPercent,
  resolveMenuOptions,
  showsFileList,
  sortTransferRows,
  transferFilterSummary,
  transferGroupFields,
  transferLineValue,
  waitingReasonLabel,
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
  // 2026-08-20 (prompts/2026-08-20-queue-pause.md): "Start now" doesn't work while the transfer
  // queue is paused -- disabled here (with a reason in the tooltip) *and* rejected server-side
  // with a 409 (`core/queue.py.QueuePausedError`); reordering (the chevrons above) is
  // deliberately unaffected -- see this task's own settled decisions for why.
  queuePaused: boolean
  onOpenDrawer: (job: JobOut) => void
  // The chevron reorder controls (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2) --
  // one handler for ▲/▼/▲▲, replacing the previous single-purpose `onMoveToTop`.
  onMove: (job: JobOut, direction: MoveDirection) => void
  onStartNow: (job: JobOut, ratePercent: StartNowRatePercent | undefined) => void
  onStop: (job: JobOut) => void
  onRetry: (job: JobOut) => void
  onDismiss: (job: JobOut) => void
  // The manual escape hatch (2026-08-20, docs/transfers-redesign-spec.md §3.2's
  // pipeline-completion rule) -- `'complete'`/`'failed'` files a wedged in-flight row out of the
  // Active box, `null` undoes a resolution set by mistake. A classification only; see
  // `api/client.ts.resolveItem`.
  onResolve: (job: JobOut, outcome: 'complete' | 'failed' | null) => void
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
  queuePaused,
  onOpenDrawer,
  onMove,
  onStartNow,
  onStop,
  onRetry,
  onDismiss,
  onResolve,
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
  // 2026-08-20 (docs/transfers-redesign-spec.md §3.2's pipeline-completion rule): what this row
  // is still waiting on, and whether a human already resolved it by hand. Both come straight off
  // the server's own classification (`core/pipeline_flight.py`) -- nothing here re-derives either.
  const waiting = waitingReasonLabel(job.pipeline_waiting_reason)
  const manual = manualOutcomeLabel(job)

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
        {/* 2026-08-21 ("we lost that"): this row's own ticking fill, the one place on this page
         * that lost it when the single-line collapse (2026-08-15) moved everything else into the
         * expand panel. `queueRowPercent` reuses `lib/fileTree.ts.stateProgressPercent` fed this
         * row's own bytes-done/bytes-total, the same number `transferLineValue` below already
         * renders as text -- showing it twice (chip + figure column) is explicitly wanted, not a
         * duplication to fix ("no % is good it is small but the chip updating makes it dynamic
         * and cool"). */}
        <StateChip state={chipStateFor(job)} percent={queueRowPercent(job, live)} />
        {/* What this row is still waiting on (2026-08-20, docs/transfers-redesign-spec.md §3.2's
         * pipeline-completion rule) -- Verifying / Extracting / Processing / Awaiting import /
         * Deleting source. The point of the Queue tab is to say what is moving *and why*, so a
         * row that stays under Active/pending after its transfer finished has to explain itself
         * rather than looking stuck. Derived from the same server-side `CASE` that decides which
         * box the row is in (`JobOut.pipeline_waiting_reason`), so the label and the box can
         * never disagree; `null` (nothing rendered) for queued/running, whose own state chip
         * immediately to the left already says what they're doing. */}
        {waiting && (
          <span
            className="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
            title="This release's transfer has finished but its pipeline hasn't — it stays under Active/pending until every step is done"
          >
            {waiting}
          </span>
        )}
        {/* A row a human resolved by hand (2026-08-20) must *show* that rather than looking like
         * a normal completion -- otherwise the Events page's audit trail says one thing and this
         * row says another. It is a classification only: nothing was verified, imported,
         * deleted, or cleaned up on the strength of this. */}
        {manual && (
          <span
            className="shrink-0 rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold text-violet-800 dark:bg-violet-900/40 dark:text-violet-300"
            title="Resolved by hand — a classification only. Nothing was imported, deleted, or cleaned up because of it; the pipeline's own record is on the Events page."
          >
            {manual}
          </span>
        )}
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
                  disabled={busy || queuePaused}
                  title={queuePaused ? 'Unavailable while the transfer queue is paused' : undefined}
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
          {/* The manual escape hatch (2026-08-20, docs/transfers-redesign-spec.md §3.2's
           * pipeline-completion rule) -- offered only on a row that is *in flight but no longer
           * transferring* (`canResolveManually`), i.e. exactly the rows that can get wedged.
           * Every blocking condition has a bounded automatic exit, but automatic exits are
           * necessary rather than sufficient, and a box that can accumulate rows nothing is
           * working on stops being trustworthy.
           *
           * **A classification only, and this is not negotiable.** It writes one item column and
           * moves the row between two boxes on this page. It never advances the `move`-mode
           * delete ladder, is never read as a confirmed *arr import, and never triggers
           * notify/cleanup/post-processing -- DESIGN.md §7.3 makes a source delete wait on a
           * *confirmed* import held across two consecutive poller passes precisely because that
           * delete is irreversible, and a button click is not that evidence. See migration 025
           * and `api/jobs.py.resolve_item`. */}
          {canResolveManually(job) && (
            <ResolveMenu
              disabled={busy}
              options={resolveMenuOptions(job.manual_outcome != null)}
              title="Mark this row resolved — a classification only. Nothing is imported, deleted, or cleaned up because of it."
              onSelect={(outcome) => onResolve(job, outcome)}
            />
          )}
          {/* Undo, for a row already filed by hand -- it is no longer in flight, so the menu
           * above isn't offered on it any more, and "resolved by mistake" needs a way back. */}
          {!canResolveManually(job) && job.manual_outcome != null && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onResolve(job, null)}
              title="Undo this manual resolution — the row goes back to being classified by the pipeline"
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900"
            >
              Undo
            </button>
          )}
          {/* Dismiss (2026-08-13): terminal rows only -- a failed job whose remote is actually
           * gone (REMOTE_GONE, permanently suppressed) had no action but Retry, which is
           * precisely wrong for it. Purely a display action on this job row -- see
           * `core/queue.py.dismiss_job`'s docstring for why it never touches the item's own
           * state or suppression. No confirmation dialog: nothing is destroyed, the record
           * stays on the History page. `succeeded` joined this set 2026-08-14
           * (prompts/2026-08-14-exit-zero-is-not-completion.md) alongside `list_jobs()` starting
           * to surface a recently-succeeded job at all -- see `isDismissable`.
           *
           * 2026-08-20: `canDismiss`, not `isDismissable`, so an **in-flight** terminal row
           * doesn't offer a button `core/queue.py.dismiss_job` now rejects with a 409 --
           * dismissing something still being worked on makes no sense, and `list_jobs()` drops a
           * dismissed job unconditionally, so it is also how a row would vanish from both boxes
           * at once. */}
          {canDismiss(job) && (
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
function FileListRow({
  row,
  jobRelPath,
  jobRunning,
}: {
  row: ReturnType<typeof mergeFileListChildren>[number]
  jobRelPath: string
  // Whether this row's own job is currently `running` (2026-08-21, `childDisplayState`'s own
  // docstring, `lib/fileTree.ts`) -- the one extra fact the chip needs to tell "actively
  // downloading" apart from "stopped part-way," which `row.state` alone can't (a child's
  // persisted state caps at `PARTIAL` either way).
  jobRunning: boolean
}) {
  const displayState = childDisplayState(row.state, jobRunning)
  const percent = stateProgressPercent(displayState, row.local_size, row.remote_size)
  const size = nodeDisplaySize({ is_dir: false, local_size: row.local_size, remote_size: row.remote_size })
  const speedLabel = childSpeedLabel(row.speed_bps)
  return (
    <li className="flex items-center gap-2 py-0.5">
      <span className="min-w-0 flex-1 truncate text-zinc-700 dark:text-zinc-300" title={row.rel_path}>
        {childDisplayName(row.rel_path, jobRelPath)}
      </span>
      <StateChip state={displayState} percent={percent} />
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
            <FileListRow key={row.rel_path} row={row} jobRelPath={job.rel_path} jobRunning={job.state === 'running'} />
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
  // `scanCompleteSeq` (2026-08-21, prompts/2026-08-21-queue-tab-rescan-button.md) feeds this
  // page's own "Rescan now" button below -- same `useLiveModel()` call as `queues`/
  // `progressByJobId` above, not a second subscription.
  const { queues, progressByJobId, childSpeedByItemId, scanCompleteSeq } = useLiveModel()
  const { rescanning, triggerRescan } = useRescan(scanCompleteSeq)
  // The Preflight box (docs/transfers-redesign-spec.md §4, prefigured; this task's own handoff
  // prompt, prompts/done/2026-08-20-preflight-box.md) -- its own poll, independent of `useJobs`'s
  // 2s cadence, since its data changes far more slowly (`hooks/usePreflight.ts`'s own comment).
  // `undefined` until the first response lands; `PreflightBox` itself decides what to render for
  // that and for "no source configured."
  const preflight = usePreflight()
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set())
  const [drawerJob, setDrawerJob] = useState<JobOut | null>(null)
  const [startNowNotice, setStartNowNotice] = useState(false)
  // Settings -> Transfer, polled (2026-08-19,
  // prompts/done/2026-08-19-start-now-bandwidth-fractions.md, for the "Start now" menu's
  // fraction options; **polled rather than fetched once** as of 2026-08-21,
  // prompts/done/2026-08-21-bandwidth-from-the-queue-page.md, because the bandwidth slider below
  // now *edits* `max_bandwidth_bps` and the two surfaces -- here and Settings -> Transfer -- have
  // to reflect each other without a reload). Same independent-poll pattern as `health` below;
  // one cheap settings read every 5s alongside the 2s `GET /api/jobs` this page already makes.
  // `undefined` until the first response lands: every "Start now" fraction reads disabled in the
  // meantime, same as "not configured" (`StartNowMenu`'s own fallback), and the slider renders
  // disabled.
  const transferSettingsFetcher = useCallback(getTransferSettings, [])
  const transferSettings = usePoll(transferSettingsFetcher, 5000)
  const maxBandwidthBps = transferSettings?.max_bandwidth_bps
  // Pause (2026-08-20, prompts/2026-08-20-queue-pause.md): `/api/health`'s `queue_paused` is
  // the one source of truth for whether the queue is currently paused -- polled independently
  // here, same "a second, independent one-shot/polled `getHealth()` call" pattern
  // `StatsHeader.tsx`/`WhatsNewDialog.tsx` already use (health is on the access-log polling
  // exemption list specifically for this). `undefined` until the first response lands, read as
  // "not paused" everywhere below -- a briefly-missing paused banner on first paint is a far
  // smaller problem than the Pause button flashing "Unpause" for a moment.
  const healthFetcher = useCallback(getHealth, [])
  const health = usePoll(healthFetcher, 5000)
  const [pauseBusy, setPauseBusy] = useState(false)
  const [pauseError, setPauseError] = useState<string | null>(null)
  // Pause-for-a-duration (2026-08-21, prompts/2026-08-21-pause-for-duration.md): the dropdown's
  // current choice, applied to whichever entry mode ("Pause after current" / "Pause now") is
  // picked next -- `''` (the default) is "until I unpause", the indefinite pause this dropdown
  // extends rather than replaces. Deliberately not reset after a pause is issued: re-opening the
  // dropdown to re-pick "10 minutes" every time would be the annoying default, not the useful
  // one.
  const [pauseDuration, setPauseDuration] = useState<'' | '1' | '10' | '30' | '60'>('')
  // The Complete box's "Dismiss" menu (2026-08-20, follow-up to phase 1 stage 4b -- see
  // `handleDismissOutcome`'s own docstring below) -- replaces both the old page-top "Dismiss
  // all" button (`dismissingAll`/`dismissAllError`/`dismissAllCount`, same three-state shape
  // reused here under new names) and "Clear all failed" (folded in: its job is now exactly
  // "Dismiss > Failed", server-side and atomic rather than a client-side `Promise.allSettled`
  // fan-out over each row's own `/dismiss` call -- see that handler's own removal note).
  const [dismissingOutcome, setDismissingOutcome] = useState(false)
  const [dismissOutcomeError, setDismissOutcomeError] = useState<string | null>(null)
  const [dismissOutcomeCount, setDismissOutcomeCount] = useState<number | null>(null)
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
  // "Dismiss list" -- its own busy/error/outcome trio, same shape the Dismiss menu's own
  // `dismissingOutcome`/`dismissOutcomeError`/`dismissOutcomeCount` above use (the page's
  // existing notification convention), kept separate rather than reusing those three: "Dismiss
  // list" is a distinct control the user can click independently of (and possibly around the
  // same time as) the Dismiss menu, which this task's own instruction keeps unchanged. It also
  // supersedes the per-queue "Dismiss Queue" control removed 2026-08-19 (docs/transfers-redesign-
  // spec.md §3.1, phase 1 stage 4a) -- filter to a queue, then "Dismiss list" does the same job.
  const [dismissingList, setDismissingList] = useState(false)
  const [dismissListError, setDismissListError] = useState<string | null>(null)
  const [dismissListCount, setDismissListCount] = useState<number | null>(null)
  // Two paginated boxes (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage 4b) --
  // 1-based, independent page state per box. `lib/pagination.ts` owns every bit of the
  // boundary/window arithmetic; this page only ever stores "which page" and hands it there.
  const [activePage, setActivePage] = useState(1)
  const [completePage, setCompletePage] = useState(1)
  // Each box's own "Show 10/20/50" rows-per-page choice (2026-08-20, prompts/2026-08-20-
  // transfers-page-size-selector.md), independent per box and remembered per browser --
  // `dashboard.bytesRange`'s own pattern (`DashboardPage.tsx`): read synchronously in the
  // initial `useState` (not a `useEffect`) so the box never paints at the default size and then
  // jumps to the saved one, and `isPageSize` (`lib/pagination.ts`) rejects anything stored that
  // isn't one of `PAGE_SIZE_OPTIONS` -- a hand-edited or stale value falls back to the box's
  // default (`ACTIVE_PAGE_SIZE`/`COMPLETE_PAGE_SIZE`, both 20) exactly like "never saved".
  const [activePageSize, setActivePageSizeState] = useState<PageSize>(
    () => readLocalStorage('transfers.activePageSize', isPageSize) ?? ACTIVE_PAGE_SIZE,
  )
  const setActivePageSize = (next: PageSize) => {
    setActivePageSizeState(next)
    writeLocalStorage('transfers.activePageSize', next)
    // A size change can strand the user on a page that no longer exists (the task's own example:
    // page 4 of 10 at size 10, switch to 50, page 4 is gone) -- reset to page 1 rather than try
    // to preserve scroll position or compute an equivalent page. The clamp effect below is a
    // second, independent safety net for anything this misses, not a substitute for it.
    setActivePage(1)
  }
  const [completePageSize, setCompletePageSizeState] = useState<PageSize>(
    () => readLocalStorage('transfers.completePageSize', isPageSize) ?? COMPLETE_PAGE_SIZE,
  )
  const setCompletePageSize = (next: PageSize) => {
    setCompletePageSizeState(next)
    writeLocalStorage('transfers.completePageSize', next)
    setCompletePage(1) // same reset-to-page-1 reasoning as the Active box above
  }

  const nodesByQueue = useMemo(() => {
    const map = new Map<number, FileNode[]>()
    for (const q of queues) map.set(q.queue_id, q.nodes)
    return map
  }, [queues])

  // --- Active / pending box (2026-08-19, docs/transfers-redesign-spec.md §3.2, phase 1 stage
  // 4b) -- client-side paginated, 20/page, over `jobs`. `jobs` itself (`useJobs`/`GET
  // /api/jobs`, `core/queue.py.list_jobs`) is deliberately unchanged in shape (see
  // docs/decisions.md for why keeping it was chosen over narrowing it) -- it still carries each
  // item's most recent terminal job too.
  //
  // **2026-08-20: the split is on pipeline completion, not job termination.** This box used to
  // narrow to `queued`/`running`, which meant a row moved to Complete the instant lftp exited --
  // while verify/extract, the *arr's confirmed import, and the deferred source delete were all
  // still outstanding. `isPipelineInFlight` reads the server's own classification
  // (`JobOut.pipeline_in_flight`, `core/pipeline_flight.py`), the *same* expression `GET
  // /api/jobs/complete` excludes from its listing and its `total`, so a row is in exactly one box
  // by construction rather than by two client/server rules that happen to agree today. ---------

  const activeJobs = useMemo(() => jobs.filter(isPipelineInFlight), [jobs])
  // How many transfers the bandwidth slider's "also apply to in-progress" would interrupt
  // (2026-08-21, prompts/done/2026-08-21-bandwidth-from-the-queue-page.md). Read off `jobs`
  // rather than fetched separately -- `state === 'running'` is exactly "has an lftp child right
  // now", the same set `POST /api/queue/bandwidth` stops server-side. It's a *preview* for the
  // confirmation, never the authority: the response's own `interrupted` is what the result
  // notice reports, since only the server knows what was running when the request landed.
  const runningCount = useMemo(() => jobs.filter((job) => job.state === 'running').length, [jobs])
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
  const activePageCount = pageCount(filteredActiveJobs.length, activePageSize)
  const activePageJobs = useMemo(
    () => paginateClientSide(filteredActiveJobs, activePage, activePageSize),
    [filteredActiveJobs, activePage, activePageSize],
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
  // server-side paginated and filtered, page size selectable (`completePageSize` above, default
  // 20), newest-finished first (`useCompleteJobs`/`GET /api/jobs/complete`). -------------------

  const {
    jobs: completeJobs,
    total: completeTotal,
    loading: completeLoading,
    error: completeError,
    refresh: refreshComplete,
  } = useCompleteJobs(completePage, debouncedSearch, completePageSize)
  const completeFilterActive = debouncedSearch.trim() !== ''
  const completePageCount = pageCount(completeTotal, completePageSize)
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
  /** The manual escape hatch (2026-08-20, docs/transfers-redesign-spec.md §3.2's
   * pipeline-completion rule) -- files a wedged in-flight row out of Active/pending with the
   * chosen outcome, or (`null`) undoes a resolution set by mistake. Refreshes both boxes, the
   * same `refreshAll` every other row action uses: this is precisely an action that moves a row
   * from one box to the other, so refreshing only one would leave the page showing it twice
   * until the next poll tick.
   *
   * **A classification only** -- `api/client.ts.resolveItem`'s own docstring and migration 025
   * have the constraint in full. Nothing here (or on the server) reads it as evidence of an
   * import, a completed post-process, or permission to delete anything.
   */
  const handleResolve = (job: JobOut, outcome: 'complete' | 'failed' | null) =>
    withBusy(job.id, () => resolveItem(job.item_id, outcome))
  const handleStartNow = (job: JobOut, ratePercent: StartNowRatePercent | undefined) => {
    if (localStorage.getItem(START_NOW_EXPLAINED_KEY) !== '1') {
      setStartNowNotice(true)
      localStorage.setItem(START_NOW_EXPLAINED_KEY, '1')
    }
    return withBusy(job.id, () => startJobNow(job.id, ratePercent))
  }

  /** The Pause control (2026-08-20, prompts/2026-08-20-queue-pause.md) -- not per-row, so it
   * doesn't go through `withBusy`/`busyIds` (those are keyed by job id); a page-level busy flag
   * disables the control itself for the duration of the request instead. `refreshAll()` on
   * success so a "pause now" is reflected immediately (jobs that were `running` a moment ago now
   * read `queued`) rather than waiting for the next poll tick.
   */
  const handlePause = async (mode: 'after_current' | 'now') => {
    setPauseBusy(true)
    setPauseError(null)
    try {
      const durationMinutes = pauseDuration === '' ? undefined : Number(pauseDuration)
      await pauseQueue(mode === 'now', durationMinutes as 1 | 10 | 30 | 60 | undefined)
      refreshAll()
    } catch (err) {
      setPauseError(err instanceof Error ? err.message : String(err))
    } finally {
      setPauseBusy(false)
    }
  }

  const handleUnpause = async () => {
    setPauseBusy(true)
    setPauseError(null)
    try {
      await unpauseQueue()
      refreshAll()
    } catch (err) {
      setPauseError(err instanceof Error ? err.message : String(err))
    } finally {
      setPauseBusy(false)
    }
  }

  /** The Complete box's "Dismiss" menu (2026-08-20, follow-up to phase 1 stage 4b from the
   * user's browser review, `prompts/2026-08-20-transfers-dismiss-menu-and-counts.md`) --
   * replaces the old page-top "Dismiss all" button (which only ever offered every dismissable
   * job, unfiltered) with an outcome picker (`lib/transferPanel.ts.dismissMenuOptions`: All,
   * Downloaded, Failed, Stopped) that lives where the rows it acts on do. Still one server-side
   * bulk call (`core/queue.py.dismiss_all_terminal`), not a client-side `Promise.allSettled`
   * fan-out -- the same preference "Dismiss all" was already built on, now also true of the
   * control this replaces below.
   *
   * **Folds in "Clear all failed"** -- that control's whole job ("dismiss every currently-failed
   * job", `Promise.allSettled` over each row's own `/dismiss` call) is now exactly what choosing
   * "Failed" here does, server-side and atomic in one request instead of N. `outcome="failed"`
   * plus whatever `debouncedSearch` currently is reproduces its old scope (and, once a filter is
   * active, narrows it further -- the decided composition, see `docs/decisions.md`) with no
   * loss of correctness: a single bulk `UPDATE` can only fail as a whole (network/HTTP), so
   * there is no per-row partial-failure case left to report the way `Promise.allSettled` had to
   * account for.
   *
   * **Composes with the current name filter, decided 2026-08-20** (`docs/decisions.md`): sends
   * `debouncedSearch.trim()` alongside whichever `outcome` was chosen (`undefined` for "All"),
   * so this control always acts on exactly the terminal rows the Complete box is currently
   * showing, narrowed further by outcome if one was picked -- never a separate "ignore whatever
   * filter is active" scope. This is *not* a re-scoping of "Dismiss list" below, which keeps its
   * own independent, unchanged meaning (name-filter-only, no outcome) per this task's own
   * instruction.
   */
  const handleDismissOutcome = async (outcome: JobOut['state'] | null) => {
    setDismissingOutcome(true)
    setDismissOutcomeError(null)
    setDismissOutcomeCount(null)
    try {
      const trimmedFilter = debouncedSearch.trim()
      const res = await dismissAllJobs(
        undefined,
        undefined,
        trimmedFilter !== '' ? trimmedFilter : undefined,
        outcome ?? undefined,
      )
      setDismissOutcomeCount(res.dismissed)
      refreshAll()
    } catch (err) {
      setDismissOutcomeError(errorMessage(err))
    } finally {
      setDismissingOutcome(false)
    }
  }

  /** "Dismiss list" (2026-08-19, prompts/2026-08-19-transfers-name-filter.md) -- the name
   * filter's own dedicated control, unchanged by the 2026-08-20 Dismiss-menu follow-up above per
   * that task's own explicit instruction ("'Dismiss list' keeps its current meaning"): name
   * filter only, no outcome, independent of whatever outcome the Dismiss menu was last used
   * with. Supersedes the per-queue "Dismiss Queue" control (v0.2.3, `278e10f`), removed
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

  // The Complete box's own "Dismiss" menu, disabled/labelled off `completeTotal` -- the same
  // count "All" would act on (it already reflects whatever name filter is active, composing the
  // same way `handleDismissOutcome` composes its own request). Titled only while disabled, same
  // "explain why, not just that" convention `dismissListTitle` above already follows.
  const dismissMenuDisabled = completeTotal === 0
  const dismissMenuTitle =
    completeTotal === 0
      ? completeFilterActive
        ? 'No dismissable rows match this filter'
        : 'Nothing dismissable yet'
      : 'Dismiss by outcome -- All, or narrow to one'
  const dismissMenuLabel = completeTotal > 0 ? `Dismiss (${completeTotal})` : 'Dismiss'

  const drawerNodes = drawerJob ? (nodesByQueue.get(drawerJob.queue_id) ?? []) : []

  return (
    <div className="flex flex-col gap-3">
      {/* Pause (2026-08-20, prompts/2026-08-20-queue-pause.md): the control lives at the very
       * top of the Queue tab, above every other control on this page -- pausing is a page-level
       * action, not a per-row one, and "a queue that silently does nothing is a support question
       * waiting to happen" is the task's own reasoning for making the paused state unmistakable
       * rather than a quiet badge. Reordering (the chevrons below) and auto-queue/manual Queue
       * clicks keep working while paused -- only admission itself stops -- so this banner reads
       * as "nothing new is starting," not "nothing is happening."
       *
       * The duration dropdown (2026-08-21, prompts/2026-08-21-pause-for-duration.md) sits next
       * to the Pause control rather than inside `PauseMenu.tsx` -- both entry modes ("Pause
       * after current" / "Pause now") stay exactly as they were, this just picks what deadline,
       * if any, whichever one is chosen next carries. `''` (the default) is "until I unpause",
       * so the two-option menu's existing behavior is unchanged unless a duration is picked
       * first. Once paused, the deadline (if any) is shown via `pauseResumeLabel` rather than
       * a bare "paused" -- a queue about to restart itself in 40 minutes has to say so. */}
      <div className="flex flex-wrap items-center gap-3">
        {health?.queue_paused ? (
          <>
            <span className="flex items-center gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
              ● Queue paused — nothing new is being admitted
              {pauseResumeLabel(health.queue_paused_until ?? null) &&
                ` (${pauseResumeLabel(health.queue_paused_until ?? null)})`}
            </span>
            <button
              type="button"
              disabled={pauseBusy}
              onClick={handleUnpause}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              {pauseBusy ? 'Resuming…' : 'Unpause'}
            </button>
          </>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              <label htmlFor="pause-duration" className="text-xs text-zinc-500 dark:text-zinc-400">
                For
              </label>
              <select
                id="pause-duration"
                value={pauseDuration}
                onChange={(e) => setPauseDuration(e.target.value as typeof pauseDuration)}
                disabled={pauseBusy}
                className="rounded-md border border-zinc-300 bg-white px-1.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
              >
                <option value="">until I unpause</option>
                <option value="1">1 minute</option>
                <option value="10">10 minutes</option>
                <option value="30">30 minutes</option>
                <option value="60">60 minutes</option>
              </select>
            </div>
            <PauseMenu disabled={pauseBusy} onSelect={handlePause} />
          </>
        )}
        {pauseError && (
          <span className="text-xs text-red-600 dark:text-red-400">
            Couldn't update the pause state: {pauseError}
          </span>
        )}
      </div>

      {/* The site bandwidth slider (2026-08-21,
       * prompts/done/2026-08-21-bandwidth-from-the-queue-page.md) -- directly under Pause,
       * because the two are the same kind of control: page-level knobs on *admission*, not
       * per-row actions. It edits the one site-wide `max_bandwidth_bps` Settings -> Transfer
       * also owns (DESIGN.md §4.5), never a per-queue limit, and offers the two genuinely
       * different applications of a change: future items only (nothing is interrupted, the
       * §4.5 invariant untouched) or also in-progress (every running transfer is stopped and
       * re-admitted at the new rate, confirmed first because it is a real interruption). */}
      <BandwidthControl
        settings={transferSettings}
        runningCount={runningCount}
        queuePaused={health?.queue_paused ?? false}
      />

      {/* Rescan now (2026-08-21, prompts/2026-08-21-queue-tab-rescan-button.md) -- the Files tab
       * has had this since early on; the Queue tab had no way to make lftpweb look at the
       * seedbox without switching tabs, which stopped making sense once Queue became the
       * default landing page (v0.3.0). Grouped with Pause/Bandwidth above as a page-level
       * control (it acts on every queue, not one box's rows) but kept in its own row rather than
       * folded into either: those two are admission knobs (BandwidthControl's own comment), this
       * is a scan trigger -- a different kind of "whole instance" action. Shares
       * `hooks/useRescan.ts` with `FilesPage.tsx` rather than a second copy of the
       * baseline-sequence dance; see that hook's own docstring for why this can't be a
       * `setTimeout` or a blocking endpoint. No "scanned Xs ago" reading here -- the Files tab
       * shows that per queue section, but the Queue tab's list is single, globally-ordered, and
       * ungrouped (v0.3.0 dropped grouping because admission is global), so there is no one
       * queue's timestamp that would honestly stand in for all of them. */}
      <div>
        <button
          type="button"
          onClick={triggerRescan}
          disabled={rescanning}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {rescanning ? 'Rescanning…' : 'Rescan now'}
        </button>
      </div>

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

      {/* Preflight (docs/transfers-redesign-spec.md §4, prefigured; this task's own handoff
       * prompt, prompts/done/2026-08-20-preflight-box.md) -- first in the pipeline, so it sits
       * above every other box on this tab, including the filter/pause controls' own subject
       * matter (Active/Complete). Renders nothing at all until it has data and a configured
       * source to show -- see `components/PreflightBox.tsx`'s own docstring. */}
      <PreflightBox response={preflight} />

      {/* Name filter + "Dismiss list" (2026-08-19, prompts/2026-08-19-transfers-name-filter.md)
       * -- start typing and only rows whose `rel_path` contains the text stay visible: instantly
       * in the Active/pending box below (client-side, bounded), after a short debounce in the
       * Complete box (server-side, paginated -- `useCompleteJobs`). "Dismiss list" is a separate
       * control scoped to exactly what the filter currently matches, server-side, across every
       * page (2026-08-19, phase 1 stage 4b -- `handleDismissList`'s own docstring) -- unchanged
       * by the 2026-08-20 Dismiss-menu follow-up (that control moved into the Complete box's own
       * header below, and folded "Clear all failed" into itself; see `handleDismissOutcome`'s
       * docstring). */}
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
       * `job.state === 'queued'`/`'running'` guards `Row` already had.
       *
       * **Both boxes now always render their own header/rows-or-empty-state/footer shell**
       * (2026-08-20, follow-up to phase 1 stage 4b from the user's browser review) -- a
       * pre-existing inconsistency the Complete box never had and the Active box always did:
       * the Active box used to render nothing at all -- no header, no page-size selector, no
       * "Page X of Y" readout -- whenever it had zero rows to show (empty queue, or a filter
       * matching nothing), while the Complete box's shell was already unconditional. Both boxes'
       * own empty states now live *inside* their shell rather than as separate top-level blocks
       * above it, the same place the Complete box's already lived. */}
      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Active / pending
        </h2>

        {/* The box's empty state reads off `activeJobs` (2026-08-19, phase 1 stage 4b), not
         * `jobs` -- `jobs` still carries each item's most recent terminal job too (unchanged,
         * docs/decisions.md), which would otherwise suppress this message the moment anything
         * had ever finished, even with nothing currently active. Wording widened 2026-08-20:
         * this box now also holds rows whose transfer is done but whose pipeline isn't
         * (verifying, extracting, awaiting import, deleting source), so "nothing queued or
         * downloading" no longer described everything its absence rules out.
         *
         * **One line, not a padded `h-40` block** (2026-08-21, user's browser review: "Active
         * box shrinks to one row when 1 active item. then expands to 5 rows when nothing is
         * going on ... only expand when we have more rows up to the max show size") -- height
         * follows content, always; zero rows is the *emptiest* state and must take the least
         * room, not the most. Matches `PreflightBox.tsx`'s own "Nothing in preflight." line,
         * the same rule already applied there, rather than a second empty-state idiom. */}
        {activeJobs.length === 0 && (
          <p className="text-sm text-zinc-400 dark:text-zinc-600">Nothing in flight — queue an item from Files.</p>
        )}

        {/* The filter's own empty state for this box (2026-08-19) -- distinct from the "nothing
         * in flight" one above: there *are* active transfers, none of them just happen to match
         * the filter text. Same one-line treatment as the empty-queue state above. */}
        {activeJobs.length > 0 && filterActive && filteredActiveJobs.length === 0 && (
          <p className="flex flex-wrap items-center gap-2 text-sm text-zinc-400 dark:text-zinc-600">
            <span>No active transfers match "{search.trim()}".</span>
            <button
              type="button"
              onClick={() => setSearch('')}
              className="text-xs text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
            >
              Clear filter
            </button>
          </p>
        )}

        {activePageJobs.length > 0 && (
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
                queuePaused={health?.queue_paused ?? false}
                onOpenDrawer={setDrawerJob}
                onMove={handleMove}
                onStartNow={handleStartNow}
                onStop={handleStop}
                onRetry={handleRetry}
                onDismiss={handleDismiss}
                onResolve={handleResolve}
                busy={busyIds.has(job.id)}
              />
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {pageReadout(activePage, activePageCount, filteredActiveJobs.length)}
          </span>
          <div className="flex items-center gap-2">
            <PageSizeSelect
              id="active"
              value={activePageSize}
              options={PAGE_SIZE_OPTIONS}
              onChange={setActivePageSize}
            />
            <Pager current={activePage} count={activePageCount} onChange={setActivePage} />
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Complete
          </h2>
          {/* The Dismiss menu (2026-08-20, follow-up to phase 1 stage 4b) -- moved here from the
           * page top ("the dismissall button should move down the top of the completed
           * section", the user's own report) and widened from a single "Dismiss all" button into
           * an outcome picker (`handleDismissOutcome`'s own docstring has the full reasoning,
           * including what happened to "Clear all failed"). */}
          <DismissMenu
            disabled={dismissMenuDisabled}
            busy={dismissingOutcome}
            label={dismissMenuLabel}
            title={dismissMenuTitle}
            options={dismissMenuOptions()}
            onSelect={handleDismissOutcome}
          />
        </div>

        {dismissOutcomeCount != null && (
          <div className="flex items-center justify-between gap-3 rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
            <span className="font-medium">
              Dismissed {dismissOutcomeCount} job{dismissOutcomeCount === 1 ? '' : 's'}
            </span>
            <button
              type="button"
              onClick={() => setDismissOutcomeCount(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
        )}

        {dismissOutcomeError && (
          <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <span>Couldn't dismiss: {dismissOutcomeError}</span>
            <button
              type="button"
              onClick={() => setDismissOutcomeError(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
        )}

        {completeError && (
          <div className="flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <span>Couldn't load completed transfers: {completeError}</span>
          </div>
        )}

        {/* One line, not a padded `h-40` block -- the same fix and the same reasoning as the
         * Active box's own empty state above (2026-08-21): this box shared the identical
         * `h-40` dashed panel, so it padded out to the same five-ish rows of empty space
         * whenever nothing had finished yet. */}
        {!completeLoading && completeJobs.length === 0 && (
          <p className="flex flex-wrap items-center gap-2 text-sm text-zinc-400 dark:text-zinc-600">
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
          </p>
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
                queuePaused={health?.queue_paused ?? false}
                onOpenDrawer={setDrawerJob}
                onMove={handleMove}
                onStartNow={handleStartNow}
                onStop={handleStop}
                onRetry={handleRetry}
                onDismiss={handleDismiss}
                onResolve={handleResolve}
                busy={busyIds.has(job.id)}
              />
            ))}
          </div>
        )}

        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            {pageReadout(completePage, completePageCount, completeTotal)}
          </span>
          <div className="flex items-center gap-2">
            <PageSizeSelect
              id="complete"
              value={completePageSize}
              options={PAGE_SIZE_OPTIONS}
              onChange={setCompletePageSize}
            />
            <Pager current={completePage} count={completePageCount} onChange={setCompletePage} />
          </div>
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
