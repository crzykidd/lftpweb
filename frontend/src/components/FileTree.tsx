import { useVirtualizer } from '@tanstack/react-virtual'
import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { deleteItem, queueItem, stopItem } from '../api/client'
import type { FileNode } from '../api/types'
import { formatBytes, percentValue, stateAgeLabel } from '../lib/format'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import { LifecycleIcons } from './LifecycleIcons'
import { StateChip } from './StateChip'

// One shared ticker per tree, not a `setInterval` per row (migration 006's `state_changed_at`
// column, DESIGN.md §9.2). This page can hold thousands of rows; a per-row timer would mean
// thousands of live intervals for a reading that only needs to refresh every few seconds. A
// single bumped counter here forces a re-render of whatever rows are currently mounted --
// cheap, because the virtualizer only ever mounts the visible slice.
const AGE_TICK_INTERVAL_MS = 15_000

const ROW_HEIGHT_PX = 32

const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

interface TreeEntry extends FileNode {
  name: string
  depth: number
  children: TreeEntry[]
}

/** What a row's own size column shows -- a directory's `remote_size` (already a rollup,
 * `core/reconcile.py`), a file's `local_size` falling back to `remote_size` (a not-yet-touched
 * file has no local bytes to show at all). Named and shared so `Row` and the `size` sort key
 * (below) can never quietly disagree about what "size" means for a node.
 */
