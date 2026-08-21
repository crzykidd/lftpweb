import { useVirtualizer } from '@tanstack/react-virtual'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { getHistoryEvents, getHistoryJobs, getRemovalGraceSettings } from '../api/client'
import type { FileNode, HistoryEventOut, HistoryJobOut, RemovalGraceSettingsOut } from '../api/types'
import { childDisplayState, stateProgressPercent } from '../lib/fileTree'
import {
  bothSidesRows,
  formatBytes,
  formatEta,
  formatPercent,
  formatRate,
  formatRelativeTimeIntl,
  isRemovalGracePending,
  removalGraceLabel,
} from '../lib/format'
import { averageSpeedBps, elapsedSeconds } from '../lib/transferTiming'
import { EventsLinkButton } from './EventsLinkButton'
import { StateChip } from './StateChip'

const ROW_HEIGHT_PX = 40

// A small, fixed number of recent rows -- not everything, not a second unbounded History page
// (DESIGN.md §9.2, docs/decisions.md phase 6 entry on why `output_tail` never ships inline in
// an unbounded list). This is "a little history," per the task's own wording, fetched exactly
// once when the drawer opens, never per row and never eagerly for the whole tree.
const HISTORY_LIMIT = 10

interface ItemDrawerProps {
  title: string
  rootRelPath: string
  // The item this drawer is about, for the on-open history fetch below -- `null` only for a
  // row that (in principle) has no persisted `item.id` yet; the history section simply doesn't
  // render for one, same as `FileTree.tsx`'s own `rowAction`/`canDeleteLocal` treat a null id
  // as "nothing to act on." `TransfersPage.tsx` supplies `job.item_id` directly; `FileTree.tsx`
  // supplies the clicked row's own `entry.id`.
  itemId: number | null
  nodes: FileNode[]
  onClose: () => void
  // The owning queue's `local_path` (2026-08-14, "folder prefix during transfer") -- optional
  // because not every caller has it loaded (`TransfersPage.tsx` doesn't fetch queue configs
  // today). `undefined` simply means the physical-location line below doesn't render; every
  // other section of the drawer is unaffected.
  localPath?: string
}

/** The item's *actual* on-disk path right now (2026-08-14, "folder prefix during transfer",
 * `core/download_prefix.py`) -- distinct from `rootRelPath`, which is the logical name the
 * Files tree always shows and never carries a prefix (`core/queue.py._spawn_decision`'s own
 * comment has the full "why the tree needs no special-casing" argument; this is the one place
 * that answers "where is this file *right now*", the drawer's own job per that task).
 * `node.pending_download_prefix` (`core/itemview.py`) is `null` once nothing is in flight under
 * a prefixed name, in which case this is just `<localPath>/<rel_path>`.
 */
function physicalLocalPath(localPath: string, node: FileNode): string {
  const base = localPath.replace(/\/+$/, '')
  if (!node.pending_download_prefix) return `${base}/${node.rel_path}`
  const parts = node.rel_path.split('/')
  const name = parts.pop() ?? node.rel_path
  const parent = parts.join('/')
  const dir = parent ? `${base}/${parent}` : base
  return `${dir}/${node.pending_download_prefix}${name}`
}

/** The rename is now the *last* step of post-processing, not the first (2026-08-14,
 * prompts/done/2026-08-14-rename-after-postprocessing-not-before.md) -- so `state` can be
 * `DOWNLOADING`, `DOWNLOADED`, `VERIFYING`, `EXTRACTING`, `VERIFIED`, `EXTRACTED`, `CORRUPT`, or
 * `EXTRACT_FAILED` while `pending_download_prefix` is still set, not just `DOWNLOADING`. The old
 * "currently downloading... once the transfer completes" copy was simply wrong once verify/
 * extract could run for a minute or more after the transfer itself was already done, and
 * actively misleading for `CORRUPT`/`EXTRACT_FAILED` -- those items are never renamed at all
 * (the whole point of moving the rename here: an importer must never see an unverified release
 * under its real name), so "will be renamed once it completes" was a promise that specific item
 * would never keep.
 */
