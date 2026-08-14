import { useVirtualizer } from '@tanstack/react-virtual'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react'
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { deleteItem, getSettleSettings, queueItem, stopItem } from '../api/client'
import type { FileNode, SettleSettingsOut } from '../api/types'
import {
  bothSidesRows,
  formatBytes,
  formatPercent,
  hasBothSides,
  isStillArriving,
  percentValue,
  settleArrivingLabel,
  settleArrivingShortLabel,
  settleWaitLabel,
  settleWaitShortLabel,
  stateAgeLabel,
  transferSpeedLabel,
  transferSpeedSortValue,
} from '../lib/format'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import { DetailButton, LifecycleIcons } from './LifecycleIcons'
import { ItemDrawer } from './ItemDrawer'
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

// Exported (trivial, non-behavioral) so FileTree.test.ts can build fixtures and call the pure
// tree/collapse/filter/column-width helpers below directly, without rendering the component --
// prompts/2026-08-13-frontend-test-runner.md's own instruction was to add exports, not to
// restructure working code, to make this logic reachable from a test.
export interface TreeEntry extends FileNode {
  name: string
  depth: number
  children: TreeEntry[]
  /** The live, EMA-smoothed instantaneous rate from the `progress` WS message
   * (2026-08-14, prompts/2026-08-14-files-page-speed-column.md), looked up by this row's own
   * `id` from the `speedByItemId` map `FileTree` threads in from `useLiveModel`. Not part of
   * `FileNode`'s wire shape -- computed here by `buildTree`, the same way `name`/`depth` are,
   * so it's available wherever a `TreeEntry` is (sorting, the Row cell) without a second prop
   * drilled down to `Row`. `null` for any row that isn't the parent item of a currently-running
   * job -- including every child of a mirroring directory, which never gets its own speed on
   * the wire at all (`core/queue.py._publish_child_progress`'s own docstring). See
   * `lib/format.ts`'s module comment above `transferSpeedLabel` for why this is never a derived
   * average.
   */
  speed_bps: number | null
}

/** What a row's own size column shows. A **file** prefers `local_size` (falling back to
 * `remote_size` only when local is unknown) so an in-progress download's cell reads as live
 * progress, not the eventual total. A **directory** is the opposite: `remote_size` first --
 * it's the rollup total for the whole subtree while anything is still incomplete, which is the
 * more useful "how big is this" reading -- falling back to `local_size` only when
 * `remote_size` is unknown. That fallback used to not exist at all (2026-08-13,
 * `prompts/2026-08-13-delete-state-truthfulness.md` defect 4): a completed directory on a
 * `move` queue has `remote_size` go `NULL` the moment its verified remote copy is deleted
 * (DESIGN.md §6/§7.4), so every file inside it kept showing a size (their own fallback already
 * covered that) while the directory row wrapping them went blank. Both sizes are already
 * rollups from `core/reconcile.py`, so there's nothing new to compute, just the same shape of
 * fallback files already had. Named and shared so `Row`, the hover tooltip, and the `size`
 * sort key (`sortValue` below) all read this one function and can never quietly disagree about
 * what "size" means for a node.
 */
function nodeDisplaySize(entry: TreeEntry): number | null {
  return entry.is_dir
    ? (entry.remote_size ?? entry.local_size)
    : (entry.local_size ?? entry.remote_size)
}