function nodeDisplaySize(entry: TreeEntry): number | null {
  return entry.is_dir ? entry.remote_size : (entry.local_size ?? entry.remote_size)
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

/** Depth-first, respecting `isCollapsed` -- this is what gets virtualized. A collapsed
 * directory's children simply never enter the flat list, so scroll math stays correct
 * without the virtualizer needing to know anything about tree structure. A predicate rather
 * than a `Set` of collapsed paths (2026-08-13) -- the persisted collapse preference below is
 * "default plus exceptions," not an enumerable set of collapsed paths, and a predicate is the
 * one shape that works for both that and the old plain-`Set` caller.
 */
function flatten(roots: TreeEntry[], isCollapsed: (path: string) => boolean): TreeEntry[] {
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

// --- Sorting (2026-08-13): reorders siblings within each parent, never the flattened array --
// flattening is what the virtualizer walks, and sorting it directly would tear children away
// from their parents. `sortTree` below runs on the built tree, before `flatten`.

type SortKey = 'name' | 'size' | 'state_changed_at' | 'percent'
type SortDir = 'asc' | 'desc'

const SORT_KEYS: SortKey[] = ['name', 'size', 'state_changed_at', 'percent']
const SORT_LABELS: Record<SortKey, string> = {
  name: 'Name',
  size: 'Size',
  state_changed_at: 'Last change',
  percent: '% complete',
}

function sortValue(entry: TreeEntry, key: SortKey): string | number | null {
  switch (key) {
    case 'name':
      return entry.name.toLowerCase()
    case 'size':
      return nodeDisplaySize(entry)
    case 'state_changed_at':
      return entry.state_changed_at
    case 'percent':
      return percentValue(entry.local_size, entry.remote_size)
  }
}

/** Null/absent values always sort last, regardless of direction -- an "unknown" reading
 * (no `remote_size`, no `state_changed_at` yet) staying put rather than jumping to the top the
 * moment a user flips to descending is the less surprising behavior of the two.
 */
function compareValues(a: string | number | null, b: string | number | null, dir: SortDir): number {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  const cmp = typeof a === 'string' && typeof b === 'string' ? a.localeCompare(b) : (a as number) - (b as number)
  return dir === 'asc' ? cmp : -cmp
}

function sortSiblingsRecursive(entries: TreeEntry[], key: SortKey, dir: SortDir): void {
  entries.sort((a, b) => {
    if (key === 'name') {
      // Preserves the tree's existing default look (directories grouped before files) --
      // direction flips the name ordering within each group, not the grouping itself.
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
      const cmp = a.name.localeCompare(b.name)
      return dir === 'asc' ? cmp : -cmp
    }
    const cmp = compareValues(sortValue(a, key), sortValue(b, key), dir)
    if (cmp !== 0) return cmp
    // Tie-break so equal values don't jitter across renders: directories first, then name.
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1
    return a.name.localeCompare(b.name)
  })
  for (const entry of entries) sortSiblingsRecursive(entry.children, key, dir)
}

/** Sorts every level of the tree by `key`/`dir`, siblings only -- a directory's own position
 * among its siblings is decided by its rollup size/percent/`state_changed_at` (already computed
 * by `core/reconcile.py`/`core/itemview.py`, nothing derived here), and its children are sorted
 * the same way one level down. Returns a fresh tree (deep-cloned) rather than mutating `roots`
 * in place, so this stays a pure function of its inputs -- safe to call from a `useMemo`.
 */
function sortTree(roots: TreeEntry[], key: SortKey, dir: SortDir): TreeEntry[] {
  const clone = (entries: TreeEntry[]): TreeEntry[] =>
    entries.map((entry) => ({ ...entry, children: clone(entry.children) }))
  const cloned = clone(roots)
  sortSiblingsRecursive(cloned, key, dir)
  return cloned
}

// --- Collapse preference (2026-08-13): "default plus exceptions," not a saved set of ---------
// collapsed paths -- a directory that appears later (over the WebSocket) inherits the current
// default automatically; only per-row overrides are tracked explicitly.

interface CollapsePreference {
  defaultCollapsed: boolean
  exceptions: string[]
}

const DEFAULT_COLLAPSE_PREFERENCE: CollapsePreference = { defaultCollapsed: false, exceptions: [] }

function isCollapsePreference(value: unknown): value is CollapsePreference {
  if (typeof value !== 'object' || value == null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.defaultCollapsed === 'boolean' &&
    Array.isArray(v.exceptions) &&
    v.exceptions.every((p) => typeof p === 'string')
  )
}

interface SortPreference {
  key: SortKey
  dir: SortDir
}

const DEFAULT_SORT_PREFERENCE: SortPreference = { key: 'name', dir: 'asc' }

function isSortPreference(value: unknown): value is SortPreference {
  if (typeof value !== 'object' || value == null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.key === 'string' &&
    (SORT_KEYS as string[]).includes(v.key) &&
    (v.dir === 'asc' || v.dir === 'desc')
  )
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

/** Whether "Delete local" (DESIGN.md §9.2; prompts/open-issues.md "7 + 8") makes sense to
 * offer at all -- a node with no local content has nothing this action could do. The backend
 * (`core/local_delete.py.delete_local`) still runs every guard regardless (active job,
 * mount sentinel, path containment) and can withhold even when this returns true; this is
 * only about not showing a button that could never do anything, not a prediction of the
 * guard outcome.
 */
const NO_LOCAL_CONTENT_STATES = new Set(['REMOTE_ONLY', 'EXCLUDED', 'REMOVED_LOCAL', 'REMOVED_BOTH'])
function canDeleteLocal(node: FileNode): boolean {
  return node.id != null && !NO_LOCAL_CONTENT_STATES.has(node.state)
}

/** Bytes this node's delete would free -- the same "how much is there" reading the size
 * column already shows (`Row`'s own `size` computation), reused so the confirmation dialog's
 * total can never disagree with what's rendered per-row.
 */
function localBytes(node: FileNode): number {
  return node.local_size ?? node.remote_size ?? 0
}

/** Whether this node still has a remote copy -- `remote_size` is `null` only for `LOCAL_ONLY`
 * (never tracked remotely; everything else was seen on a scan). Drives the delete
 * confirmation's "what happens to this after I delete it" wording: a remote copy surviving
 * means lftpweb will never re-fetch it on its own (`core/local_delete.py.delete_local` always
 * writes `REMOVED_BOTH` + `auto_queue_suppressed`, which auto-queue excludes unconditionally,
 * regardless of the `re_download_externally_removed` setting -- that setting only ever governs
 * an item *something else* removed, never one this app just deleted).
 */
function hasRemoteCopy(node: FileNode): boolean {
  return node.remote_size != null
}

/** The delete confirmation's remote-copy sentence -- factual and short, telling the user what
 * happens rather than warning them off a safe action (a remote copy surviving is the normal,
 * expected outcome of a `copy`-mode delete, not something to be alarmed about).
 */
function remoteCopyNote(total: number, remoteCount: number): string {
  const localOnlyCount = total - remoteCount
  if (remoteCount === total) {
    return total === 1
      ? 'Its remote copy stays untouched, and lftpweb will not re-fetch it -- it never re-downloads what it just deleted itself.'
      : 'Their remote copies stay untouched, and lftpweb will not re-fetch any of them -- it never re-downloads what it just deleted itself.'
  }
  if (remoteCount === 0) {
    return total === 1
      ? 'It has no remote copy -- once deleted, it is gone entirely.'
      : 'None of them have a remote copy -- once deleted, they are gone entirely.'
  }
  return (
    `${remoteCount} of ${total} still ${remoteCount === 1 ? 'has' : 'have'} a remote copy, which ` +
    `stays untouched and will not be re-fetched; the other ${localOnlyCount} ${
      localOnlyCount === 1 ? 'has' : 'have'
    } no remote copy and will be gone entirely.`
  )
}

/** The state chip's inline fill (2026-08-13): only where a percentage means something.
 * `DOWNLOADING`/`PARTIAL` are the two states an in-progress read is actually informative for --
 * a complete item doesn't need a 100% bar, and `REMOTE_ONLY`/`EXCLUDED` have no denominator
 * that means anything yet. `percentValue` already guards the `NaN`/divide-by-zero cases (no or
 * non-positive `remote_size`), so a queue whose `remote_size` hasn't arrived yet just shows no
 * bar rather than a broken one.
 */
function stateProgressPercent(node: FileNode): number | null {
  if (node.state !== 'DOWNLOADING' && node.state !== 'PARTIAL') return null
  return percentValue(node.local_size, node.remote_size)
}

interface RowProps {
  entry: TreeEntry
  isCollapsed: boolean
  isSelected: boolean
  onToggleCollapse: (path: string) => void
  onToggleSelect: (entry: TreeEntry, shiftKey: boolean) => void
  onAction: (entry: TreeEntry) => void
  onDeleteRequest: (entry: TreeEntry) => void
  actionBusy: boolean
}

function Row({
  entry,
  isCollapsed,
  isSelected,
  onToggleCollapse,
  onToggleSelect,
  onAction,
  onDeleteRequest,
  actionBusy,
}: RowProps) {
  const size = nodeDisplaySize(entry)
  const action = rowAction(entry)
  const deletable = canDeleteLocal(entry)

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
        <StateChip state={entry.state} percent={stateProgressPercent(entry)} />
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
      {/* Lifecycle icons (2026-08-13): R/L/V/E, one glyph per `entry.facets`
          (`core/itemview.py`) -- the accumulated lifecycle, alongside the state chip's current
          verb rather than folded into it. */}
      <span className="flex w-20 shrink-0 justify-end">
        <LifecycleIcons node={entry} />
      </span>
      {/* migration 006's `state_changed_at`: "when did this row last move," labeled by the
          state it's already showing rather than a second, redundant chip. Absolute time in
          local time on hover -- History's date filters are UTC-only (a documented phase 6
          limitation), and this sidesteps that question rather than inheriting it. */}
      <span
        className="w-32 shrink-0 truncate text-right text-xs text-zinc-500 dark:text-zinc-400"
        title={entry.state_changed_at ? new Date(entry.state_changed_at).toLocaleString() : undefined}
      >
        {stateAgeLabel(entry.state, entry.state_changed_at)}
      </span>
      <span className="flex w-32 shrink-0 justify-end gap-1 text-right">
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
        {deletable && (
          <button
            type="button"
            disabled={actionBusy}
            onClick={() => onDeleteRequest(entry)}
            title="Delete the local copy -- this cannot be undone"
            className="rounded border border-red-300 px-1.5 py-0.5 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
          >
            Delete
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
  action: 'queue' | 'stop' | 'delete'
  total: number
  succeeded: number
  failures: BulkFailure[]
}

const BULK_OUTCOME_LABEL: Record<BulkOutcome['action'], string> = {
  queue: 'Queue selected',
  stop: 'Stop selected',
  delete: 'Delete',
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
  // The shared age ticker (module docstring above): bumping this forces a re-render of
  // whatever rows are currently mounted, which is all `stateAgeLabel` needs to catch up --
  // it's computed fresh from `Date.now()` on every render, not memoized against this value.
  const [, bumpAgeTick] = useReducer((c: number) => c + 1, 0)
  useEffect(() => {
    const id = setInterval(() => bumpAgeTick(), AGE_TICK_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  // Sort preference (2026-08-13): read synchronously in the initial `useState`, not a
  // `useEffect`, or the tree paints in the default order and then snaps into the saved one on
  // first load. Same storage helper and failure handling as the collapse preference below --
  // one mechanism, not two.
  const [sortPref, setSortPref] = useState<SortPreference>(
    () => readLocalStorage('files.sort', isSortPreference) ?? DEFAULT_SORT_PREFERENCE,
  )
  const setSort = (next: SortPreference) => {
    setSortPref(next)
    writeLocalStorage('files.sort', next)
  }

  const tree = useMemo(
    () => sortTree(buildTree(nodes), sortPref.key, sortPref.dir),
    [nodes, sortPref],
  )

  // Collapse preference (2026-08-13): "default plus exceptions," not a saved set of collapsed
  // paths -- see this task's own prompt for why persisting the collapsed set directly breaks
  // the moment a new directory arrives over the WebSocket after the set was saved. Also read
  // synchronously, for the same first-paint reason as the sort preference above.
  const [collapsePref, setCollapsePrefState] = useState<CollapsePreference>(
    () => readLocalStorage('files.collapse', isCollapsePreference) ?? DEFAULT_COLLAPSE_PREFERENCE,
  )
  const setCollapsePref = (next: CollapsePreference) => {
    setCollapsePrefState(next)
    writeLocalStorage('files.collapse', next)
  }
  const exceptionSet = useMemo(() => new Set(collapsePref.exceptions), [collapsePref.exceptions])
  const isPathCollapsed = (path: string): boolean =>
    exceptionSet.has(path) ? !collapsePref.defaultCollapsed : collapsePref.defaultCollapsed

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [lastClickedPath, setLastClickedPath] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<Set<string>>(new Set())
  const [bulkOutcome, setBulkOutcome] = useState<BulkOutcome | null>(null)
  const [searchText, setSearchText] = useState('')
  const [stateFilter, setStateFilter] = useState('')
  // The *arr-import case (item 8, prompts/2026-08-13-lifecycle-icons.md): an item with a
  // download history but no local presence right now (`core/itemview.py`'s "local" facet
  // reads `reason: "missing"` -- distinct from "never downloaded," which this checkbox does
  // not match). Composes with the text/state filters below via the same `visiblePaths` set.
  const [missingOnly, setMissingOnly] = useState(false)
  // Delete is irreversible (Queue/Stop are not) -- a confirmation step sits between "the user
  // asked to delete" and "anything actually runs," for both the per-row button and the bulk
  // action. `null` = no pending confirmation; otherwise the exact entries about to be deleted,
  // so the dialog's count/byte total is read from the same list the delete itself will use.
  const [pendingDelete, setPendingDelete] = useState<TreeEntry[] | null>(null)

  // Every entry regardless of collapse state -- selection and the state-filter dropdown's
  // own option list must survive a directory being collapsed, and a text/state match inside
  // a collapsed directory must still be findable (below). Always the *sorted* tree's own
  // order (`tree` above), so filtering never has to re-sort.
  const fullFlat = useMemo(() => flatten(tree, () => false), [tree])
  const byPath = useMemo(() => new Map(fullFlat.map((e) => [e.rel_path, e])), [fullFlat])
  const availableStates = useMemo(
    () => [...new Set(nodes.map((n) => n.state))].sort(),
    [nodes],
  )

  const filtersActive = stateFilter !== '' || searchText.trim() !== '' || missingOnly

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
  // must still surface matches inside it, so a filter's flat list ignores the collapse
  // preference entirely rather than compounding with it -- and the preference itself is left
  // untouched, so it applies again unchanged the moment filters clear.
  const visiblePaths = useMemo(() => {
    if (!filtersActive) return null
    const needle = searchText.trim().toLowerCase()
    const visible = new Set<string>()
    for (const entry of fullFlat) {
      if (stateFilter && entry.state !== stateFilter) continue
      if (missingOnly && !(entry.downloaded_at != null && entry.facets.local.reason === 'missing')) {
        continue
      }
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
  }, [filtersActive, fullFlat, missingOnly, searchText, stateFilter])

  const flat = useMemo(() => {
    if (!filtersActive || visiblePaths == null) return flatten(tree, isPathCollapsed)
    return fullFlat.filter((e) => visiblePaths.has(e.rel_path))
    // isPathCollapsed is a plain closure over collapsePref -- listed below instead of the
    // function identity, which is recreated every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tree, collapsePref, filtersActive, fullFlat, visiblePaths])

  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: flat.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT_PX,
    overscan: 16,
  })

  /** A per-row toggle flips this path's *exception* membership, not a collapsed/expanded bit
   * directly -- `isPathCollapsed` above is what turns "is this path an exception" back into an
   * actual collapsed/expanded reading against the current default. Persisted like every other
   * change to the preference (2026-08-13): whether a manual per-row override survives a reload
   * was this task's own call to make, and persisting it (rather than resetting to the default
   * every time) is the more consistent choice, since it goes through the exact same mechanism.
   */
  const toggleCollapse = (path: string) => {
    const nextExceptions = new Set(collapsePref.exceptions)
    if (nextExceptions.has(path)) nextExceptions.delete(path)
    else nextExceptions.add(path)
    setCollapsePref({ defaultCollapsed: collapsePref.defaultCollapsed, exceptions: [...nextExceptions] })
  }

  /** Expand all / Collapse all set the *default* and clear every exception -- not an
   * enumerated set of paths (2026-08-13; see the module-level comment on `CollapsePreference`
   * for why enumerating breaks the moment a new directory arrives over the WebSocket). This is
   * also what makes the preference itself trivial to persist: two scalars, not a set whose
   * membership would need reconciling against the live tree on every load.
   */
  const expandAll = () => setCollapsePref({ defaultCollapsed: false, exceptions: [] })
  const collapseAll = () => setCollapsePref({ defaultCollapsed: true, exceptions: [] })

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
   *
   * `targets` is explicit (not always `selectedEntries`) so this same runner covers a
   * single-row Delete confirmation, not just the multi-select bulk bar -- one mechanism for
   * both, per the task's own instruction not to build a parallel one.
   */
  const runAction = async (action: BulkOutcome['action'], targets: TreeEntry[]) => {
    if (targets.length === 0) return
    setBulkBusy(true)
    setBulkOutcome(null)
    try {
      const results = await Promise.allSettled(
        targets.map((e) => {
          if (action === 'queue') return queueItem(e.id as number)
          if (action === 'stop') return stopItem(e.id as number)
          return deleteItem(e.id as number)
        }),
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

  const bulkQueue = () => runAction('queue', selectedEntries)
  const bulkStop = () => runAction('stop', selectedEntries)

  const deletableSelected = useMemo(() => selectedEntries.filter(canDeleteLocal), [selectedEntries])
  const requestDeleteRow = (entry: TreeEntry) => setPendingDelete([entry])
  const requestDeleteSelected = () => {
    if (deletableSelected.length > 0) setPendingDelete(deletableSelected)
  }
  const confirmDelete = async () => {
    const targets = pendingDelete
    setPendingDelete(null)
    if (targets) await runAction('delete', targets)
  }
  const pendingDeleteBytes = useMemo(
    () => (pendingDelete ?? []).reduce((sum, e) => sum + localBytes(e), 0),
    [pendingDelete],
  )
  // Split by whether a remote copy survives the delete -- entirely different outcomes worth
  // telling the user apart (see `hasRemoteCopy`'s own docstring for why "will this come back"
  // is always answerable from `remote_size` alone, never a guess).
  const pendingDeleteRemoteCount = useMemo(
    () => (pendingDelete ?? []).filter(hasRemoteCopy).length,
    [pendingDelete],
  )

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
        <label className="flex items-center gap-1.5 text-xs font-medium text-zinc-700 dark:text-zinc-200">
          <input
            type="checkbox"
            checked={missingOnly}
            onChange={(e) => setMissingOnly(e.target.checked)}
          />
          Missing only
        </label>
        <span className="mx-1 h-5 w-px bg-zinc-200 dark:bg-zinc-800" aria-hidden="true" />
        <label className="flex items-center gap-1.5 text-xs font-medium text-zinc-700 dark:text-zinc-200">
          Sort
          <select
            className={inputClasses}
            value={sortPref.key}
            onChange={(e) => setSort({ key: e.target.value as SortKey, dir: sortPref.dir })}
            aria-label="Sort by"
          >
            {SORT_KEYS.map((key) => (
              <option key={key} value={key}>
                {SORT_LABELS[key]}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setSort({ key: sortPref.key, dir: sortPref.dir === 'asc' ? 'desc' : 'asc' })}
          title={sortPref.dir === 'asc' ? 'Ascending -- click for descending' : 'Descending -- click for ascending'}
          aria-label={sortPref.dir === 'asc' ? 'Sort ascending' : 'Sort descending'}
          className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {sortPref.dir === 'asc' ? '↑ Asc' : '↓ Desc'}
        </button>
        <span className="mx-1 h-5 w-px bg-zinc-200 dark:bg-zinc-800" aria-hidden="true" />
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
                setMissingOnly(false)
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
            disabled={bulkBusy || deletableSelected.length === 0}
            onClick={requestDeleteSelected}
            title={
              deletableSelected.length === 0
                ? 'None of the selected rows have a local copy to delete'
                : undefined
            }
            className="rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950"
          >
            Delete selected{deletableSelected.length > 0 && ` (${deletableSelected.length})`}
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

      {/* Delete is irreversible (Queue/Stop are not) -- a confirmation dialog with the count
          and total bytes, per the task's own bar ("meet `move` mode": two-layer opt-in, a UI
          confirmation), sits between the request above and `runAction('delete', ...)`. */}
      {pendingDelete && (
        <div className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="text-red-900 dark:text-red-200">
            Delete the local copy of <strong>{pendingDelete.length}</strong>{' '}
            {pendingDelete.length === 1 ? 'item' : 'items'} ({formatBytes(pendingDeleteBytes)})?
            This only removes the local copy -- nothing remote is touched -- and cannot be
            undone.
          </p>
          <p className="text-red-900 dark:text-red-200">
            {remoteCopyNote(pendingDelete.length, pendingDeleteRemoteCount)}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmDelete}
              className="rounded-md bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-800 dark:bg-red-800 dark:hover:bg-red-700"
            >
              Delete
            </button>
            <button
              type="button"
              onClick={() => setPendingDelete(null)}
              className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
            >
              Cancel
            </button>
          </div>
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
              {BULK_OUTCOME_LABEL[bulkOutcome.action]}: {bulkOutcome.succeeded} of {bulkOutcome.total}{' '}
              succeeded
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
          {/* Column labels -- mirrors `Row`'s own widths so the header stays aligned with the
              virtualized rows beneath it. Static, not itself sortable -- the sort control
              lives in the toolbar above; this just names what the new lifecycle-icon column
              (R/L/V/E) is. */}
          <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-zinc-200 bg-zinc-50 px-2 py-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            <span className="w-4 shrink-0" />
            <span className="w-4 shrink-0" />
            <span className="min-w-0 flex-1">Name</span>
            <span className="w-24 shrink-0 text-right">Size</span>
            <span className="w-28 shrink-0 text-right">Status</span>
            <span className="w-20 shrink-0 text-right">R L V E</span>
            <span className="w-32 shrink-0 text-right">Changed</span>
            <span className="w-32 shrink-0 text-right">Actions</span>
          </div>
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
                    isCollapsed={isPathCollapsed(entry.rel_path)}
                    isSelected={selected.has(entry.rel_path)}
                    onToggleCollapse={toggleCollapse}
                    onToggleSelect={toggleSelect}
                    onAction={runRowAction}
                    onDeleteRequest={requestDeleteRow}
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