function downloadPrefixNote(state: string): string {
  if (state === 'CORRUPT' || state === 'EXTRACT_FAILED') {
    return (
      'Verification or extraction failed -- this item stays under its prefixed folder name, ' +
      'not its real one, so an importer watching this directory cannot pick it up. It will ' +
      'only be renamed if a retry succeeds.'
    )
  }
  if (state === 'DOWNLOADING') {
    return (
      'Currently downloading into a prefixed folder ("folder prefix during transfer") -- will ' +
      'be renamed to its real name once the transfer completes and post-processing finishes.'
    )
  }
  return (
    'Post-processing (verify/extract) is still running or has not finished -- this item stays ' +
    'under its prefixed folder name until it does, then it is renamed to its real name.'
  )
}

/** The physical-location panel itself -- rendered whenever the caller supplied `localPath`,
 * regardless of whether the item is currently prefixed, so "where is this file right now" has
 * one consistent answer rather than appearing only for the in-flight case.
 */
function PhysicalLocation({ localPath, node }: { localPath: string; node: FileNode }) {
  return (
    <div className="border-b border-zinc-200 px-4 py-2 text-xs dark:border-zinc-800">
      <span className="text-zinc-500 dark:text-zinc-400">Local path: </span>
      <span className="font-mono break-all text-zinc-700 dark:text-zinc-300">
        {physicalLocalPath(localPath, node)}
      </span>
      {node.pending_download_prefix && (
        <p className="mt-1 text-amber-700 dark:text-amber-400">{downloadPrefixNote(node.state)}</p>
      )}
    </div>
  )
}

/** Every file under `rootRelPath` (the queued item), whichever queue it belongs to. A
 * top-level item is either a directory (files are its descendants) or a loose file (it is
 * its own only row) -- DESIGN.md §4.7.
 */
function filesUnder(nodes: FileNode[], rootRelPath: string): FileNode[] {
  const prefix = `${rootRelPath}/`
  return nodes
    .filter((n) => !n.is_dir && (n.rel_path === rootRelPath || n.rel_path.startsWith(prefix)))
    .sort((a, b) => a.rel_path.localeCompare(b.rel_path))
}

/** Both sides, side by side -- the core of the user's own request ("Size, modified date etc.
 * ... for both sides if it exists on both sides"). Rows come from `lib/format.ts.bothSidesRows`,
 * shared with `FileTree.tsx`'s row hover card (2026-08-13,
 * prompts/2026-08-13-both-sides-hover-card.md) so the two surfaces can never quietly disagree
 * about what these numbers are. A local file short of its remote size is called out explicitly
 * (mid-transfer or truncated, per the task's own wording) rather than left for the reader to
 * notice by comparing two numbers themselves.
 */
function SideBySideDetails({ node }: { node: FileNode }) {
  const { remote_size: remoteSize, local_size: localSize } = node
  const shortfall = remoteSize != null && localSize != null && localSize < remoteSize ? remoteSize - localSize : null
  const rows = bothSidesRows(node)

  return (
    <div className="border-b border-zinc-200 px-4 py-3 text-sm dark:border-zinc-800">
      <div className="grid grid-cols-[auto_1fr_1fr] items-baseline gap-x-3 gap-y-1">
        <span />
        <span className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Remote
        </span>
        <span className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
          Local
        </span>
        {rows.map((row) => (
          <Fragment key={row.label}>
            <span className="text-xs text-zinc-500 dark:text-zinc-400">{row.label}</span>
            <span>{row.remote}</span>
            <span>{row.local}</span>
          </Fragment>
        ))}
      </div>
      {shortfall != null && shortfall > 0 && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
          Local is {formatBytes(shortfall)} short of remote ({formatPercent(localSize, remoteSize)} complete) --
          mid-transfer or truncated.
        </p>
      )}
    </div>
  )
}

/** The removal grace period, spelled out (2026-08-14, prompts/2026-08-14-removal-grace-
 * countdown.md, DESIGN.md §3.2 rule 3 / §7.3): `FileTree.tsx`'s state chip carries the short
 * form (`removalGraceShortLabel`) in the tight Status column; the drawer is where someone goes
 * to answer "what is actually happening to this item," so it gets the full sentence plus the
 * absolute timestamp `removalGraceLabel`'s relative wording alone doesn't give -- the same
 * "chip gets the short form, drawer/hover gets the long one" split the settle gate's own
 * countdown already established. Renders nothing when `isRemovalGracePending` is false, same
 * gate `FileTree.tsx`'s `Row` uses for its own substitution, so the two surfaces can't
 * disagree about which rows this applies to.
 */
