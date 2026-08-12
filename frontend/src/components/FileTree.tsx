import { useVirtualizer } from '@tanstack/react-virtual'
import { useMemo, useRef, useState } from 'react'
import { queueItem, stopItem } from '../api/client'
import type { FileNode } from '../api/types'
import { formatBytes } from '../lib/format'
import { StateChip } from './StateChip'

const ROW_HEIGHT_PX = 32

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

/** DESIGN.md §9.2's Files tree: virtualized (`@tanstack/react-virtual`, smooth at 10k+
 * rows -- deferred in phase 2, see docs/decisions.md), collapsible, per-row state chip,
 * size, and a contextual Queue/Stop action. Multi-select with shift-range plus bulk actions
 * (§9.2) lives above the virtualized list so it can act on rows that are currently scrolled
 * out of view, not just what's rendered.
 */
export function FileTree({ nodes }: { nodes: FileNode[] }) {
  const tree = useMemo(() => buildTree(nodes), [nodes])
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [lastClickedPath, setLastClickedPath] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<Set<string>>(new Set())

  const flat = useMemo(() => flatten(tree, collapsed), [tree, collapsed])
  const byPath = useMemo(() => new Map(flat.map((e) => [e.rel_path, e])), [flat])

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

  const bulkQueue = async () => {
    setBulkBusy(true)
    try {
      await Promise.all(selectedEntries.map((e) => queueItem(e.id as number)))
      clearSelection()
    } finally {
      setBulkBusy(false)
    }
  }

  const bulkStop = async () => {
    setBulkBusy(true)
    try {
      await Promise.all(selectedEntries.map((e) => stopItem(e.id as number)))
      clearSelection()
    } finally {
      setBulkBusy(false)
    }
  }

  if (tree.length === 0) {
    return <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">Nothing scanned yet.</p>
  }

  return (
    <div className="flex flex-col gap-2">
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
    </div>
  )
}
