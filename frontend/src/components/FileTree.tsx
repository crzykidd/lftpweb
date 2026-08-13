import { useVirtualizer } from '@tanstack/react-virtual'
import { useMemo, useRef, useState } from 'react'
import { queueItem, stopItem } from '../api/client'
import type { FileNode } from '../api/types'
import { formatBytes } from '../lib/format'
import { StateChip } from './StateChip'

const ROW_HEIGHT_PX = 32

const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

interface TreeEntry extends FileNode {
  name: string
  depth: number
  children: TreeEntry[]
}

function buildTree(nodes: FileNode[]): TreeEntry[] {
  const byPath = new Map<string, TreeEntry>()
  const roots: TreeEntry[] = []

  // Parents always sort before children by path-segment count (DESIGN.md §5/§9.2's model
  // persists a row for every node, file or directory, so every ancestor is guaranteed to be
  // present in `nodes` — this doesn't need to tolerate a missing parent, but does so
  // defensively rather than dropping an orphaned node from the view).
  const sorted = [...nodes].sort(
    (a, b) => a.rel_path.split('/').length - b.rel_path.split('/').length,
  )

  for (const node of sorted) {
    const lastSlash = node.rel_path.lastIndexOf('/')
    const name = lastSlash === -1 ? node.rel_path : node.rel_path.slice(lastSlash + 1)
    const parentPath = lastSlash === -1 ? null : node.rel_path.slice(0, lastSlash)
    const parent = parentPath ? byPath.get(parentPath) : undefined
    const entry: TreeEntry = { ...node, name, depth: parent ? parent.depth + 1 : 0, children: [] }
    byPath.set(node.rel_path, entry)
    if (parent) parent.children.push(entry)
    else roots.push(entry)
  }

  const byNameThenDir = (a: TreeEntry, b: TreeEntry) =>
    a.is_dir === b.is_dir ? a.name.localeCompare(b.name) : a.is_dir ? -1 : 1
  const sortRecursive = (entries: TreeEntry[]) => {
    entries.sort(byNameThenDir)
    for (const e of entries) sortRecursive(e.children)
  }
  sortRecursive(roots)
  return roots
}

/** Depth-first, respecting `collapsed` -- this is what gets virtualized. A collapsed
 * directory's children simply never enter the flat list, so scroll math stays correct
 * without the virtualizer needing to know anything about tree structure.
 */
function flatten(roots: TreeEntry[], collapsed: Set<string>): TreeEntry[] {
  const out: TreeEntry[] = []
  const walk = (entries: TreeEntry[]) => {
    for (const entry of entries) {
      out.push(entry)
      if (entry.is_dir && !collapsed.has(entry.rel_path)) walk(entry.children)
    }
  }
  walk(roots)
  return out
}

/** What this row's own action button offers, if anything (DESIGN.md §9.2, §4.7). Manual
 * queueing always wins over auto-queue suppression and is never filtered by the UI based on
 * state (STOPPED/FAILED included) -- the one exception is a node with nothing remote to
 * fetch at all (`LOCAL_ONLY`), where there is nothing a "Queue" action could mean.
 */
function rowAction(node: FileNode): 'queue' | 'stop' | null {
  if (node.id == null) return null
  if (node.state === 'QUEUED' || node.state === 'DOWNLOADING') return 'stop'
  if (node.state === 'LOCAL_ONLY') return null
  return 'queue'
}

interface RowProps {
  entry: TreeEntry
  isCollapsed: boolean
  isSelected: boolean
  onToggleCollapse: (path: string) => void
  onToggleSelect: (entry: TreeEntry, shiftKey: boolean) => void
  onAction: (entry: TreeEntry) => void
  actionBusy: boolean
}

