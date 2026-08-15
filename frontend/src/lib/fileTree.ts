// Pure tree/sort/collapse/facet/column-width logic for the Files page, extracted from
// `components/FileTree.tsx` (audit P1, docs/audit-v0.1.0.md) so a change to any of it loads this
// ~350-line module instead of the 2000+-line component. Everything here is a pure function of its
// inputs (no React, no hooks, no DOM) -- which is exactly what let `FileTree.test.ts` already
// exercise it directly, and why it was the safe first slice of that component to move out. The
// component keeps every JSX/stateful piece and imports these back by name.

import type { CSSProperties } from 'react'
import type { FileNode } from '../api/types'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import {
  childEtaS,
  childSpeedLabel,
  childSpeedSortValue,
  formatEta,
  percentValue,
  transferEtaLabel,
  transferSpeedLabel,
  transferSpeedSortValue,
} from './format'

/** How long a `child_progress` sample (2026-08-14, "per-file speed inside a mirror") is
 * trusted before `buildTree` treats it as stale and resolves `child_speed_bps` to `null` --
 * the frontend half of the freshness-gating decision (see `lib/format.ts`'s module comment
 * above `childSpeedLabel`, and docs/decisions.md). The backend throttles a live child's own
 * publish cadence to roughly `CHILD_PROGRESS_THROTTLE_TICKS * tick_s` while it keeps changing
 * (~3s at the default `tick_s`, `core/queue.py`) but that constant isn't on the wire and
 * `tick_s` is configurable, so this is a generous, independent multiple of the *default* rather
 * than a value derived from a setting the frontend can't see -- loose enough to absorb normal
 * jitter (a slow tick, WS latency) without a value flickering off between two genuinely live
 * samples, tight enough that a child that actually stopped changing reads as "not transferring"
 * again within a few seconds, not indefinitely.
 */
export const CHILD_SPEED_FRESHNESS_MS = 10_000

export interface TreeEntry extends FileNode {
  name: string
  depth: number
  children: TreeEntry[]
  /** The live, EMA-smoothed instantaneous rate from the `progress` WS message, looked up by this
   * row's own `id` from the `speedByItemId` map `FileTree` threads in from `useLiveModel`. `null`
   * for any row that isn't the parent item of a currently-running job. */
  speed_bps: number | null
  /** A **child** file's own live rate (from the `child_progress` WS message), resolved to `null`
   * here whenever the sample is older than `CHILD_SPEED_FRESHNESS_MS`. */
  child_speed_bps: number | null
  /** The **job-level** ETA (`core/progress.py.JobProgress.eta_s`), looked up by this row's own
   * `id` from the `etaByItemId` map. `null` for the same rows `speed_bps` is `null` for. */
  eta_s: number | null
}

/** What a row's own size column shows. A **file** prefers `local_size` (falling back to
 * `remote_size` only when local is unknown) so an in-progress download reads as live progress; a
 * **directory** is the opposite (`remote_size` first, the rollup total), falling back to
 * `local_size` only when `remote_size` is unknown -- the latter matters once a `move` queue
 * deletes a completed directory's verified remote copy and its `remote_size` goes NULL. Both are
 * already rollups from `core/reconcile.py`. Shared so `Row`, the hover tooltip, and the `size`
 * sort key all read one function and can't disagree about what "size" means. */
export function nodeDisplaySize(entry: TreeEntry): number | null {
  return entry.is_dir
    ? (entry.remote_size ?? entry.local_size)
    : (entry.local_size ?? entry.remote_size)
}

/** The Speed column's text for one row -- prefers the row's own **job-level** rate and falls back
 * to its **child-level** rate only when the job-level reading has nothing to show. The two can
 * never both apply to the same row in a way that reads as additive (see the field docstrings). */
export function effectiveSpeedLabel(entry: TreeEntry): string {
  const jobLabel = transferSpeedLabel(entry.state, entry.speed_bps)
  return jobLabel !== '—' ? jobLabel : childSpeedLabel(entry.child_speed_bps)
}

/** `effectiveSpeedLabel`'s sort-value counterpart -- same job-level-first, child-level-fallback
 * shape, both already null-safe/null-last via `compareValues`. */
