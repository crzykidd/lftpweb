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
 *
 * 2026-08-19 (prompts/2026-08-19-transfers-row-shows-eta.md): a third, running-only figure --
 * "<duration> left" -- answers "how long until this finishes" without expanding. Same live/job
 * fallback discipline the expand panel's own `eta` already uses (`opts.live?.eta_s ?? job.eta_s`
 * in `transferGroupFields` below), through the same `formatEta` its "(ETA <eta>)" reading already
 * goes through. Omitted entirely (not "—", not "0s") when that comes back `null` -- a transfer
 * that only just started has no ETA yet, and a blank figure says that more honestly than a
 * placeholder would. Worded as a "left" suffix rather than an "ETA " prefix (the panel's own
 * wording): this line already reads left-to-right as percent, then rate, then a third bare
 * duration -- an "ETA " prefix would separate that duration from the two figures beside it more
 * than the row's own "·" idiom does, where a one-word suffix keeps the same rhythm while still
 * resolving on sight that it's remaining time, not elapsed.
 */
export function transferLineValue(job: JobOut, live?: LiveProgress): string {
  if (job.state === 'running') {
    const bytesDone = live?.bytes_done ?? job.bytes_done
    const bytesTotal = (live?.bytes_total ?? job.bytes_total) ?? job.bytes_total
    const speed = live?.speed_bps ?? job.speed_bps ?? 0
    const eta = live?.eta_s ?? job.eta_s
    const base = `${formatPercent(bytesDone, bytesTotal)} · ${formatRate(speed)}`
    return eta != null ? `${base} · ${formatEta(eta)} left` : base
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
 * in `core/queue.py.list_jobs`'s own `ORDER BY queue_position ASC, id ASC` (2026-08-19,
 * docs/transfers-redesign-spec.md §3.4 -- was `rank DESC, queued_at ASC`), which is the
 * *scheduler's* run order and says nothing about when a terminal job actually finished; a job
 * that failed hours ago could sit above one that just succeeded, if the failed one happened to
 * have a lower `queue_position`. This still trusts that same input order for
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

/** Whether a job's Dismiss button (2026-08-13, `core/queue.py.dismiss_job`) should show for a
 * row in this state -- must match that endpoint's own guard (`JobNotDismissableError`) exactly,
 * or a click here would just surface a 409. `succeeded` joined 2026-08-14
 * (prompts/2026-08-14-exit-zero-is-not-completion.md) alongside `list_jobs()` starting to
 * surface a recently-succeeded job on the Transfers page at all -- a completed transfer needs
 * the same "stop showing this row" action a failed or stopped one already had.
 *
 * Lives here, not in `TransfersPage.tsx` (where it shipped 2026-08-13), since 2026-08-17
 * (prompts/2026-08-17-transfers-dismiss-per-queue.md) -- originally so the (now-removed,
 * 2026-08-19) group header's own "Dismiss Queue" control could reuse it without `lib/` importing
 * from `pages/`; `dismissableJobIds` below is the same reasoning applied to "Dismiss list."
 * `TransfersPage.tsx` re-exports this name so its own existing import (and
 * `TransfersPage.test.ts`'s) keeps working unchanged.
 */
export function isDismissable(state: JobOut['state']): boolean {
  return state === 'failed' || state === 'cancelled' || state === 'succeeded'
}

// --- Chevron reordering (2026-08-19, docs/transfers-redesign-spec.md §3.4 stage 2,
// prompts/2026-08-19-queue-reorder-chevrons.md): "can this row move" is pure enough to live
// here and be unit-tested directly, rather than inlined in `Row`'s own JSX. -------------------

/** Whether a queued row's ▲ (up one) / ▲▲ (to top) chevrons should be enabled -- disabled on
 * the very first queued row in the global order, since there's nothing above it to trade places
 * with; the position number beside it already says so. `queuePosition` is `TransfersPage.tsx`'s
 * own 1-based `queuePositions` reading for this job (`undefined` for a non-queued row, which
 * never renders chevrons at all -- `Row`'s own `job.state === 'queued'` guard covers that
 * case before this function is ever called). The backend's own edge-case handling
 * (`core/queue.py.TransferQueue.move_job`) makes an out-of-turn request a silent no-op rather
 * than an error regardless -- this is the UI affordance on top of that, not the only guard.
 */
export function canMoveUp(queuePosition: number | undefined): boolean {
  return queuePosition != null && queuePosition > 1
}

/** The ▼ (down one) chevron's counterpart -- disabled on the last queued row in the global
 * order. `queuedCount` is `TransfersPage.tsx`'s own `queuePositions.size` (how many rows are
 * currently `queued`, the same count the page already derives for nothing else today).
 */
export function canMoveDown(queuePosition: number | undefined, queuedCount: number): boolean {
  return queuePosition != null && queuePosition < queuedCount
}

// --- Fast-lane marker (2026-08-19, docs/transfers-redesign-spec.md §3.5, phase 1 stage 4a):
// small items (under `small_item_threshold_bytes`, 10 MB default, DESIGN.md §4.5) admit from a
// separate lane with its own concurrency cap and reserved bandwidth, so a job at #9 can
// genuinely start before the main-lane job at #2. Grouped-by-queue rows made that easy to miss;
// one intermixed ordered list reads it as a bug unless the row explains itself. Decided: keep
// one `1..N` numbering (not per-lane numbering, not a third box) and mark fast-lane rows --
// `isFastLane` is the predicate, `FAST_LANE_HINT` the tooltip copy the row renders alongside it.
// -----------------------------------------------------------------------------------------

export function isFastLane(job: JobOut): boolean {
  return job.lane === 'small'
}

/** The fast-lane badge's tooltip -- explains *why* a marked row can start before a lower-numbered
 * one, rather than just flagging that it can. Its own constant (not inlined at the one call site)
 * so the wording is unit-testable without mounting `Row`.
 */
export const FAST_LANE_HINT =
  'Small file -- transfers on its own lane and may start before higher-numbered items'

// --- Name filter + "Dismiss list" (2026-08-19, prompts/2026-08-19-transfers-name-filter.md):
// a busy install's Transfers page has no way to narrow a long list. Pure logic only, same
// discipline this whole file already follows -- `TransfersPage.tsx` applies this before
// rendering the flat, globally-ordered list. -----------------------------------------------

/** Case-insensitive substring filter over `rel_path` only -- **not** `queue_name` too, since a
 * queue named e.g. "movies" would otherwise make every row in it match the word "movies", which
 * is not what a name filter means. `JobOut` has no separate `name` field; `rel_path` is the
 * item's path within the queue and already contains the name, so a substring match over it
 * covers both a bare name and a nested one (`at.first.sight` matches literally, same as any
 * other substring -- no glob/regex parsing).
 *
 * An empty/whitespace-only `search` returns `jobs` **unchanged and by identity**, not a copy --
 * same "don't churn a downstream `useMemo` for a no-op filter" reasoning `FileTree.tsx`'s own
 * `visiblePaths` follows for its filtered flat list. Preserves input order; the caller
 * (`TransfersPage.tsx`) has already sorted via `sortTransferRows` above.
 */
export function filterTransferJobs(jobs: JobOut[], search: string): JobOut[] {
  const needle = search.trim().toLowerCase()
  if (!needle) return jobs
  return jobs.filter((job) => job.rel_path.toLowerCase().includes(needle))
}

/** The "Dismiss list" button's own id list (2026-08-19) -- every id in `jobs` that
 * `isDismissable` allows, reusing that exact predicate rather than re-deriving the terminal-
 * state set a third time. Callers pass this straight to `dismissAllJobs`'s `job_ids` param
 * (`api/client.ts`) as **one** request -- `core/queue.py.dismiss_all_terminal`'s own docstring
 * is explicit that this is a single server-side `UPDATE`, never a client-side loop over every
 * dismissable row's own `/dismiss` call.
 */
export function dismissableJobIds(jobs: JobOut[]): number[] {
  return jobs.filter((job) => isDismissable(job.state)).map((job) => job.id)
}

/** The name filter's own "showing N of M" readout, alongside the input. Same shape as the Logs
 * filter's `lib/logFilter.ts.logFilterSummary` (`null` while the filter is empty, so the caller
 * renders nothing rather than a no-op "showing 12 of 12" on every load) -- but not that function
 * itself: its string is hardcoded to say "lines", which would misdescribe a page whose rows are
 * transfers, not log lines. A small sibling here instead of parameterizing that one for a word
 * neither of its two other current wordings needs.
 */
export function transferFilterSummary(shown: number, total: number, search: string): string | null {
  if (!search.trim()) return null
  return `Showing ${shown} of ${total} transfer${total === 1 ? '' : 's'}`
}

// --- Group counts -- History's own group header still needs these (`formatQueueGroupCounts`,
// `historyQueueGroupCounts` below). Transfers' own grouping (and the client-computed
// `queueGroupSummary`/`groupJobsByQueue`/`groupHasDismissable` it needed) was removed 2026-08-19
// (docs/transfers-redesign-spec.md §3.1, phase 1 stage 4a) when the Queue tab dropped per-queue
// grouping for one globally-ordered list -- see docs/decisions.md for the reversal and its
// cause. --------------------------------------------------------------------------------------

/** A group header's job counts by outcome. Four buckets came from the original task -- active
 * (running), queued, succeeded, failed -- plus a `cancelled` (Stopped) row, a real fifth outcome
 * `isDismissable`/`chipStateFor` already both treat as first-class; dropping it from the header's
 * counts would make them not sum to the group's own row count, silently hiding rather than naming
 * it (docs/decisions.md). It follows `failed` in `formatQueueGroupCounts`'s enumeration order
 * below and is omitted at zero exactly like every other bucket.
 */
export interface QueueGroupCounts {
  active: number
  queued: number
  succeeded: number
  failed: number
  stopped: number
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
 * zero, a mostly-defensive empty string rather than a case History's own caller
 * (`historyQueueGroupCounts`) produces in practice.
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
// section owns per-queue collapse under its own storage key. This map shape/read-write logic
// used to be shared with Transfers' own per-queue collapse (`readCollapsedQueues`/
// `writeCollapsedQueues`, its own storage key) -- that side was removed 2026-08-19
// (docs/transfers-redesign-spec.md §3.1, phase 1 stage 4a) when the Queue tab dropped grouping
// entirely, so History is the sole caller now. `readCollapsedQueuesFor`/`writeCollapsedQueuesFor`
// below still take the storage key explicitly rather than being inlined into the History-named
// functions -- the cheapest shape if a second collapsible grouped view ever needs this again.

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

export function readHistoryCollapsedQueues(): QueueCollapseMap {
  return readCollapsedQueuesFor(HISTORY_COLLAPSED_QUEUES_KEY)
}

export function writeHistoryCollapsedQueues(map: QueueCollapseMap): void {
  writeCollapsedQueuesFor(HISTORY_COLLAPSED_QUEUES_KEY, map)
}

// --- History jobs section's own group header aggregate (2026-08-16) -- the server-computed,
// filter-honest `HistoryQueueSummaryOut` (api/history.py._queue_summaries), formatted for the
// group header via `formatQueueGroupCounts`/`QueueGroupCounts` above (originally shared with
// Transfers' own now-removed client-computed group summary -- see that section's own comment).
// History's three outcome buckets (succeeded/failed/cancelled) are a subset of the five
// `QueueGroupCounts` names, so mapping onto it with `active`/`queued` pinned at 0 (never
// rendered, since `formatQueueGroupCounts` omits zero counts) gets identical output for identical
// meaning with no new formatting logic to maintain. ---------------------------------------------

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

/** Whether a failed row's expand panel needs the on-demand `GET .../output` fetch, or can render
 * a static empty state straight from the list row it already has (2026-08-17,
 * prompts/2026-08-17-interrupted-job-popout-explains-itself.md). Before this,
 * `HistoryJobsSection.tsx`'s `handleToggle` only fetched when `has_output_tail` was true, and the
 * panel's "No output was captured for this job." message only rendered once a fetch actually
 * landed -- so a failed job with no captured output (an INTERRUPTED job recovered before
 * `core/queue.py`'s startup sweep started writing one, or any other tail-less failure already
 * sitting in a user's database) expanded to a completely blank panel: the empty-state copy
 * existed in the code but was unreachable for exactly the one failure class that most needed it.
 * `job.error_class` is already on the list row, so the static case needs no fetch at all.
 */
export type FailedJobPanelContent = { kind: 'fetch' } | { kind: 'static'; errorClass: string | null }

export function failedJobPanelContent(job: HistoryJobOut): FailedJobPanelContent {
  return job.has_output_tail ? { kind: 'fetch' } : { kind: 'static', errorClass: job.error_class }
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