function Row({ entry, isCollapsed, isSelected, onToggleCollapse, onToggleSelect, onAction, actionBusy }: RowProps) {
  const size = entry.is_dir ? entry.remote_size : (entry.local_size ?? entry.remote_size)
  const action = rowAction(entry)

  return (
    <div
      className={`flex items-center gap-2 border-b border-zinc-100 px-2 text-sm dark:border-zinc-900 ${
        isSelected ? 'bg-sky-50 dark:bg-sky-950/30' : 'hover:bg-zinc-50 dark:hover:bg-zinc-900'
      }`}
      style={{ height: ROW_HEIGHT_PX, paddingLeft: `${entry.depth * 1.25 + 0.5}rem` }}
    >
      <input
        type="checkbox"
        className="shrink-0"
        checked={isSelected}
        disabled={entry.id == null}
        readOnly
        onClick={(e) => {
          e.stopPropagation()
          onToggleSelect(entry, e.shiftKey)
        }}
        aria-label={`Select ${entry.name}`}
      />
      {entry.is_dir ? (
        <button
          type="button"
          onClick={() => onToggleCollapse(entry.rel_path)}
          className="w-4 shrink-0 text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200"
          aria-label={isCollapsed ? 'Expand' : 'Collapse'}
        >
          {isCollapsed ? '▸' : '▾'}
        </button>
      ) : (
        <span className="w-4 shrink-0" />
      )}
      <span className="min-w-0 flex-1 truncate" title={entry.rel_path}>
        {entry.name}
        {entry.is_dir && '/'}
      </span>
      <span className="w-24 shrink-0 text-right text-zinc-500 dark:text-zinc-400">
        {size != null ? formatBytes(size) : '—'}
      </span>
      <span className="w-28 shrink-0 text-right">
        <StateChip state={entry.state} />
        {/* The settle gate (prompts/open-issues.md #2): most REMOTE_ONLY items pass through
            this on every first sighting, so it's deliberately quiet -- a small dot, not a
            second chip -- rather than something that reads as a problem. */}
        {entry.state === 'REMOTE_ONLY' && entry.substate === 'settling' && (
          <span
            className="ml-1 inline-block h-1.5 w-1.5 rounded-full bg-sky-400 align-middle dark:bg-sky-500"
            title="Waiting for another scan to confirm this item has stopped changing before it's queued"
          />
        )}
      </span>
      <span className="w-16 shrink-0 text-right">
        {action && (
          <button
            type="button"
            disabled={actionBusy}
            onClick={() => onAction(entry)}
            className={`rounded px-1.5 py-0.5 text-xs font-medium disabled:opacity-50 ${
              action === 'stop'
                ? 'border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950'
                : 'border border-zinc-300 text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'
            }`}
          >
            {action === 'stop' ? 'Stop' : 'Queue'}
          </button>
        )}
      </span>
    </div>
  )
}

/** A bulk action's outcome, reported honestly rather than swallowed (phase 9, DESIGN.md
 * §9.2): "7 of 10 queued" plus which ones failed and why, not a silent `Promise.all` that
 * throws on the first rejection and leaves the other 9 outcomes unknown to the user.
 */
interface BulkFailure {
  rel_path: string
  name: string
  error: string
}

interface BulkOutcome {
  action: 'queue' | 'stop'
  total: number
  succeeded: number
  failures: BulkFailure[]
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason)
}

/** DESIGN.md §9.2's Files tree: virtualized (`@tanstack/react-virtual`, smooth at 10k+
 * rows -- deferred in phase 2, see docs/decisions.md), collapsible, per-row state chip,
 * size, and a contextual Queue/Stop action. Multi-select with shift-range plus bulk actions
 * (§9.2) lives above the virtualized list so it can act on rows that are currently scrolled
 * out of view, not just what's rendered. Phase 9 adds text/state filters (client-side --
 * this page is WS-driven with the whole queue's tree already in the browser, unlike
 * History's server-paginated model, so there is no endpoint to add) and honest partial-
 * failure reporting for bulk Queue/Stop.
 */
