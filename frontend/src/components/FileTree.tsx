import { useMemo, useState } from 'react'
import type { FileNode } from '../api/types'
import { formatBytes } from '../lib/format'
import { StateChip } from './StateChip'

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

function Row({ entry, collapsed, onToggle }: { entry: TreeEntry; collapsed: Set<string>; onToggle: (path: string) => void }) {
  const isCollapsed = collapsed.has(entry.rel_path)
  const size = entry.is_dir ? entry.remote_size : (entry.local_size ?? entry.remote_size)

  return (
    <>
      <div
        className="flex items-center gap-2 border-b border-zinc-100 px-2 py-1 text-sm hover:bg-zinc-50 dark:border-zinc-900 dark:hover:bg-zinc-900"
        style={{ paddingLeft: `${entry.depth * 1.25 + 0.5}rem` }}
      >
        {entry.is_dir ? (
          <button
            type="button"
            onClick={() => onToggle(entry.rel_path)}
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
      </div>
      {entry.is_dir && !isCollapsed && entry.children.map((child) => (
        <Row key={child.rel_path} entry={child} collapsed={collapsed} onToggle={onToggle} />
      ))}
    </>
  )
}

/** DESIGN.md §9.2's Files tree, scoped to what phase 2 has: read-only, collapsible,
 * per-row state chip and size, grouped by queue (the caller renders one `<FileTree>` per
 * queue). Full row virtualization for 10k+ rows is deferred to the phase 9 polish pass
 * (§13) — see docs/decisions.md.
 */
export function FileTree({ nodes }: { nodes: FileNode[] }) {
  const tree = useMemo(() => buildTree(nodes), [nodes])
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  const toggle = (path: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  if (tree.length === 0) {
    return <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">Nothing scanned yet.</p>
  }

  return (
    <div className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
      {tree.map((entry) => (
        <Row key={entry.rel_path} entry={entry} collapsed={collapsed} onToggle={toggle} />
      ))}
    </div>
  )
}
