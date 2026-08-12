import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useMemo, useRef } from 'react'
import type { FileNode } from '../api/types'
import { formatBytes, formatPercent } from '../lib/format'
import { StateChip } from './StateChip'

const ROW_HEIGHT_PX = 40

interface ItemDrawerProps {
  title: string
  rootRelPath: string
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
 */
export function ItemDrawer({ title, rootRelPath, nodes, onClose }: ItemDrawerProps) {
  const files = useMemo(() => filesUnder(nodes, rootRelPath), [nodes, rootRelPath])
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