export function FileTree({ nodes }: { nodes: FileNode[] }) {
  const tree = useMemo(() => buildTree(nodes), [nodes])
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [lastClickedPath, setLastClickedPath] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<Set<string>>(new Set())
  const [bulkOutcome, setBulkOutcome] = useState<BulkOutcome | null>(null)
  const [searchText, setSearchText] = useState('')
  const [stateFilter, setStateFilter] = useState('')

  // Every entry regardless of collapse state -- selection and the state-filter dropdown's
  // own option list must survive a directory being collapsed, and a text/state match inside
  // a collapsed directory must still be findable (below).
  const fullFlat = useMemo(() => flatten(tree, new Set()), [tree])
  const byPath = useMemo(() => new Map(fullFlat.map((e) => [e.rel_path, e])), [fullFlat])
  const availableStates = useMemo(
    () => [...new Set(nodes.map((n) => n.state))].sort(),
    [nodes],
  )

  const filtersActive = stateFilter !== '' || searchText.trim() !== ''

  // Whether there is any directory to fold/unfold at all -- drives disabling Expand/Collapse
  // all the same way other controls on this page disable for an empty state, rather than a
  // click that silently does nothing.
  const hasDirectories = useMemo(() => fullFlat.some((e) => e.is_dir), [fullFlat])

  // Both buttons disable for two unrelated reasons (no directories at all, or an active
  // filter) and used to share one `title` that only explained the filter case -- a queue
  // with no directories rendered greyed out with no explanation and read as a broken
  // feature (the user hit exactly this). `undefined` when enabled, since a `title` on an
  // enabled button is just clutter.
  const disabledReason = (verb: 'expand' | 'collapse'): string | undefined => {
    if (!hasDirectories) return `Nothing to ${verb} -- this queue has no directories`
    if (filtersActive) return 'Clear filters to change collapse state'
    return undefined
  }

  // A match plus every one of its ancestor directories (so the tree stays navigable down to
  // the hit) -- computed over the *full*, uncollapsed set, then substituted for the normal
  // collapse-respecting flatten below. Filtering while a directory happens to be collapsed
  // must still surface matches inside it, so a filter's flat list ignores `collapsed`
  // entirely rather than compounding with it.
  const visiblePaths = useMemo(() => {
    if (!filtersActive) return null
    const needle = searchText.trim().toLowerCase()
    const visible = new Set<string>()
    for (const entry of fullFlat) {
      if (stateFilter && entry.state !== stateFilter) continue
      if (needle && !entry.name.toLowerCase().includes(needle) && !entry.rel_path.toLowerCase().includes(needle)) {
        continue
      }
      let path: string | null = entry.rel_path
      while (path != null && !visible.has(path)) {
        visible.add(path)
        const lastSlash = path.lastIndexOf('/')
        path = lastSlash === -1 ? null : path.slice(0, lastSlash)
      }
    }
    return visible
  }, [filtersActive, fullFlat, searchText, stateFilter])

  const flat = useMemo(() => {
    if (!filtersActive || visiblePaths == null) return flatten(tree, collapsed)
    return fullFlat.filter((e) => visiblePaths.has(e.rel_path))
  }, [tree, collapsed, filtersActive, fullFlat, visiblePaths])

  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: flat.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT_PX,
    overscan: 16,
  })

  const toggleCollapse = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  /** `collapsed` starts empty (default expanded, see the field above), so expand-all is just
   * clearing it, and collapse-all is filling it with every directory path. Built from
   * `fullFlat` -- already a full, uncollapsed walk of the whole tree (above) -- rather than
   * re-walking `tree`, so this stays one O(tree size) pass over data already computed for
   * filtering, not a second traversal. Both are pure `Set` replacements, no per-row effects.
   */
  const expandAll = () => setCollapsed(new Set())
  const collapseAll = () => {
    setCollapsed(new Set(fullFlat.filter((e) => e.is_dir).map((e) => e.rel_path)))
  }

  const toggleSelect = (entry: TreeEntry, shiftKey: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (shiftKey && lastClickedPath != null) {
        const fromIdx = flat.findIndex((e) => e.rel_path === lastClickedPath)
        const toIdx = flat.findIndex((e) => e.rel_path === entry.rel_path)
        if (fromIdx !== -1 && toIdx !== -1) {
          const [lo, hi] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx]
          for (let i = lo; i <= hi; i++) {
            if (flat[i].id != null) next.add(flat[i].rel_path)
          }
        }
      } else if (next.has(entry.rel_path)) {
        next.delete(entry.rel_path)
      } else {
        next.add(entry.rel_path)
      }
      return next
    })
    setLastClickedPath(entry.rel_path)
  }

  const clearSelection = () => setSelected(new Set())

  const runRowAction = async (entry: TreeEntry) => {
    if (entry.id == null) return
    setRowBusy((prev) => new Set(prev).add(entry.rel_path))
    try {
      const action = rowAction(entry)
      if (action === 'queue') await queueItem(entry.id)
      else if (action === 'stop') await stopItem(entry.id)
    } finally {
      setRowBusy((prev) => {
        const next = new Set(prev)
        next.delete(entry.rel_path)
        return next
      })
    }
  }

  const selectedEntries = useMemo(
    () => [...selected].map((path) => byPath.get(path)).filter((e): e is TreeEntry => e != null && e.id != null),
    [selected, byPath],
  )

  /** `Promise.allSettled`, not `Promise.all` -- one rejection must not hide the outcome of
   * the other N-1 requests, and the caller (DESIGN.md §9.2, phase 9) needs to say "7 of 10
   * queued, these 3 failed because …" rather than either "all failed" (the first rejection
   * wins with `Promise.all`) or silently swallowing which ones didn't make it. Entries that
   * failed stay selected afterward so the summary's list lines up with what's still checked
   * and a retry is one click away; entries that succeeded are deselected.
   */
  const runBulk = async (action: 'queue' | 'stop') => {
    const targets = selectedEntries
    if (targets.length === 0) return
    setBulkBusy(true)
    setBulkOutcome(null)
    try {
      const results = await Promise.allSettled(
        targets.map((e) => (action === 'queue' ? queueItem(e.id as number) : stopItem(e.id as number))),
      )
      const failures: BulkFailure[] = []
      const succeededPaths = new Set<string>()
      results.forEach((result, i) => {
        const entry = targets[i]
        if (result.status === 'fulfilled') succeededPaths.add(entry.rel_path)
        else failures.push({ rel_path: entry.rel_path, name: entry.name, error: errorMessage(result.reason) })
      })
      setSelected((prev) => {
        const next = new Set(prev)
        for (const path of succeededPaths) next.delete(path)
        return next
      })
      setBulkOutcome({ action, total: targets.length, succeeded: succeededPaths.size, failures })
    } finally {
      setBulkBusy(false)
    }
  }

  const bulkQueue = () => runBulk('queue')
  const bulkStop = () => runBulk('stop')

  if (tree.length === 0) {
    return <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">Nothing scanned yet.</p>
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          className={inputClasses}
          placeholder="Search name or path…"
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          aria-label="Search files"
        />
        <select
          className={inputClasses}
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
          aria-label="Filter by state"
        >
          <option value="">All states</option>
          {availableStates.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!hasDirectories || filtersActive}
          onClick={expandAll}
          title={disabledReason('expand')}
          className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          Expand all
        </button>
        <button
          type="button"
          disabled={!hasDirectories || filtersActive}
          onClick={collapseAll}
          title={disabledReason('collapse')}
          className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          Collapse all
        </button>
        {filtersActive && (
          <>
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              {flat.length} of {fullFlat.length} shown
            </span>
            <button
              type="button"
              onClick={() => {
                setSearchText('')
                setStateFilter('')
              }}
              className="rounded-md px-2 py-1 text-xs font-medium text-zinc-600 underline hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
            >
              Clear filters
            </button>
          </>
        )}
      </div>

      {selected.size > 0 && (
        <div className="flex items-center gap-2 rounded-md border border-sky-300 bg-sky-50 px-3 py-1.5 text-sm dark:border-sky-900 dark:bg-sky-950/40">
          <span className="font-medium text-sky-900 dark:text-sky-200">{selected.size} selected</span>
          <button
            type="button"
            disabled={bulkBusy}
            onClick={bulkQueue}
            className="rounded-md border border-sky-400 px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-100 disabled:opacity-50 dark:border-sky-700 dark:text-sky-200 dark:hover:bg-sky-900"
          >
            Queue selected
          </button>
          <button
            type="button"
            disabled={bulkBusy}
            onClick={bulkStop}
            className="rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
          >
            Stop selected
          </button>
          <button
            type="button"
            onClick={clearSelection}
            className="rounded-md px-2 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
          >
            Clear
          </button>
        </div>
      )}

      {bulkOutcome && (
        <div
          className={`rounded-md border px-3 py-2 text-sm ${
            bulkOutcome.failures.length === 0
              ? 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200'
              : 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200'
          }`}
        >
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">
              {bulkOutcome.action === 'queue' ? 'Queue selected' : 'Stop selected'}: {bulkOutcome.succeeded} of{' '}
              {bulkOutcome.total} succeeded
              {bulkOutcome.failures.length > 0 && `, ${bulkOutcome.failures.length} failed`}
            </span>
            <button
              type="button"
              onClick={() => setBulkOutcome(null)}
              className="shrink-0 text-xs underline decoration-dotted"
            >
              Dismiss
            </button>
          </div>
          {bulkOutcome.failures.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-xs">
              {bulkOutcome.failures.map((f) => (
                <li key={f.rel_path}>
                  <span className="font-mono">{f.name}</span> — {f.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {flat.length === 0 ? (
        <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">No files match these filters.</p>
      ) : (
        <div
          ref={scrollRef}
          className="max-h-[70vh] overflow-auto rounded-md border border-zinc-200 dark:border-zinc-800"
        >
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const entry = flat[virtualRow.index]
              return (
                <div
                  key={entry.rel_path}
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    width: '100%',
                    height: virtualRow.size,
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                >
                  <Row
                    entry={entry}
                    isCollapsed={collapsed.has(entry.rel_path)}
                    isSelected={selected.has(entry.rel_path)}
                    onToggleCollapse={toggleCollapse}
                    onToggleSelect={toggleSelect}
                    onAction={runRowAction}
                    actionBusy={rowBusy.has(entry.rel_path)}
                  />
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