function RemovalGraceNotice({ node, graceSettings }: { node: FileNode; graceSettings: RemovalGraceSettingsOut | null }) {
  if (!isRemovalGracePending(node)) return null

  return (
    <div className="border-b border-zinc-200 px-4 py-2 text-xs dark:border-zinc-800">
      <p className="text-amber-700 dark:text-amber-400">{removalGraceLabel(node, graceSettings)}</p>
      {node.first_missing_at != null && (
        <p className="mt-0.5 text-zinc-500 dark:text-zinc-400">
          First noticed missing: {new Date(node.first_missing_at).toLocaleString()}.
        </p>
      )}
    </div>
  )
}

interface ChronologyEntry {
  label: string
  ts: string
}

/** The lifecycle facts already persisted on the row (`core/itemview.py`) -- "the steps we made
 * through the lifecycle," per the task's own framing -- rendered as a chronology (sorted by
 * when each actually happened) rather than an unordered field dump. ISO-8601 UTC strings sort
 * correctly as plain strings (same fact `api/history.py`'s own `since`/`until` filters rely
 * on), so no date parsing is needed just to order them. `state_changed_at` is included
 * alongside the milestones even though it can coincide with one of them -- it answers a
 * different question ("when did the state machine last move," not "when was this milestone
 * earned") and the task's own list names both.
 */
function lifecycleChronology(node: FileNode): ChronologyEntry[] {
  const candidates: ChronologyEntry[] = [
    { label: 'First seen', ts: node.first_seen_at ?? '' },
    { label: 'Downloaded', ts: node.downloaded_at ?? '' },
    { label: 'Verified', ts: node.verified_at ?? '' },
    { label: 'Extracted', ts: node.extracted_at ?? '' },
    { label: 'First missing', ts: node.first_missing_at ?? '' },
    { label: 'Remote deleted', ts: node.remote_deleted_at ?? '' },
    // 2026-08-14, prompts/2026-08-14-extracted-archives-rest-as-extracted.md: this row is a
    // spent archive volume, removed after its own contents were extracted -- distinct from
    // "Extracted" above, which is the *parent* release's own milestone (only ever set on the
    // top-level item's row, `core/postprocess.py._do_extract`) and never fires for a child
    // volume like this one.
    { label: 'Archive removed', ts: node.deleted_archive_at ?? '' },
    { label: 'State last changed', ts: node.state_changed_at ?? '' },
  ]
  return candidates.filter((c) => c.ts !== '').sort((a, b) => a.ts.localeCompare(b.ts))
}