export function buildTree(nodes: FileNode[], speedByItemId: Record<number, number> = {}): TreeEntry[] {
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
    const entry: TreeEntry = {
      ...node,
      name,
      depth: parent ? parent.depth + 1 : 0,
      children: [],
      speed_bps: node.id != null ? (speedByItemId[node.id] ?? null) : null,
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
 * directory's children simply never enter the flat list, so scroll math stays correct
 * without the virtualizer needing to know anything about tree structure. A predicate rather
 * than a `Set` of collapsed paths (2026-08-13) -- the persisted collapse preference below is
 * "default plus exceptions," not an enumerable set of collapsed paths, and a predicate is the
 * one shape that works for both that and the old plain-`Set` caller.
 */
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

// --- Sorting (2026-08-13): reorders siblings within each parent, never the flattened array --
// flattening is what the virtualizer walks, and sorting it directly would tear children away
// from their parents. `sortTree` below runs on the built tree, before `flatten`.

export type SortKey = 'name' | 'size' | 'speed' | 'state_changed_at' | 'percent'
export type SortDir = 'asc' | 'desc'

const SORT_KEYS: SortKey[] = ['name', 'size', 'speed', 'state_changed_at', 'percent']
const SORT_LABELS: Record<SortKey, string> = {
  name: 'Name',
  size: 'Size',
  speed: 'Speed',
  state_changed_at: 'Last change',
  percent: '% complete',
}

/** Column header, itself the sort control (2026-08-13, prompts/2026-08-13-files-ux-pass.md
 * item 1) -- replaces the previous separate "Sort by" dropdown plus asc/desc toggle button.
 * Click sorts by this column; click again reverses direction, the conventional shape the task's
 * own prompt asked for rather than a new affordance. A caret next to the label marks the active
 * column and its direction -- `▲`/`▼`, the same style of glyph this file already uses for the
 * collapse arrows (`▸`/`▾`) rather than reaching for an icon. `title` carries the full "Last
 * change"/"% complete" wording (`SORT_LABELS`) even where the header's own visible text is
 * shorter ("Changed"/"Status") so the column's real sort key is still discoverable on hover.
 * A header that isn't sortable is a plain `<span>` (unchanged, below), not this component --
 * "must not look clickable" from the same prompt.
 */
function SortHeaderButton({
  sortKey,
  label,
  title,
  sortPref,
  onSort,
  className,
}: {
  sortKey: SortKey
  label: string
  title?: string
  sortPref: SortPreference
  onSort: (key: SortKey) => void
  className: string
}) {
  const active = sortPref.key === sortKey
  return (
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      title={title}
      aria-label={
        active
          ? `Sort by ${SORT_LABELS[sortKey]}, ${sortPref.dir === 'asc' ? 'ascending' : 'descending'} -- click to reverse`
          : `Sort by ${SORT_LABELS[sortKey]}`
      }
      className={`flex items-center gap-0.5 truncate hover:text-zinc-900 dark:hover:text-zinc-100 ${
        active ? 'text-zinc-900 dark:text-zinc-100' : ''
      } ${className}`}
    >
      {label}
      {active && <span aria-hidden="true">{sortPref.dir === 'asc' ? '▲' : '▼'}</span>}
    </button>
  )
}

function sortValue(entry: TreeEntry, key: SortKey): string | number | null {
  switch (key) {
    case 'name':
      return entry.name.toLowerCase()
    case 'size':
      return nodeDisplaySize(entry)
    case 'speed':
      // Non-transferring rows sort to one end regardless of direction (`compareValues`'s
      // existing null-last rule) rather than interleaving by a coincidental zero -- see
      // `transferSpeedSortValue`'s own docstring in `lib/format.ts`.
      return transferSpeedSortValue(entry.state, entry.speed_bps)
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
export function sortTree(roots: TreeEntry[], key: SortKey, dir: SortDir): TreeEntry[] {
  const clone = (entries: TreeEntry[]): TreeEntry[] =>
    entries.map((entry) => ({ ...entry, children: clone(entry.children) }))
  const cloned = clone(roots)
  sortSiblingsRecursive(cloned, key, dir)
  return cloned
}

// --- Collapse preference (2026-08-13): "default plus exceptions," not a saved set of ---------
// collapsed paths -- a directory that appears later (over the WebSocket) inherits the current
// default automatically; only per-row overrides are tracked explicitly.

export interface CollapsePreference {
  defaultCollapsed: boolean
  exceptions: string[]
}

const DEFAULT_COLLAPSE_PREFERENCE: CollapsePreference = { defaultCollapsed: false, exceptions: [] }

export function isCollapsePreference(value: unknown): value is CollapsePreference {
  if (typeof value !== 'object' || value == null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.defaultCollapsed === 'boolean' &&
    Array.isArray(v.exceptions) &&
    v.exceptions.every((p) => typeof p === 'string')
  )
}

/** The default-plus-exceptions model itself, in one place (2026-08-13,
 * prompts/2026-08-13-frontend-test-runner.md) -- pulled out of the `isPathCollapsed` closure
 * below purely so it's callable from a test without rendering the component; behavior is
 * unchanged. A path in `exceptions` reads as the *opposite* of the default; every other path --
 * including one that has never been seen before, e.g. a directory that just arrived over the
 * WebSocket -- reads as the default itself. That "unknown path falls through to the default"
 * behavior, not any explicit per-path bookkeeping, is what makes a newly-arrived directory
 * inherit the current default automatically.
 */
export function resolveCollapsed(defaultCollapsed: boolean, exceptions: Set<string>, path: string): boolean {
  return exceptions.has(path) ? !defaultCollapsed : defaultCollapsed
}

export interface SortPreference {
  key: SortKey
  dir: SortDir
}

const DEFAULT_SORT_PREFERENCE: SortPreference = { key: 'name', dir: 'asc' }

export function isSortPreference(value: unknown): value is SortPreference {
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
 *
 * `'redownload'` (2026-08-13, `prompts/2026-08-13-delete-state-truthfulness.md` defect 2) is
 * the same click as `'queue'` -- `Row` below dispatches both through the identical `onAction`
 * handler -- just a different label for one specific case: a row *we* deleted
 * (`suppressed_reason === 'deleted_local'`) whose remote copy has since come back
 * (`hasRemoteCopy`, defined below). Derived from the suppression reason plus remote presence,
 * never from the state string alone, because `REMOVED_LOCAL`/`REMOVED_BOTH` can also be
 * produced with no suppression at all (`core/mount_sentinel.py.resolve_vanished`, defect 3) --
 * that row is a plain "Queue", not a "Re-Download". "Queue" reads like a brand-new item;
 * "Re-Download" tells the user this is the release they already had, back again, and nothing
 * fetches it automatically.
 */
function rowAction(node: FileNode): 'queue' | 'stop' | 'redownload' | null {
  if (node.id == null) return null
  if (node.state === 'QUEUED' || node.state === 'DOWNLOADING') return 'stop'
  if (node.state === 'LOCAL_ONLY') return null
  if (node.suppressed_reason === 'deleted_local' && hasRemoteCopy(node)) return 'redownload'
  return 'queue'
}

/** Whether "Delete local" (DESIGN.md §9.2; prompts/open-issues.md "7 + 8") makes sense to
 * offer at all -- a node with no local content has nothing this action could do. `DOWNLOADING`/
 * `QUEUED` are deliberately *not* excluded: a node mid-transfer has real partial bytes on disk
 * (or is about to), and (2026-08-13, `prompts/2026-08-13-delete-during-transfer.md`)
 * `api/jobs.py.delete_item` now stops that transfer first rather than refusing outright, so
 * offering the button here is no longer a dead end -- `hasActiveJob` below is what the
 * confirmation panel uses to say so. The backend (`core/local_delete.py.delete_local`) still
 * runs its own guards regardless (mount sentinel, path containment, a delete already in
 * flight) and can withhold even when this returns true; this is only about not showing a
 * button that could never do anything, not a prediction of the guard outcome.
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
 * confirmation's "what happens to this after I delete it" wording (a remote copy surviving
 * means `delete_local` writes `REMOVED_LOCAL`, not `REMOVED_BOTH` -- `core/local_delete.py.
 * _removed_state_for`) and, since 2026-08-13, `rowAction`'s "Re-Download" label above. Either
 * way lftpweb will never re-fetch what it deleted itself on its own: `delete_local` always sets
 * `auto_queue_suppressed`, which auto-queue excludes unconditionally regardless of the
 * `re_download_externally_removed` setting -- that setting only ever governs an item
 * *something else* removed, never one this app just deleted.
 */
function hasRemoteCopy(node: FileNode): boolean {
  return node.remote_size != null
}

/** Whether this node currently has an active job -- the identical presence test `rowAction`
 * above already uses to decide whether to show "Stop" instead of "Queue". Drives the delete
 * confirmation's "this will cancel a transfer" sentence (2026-08-13,
 * `prompts/2026-08-13-delete-during-transfer.md`): `api/jobs.py.delete_item` now stops an
 * active job before deleting rather than refusing outright (backend: `core/local_delete.py`'s
 * own "no active job" guard is unchanged -- the endpoint satisfies it itself, see that
 * module's docstring), so the confirmation has to say that plainly rather than leaving the
 * user to find out after clicking Delete.
 */
function hasActiveJob(node: FileNode): boolean {
  return node.state === 'QUEUED' || node.state === 'DOWNLOADING'
}

/** The delete confirmation's active-transfer sentence -- deliberately its own line, not folded
 * into `remoteCopyNote` below: a selection can be transferring *and* have a surviving remote
 * copy at once (the common case, since a `copy`-mode item's remote copy is untouched either
 * way), and both facts change what the user should expect, so neither should crowd the other
 * out. `null` when nothing selected is active, so the caller can skip rendering the line
 * entirely rather than showing an empty one.
 */
function activeTransferNote(total: number, activeCount: number): string | null {
  if (activeCount === 0) return null
  if (activeCount === total) {
    return total === 1
      ? "It's transferring right now -- deleting will cancel that transfer first, then remove what has downloaded so far."
      : "All of them are transferring right now -- deleting will cancel each transfer first, then remove what has downloaded so far."
  }
  return (
    `${activeCount} of ${total} ${activeCount === 1 ? 'is' : 'are'} transferring right now -- ` +
    `deleting will cancel ${activeCount === 1 ? 'it' : 'them'} first, then remove what has ` +
    'downloaded so far.'
  )
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

// --- Row hover card (2026-08-13, prompts/2026-08-13-both-sides-hover-card.md) -----------------
// Replaces the previous native `title` tooltip (a plain `\n`-joined string -- no columns, no
// styling, no control over timing or position) with a real, portal-rendered card showing size
// and modified date **remote and local side by side**, per the user's own request. The native
// `title` on the name span is removed outright rather than kept alongside this: both are
// defensible in isolation, but a `title` hovering the same element would start its own ~1s
// browser-native timer independent of this card's, so a long-enough hover would show *both* at
// once -- not defensible. `ItemDrawer.tsx`'s info-icon route remains the pre-hydration/no-JS/
// touch fallback (the drawer's own click target, `DetailButton`, is a real button, not a hover
// affordance) -- see that component's docstring.

/** The hover card's imperative controller. `Row` -- a virtualized, frequently mounting and
 * unmounting child -- drives this through a stable ref rather than through lifted React state,
 * so a show/hide only ever re-renders `HoverCardHost` (and the portal it draws into), never
 * `FileTree` or any other row. "Do not re-render the tree to show it; the card is one element,
 * not per-row state" is the task's own bar.
 */
interface HoverCardHandle {
  /** Schedules the card open over `anchorEl` for `entry`, after a delay. `immediate` skips the
   * delay -- used for keyboard focus, where the user already made an explicit request and a
   * 400ms pause would just read as lag.
   */
  requestShow: (entry: TreeEntry, anchorEl: HTMLElement, immediate?: boolean) => void
  /** Schedules the card closed, after a short delay so a pointer passing briefly off the row
   * doesn't flicker it shut. `immediate` skips the delay -- keyboard blur, a list scroll, and
   * the anchor row's own unmount cleanup all need it closed *now*, not 150ms from now.
   */
  requestHide: (immediate?: boolean) => void
  /** Closes the card immediately, but only if it is currently anchored to `path` -- called from
   * every row's own unmount cleanup. The virtualizer unmounts rows constantly as they scroll out
   * of view (only ~16 rows of overscan); a card left floating over whatever row now occupies
   * that slot in the DOM would be worse than no card at all, so every row guarantees this on its
   * way out regardless of whether it was ever actually the anchor.
   */
  cancelIfAnchor: (path: string) => void
}

const HOVER_SHOW_DELAY_MS = 400
const HOVER_HIDE_DELAY_MS = 150

/** The card's contents -- built entirely from `lib/format.ts.bothSidesRows`/`hasBothSides`, the
 * same functions `ItemDrawer.tsx`'s `SideBySideDetails` reads, so the two surfaces can never
 * quietly disagree about what these numbers are. Two columns only when both sides actually have
 * something to show (`hasBothSides`); otherwise one labelled column -- a two-column layout with
 * a permanently empty half (`LOCAL_ONLY`, `REMOTE_ONLY`, a deleted item) reads worse than a
 * single column, per the task's own bar.
 */
function HoverCardBody({ entry }: { entry: TreeEntry }) {
  const rows = bothSidesRows(entry)
  const both = hasBothSides(entry)
  const remotePresent = entry.remote_size != null
  const singleSideLabel = remotePresent
    ? 'Remote only'
    : entry.local_size != null
      ? 'Local only'
      : 'No copy on either side'

  return (
    <div className="flex flex-col gap-1.5">
      <p className="max-w-[22rem] font-medium break-words text-zinc-900 dark:text-zinc-100">{entry.rel_path}</p>
      {both ? (
        <div className="grid grid-cols-[auto_1fr_1fr] items-baseline gap-x-3 gap-y-0.5">
          <span />
          <span className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Remote
          </span>
          <span className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Local
          </span>
          {rows.map((row) => (
            <Fragment key={row.label}>
              <span className="text-zinc-500 dark:text-zinc-400">{row.label}</span>
              <span>{row.remote}</span>
              <span>{row.local}</span>
            </Fragment>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            {singleSideLabel}
          </span>
          {rows.map((row) => (
            <div key={row.label} className="flex items-baseline justify-between gap-3">
              <span className="text-zinc-500 dark:text-zinc-400">{row.label}</span>
              <span>{remotePresent ? row.remote : row.local}</span>
            </div>
          ))}
        </div>
      )}
      {/* Percent is only meaningful once both sides exist -- a lone REMOTE_ONLY/LOCAL_ONLY
          reading has no "of what" to be a percentage of. `percentValue`/`formatPercent` already
          guard the divide-by-zero/NaN cases, but this is about not cluttering a one-sided card
          with a line that could only ever read as absent. */}
      {both && (
        <p className="text-zinc-500 dark:text-zinc-400">Complete: {formatPercent(entry.local_size, entry.remote_size)}</p>
      )}
    </div>
  )
}

/** The portal-rendered card -- positioned in the viewport from `anchorEl`'s own
 * `getBoundingClientRect()`, never from anything in the virtualized list's own layout, because
 * the anchor row can scroll (or unmount) out from under it at any moment. Painted first at
 * `opacity: 0` so its real rendered size can be measured (`cardRef`), then placed and revealed
 * in one `useLayoutEffect` -- avoids a visible jump from a guessed position to the real one.
 * Flips above the anchor when there isn't room below, and clamps both axes into the viewport
 * with `POPOVER_EDGE_MARGIN_PX` of breathing room rather than ever overflowing off-screen --
 * that arithmetic now lives in `lib/popoverPosition.ts.placePopover`, shared verbatim with
 * `FieldHelp.tsx`'s settings-field popover so the two can never drift apart on edge behaviour.
 */
function HoverCardContent({ entry, anchorEl }: { entry: TreeEntry; anchorEl: HTMLElement }) {
  const cardRef = useRef<HTMLDivElement>(null)
  const [style, setStyle] = useState<CSSProperties>({ position: 'fixed', top: 0, left: 0, opacity: 0 })

  useLayoutEffect(() => {
    const card = cardRef.current
    if (card == null) return
    const { top, left } = placePopover(
      anchorEl.getBoundingClientRect(),
      card.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight },
      POPOVER_EDGE_MARGIN_PX,
    )
    setStyle({ position: 'fixed', top, left, opacity: 1 })
  }, [entry, anchorEl])

  return (
    // `pointer-events-none` (task's own bar): this card must never swallow a click meant for
    // the row underneath it, a sort header, or a column resize handle (`a4a626d`) -- it shows
    // read-only text, so nothing inside it ever needed to be clickable.
    <div
      ref={cardRef}
      role="tooltip"
      className="pointer-events-none fixed z-50 max-w-[min(24rem,calc(100vw-16px))] rounded-md border border-zinc-200 bg-white px-3 py-2 text-xs text-zinc-700 shadow-lg dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200"
      style={style}
    >
      <HoverCardBody entry={entry} />
    </div>
  )
}

/** Owns the hover card's only piece of state (`open`) and renders it into `document.body` via a
 * portal -- mounted once by `FileTree`, outside the virtualized row list, so a show/hide only
 * ever re-renders this one component, never the rows. `Row` drives it entirely through the
 * imperative `controllerRef` handle (assigned in the effect below), never through props or
 * context that would force `FileTree` itself to re-render on every hover.
 */
function HoverCardHost({ controllerRef }: { controllerRef: RefObject<HoverCardHandle | null> }) {
  const [open, setOpen] = useState<{ entry: TreeEntry; anchorEl: HTMLElement } | null>(null)
  const showTimer = useRef<number | null>(null)
  const hideTimer = useRef<number | null>(null)

  useEffect(() => {
    const clearTimers = () => {
      if (showTimer.current != null) window.clearTimeout(showTimer.current)
      if (hideTimer.current != null) window.clearTimeout(hideTimer.current)
      showTimer.current = null
      hideTimer.current = null
    }

    controllerRef.current = {
      requestShow(entry, anchorEl, immediate = false) {
        clearTimers()
        if (immediate) {
          setOpen({ entry, anchorEl })
          return
        }
        showTimer.current = window.setTimeout(() => {
          showTimer.current = null
          setOpen({ entry, anchorEl })
        }, HOVER_SHOW_DELAY_MS)
      },
      requestHide(immediate = false) {
        clearTimers()
        if (immediate) {
          setOpen(null)
          return
        }
        hideTimer.current = window.setTimeout(() => {
          hideTimer.current = null
          setOpen(null)
        }, HOVER_HIDE_DELAY_MS)
      },
      cancelIfAnchor(path) {
        clearTimers()
        setOpen((prev) => (prev != null && prev.entry.rel_path === path ? null : prev))
      },
    }
    return () => {
      clearTimers()
      controllerRef.current = null
    }
    // `controllerRef` is a plain ref object, stable across renders -- included for
    // exhaustive-deps correctness, not because it ever actually changes.
  }, [controllerRef])

  if (open == null) return null
  return createPortal(<HoverCardContent entry={open.entry} anchorEl={open.anchorEl} />, document.body)
}

// --- Facet filter (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 2): replaces the
// previous "Missing only" checkbox -- a real diagnostic (`downloaded_at` set but no local
// presence, the *arr-import case) that the user could not tell the meaning of from its name
// alone, which is the whole verdict on it. A dropdown over the lifecycle facets that already
// exist (`core/itemview.py`, `LifecycleIcons.tsx`) instead of a second, parallel filtering
// mechanism -- composes with the text/state filters through the same `visiblePaths` set below.

export type FacetFilter = '' | 'has_remote' | 'has_local' | 'extracted' | 'not_extracted' | 'missing_locally'

const FACET_FILTER_LABELS: Record<FacetFilter, string> = {
  '': 'All items',
  has_remote: 'Has remote copy',
  has_local: 'Has local copy',
  extracted: 'Extracted',
  not_extracted: 'Not extracted',
  // Names itself, unlike the checkbox it replaces -- the exact behavior the old "Missing only"
  // checkbox had (`downloaded_at` set, `facets.local.reason === 'missing'`), just findable now.
  missing_locally: 'Downloaded but missing locally',
}

const FACET_FILTER_VALUES: FacetFilter[] = [
  '',
  'has_remote',
  'has_local',
  'extracted',
  'not_extracted',
  'missing_locally',
]

/** One predicate per facet-filter option, each keyed off `core/itemview.py`'s own
 * `level`/`reason` codes rather than re-deriving presence from raw bytes here -- the frontend
 * composes what the backend already classified, it doesn't reclassify.
 */
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

// --- Column widths (2026-08-13, prompts/2026-08-13-resizable-file-columns.md): one shared -----
// definition drives both the header row and `Row` -- previously the header hardcoded Tailwind
// widths (`w-24`/`w-28`/`w-20`/`w-32`) and `Row` hardcoded a second, matching set, kept in sync
// by hand with nothing stopping them drifting apart. `ColumnDef` below is that one definition;
// both callers read it (`RESIZABLE_COLUMNS`, `fixedColumnStyle`), so a header and its column
// can never disagree on width again, and it's also what makes resizing possible at all.

interface ColumnDef {
  id: string
  label: string
  defaultWidth: number
  minWidth: number
  align: 'right' | 'left'
  sortKey?: SortKey
  /** Overrides the header's own hover text -- only "Status" needs one today, to spell out that
   * it sorts by % complete despite its shorter visible label (unchanged from before this task).
   */
  title?: string
}

/** The five fixed-width columns, in render order. **Name is not here** -- it keeps flexing
 * (`flex-1`, a floor of `NAME_MIN_WIDTH_PX`) and absorbs whatever space these five don't claim,
 * which is also why only these five get a drag handle: Name's width is derived from the
 * container and the other five, never set directly. Resizing one of these five just changes how
 * much of the remaining space Name gets -- the model already implied by "Name flexes, the rest
 * are fixed" before this task, kept rather than switched to a two-column paired resize, per the
 * task's own instruction to keep it unless there's a reason not to (there wasn't one).
 */
export const RESIZABLE_COLUMNS: ColumnDef[] = [
  { id: 'size', label: 'Size', defaultWidth: 96, minWidth: 56, align: 'right', sortKey: 'size' },
  // Speed (2026-08-14, prompts/2026-08-14-files-page-speed-column.md), between Size and Status
  // per that task's own placement. Blank/dash for anything not actively downloading -- never
  // `0 B/s` for a row that simply isn't transferring (`lib/format.ts.transferSpeedLabel`) -- so
  // this column reads mostly empty at a glance, which is correct: most rows aren't transferring
  // at any given moment. `minWidth` matches `size`'s own floor; `defaultWidth` a touch wider to
  // fit "12.3 MB/s" without truncating, unverified against a real browser (no UI access in this
  // environment) but reasoned from the longest realistic rate string this format produces.
  { id: 'speed', label: 'Speed', defaultWidth: 88, minWidth: 56, align: 'right', sortKey: 'speed' },
  {
    id: 'status',
    label: 'Status',
    // Was 112px (`w-28`), widened by 16px in the same audit that shortened the settle-wait
    // text below -- a comfortable margin for the longest bare state names this chip ever
    // shows verbatim ("EXTRACT_FAILED", "DOWNLOADING 100%"), unverified against a real
    // browser (no UI access in this environment) but a reasoned default, not a guess.
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

/** Name's own floor -- not a `ColumnDef` entry since nothing ever sets Name's width directly
 * (see the comment above), but it still needs a minimum so an aggressively widened neighbor
 * can't squeeze it to nothing. Below this, the row's total width exceeds the scroll
 * container's, which is what makes the container's own horizontal scrollbar (unchanged,
 * `overflow-auto` on `scrollRef`'s div) appear -- a deliberate choice over clamping total row
 * width to the container and letting every column get proportionally thinner instead: a
 * user who drags a column wide presumably wants to see it wide, not have that undone by
 * squeezing everything else.
 */
const NAME_MIN_WIDTH_PX = 160

const RESIZE_STEP_PX = 8
const RESIZE_STEP_LARGE_PX = 32

export type ColumnWidths = Record<string, number>

export function defaultColumnWidths(): ColumnWidths {
  return Object.fromEntries(RESIZABLE_COLUMNS.map((c) => [c.id, c.defaultWidth]))
}

export function columnMinWidth(id: string): number {
  return RESIZABLE_COLUMNS.find((c) => c.id === id)?.minWidth ?? 40
}

/** No maximum -- growing a column just eats into Name's share (down to `NAME_MIN_WIDTH_PX`) or,
 * past that, widens the row past the scroll container, which picks up its own horizontal
 * scrollbar (see `NAME_MIN_WIDTH_PX`'s own comment). A ceiling would just be one more number to
 * justify with no real failure mode it prevents -- a column dragged absurdly wide is recoverable
 * with one double-click, unlike one dragged to zero.
 */
export function clampColumnWidth(id: string, width: number): number {
  return Math.max(columnMinWidth(id), Math.round(width))
}

export function isColumnWidths(value: unknown): value is ColumnWidths {
  if (typeof value !== 'object' || value == null) return false
  return Object.entries(value as Record<string, unknown>).every(
    ([, v]) => typeof v === 'number' && Number.isFinite(v),
  )
}

/** Saved widths merged onto the defaults -- an id no longer in `RESIZABLE_COLUMNS` (a column
 * removed or renamed later) is silently dropped rather than applied to whatever now occupies
 * that slot, per the task's own instruction to store and read by id, never by position, and to
 * ignore unknown ids on read. Re-clamps every surviving value in case its `minWidth` changed
 * since it was saved.
 */
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
 * property, `--col-size-<id>`, rather than off `widths` state directly -- that indirection is
 * the whole mechanism that keeps a drag off the React render path (see `ColumnResizeHandle`
 * below): the property is written straight to the DOM on every `pointermove` frame via a ref,
 * with no `setState` and so no re-render of the virtualized list, which can hold thousands of
 * rows. `width`/`minWidth`/`maxWidth` are all pinned to the same value so a flex child can never
 * grow past it to fit its own content (e.g. a long state name) -- that used to be how a cell's
 * content could bleed into its neighbor instead of clipping to its own column.
 */
function fixedColumnStyle(id: string): CSSProperties {
  const v = `var(--col-size-${id})`
  return { width: v, minWidth: v, maxWidth: v }
}

/** The drag handle living at the **left** edge of one resizable header cell (a sibling of the
 * header's label/`SortHeaderButton`, never nested inside it -- clicks on the handle can
 * therefore never bubble into the sort button's own `onClick`, so a drag can't accidentally
 * fire a sort; `stopPropagation` below is belt-and-suspenders, not the only thing preventing
 * it). Pointer events, not mouse events (`setPointerCapture` on `pointerdown`), so a drag keeps
 * tracking the pointer even once it leaves the 8px-wide handle, and so this also works on
 * touch. Drag state lives in a plain ref, not React state -- writing the live width to state on
 * every `pointermove` would re-render the whole visible window on every frame of the drag; the
 * only `setState` this ever causes is the one `onCommit` call on `pointerup` (or the equivalent
 * single call from a keyboard step or a double-click reset).
 *
 * **Left edge, not right -- fixed 2026-08-14
 * (`prompts/2026-08-14-files-page-speed-column.md`, step 1b), reported live as "the line to
 * drag is on the right [edge of a column] but [that column] moves its left side."** This was
 * never an off-by-one in which column a handle resizes -- `RESIZABLE_COLUMNS` (above) are five
 * (now six) fixed-width columns and **Name is the only flex item**, absorbing whatever width a
 * resize adds or removes. Work out what that means geometrically: widening column K by `delta`
 * shrinks Name by the same `delta` (it's the only thing that can give); Name sits to the left of
 * every fixed column, so that shrink is a leftward shift applied uniformly to every fixed
 * column's own left edge, K's included. K's own width grew by `delta` in the same step, and
 * growing a box whose left edge just moved left by `delta` while adding `delta` of width back
 * lands its **right edge exactly where it started** -- unchanged. Everything to the right of K
 * (unaffected by K's resize at all, by the same argument one level up) doesn't move either. So
 * for *any* of these columns, resizing it moves its **left edge** and leaves its right edge (and
 * everything after it) exactly where it was -- "the column grows leftward" (this file's own
 * comment above `RESIZABLE_COLUMNS` already said as much). A handle glued to K's *right* edge
 * (the old `-right-1` position) therefore never moves at all when K resizes; a handle at K's
 * *left* edge is the one pixel that actually tracks the drag.
 *
 * That also flips the arithmetic below: a box anchored on its right edge, resized from its left
 * edge, gets **wider** as the left edge moves **left of** the pointer's start position -- the
 * conventional feel of dragging a left-side resize handle (like a window's left edge: drag it
 * further left, the window gets wider). So the live width is `startWidth - (clientX - startX)`,
 * not `+`, the one deliberate sign flip in `handlePointerMove`/`finishDrag` below.
 * `handleKeyDown` needs no equivalent change -- it already reasons in the abstract ("bigger"/
 * "smaller" per arrow key), never in raw screen-pixel deltas, so it was never affected by which
 * edge the handle sits on.
 *
 * Two options existed for fixing this (that task's own brief): move the handle (done here,
 * minimal, and it happens to land the leftmost resizable column's handle exactly on the
 * Name|Size boundary -- the one boundary a user can actually see move) or resize the dragged
 * column and Name together so the grabbed edge stays under the cursor regardless of which side
 * it's on ("paired resize"). Paired resize was already considered and rejected during `a4a626d`
 * (see the comment above `RESIZABLE_COLUMNS`) as more code for no behavioural gain over "Name
 * flexes, the rest are fixed" -- nothing about this bug changes that trade-off, so the fix here
 * keeps that model rather than reversing it.
 */
function ColumnResizeHandle({
  column,
  currentWidth,
  containerRef,
  onCommit,
}: {
  column: ColumnDef
  currentWidth: number
  containerRef: RefObject<HTMLDivElement | null>
  onCommit: (id: string, width: number) => void
}) {
  const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null)

  const writeLiveWidth = (width: number) => {
    containerRef.current?.style.setProperty(`--col-size-${column.id}`, `${width}px`)
  }

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation()
    dragRef.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: currentWidth }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  const handlePointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || e.pointerId !== drag.pointerId) return
    // Direct DOM write via the ref -- the module comment above (and the task's own bar) is why
    // this is not `setState`. `startWidth - delta`, not `+` -- see this component's own
    // docstring ("Left edge, not right") for why a left-edge handle on a right-anchored column
    // must subtract the pointer's rightward movement to widen it, not add it.
    writeLiveWidth(clampColumnWidth(column.id, drag.startWidth - (e.clientX - drag.startX)))
  }

  const finishDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (!drag || e.pointerId !== drag.pointerId) return
    dragRef.current = null
    onCommit(column.id, clampColumnWidth(column.id, drag.startWidth - (e.clientX - drag.startX)))
  }

  const handleKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const step = e.shiftKey ? RESIZE_STEP_LARGE_PX : RESIZE_STEP_PX
    const delta = e.key === 'ArrowRight' ? step : -step
    onCommit(column.id, clampColumnWidth(column.id, currentWidth + delta))
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${column.label} column`}
      aria-valuenow={Math.round(currentWidth)}
      aria-valuemin={column.minWidth}
      tabIndex={0}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={finishDrag}
      onPointerCancel={finishDrag}
      onDoubleClick={(e) => {
        // Reset to default (2026-08-13): the escape hatch for a column dragged down toward its
        // minimum -- cheap and conventional rather than requiring a drag back to undo one.
        e.stopPropagation()
        onCommit(column.id, column.defaultWidth)
      }}
      onKeyDown={handleKeyDown}
      onClick={(e) => e.stopPropagation()}
      className="absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize touch-none rounded bg-zinc-300/50 hover:bg-zinc-400 focus-visible:bg-sky-500 focus-visible:outline-none dark:bg-zinc-700/50 dark:hover:bg-zinc-500 dark:focus-visible:bg-sky-400"
    />
  )
}

interface RowProps {
  entry: TreeEntry
  isCollapsed: boolean
  isSelected: boolean
  onToggleCollapse: (path: string) => void
  onToggleSelect: (entry: TreeEntry, shiftKey: boolean) => void
  onAction: (entry: TreeEntry) => void
  onDeleteRequest: (entry: TreeEntry) => void
  onOpenDrawer: (entry: TreeEntry) => void
  actionBusy: boolean
  // The settle gate's site-wide constants (2026-08-13, item 3) -- fetched once by `FileTree`,
  // threaded down rather than re-fetched per row. See that fetch's own comment for the `null`
  // cases.
  settleSettings: SettleSettingsOut | null
  // The hover card's imperative controller (2026-08-13) -- a stable ref, not state, so wiring it
  // to every row never itself causes a re-render. See `HoverCardHandle`'s own docstring.
  hoverCardRef: RefObject<HoverCardHandle | null>
}

function Row({
  entry,
  isCollapsed,
  isSelected,
  onToggleCollapse,
  onToggleSelect,
  onAction,
  onDeleteRequest,
  onOpenDrawer,
  actionBusy,
  settleSettings,
  hoverCardRef,
}: RowProps) {
  const size = nodeDisplaySize(entry)
  const action = rowAction(entry)
  const deletable = canDeleteLocal(entry)
  // 2026-08-13 (prompts/2026-08-13-delete-state-truthfulness.md defect 1): `substate ===
  // 'removing'` overlays the chip regardless of `state` -- the row's real `state` is left
  // untouched server-side for the duration (`core/local_delete.py.delete_local`'s own
  // docstring), so this is purely a display substitution, not a claim that `state` itself
  // changed. The delete button is also hidden while this is true: a second delete request
  // for the same item is already withheld server-side (`DeleteInFlight`), but there's no
  // reason to invite the click.
  const isRemoving = entry.substate === 'removing'
  // The settle gate's wait (2026-08-13, item 3): same shape as `isRemoving` above -- a display
  // substitution over the chip, not a change to `entry.state` itself, which stays REMOTE_ONLY
  // server-side for the duration (`core/settle.py`).
  const isSettling = entry.state === 'REMOTE_ONLY' && entry.substate === 'settling'
  // Two different sentences for the same `substate === 'settling'` row (2026-08-13,
  // prompts/2026-08-13-settle-progress-visibility.md): `isStillArriving` picks out the case
  // the countdown below has nothing useful to say about yet -- `settle_matched_scans === 1`,
  // i.e. nothing has been confirmed unchanged even once (either a first-ever sighting, or the
  // fingerprint changed on the most recent scan and reset the count -- `core/settle.py`'s
  // counter doesn't distinguish the two, deliberately; see `lib/format.ts.isStillArriving`'s
  // own comment). Only meaningful nested inside `isSettling`; `isStillArriving` itself doesn't
  // check `substate` (the field is already `null` off it whenever `substate !== 'settling'`,
  // per `core/itemview.py`'s gate).
  const isStillArrivingRow = isSettling && isStillArriving(entry)

  // The hover card's anchor element (2026-08-13) -- `nameRef` is what `HoverCardContent`
  // positions itself against, never anything about this row's own place in the virtualized
  // list. Cleanup runs on every unmount unconditionally, whether or not this row was ever
  // actually the open card's anchor -- `cancelIfAnchor` is a cheap no-op otherwise, and the
  // virtualizer unmounts rows constantly as they scroll out of view, so this is the one place
  // guaranteed to run before a recycled slot could show a stale card (see `HoverCardHandle`'s
  // own docstring).
  const nameRef = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    // Reading `hoverCardRef.current` inside the cleanup itself, not a variable captured at
    // effect-setup time, is deliberate here -- the lint rule's usual worry is a DOM ref React
    // has already nulled out by the time cleanup runs, which doesn't apply: `hoverCardRef` is
    // our own imperative controller, assigned once by `HoverCardHost` and stable for the whole
    // list's lifetime, so the freshest reading is the correct one to act on at unmount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => hoverCardRef.current?.cancelIfAnchor(entry.rel_path)
  }, [entry.rel_path, hoverCardRef])

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
      {/* The primary detail-drawer affordance (2026-08-13) -- a control, not a status, so it
          sits here rather than among the lifecycle icons and reads visibly quieter than them
          (`DetailButton`'s own styling). Disabled for a row with no `id` -- there is no item
          to fetch history for yet (see `ItemDrawer`'s own itemId handling). */}
      <DetailButton label={entry.name} onOpen={() => onOpenDrawer(entry)} />
      {/* The hover card's anchor (2026-08-13) -- no native `title` here (see the module comment
          above `HoverCardHandle`: showing both would double up on a long hover). `tabIndex={0}`
          makes the card reachable by keyboard, not just a mouse -- `onFocus` opens it
          immediately (no delay: the user already made an explicit request), `onBlur` closes it
          immediately. `onPointerEnter`/`onPointerLeave` ignore touch (`pointerType`) --
          `DetailButton`'s drawer is the touch route, not this card, per the task's own
          instruction not to make hover serve touch too. */}
      <span
        ref={nameRef}
        tabIndex={0}
        className="flex-1 truncate outline-none focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:ring-inset"
        style={{ minWidth: NAME_MIN_WIDTH_PX }}
        onPointerEnter={(e) => {
          if (e.pointerType === 'touch' || nameRef.current == null) return
          hoverCardRef.current?.requestShow(entry, nameRef.current)
        }}
        onPointerLeave={() => hoverCardRef.current?.requestHide()}
        onFocus={() => {
          if (nameRef.current == null) return
          hoverCardRef.current?.requestShow(entry, nameRef.current, true)
        }}
        onBlur={() => hoverCardRef.current?.requestHide(true)}
      >
        {entry.name}
        {entry.is_dir && '/'}
      </span>
      <span
        className="shrink-0 overflow-hidden text-right text-zinc-500 dark:text-zinc-400"
        style={fixedColumnStyle('size')}
      >
        {size != null ? formatBytes(size) : '—'}
      </span>
      {/* Speed (2026-08-14, prompts/2026-08-14-files-page-speed-column.md): the live rate from
          the `progress` WS message, already resolved onto `entry.speed_bps` by `buildTree`.
          `transferSpeedLabel` is the one place that decides blank-vs-shown -- gated on
          `entry.state === 'DOWNLOADING'`, never on the value itself, so a real `0 B/s` on a
          stalled-but-still-running transfer still shows as `0 B/s`, not blank (see that
          function's own docstring in `lib/format.ts`). */}
      <span
        className="shrink-0 overflow-hidden text-right text-zinc-500 dark:text-zinc-400"
        style={fixedColumnStyle('speed')}
      >
        {transferSpeedLabel(entry.state, entry.speed_bps)}
      </span>
      <span className="shrink-0 overflow-hidden text-right" style={fixedColumnStyle('status')}>
        {/* The settle gate's wait (2026-08-13, item 3): was a 6px dot next to the chip
            (effectively invisible) -- now a readable substitution over the chip itself, the
            same substitution shape `isRemoving`/REMOVING above already established, with a
            countdown ("1 of 2 scans, 35s of 60s"). Shortened to `settleWaitShortLabel` for the
            chip's own in-cell text (2026-08-13, prompts/2026-08-13-resizable-file-columns.md)
            -- the full sentence (`settleWaitLabel`) simply didn't fit this column and is no
            longer lost, just moved to the chip's own `title` (hover).

            **`isStillArrivingRow` picks a different sentence pair, same chip**
            (2026-08-13, prompts/2026-08-13-settle-progress-visibility.md): the countdown above
            reads as stuck at "1 of 2" for as long as an item's fingerprint keeps changing scan
            to scan (a large directory still being copied onto the seedbox) -- while that's
            true there is nothing confirmed yet for the countdown to count, so this shows the
            byte count climbing instead (`settleArrivingShortLabel`/`settleArrivingLabel`). Both
            pairs share `SETTLING`'s amber chip styling; only the words differ. */}
        <StateChip
          state={isRemoving ? 'REMOVING' : isSettling ? 'SETTLING' : entry.state}
          percent={isRemoving || isSettling ? null : stateProgressPercent(entry)}
          label={
            isStillArrivingRow
              ? settleArrivingShortLabel(entry)
              : isSettling
                ? settleWaitShortLabel(entry, settleSettings)
                : undefined
          }
          title={
            isStillArrivingRow
              ? settleArrivingLabel(entry)
              : isSettling
                ? settleWaitLabel(entry, settleSettings)
                : undefined
          }
        />
      </span>
      {/* Lifecycle icons (2026-08-13): R/L/V/E, one glyph per `entry.facets`
          (`core/itemview.py`) -- the accumulated lifecycle, alongside the state chip's current
          verb rather than folded into it. `settle` threads through to `LifecycleIcons`'s own
          settle-gate override on R (its own docstring). */}
      <span className="flex shrink-0 justify-end overflow-hidden" style={fixedColumnStyle('lifecycle')}>
        <LifecycleIcons node={entry} settle={settleSettings} />
      </span>
      {/* migration 006's `state_changed_at`: "when did this row last move," labeled by the
          state it's already showing rather than a second, redundant chip. Absolute time in
          local time on hover -- History's date filters are UTC-only (a documented phase 6
          limitation), and this sidesteps that question rather than inheriting it. */}
      <span
        className="shrink-0 overflow-hidden truncate text-right text-xs text-zinc-500 dark:text-zinc-400"
        style={fixedColumnStyle('changed')}
        title={entry.state_changed_at ? new Date(entry.state_changed_at).toLocaleString() : undefined}
      >
        {stateAgeLabel(entry.state, entry.state_changed_at)}
      </span>
      <span
        className="flex shrink-0 justify-end gap-1 overflow-hidden text-right"
        style={fixedColumnStyle('actions')}
      >
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
            {action === 'stop' ? 'Stop' : action === 'redownload' ? 'Re-Download' : 'Queue'}
          </button>
        )}
        {deletable && !isRemoving && (
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
export function FileTree({
  nodes,
  connected = true,
  speedByItemId,
  selected,
  onSelectionChange,
}: {
  nodes: FileNode[]
  /** Whether the WebSocket is open, i.e. whether the connect-time `snapshot` has arrived.
   * Only used to word the empty state: with the socket open the snapshot is authoritative, so
   * an empty tree really does mean "there is nothing on either side" -- not "we haven't looked
   * yet", which is what this used to claim regardless (found 2026-08-13). */
  connected?: boolean
  /** The `progress` WS message's `speed_bps`, keyed by `item_id` (2026-08-14,
   * `prompts/2026-08-14-files-page-speed-column.md`) -- `useLiveModel.ts`'s own map, threaded
   * in by `FilesPage.tsx` rather than a second subscription here. Fed straight into `buildTree`
   * below so every `TreeEntry` carries its own `speed_bps`, the one place both sorting and the
   * Row cell read it from.
   */
  speedByItemId: Record<number, number>
  /** The Files-page selection, lifted to `FilesPage.tsx` (2026-08-14,
   * `prompts/2026-08-14-reset-panel-counts-and-layout.md`) so `QueueResetControls.tsx`'s unified
   * Selected scope and this component's own multi-select can never disagree about what's
   * selected. This component still owns every *mechanic* of selecting (click, shift-range,
   * `lastClickedPath` below) -- only the `Set` itself moved up. Keyed by `rel_path`, unchanged
   * from when this was local state. `syncMode`/`autoQueueEnabled`/`scanIntervalS` used to be
   * accepted here too (2026-08-13, `prompts/2026-08-13-reset-item-tracking.md`), purely to feed
   * this component's own "Reset selected" panel's warning text -- that whole panel moved to
   * `QueueResetControls.tsx` (2026-08-14, `prompts/2026-08-14-reset-panel-counts-and-layout.md`),
   * so those three props are gone from here rather than kept unread.
   */
  selected: Set<string>
  onSelectionChange: (next: Set<string>) => void
}) {
  // The shared age ticker (module docstring above): bumping this forces a re-render of
  // whatever rows are currently mounted, which is all `stateAgeLabel` needs to catch up --
  // it's computed fresh from `Date.now()` on every render, not memoized against this value.
  const [, bumpAgeTick] = useReducer((c: number) => c + 1, 0)
  useEffect(() => {
    const id = setInterval(() => bumpAgeTick(), AGE_TICK_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  // The settle gate's own constants (2026-08-13, item 3) -- `REQUIRED_SETTLE_SCANS`/
  // `SETTLE_MIN_AGE_S`, fetched once for the whole tree via the existing `GET
  // /api/settings/settle` endpoint (already built for Settings -> Transfer's not-yet-existing
  // UI), not per row: they're site-wide constants, not something that varies row to row.
  // `null` until the fetch resolves, or forever on failure -- `settleWaitLabel`
  // (`LifecycleIcons.tsx`) degrades to a bare "Waiting for changes" either way, never blocking
  // the tree from rendering.
  const [settleSettings, setSettleSettings] = useState<SettleSettingsOut | null>(null)
  useEffect(() => {
    getSettleSettings()
      .then(setSettleSettings)
      .catch(() => {
        // Degrades gracefully -- see the comment above.
      })
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
  // The header buttons' own click handler (2026-08-13, item 1): clicking the active column's
  // header reverses direction; clicking a different column switches to it, ascending -- the
  // conventional shape the task's own prompt asked for ("not sure on where to put asc/desc...
  // do the conventional thing").
  const toggleSort = (key: SortKey) => {
    setSort(sortPref.key === key ? { key, dir: sortPref.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })
  }

  const tree = useMemo(
    () => sortTree(buildTree(nodes, speedByItemId), sortPref.key, sortPref.dir),
    [nodes, speedByItemId, sortPref],
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
    resolveCollapsed(collapsePref.defaultCollapsed, exceptionSet, path)

  const [lastClickedPath, setLastClickedPath] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<Set<string>>(new Set())
  const [bulkOutcome, setBulkOutcome] = useState<BulkOutcome | null>(null)
  const [searchText, setSearchText] = useState('')
  const [stateFilter, setStateFilter] = useState('')
  // The facet filter (2026-08-13): replaces "Missing only" -- see the module-level comment on
  // `FacetFilter` above for why. Composes with the text/state filters below via the same
  // `visiblePaths` set.
  const [facetFilter, setFacetFilter] = useState<FacetFilter>('')
  // Delete is irreversible (Queue/Stop are not) -- a confirmation step sits between "the user
  // asked to delete" and "anything actually runs," for both the per-row button and the bulk
  // action. `null` = no pending confirmation; otherwise the exact entries about to be deleted,
  // so the dialog's count/byte total is read from the same list the delete itself will use.
  const [pendingDelete, setPendingDelete] = useState<TreeEntry[] | null>(null)

  // "Reset item tracking" no longer has a bulk panel of its own here (2026-08-13,
  // prompts/2026-08-13-reset-item-tracking.md shipped it as a bulk action beside Delete;
  // 2026-08-14, prompts/2026-08-14-reset-panel-counts-and-layout.md folded it into
  // `QueueResetControls.tsx`'s unified Selected scope instead, which reads `selected` -- now a
  // lifted prop, not local state -- directly). See that component for the confirm flow this
  // replaced.

  // The item drawer (2026-08-13, prompts/2026-08-13-files-detail-inspector.md) -- opened by a
  // row's `DetailButton`, never by the row click itself (that drives selection, above). `null`
  // = closed. Holding the whole `TreeEntry` rather than just a path means the drawer's title
  // survives even if this exact row scrolls out of the virtualizer's mounted window while open.
  const [drawerEntry, setDrawerEntry] = useState<TreeEntry | null>(null)

  // The hover card's controller (2026-08-13, prompts/2026-08-13-both-sides-hover-card.md) -- a
  // plain ref, not state (see `HoverCardHandle`'s own docstring for why). Every row is handed
  // this same stable ref rather than getting its own piece of state.
  const hoverCardRef = useRef<HoverCardHandle | null>(null)
  // The card hides on any scroll, not just the row list's own -- a capturing listener on
  // `window` catches scroll events fired anywhere in the tree, including `scrollRef`'s own
  // container below: scroll events don't bubble, but a capture-phase listener still sees them on
  // the way down to their target regardless. `immediate`: a stale card hanging in place while
  // the list scrolls under it is wrong right away, not 150ms from now.
  useEffect(() => {
    const onScroll = () => hoverCardRef.current?.requestHide(true)
    window.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => window.removeEventListener('scroll', onScroll, true)
  }, [])

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

  const filtersActive = stateFilter !== '' || searchText.trim() !== '' || facetFilter !== ''

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
      if (!matchesFacetFilter(entry, facetFilter)) continue
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
  }, [filtersActive, fullFlat, facetFilter, searchText, stateFilter])

  const flat = useMemo(() => {
    if (!filtersActive || visiblePaths == null) return flatten(tree, isPathCollapsed)
    return fullFlat.filter((e) => visiblePaths.has(e.rel_path))
    // isPathCollapsed is a plain closure over collapsePref -- listed below instead of the
    // function identity, which is recreated every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tree, collapsePref, filtersActive, fullFlat, visiblePaths])

  const scrollRef = useRef<HTMLDivElement>(null)

  // Column widths (2026-08-13): read synchronously in the initial `useState`, same reasoning
  // and same mechanism as the sort/collapse preferences above -- a `useEffect` would paint at
  // the defaults and then jump once the saved widths loaded.
  const [columnWidths, setColumnWidths] = useState<ColumnWidths>(() =>
    mergeColumnWidths(readLocalStorage('files.columnWidths', isColumnWidths)),
  )
  // Kept in sync every render so the callback ref below (which only runs on mount/unmount, not
  // on every render) can still read the *current* widths the one time it needs them -- the
  // container's CSS variables for an already-mounted element are instead kept current by the
  // `useEffect` a few lines down, which does run on every `columnWidths` change.
  const columnWidthsRef = useRef(columnWidths)
  columnWidthsRef.current = columnWidths

  const commitColumnWidth = (id: string, width: number) => {
    setColumnWidths((prev) => {
      const next = { ...prev, [id]: clampColumnWidth(id, width) }
      writeLocalStorage('files.columnWidths', next)
      return next
    })
  }

  /** A callback ref, not a plain object one -- `scrollRef`'s div only exists while `flat.length
   * > 0` (below), so it mounts and unmounts as filters change, and a plain ref's value would sit
   * on default (unset) CSS variables until the next time `columnWidths` itself happened to
   * change. This runs exactly when the element (re)appears, seeding it from whatever's current
   * right then; the `useEffect` below covers every *later* change while it stays mounted.
   */
  const attachScrollRef = useCallback((el: HTMLDivElement | null) => {
    scrollRef.current = el
    if (el == null) return
    for (const col of RESIZABLE_COLUMNS) {
      el.style.setProperty(`--col-size-${col.id}`, `${columnWidthsRef.current[col.id] ?? col.defaultWidth}px`)
    }
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (el == null) return
    for (const col of RESIZABLE_COLUMNS) {
      el.style.setProperty(`--col-size-${col.id}`, `${columnWidths[col.id] ?? col.defaultWidth}px`)
    }
  }, [columnWidths])

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
    const next = new Set(selected)
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
    onSelectionChange(next)
    setLastClickedPath(entry.rel_path)
  }

  const clearSelection = () => onSelectionChange(new Set())

  const runRowAction = async (entry: TreeEntry) => {
    if (entry.id == null) return
    setRowBusy((prev) => new Set(prev).add(entry.rel_path))
    try {
      const action = rowAction(entry)
      if (action === 'queue' || action === 'redownload') await queueItem(entry.id)
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
      const nextSelection = new Set(selected)
      for (const path of succeededPaths) nextSelection.delete(path)
      onSelectionChange(nextSelection)
      setBulkOutcome({ action, total: targets.length, succeeded: succeededPaths.size, failures })
    } finally {
      setBulkBusy(false)
    }
  }

  /** 2026-08-13 (prompts/2026-08-13-lftp-timestamped-temp-files.md): "Queue selected" used to
   * call `queueItem` for every selected row regardless of state, including rows already
   * `QUEUED`/`DOWNLOADING` -- unlike the single-row action (`runRowAction`, which only ever
   * fires `queueItem` when `rowAction` says `'queue'`/`'redownload'`), a multi-select could
   * include an already-active row and this button would happily re-request it. The backend
   * (`core/queue.py.enqueue_item`) is now idempotent against that -- it returns the existing
   * job rather than spawning a second process -- so this was never a duplicate-process risk,
   * but it's still a pointless request and a confusing "succeeded" outcome for a row that was
   * never going to do anything. Filtered here to match `rowAction`'s own rule, the same way
   * `deletableSelected` below already filters for Delete.
   */
  const queueableSelected = useMemo(
    () => selectedEntries.filter((e) => rowAction(e) === 'queue' || rowAction(e) === 'redownload'),
    [selectedEntries],
  )
  const bulkQueue = () => runAction('queue', queueableSelected)
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
  // How many of the pending selection are actively transferring right now (2026-08-13,
  // prompts/2026-08-13-delete-during-transfer.md) -- a bulk selection can mix transferring and
  // idle items, so this is a count, not a boolean, exactly like `pendingDeleteRemoteCount`
  // above.
  const pendingDeleteActiveCount = useMemo(
    () => (pendingDelete ?? []).filter(hasActiveJob).length,
    [pendingDelete],
  )

  if (tree.length === 0) {
    return (
      <p className="p-3 text-sm text-zinc-500 dark:text-zinc-400">
        {connected
          ? 'No files — nothing found on the remote or the local side for this queue.'
          : 'Connecting…'}
      </p>
    )
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
        {/* The facet filter (2026-08-13): replaces "Missing only" -- see the module-level
            comment on `FacetFilter` above. */}
        <select
          className={inputClasses}
          value={facetFilter}
          onChange={(e) => setFacetFilter(e.target.value as FacetFilter)}
          aria-label="Filter by lifecycle facet"
        >
          {FACET_FILTER_VALUES.map((f) => (
            <option key={f} value={f}>
              {FACET_FILTER_LABELS[f]}
            </option>
          ))}
        </select>
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
                setFacetFilter('')
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
            disabled={bulkBusy || queueableSelected.length === 0}
            onClick={bulkQueue}
            title={
              queueableSelected.length === 0
                ? 'None of the selected rows can be queued (already transferring, or nothing to fetch)'
                : undefined
            }
            className="rounded-md border border-sky-400 px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-sky-700 dark:text-sky-200 dark:hover:bg-sky-900"
          >
            Queue selected{queueableSelected.length > 0 && ` (${queueableSelected.length})`}
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
          {/* "Reset item tracking" no longer has its own button here (2026-08-14,
              prompts/2026-08-14-reset-panel-counts-and-layout.md) -- it's the Selected scope of
              the unified `QueueResetControls.tsx` control below the tree, which reads this same
              `selected` set (now a lifted prop). See that component for the confirm flow. */}
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
          confirmation), sits between the request above and `runAction('delete', ...)`. One
          dialog, not two (2026-08-13, prompts/2026-08-13-delete-during-transfer.md): the user
          asked for a confirmation that says an active transfer will be cancelled, not a second
          confirmation step stacked on this one, so the active-transfer fact is a line inside
          the same panel, right alongside (never replacing) the remote-copy line -- a selection
          can be transferring *and* have a surviving remote copy at once, and both are true
          statements the user should see together. */}
      {pendingDelete && (
        <div className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="text-red-900 dark:text-red-200">
            Delete the local copy of <strong>{pendingDelete.length}</strong>{' '}
            {pendingDelete.length === 1 ? 'item' : 'items'} ({formatBytes(pendingDeleteBytes)})?
            This only removes the local copy -- nothing remote is touched -- and cannot be
            undone.
          </p>
          {pendingDeleteActiveCount > 0 && (
            <p className="font-medium text-red-900 dark:text-red-200">
              {activeTransferNote(pendingDelete.length, pendingDeleteActiveCount)}
            </p>
          )}
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
          ref={attachScrollRef}
          className="max-h-[70vh] overflow-auto rounded-md border border-zinc-200 dark:border-zinc-800"
        >
          {/* Column labels -- driven by the same `RESIZABLE_COLUMNS` definition `Row` reads
              (2026-08-13; see that block's own comment for why there was previously a second,
              hand-synced copy of every width here). Sortable columns (2026-08-13, item 1) are
              themselves the sort control -- `SortHeaderButton` above -- rather than a separate
              widget; every other header stays a plain `<span>` so it never looks clickable.
              "Status" sorts by percent-complete (`SORT_KEYS`'s `'percent'`) -- the state chip's
              own fill is where that percentage already shows, so that's the header it belongs
              to; its `title` spells out "% complete" for anyone who hovers to check. Each
              resizable column also carries a drag handle (`ColumnResizeHandle`) at its left
              edge (2026-08-14: moved from the right edge, which never tracked the cursor -- see
              that component's own docstring), a sibling of the label rather than nested inside
              it -- see that component's own comment for why a drag can never fire a sort. */}
          <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-zinc-200 bg-zinc-50 px-2 py-1 text-[10px] font-medium tracking-wide text-zinc-500 uppercase dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            <span className="w-4 shrink-0" />
            <span className="w-4 shrink-0" />
            <span className="w-4 shrink-0" />
            <div className="flex min-w-0 flex-1 items-center" style={{ minWidth: NAME_MIN_WIDTH_PX }}>
              <SortHeaderButton
                sortKey="name"
                label="Name"
                sortPref={sortPref}
                onSort={toggleSort}
                className="w-full justify-start"
              />
            </div>
            {RESIZABLE_COLUMNS.map((col) => (
              <div key={col.id} className="relative flex shrink-0 items-center" style={fixedColumnStyle(col.id)}>
                {col.sortKey ? (
                  <SortHeaderButton
                    sortKey={col.sortKey}
                    label={col.label}
                    title={col.title}
                    sortPref={sortPref}
                    onSort={toggleSort}
                    className={`w-full ${col.align === 'right' ? 'justify-end' : 'justify-start'}`}
                  />
                ) : (
                  <span className={`w-full truncate ${col.align === 'right' ? 'text-right' : ''}`}>
                    {col.label}
                  </span>
                )}
                <ColumnResizeHandle
                  column={col}
                  currentWidth={columnWidths[col.id] ?? col.defaultWidth}
                  containerRef={scrollRef}
                  onCommit={commitColumnWidth}
                />
              </div>
            ))}
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
                    onOpenDrawer={setDrawerEntry}
                    actionBusy={rowBusy.has(entry.rel_path)}
                    settleSettings={settleSettings}
                    hoverCardRef={hoverCardRef}
                  />
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* The item drawer (2026-08-13, prompts/2026-08-13-files-detail-inspector.md) --
          `nodes` is already this queue's whole tree (this component's own prop), exactly what
          `ItemDrawer` needs; no second fetch. `itemId` comes straight off the clicked row --
          `TransfersPage.tsx` supplies the same prop from `job.item_id` for its own entry
          point, which this generalises rather than replaces. */}
      {drawerEntry && (
        <ItemDrawer
          title={drawerEntry.name}
          rootRelPath={drawerEntry.rel_path}
          itemId={drawerEntry.id}
          nodes={nodes}
          onClose={() => setDrawerEntry(null)}
        />
      )}

      {/* The hover card (2026-08-13) -- mounted once, outside the virtualized row list, so its
          own open/close state never touches `FileTree`'s render or any row's. See
          `HoverCardHost`'s own docstring. */}
      <HoverCardHost controllerRef={hoverCardRef} />
    </div>
  )
}
