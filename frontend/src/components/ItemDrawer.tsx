import { useVirtualizer } from '@tanstack/react-virtual'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { getHistoryEvents, getHistoryJobs } from '../api/client'
import type { FileNode, HistoryEventOut, HistoryJobOut } from '../api/types'
import { bothSidesRows, formatBytes, formatPercent, formatRelativeTimeIntl } from '../lib/format'
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
        <ul className="flex flex-col gap-1">
          {state.jobs.map((j) => (
            <li key={`job-${j.id}`} className="flex items-baseline justify-between gap-3 text-xs">
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
            </li>
          ))}
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

function Row({ node, rootRelPath }: { node: FileNode; rootRelPath: string }) {
  const label = node.rel_path === rootRelPath ? node.rel_path : node.rel_path.slice(rootRelPath.length + 1)
  const transferred = node.local_size ?? 0
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
        <StateChip state={node.state} />
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
export function ItemDrawer({ title, rootRelPath, itemId, nodes, onClose }: ItemDrawerProps) {
  const files = useMemo(() => filesUnder(nodes, rootRelPath), [nodes, rootRelPath])
  // The clicked item's own row -- distinct from `files` above, which is only the *descendant*
  // files (and excludes directories entirely). A directory's own size/mtime/lifecycle facts
  // live on its own row, matched by the exact rel_path the caller opened the drawer for.
  const rootNode = useMemo(() => nodes.find((n) => n.rel_path === rootRelPath) ?? null, [nodes, rootRelPath])
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
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
          >
            Close
          </button>
        </div>

        {/* Both sides, the lifecycle chronology, and a little history -- all item-level
            (`rootNode`/`itemId`), not per descendant file, so they render once above the
            per-file breakdown rather than repeating for every row in it. */}
        {rootNode && <SideBySideDetails node={rootNode} />}
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
                    <Row node={node} rootRelPath={rootRelPath} />
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