function LifecycleChronology({ node }: { node: FileNode }) {
  const entries = useMemo(() => lifecycleChronology(node), [node])
  if (entries.length === 0) return null

  return (
    <div className="border-b border-zinc-200 px-4 py-3 text-sm dark:border-zinc-800">
      <h3 className="mb-1.5 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
        Lifecycle
      </h3>
      <ol className="flex flex-col gap-1">
        {entries.map((e) => (
          <li key={e.label} className="flex items-baseline justify-between gap-3">
            <span className="text-zinc-700 dark:text-zinc-300">{e.label}</span>
            <span className="text-zinc-500 dark:text-zinc-400" title={new Date(e.ts).toLocaleString()}>
              {formatRelativeTimeIntl(e.ts)}
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}

interface HistoryState {
  jobs: HistoryJobOut[]
  events: HistoryEventOut[]
  loading: boolean
  error: string | null
}

const EMPTY_HISTORY: HistoryState = { jobs: [], events: [], loading: false, error: null }

/** "A little history," per the task's own request -- fetched exactly once, when this component
 * mounts (which only happens while the drawer is open) or when `itemId` itself changes, never
 * per row and never for the whole tree. Both endpoints are already server-capped
 * (`api/history.py.MAX_LIMIT`); `HISTORY_LIMIT` asks for far fewer than that on top, on
 * purpose -- a drawer needs a handful of recent rows, not a second unbounded History page (see
 * that module's own docstring on why `output_tail` never ships inline in an unbounded list --
 * the same reasoning is why this asks for 10, not 500).
 */
function HistoryPanel({ itemId }: { itemId: number }) {
  const [state, setState] = useState<HistoryState>(EMPTY_HISTORY)

  useEffect(() => {
    let cancelled = false
    setState({ ...EMPTY_HISTORY, loading: true })
    Promise.all([
      getHistoryJobs({ item_id: itemId, limit: HISTORY_LIMIT }),
      getHistoryEvents({ item_id: itemId, limit: HISTORY_LIMIT }),
    ])
      .then(([jobsResp, eventsResp]) => {
        if (cancelled) return
        setState({ jobs: jobsResp.jobs, events: eventsResp.events, loading: false, error: null })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({ ...EMPTY_HISTORY, error: err instanceof Error ? err.message : String(err) })
      })
    return () => {
      cancelled = true
    }
  }, [itemId])

  if (state.loading) {
    return (
      <div className="border-b border-zinc-200 px-4 py-3 text-xs text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
        Loading history…
      </div>
    )
  }
  if (state.error) {
    return (
      <div className="border-b border-zinc-200 px-4 py-3 text-xs text-red-600 dark:border-zinc-800 dark:text-red-400">
        Couldn't load history: {state.error}
      </div>
    )
  }
  if (state.jobs.length === 0 && state.events.length === 0) return null

  return (
    <div className="flex flex-col gap-3 border-b border-zinc-200 px-4 py-3 text-sm dark:border-zinc-800">
      <h3 className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
        History
      </h3>
      {state.jobs.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {state.jobs.map((j) => {
            // Per-attempt elapsed/average speed (2026-08-14,
            // prompts/2026-08-14-transfer-timing-and-throughput-display.md) -- "a job that
            // failed after 40 minutes at 2 MB/s tells a very different story from one that
            // failed in 3 seconds, and today both render identically." `HistoryJobOut`
            // (`api/history.py`) doesn't carry `bytes_start` the way `JobOut` does
            // (deliberately -- History's row set is unbounded, so it ships a leaner shape),
            // so this passes `0` and can overstate the rate for an attempt that resumed a
            // previous attempt's partial download (`core/metrics.py`'s "non-monotonic trap,"
            // same reasoning `averageSpeedBps`'s own doc comment spells out) -- flagged in the
            // figure's own title rather than silently claimed as exact.
            const elapsed = elapsedSeconds(j.started_at, j.finished_at)
            const avgSpeed = averageSpeedBps(j.bytes_done, 0, elapsed)
            return (
              <li key={`job-${j.id}`} className="flex flex-col gap-0.5 text-xs">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-zinc-700 dark:text-zinc-300">
                    Transfer {j.state}
                    {j.error_class && ` (${j.error_class})`}
                  </span>
                  <span
                    className="text-zinc-500 dark:text-zinc-400"
                    title={j.finished_at ? new Date(j.finished_at).toLocaleString() : undefined}
                  >
                    {j.finished_at ? formatRelativeTimeIntl(j.finished_at) : '—'}
                  </span>
                </div>
                {(elapsed != null || avgSpeed != null) && (
                  <div className="flex flex-wrap items-center gap-x-2 text-zinc-400 dark:text-zinc-500">
                    {elapsed != null && <span>{formatEta(elapsed)}</span>}
                    {avgSpeed != null && (
                      <span
                        title="Average for this attempt (bytes_done over elapsed time) -- may be inflated if this attempt resumed a previous one's partial download, since history doesn't carry bytes_start"
                      >
                        avg {formatRate(avgSpeed)}
                      </span>
                    )}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {state.events.length > 0 && (
        <ul className="flex flex-col gap-1">
          {/* Delete-audit events (remote_delete/remote_delete_withheld/local_delete/
              archive_cleanup) are the most valuable rows here, per the task's own call-out --
              they explain why bytes vanished. No special-casing needed: every event already
              carries its own `kind`/`message`, so they show up like any other row. */}
          {state.events.map((e) => (
            <li key={`event-${e.id}`} className="flex items-baseline justify-between gap-3 text-xs">
              <span
                className={
                  e.level === 'error' || e.level === 'warning'
                    ? 'text-amber-700 dark:text-amber-400'
                    : 'text-zinc-700 dark:text-zinc-300'
                }
                title={e.message}
              >
                {e.kind}
              </span>
              <span className="text-zinc-500 dark:text-zinc-400" title={new Date(e.ts).toLocaleString()}>
                {formatRelativeTimeIntl(e.ts)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Row({
  node,
  rootRelPath,
  jobRunning,
}: {
  node: FileNode
  rootRelPath: string
  // Whether this item's own job is currently running (2026-08-21, user's browser review: "the
  // sidebar for active file ... it should show downloading and the chip should show progress.
  // Not Partial") -- derived by `ItemDrawer` from `rootNode.state === 'DOWNLOADING'` (the
  // top-level item's own persisted state while its job runs, `core/queue.py._spawn_decision`),
  // not threaded in as a separate job prop: every row here already reads from `nodes`, which
  // already carries that fact. See `childDisplayState` (`lib/fileTree.ts`) for the actual rule.
  jobRunning: boolean
}) {
  const label = node.rel_path === rootRelPath ? node.rel_path : node.rel_path.slice(rootRelPath.length + 1)
  const transferred = node.local_size ?? 0
  const displayState = childDisplayState(node.state, jobRunning)
  const percent = stateProgressPercent(displayState, node.local_size, node.remote_size)
  return (
    <div
      className="flex items-center gap-3 border-b border-zinc-100 px-3 text-sm dark:border-zinc-900"
      style={{ height: ROW_HEIGHT_PX }}
    >
      <span className="min-w-0 flex-1 truncate" title={node.rel_path}>
        {label}
      </span>
      <span className="w-24 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
        {node.remote_size != null ? formatBytes(node.remote_size) : '—'}
      </span>
      <span className="w-24 shrink-0 text-right text-zinc-500 dark:text-zinc-400">{formatBytes(transferred)}</span>
      <span className="w-14 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
        {formatPercent(node.local_size, node.remote_size)}
      </span>
      <span className="w-28 shrink-0 text-right">
        {/* `percent` (2026-08-21, same review) -- this chip previously showed the bare state
         * word with no fill at all, unlike `TransfersPage.tsx`'s Queue-row file-list expansion
         * (`FileListRow`), which has always passed one. Both surfaces should read the same way
         * for the same fact ("both surfaces must agree" -- see `childDisplayState`'s docstring). */}
        <StateChip state={displayState} percent={percent} />
      </span>
    </div>
  )
}

/** DESIGN.md §9.2's item drawer: a **side drawer, not a modal**, so the queue stays visible
 * behind it -- file lists get long. Virtualized (`@tanstack/react-virtual`) because a
 * release can carry hundreds of files (§9.2's own wording); the Files page's tree uses the
 * same library for the same reason (docs/decisions.md: virtualization was deliberately
 * deferred to the phase that actually needed it, this one).
 *
 * Generalised (2026-08-13, prompts/2026-08-13-files-detail-inspector.md) from a Transfers-page-
 * only, job-keyed component into the one detail surface for an item from *either* page --
 * `FileTree.tsx`'s per-row info icon is the other caller, added by this same task. No second
 * surface: the inline expand-underneath the user originally floated was rejected in favour of
 * extending this existing drawer, so there's exactly one place that shows an item's full detail
 * rather than two drifting in and out of agreement (see the drawer's own module comment above
 * `filesUnder` for the pre-existing per-file breakdown this only adds to, never replaces).
 */
export function ItemDrawer({
  title,
  rootRelPath,
  itemId,
  nodes,
  onClose,
  localPath,
}: ItemDrawerProps) {
  const files = useMemo(() => filesUnder(nodes, rootRelPath), [nodes, rootRelPath])
  // The clicked item's own row -- distinct from `files` above, which is only the *descendant*
  // files (and excludes directories entirely). A directory's own size/mtime/lifecycle facts
  // live on its own row, matched by the exact rel_path the caller opened the drawer for.
  const rootNode = useMemo(() => nodes.find((n) => n.rel_path === rootRelPath) ?? null, [nodes, rootRelPath])
  // Whether this item's own job is currently running (2026-08-21, `Row`'s own docstring above)
  // -- read straight off `rootNode`, already fetched for the panels above, rather than a new
  // job prop: the top-level item's own `state` is the one place `DOWNLOADING` is ever actually
  // persisted (`core/queue.py._spawn_decision`), so this is true for exactly the child rows
  // below whose job is live right now, whether this drawer was opened from `TransfersPage.tsx`
  // (which has a `JobOut` to hand) or `FileTree.tsx` (which doesn't).
  const jobRunning = rootNode?.state === 'DOWNLOADING'
  const scrollRef = useRef<HTMLDivElement>(null)

  const virtualizer = useVirtualizer({
    count: files.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT_PX,
    overscan: 12,
  })

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  // The removal grace period's own constant (2026-08-14, prompts/2026-08-14-removal-grace-
  // countdown.md) -- fetched once when the drawer mounts, not per row (there is only ever one
  // `RemovalGraceNotice` per drawer, for `rootNode`). Unlike `FileTree.tsx`, which fetches this
  // once for the whole tree and threads it down to every row, the drawer is opened on demand
  // and closed again, so a self-contained fetch here (the same shape `HistoryPanel` already
  // uses) is simpler than plumbing a new prop through both of this drawer's callers
  // (`FileTree.tsx`, `TransfersPage.tsx`) for a value only this one notice needs.
  const [graceSettings, setGraceSettings] = useState<RemovalGraceSettingsOut | null>(null)
  useEffect(() => {
    getRemovalGraceSettings()
      .then(setGraceSettings)
      .catch(() => {
        // Degrades gracefully, same as FileTree.tsx's own fetch: `removalGraceLabel` still
        // renders a full sentence without the numeric countdown clause.
      })
  }, [])

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      {/* Backdrop: closes the drawer on click, but the queue behind it stays visible and
       * live -- this is deliberately not a modal (DESIGN.md §9.2). */}
      <button
        type="button"
        aria-label="Close drawer"
        className="absolute inset-0 bg-black/20 dark:bg-black/40"
        onClick={onClose}
      />
      <div className="relative flex h-full w-full max-w-xl flex-col border-l border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
        <div className="flex items-center justify-between gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-100" title={title}>
              {title}
            </h2>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              {files.length} file{files.length === 1 ? '' : 's'}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {/* The per-item Events deep link (2026-08-20, docs/transfers-redesign-spec.md §2,
             * phase 1 stage 7) -- lives here, in the drawer's own header, rather than on the
             * Files/Transfers row that opened it: both of those rows' layouts are already tight
             * and browser-unverified (`FileTree.tsx`'s own "already tight... clipping" note;
             * `TransfersPage.tsx`'s row-crowding fix), so adding a new element to either without
             * a way to check the result is exactly the risk this avoids. The drawer is already
             * the one shared surface both rows open on a single click (its own module comment,
             * below), so this is always one click further from wherever this drawer was opened. */}
            {itemId != null && <EventsLinkButton itemId={itemId} label={title} />}
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Close
            </button>
          </div>
        </div>

        {/* Both sides, the lifecycle chronology, and a little history -- all item-level
            (`rootNode`/`itemId`), not per descendant file, so they render once above the
            per-file breakdown rather than repeating for every row in it. */}
        {rootNode && localPath && <PhysicalLocation localPath={localPath} node={rootNode} />}
        {rootNode && <SideBySideDetails node={rootNode} />}
        {rootNode && <RemovalGraceNotice node={rootNode} graceSettings={graceSettings} />}
        {rootNode && <LifecycleChronology node={rootNode} />}
        {itemId != null && <HistoryPanel itemId={itemId} />}

        <div className="flex items-center gap-3 border-b border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
          <span className="min-w-0 flex-1">Name</span>
          <span className="w-24 shrink-0 text-right">Size</span>
          <span className="w-24 shrink-0 text-right">Transferred</span>
          <span className="w-14 shrink-0 text-right">Prog.</span>
          <span className="w-28 shrink-0 text-right">Status</span>
        </div>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto">
          {files.length === 0 ? (
            <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">
              No files known for this item yet.
            </p>
          ) : (
            <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const node = files[virtualRow.index]
                return (
                  <div
                    key={node.rel_path}
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      width: '100%',
                      height: virtualRow.size,
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    <Row node={node} rootRelPath={rootRelPath} jobRunning={jobRunning} />
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