export function effectiveSpeedSortValue(entry: TreeEntry): number | null {
  return transferSpeedSortValue(entry.state, entry.speed_bps) ?? childSpeedSortValue(entry.child_speed_bps)
}

/** The Speed cell's ETA text -- same job-level-first, child-level-fallback shape as
 * `effectiveSpeedLabel`. Deliberately not its own sort key (the Speed column keeps sorting by
 * rate even though its cell shows both rate and ETA). */
export function effectiveEtaLabel(entry: TreeEntry): string {
  const jobLabel = transferEtaLabel(entry.state, entry.eta_s)
  return jobLabel !== '—'
    ? jobLabel
    : formatEta(childEtaS(entry.remote_size, entry.local_size, entry.child_speed_bps))
}

export function buildTree(
  nodes: FileNode[],
  speedByItemId: Record<number, number> = {},
  childSpeedByItemId: Record<number, ChildSpeedSample> = {},
  etaByItemId: Record<number, number | null> = {},
  /** Injectable so the freshness check is deterministic in a test; defaults to the real clock. */
  now: number = Date.now(),
): TreeEntry[] {
  const byPath = new Map<string, TreeEntry>()
  const roots: TreeEntry[] = []

  // Parents always sort before children by path-segment count -- every ancestor is guaranteed
  // present, but this tolerates a missing parent defensively rather than dropping an orphan.
  const sorted = [...nodes].sort(
    (a, b) => a.rel_path.split('/').length - b.rel_path.split('/').length,
  )

  for (const node of sorted) {
    const lastSlash = node.rel_path.lastIndexOf('/')
    const name = lastSlash === -1 ? node.rel_path : node.rel_path.slice(lastSlash + 1)
    const parentPath = lastSlash === -1 ? null : node.rel_path.slice(0, lastSlash)
    const parent = parentPath ? byPath.get(parentPath) : undefined
    const childSample = node.id != null ? childSpeedByItemId[node.id] : undefined
    const entry: TreeEntry = {
      ...node,
      name,
      depth: parent ? parent.depth + 1 : 0,
      children: [],
      speed_bps: node.id != null ? (speedByItemId[node.id] ?? null) : null,
      child_speed_bps:
        childSample != null && now - childSample.receivedAt <= CHILD_SPEED_FRESHNESS_MS
          ? childSample.speedBps
          : null,
      eta_s: node.id != null ? (etaByItemId[node.id] ?? null) : null,
    }
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

/** Depth-first, respecting `isCollapsed` -- this is what gets virtualized. A collapsed
 * directory's children never enter the flat list, so scroll math stays correct without the
 * virtualizer knowing anything about tree structure. */
export function flatten(roots: TreeEntry[], isCollapsed: (path: string) => boolean): TreeEntry[] {
  const out: TreeEntry[] = []
  const walk = (entries: TreeEntry[]) => {
    for (const entry of entries) {
      out.push(entry)
      if (entry.is_dir && !isCollapsed(entry.rel_path)) walk(entry.children)
    }
  }
  walk(roots)
  return out
}

// --- Sorting: reorders siblings within each parent, never the flattened array (flattening is
// what the virtualizer walks; sorting it directly would tear children from their parents). ------

export type SortKey = 'name' | 'size' | 'speed' | 'state_changed_at' | 'percent'
export type SortDir = 'asc' | 'desc'

export const SORT_KEYS: SortKey[] = ['name', 'size', 'speed', 'state_changed_at', 'percent']

function sortValue(entry: TreeEntry, key: SortKey): string | number | null {
  switch (key) {
    case 'name':
      return entry.name.toLowerCase()
    case 'size':
      return nodeDisplaySize(entry)
    case 'speed':
      return effectiveSpeedSortValue(entry)
    case 'state_changed_at':
      return entry.state_changed_at
    case 'percent':
      return percentValue(entry.local_size, entry.remote_size)
  }
}

/** Null/absent values always sort last, regardless of direction. */
export function compareValues(a: string | number | null, b: string | number | null, dir: SortDir): number {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  const cmp = typeof a === 'string' && typeof b === 'string' ? a.localeCompare(b) : (a as number) - (b as number)
  return dir === 'asc' ? cmp : -cmp
}

function sortSiblingsRecursive(entries: TreeEntry[], key: SortKey, dir: SortDir): void {
  entries.sort((a, b) => {
    if (key === 'name') {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
      const cmp = a.name.localeCompare(b.name)
      return dir === 'asc' ? cmp : -cmp
    }
    const cmp = compareValues(sortValue(a, key), sortValue(b, key), dir)
    if (cmp !== 0) return cmp
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.name.localeCompare(b.name)
  })
  for (const entry of entries) sortSiblingsRecursive(entry.children, key, dir)
}

/** Sorts every level of the tree by `key`/`dir`, siblings only. Returns a fresh (deep-cloned)
 * tree rather than mutating `roots`, so it stays a pure function -- safe from a `useMemo`. */
export function sortTree(roots: TreeEntry[], key: SortKey, dir: SortDir): TreeEntry[] {
  const clone = (entries: TreeEntry[]): TreeEntry[] =>
    entries.map((entry) => ({ ...entry, children: clone(entry.children) }))
  const cloned = clone(roots)
  sortSiblingsRecursive(cloned, key, dir)
  return cloned
}

// --- Collapse preference: "default plus exceptions," not a saved set of collapsed paths -- a
// directory that appears later (over the WebSocket) inherits the current default automatically. --

export interface CollapsePreference {
  defaultCollapsed: boolean
  exceptions: string[]
}

export const DEFAULT_COLLAPSE_PREFERENCE: CollapsePreference = { defaultCollapsed: false, exceptions: [] }

export function isCollapsePreference(value: unknown): value is CollapsePreference {
  if (typeof value !== 'object' || value == null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.defaultCollapsed === 'boolean' &&
    Array.isArray(v.exceptions) &&
    v.exceptions.every((p) => typeof p === 'string')
  )
}

/** A path in `exceptions` reads as the opposite of the default; every other path -- including one
 * never seen before -- reads as the default itself, which is what makes a newly-arrived directory
 * inherit the current default automatically. */
export function resolveCollapsed(defaultCollapsed: boolean, exceptions: Set<string>, path: string): boolean {
  return exceptions.has(path) ? !defaultCollapsed : defaultCollapsed
}

export interface SortPreference {
  key: SortKey
  dir: SortDir
}

export const DEFAULT_SORT_PREFERENCE: SortPreference = { key: 'name', dir: 'asc' }

export function isSortPreference(value: unknown): value is SortPreference {
  if (typeof value !== 'object' || value == null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.key === 'string' &&
    (SORT_KEYS as string[]).includes(v.key) &&
    (v.dir === 'asc' || v.dir === 'desc')
  )
}

/** Whether this node still has a remote copy -- `remote_size` is `null` for `LOCAL_ONLY`, for a
 * vanished/`REMOVED_BOTH` row, and for a `move`-mode item whose verified remote copy this codebase
 * deleted on purpose. Drives `rowAction`'s gates and the delete confirmation's wording. */
export function hasRemoteCopy(node: FileNode): boolean {
  return node.remote_size != null
}

/** What this row's own action button offers, if anything (DESIGN.md §9.2, §4.7). Manual queueing
 * always wins over suppression and is never filtered by state -- except a node with nothing remote
 * to fetch (`!hasRemoteCopy`), where "Queue" would mean nothing. `'redownload'` is the same click
 * as `'queue'`, relabelled for a row we deleted (`suppressed_reason === 'deleted_local'`) whose
 * remote copy has since come back. */
export function rowAction(node: FileNode): 'queue' | 'stop' | 'redownload' | null {
  if (node.id == null) return null
  if (node.state === 'QUEUED' || node.state === 'DOWNLOADING') return 'stop'
  if (!hasRemoteCopy(node)) return null
  if (node.suppressed_reason === 'deleted_local' && hasRemoteCopy(node)) return 'redownload'
  return 'queue'
}

export type FacetFilter = '' | 'has_remote' | 'has_local' | 'extracted' | 'not_extracted' | 'missing_locally'

/** One predicate per facet-filter option, each keyed off `core/itemview.py`'s own `level`/`reason`
 * codes rather than re-deriving presence from raw bytes here. */
export function matchesFacetFilter(entry: TreeEntry, filter: FacetFilter): boolean {
  switch (filter) {
    case '':
      return true
    case 'has_remote':
      return entry.facets.remote.reason === 'present'
    case 'has_local':
      return entry.facets.local.level !== 'dim'
    case 'extracted':
      return entry.facets.extracted.reason === 'extracted'
    case 'not_extracted':
      return entry.facets.extracted.reason !== 'extracted'
    case 'missing_locally':
      return entry.downloaded_at != null && entry.facets.local.reason === 'missing'
  }
}

// --- Column widths: one shared definition drives both the header row and `Row`. ----------------

export interface ColumnDef {
  id: string
  label: string
  defaultWidth: number
  minWidth: number
  align: 'right' | 'left'
  sortKey?: SortKey
  /** Overrides the header's own hover text -- only "Status" needs one today. */
  title?: string
}

/** The five (now six) fixed-width columns, in render order. Name is not here -- it flexes and
 * absorbs whatever space these don't claim, which is also why only these get a drag handle. */
export const RESIZABLE_COLUMNS: ColumnDef[] = [
  { id: 'size', label: 'Size', defaultWidth: 96, minWidth: 56, align: 'right', sortKey: 'size' },
  { id: 'speed', label: 'Speed', defaultWidth: 128, minWidth: 64, align: 'right', sortKey: 'speed' },
  {
    id: 'status',
    label: 'Status',
    defaultWidth: 128,
    minWidth: 72,
    align: 'right',
    sortKey: 'percent',
    title: 'Sort by % complete',
  },
  { id: 'lifecycle', label: 'R L V E', defaultWidth: 80, minWidth: 68, align: 'right' },
  { id: 'changed', label: 'Changed', defaultWidth: 128, minWidth: 72, align: 'right', sortKey: 'state_changed_at' },
  { id: 'actions', label: 'Actions', defaultWidth: 128, minWidth: 88, align: 'right' },
]

export type ColumnWidths = Record<string, number>

export function defaultColumnWidths(): ColumnWidths {
  return Object.fromEntries(RESIZABLE_COLUMNS.map((c) => [c.id, c.defaultWidth]))
}

export function columnMinWidth(id: string): number {
  return RESIZABLE_COLUMNS.find((c) => c.id === id)?.minWidth ?? 40
}

/** No maximum -- growing a column just eats into Name's share, then widens the row past the scroll
 * container (which gains its own horizontal scrollbar). A column dragged absurdly wide is
 * recoverable with one double-click, unlike one dragged to zero. */
export function clampColumnWidth(id: string, width: number): number {
  return Math.max(columnMinWidth(id), Math.round(width))
}

export function isColumnWidths(value: unknown): value is ColumnWidths {
  if (typeof value !== 'object' || value == null) return false
  return Object.entries(value as Record<string, unknown>).every(
    ([, v]) => typeof v === 'number' && Number.isFinite(v),
  )
}

/** Saved widths merged onto the defaults -- an id no longer in `RESIZABLE_COLUMNS` is dropped
 * rather than applied to whatever now occupies that slot. Re-clamps every surviving value. */
export function mergeColumnWidths(saved: ColumnWidths | null): ColumnWidths {
  const widths = defaultColumnWidths()
  if (saved == null) return widths
  for (const col of RESIZABLE_COLUMNS) {
    const savedWidth = saved[col.id]
    if (typeof savedWidth === 'number' && Number.isFinite(savedWidth)) {
      widths[col.id] = clampColumnWidth(col.id, savedWidth)
    }
  }
  return widths
}

/** Both the header cell and the matching `Row` cell size themselves off the same CSS custom
 * property `--col-size-<id>` -- that indirection keeps a drag off the React render path. */
export function fixedColumnStyle(id: string): CSSProperties {
  const v = `var(--col-size-${id})`
  return { width: v, minWidth: v, maxWidth: v }
}
