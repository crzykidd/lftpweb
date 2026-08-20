import { useVirtualizer } from '@tanstack/react-virtual'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react'
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { deleteItem, getRemovalGraceSettings, getSettleSettings, queueItem, stopItem } from '../api/client'
import type { DeleteItemResponse, FileNode, RemovalGraceSettingsOut, SettleSettingsOut, SyncMode } from '../api/types'
import type { ChildSpeedSample } from '../hooks/useLiveModel'
import {
  bothSidesRows,
  deletedArchiveLabel,
  formatBytes,
  formatPercent,
  hasBothSides,
  isDeletedArchiveVolume,
  isRemovalGracePending,
  isStillArriving,
  removalGraceLabel,
  removalGraceShortLabel,
  settleArrivingLabel,
  settleArrivingShortLabel,
  settleWaitLabel,
  settleWaitShortLabel,
  stateAgeLabel,
} from '../lib/format'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'
import { ArrRowChip, DetailButton, LifecycleIcons } from './LifecycleIcons'
import { ItemDrawer } from './ItemDrawer'
import { StateChip } from './StateChip'
// Pure tree/sort/collapse/facet/column-width logic, extracted to a plain module (audit P1) --
// the component keeps every JSX/stateful piece and pulls these back in by name.
import {
  buildTree,
  canConfirmDelete,
  canDeleteLocal,
  clampColumnWidth,
  DEFAULT_COLLAPSE_PREFERENCE,
  DEFAULT_SORT_PREFERENCE,
  defaultSourceChecked,
  effectiveDeleteScope,
  effectiveEtaLabel,
  effectiveSpeedLabel,
  fixedColumnStyle,
  flatten,
  hasLocalContent,
  hasRemoteCopy,
  isCollapsePreference,
  isColumnWidths,
  isSortPreference,
  matchesFacetFilter,
  mergeColumnWidths,
  nodeDisplaySize,
  RESIZABLE_COLUMNS,
  resolveCollapsed,
  rowAction,
  shouldOfferLocalScope,
  shouldOfferSourceScope,
  showsCopyQueueSourceWarning,
  sortTree,
  stateProgressPercent,
  type CollapsePreference,
  type ColumnDef,
  type ColumnWidths,
  type FacetFilter,
  type SortKey,
  type SortPreference,
  type TreeEntry,
} from '../lib/fileTree'

// One shared ticker per tree, not a `setInterval` per row (migration 006's `state_changed_at`
// column, DESIGN.md §9.2). This page can hold thousands of rows; a per-row timer would mean
// thousands of live intervals for a reading that only needs to refresh every few seconds. A
// single bumped counter here forces a re-render of whatever rows are currently mounted --
// cheap, because the virtualizer only ever mounts the visible slice.
const AGE_TICK_INTERVAL_MS = 15_000

const ROW_HEIGHT_PX = 32


const inputClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'



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




// `canDeleteLocal`/`hasLocalContent`/`shouldOfferLocalScope` moved to `lib/fileTree.ts`
// (2026-08-17, prompts/2026-08-17-stranded-source-delete-retry.md) -- `canDeleteLocal` widened
// there to offer Delete for a no-local-content row with a surviving remote copy too (a stranded
// rung-4 source delete's escape hatch), and the pure-helper move keeps it testable without
// rendering this component, the same reasoning every other predicate in that module follows.

/** Bytes this node's delete would free -- the same "how much is there" reading the size
 * column already shows (`Row`'s own `size` computation), reused so the confirmation dialog's
 * total can never disagree with what's rendered per-row.
 */
function localBytes(node: FileNode): number {
  return node.local_size ?? node.remote_size ?? 0
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

// `stateProgressPercent` moved to `lib/fileTree.ts` (2026-08-20, docs/transfers-redesign-spec.md
// §3.3 stage 5) -- the Transfers row's own file-list expansion shares it now; see that module's
// own docstring for the full reasoning, unchanged from this file's original comment.

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


const FACET_FILTER_LABELS: Record<FacetFilter, string> = {
  '': 'All items',
  has_remote: 'Has remote copy',
  has_local: 'Has local copy',
  extracted: 'Extracted',
  not_extracted: 'Not extracted',
  // Names itself, unlike the checkbox it replaces -- the exact behavior the old "Missing only"
  // checkbox had (`downloaded_at` set, `facets.local.reason === 'missing'`), just findable now.
  missing_locally: 'Downloaded but missing locally',
  // Sonarr/Radarr integration (docs/arr-integration-spec.md "UI"): every row with a non-null
  // `arr_status` -- "being watched through the pipeline," in the spec's own words.
  arr_tracked: '*arr-tracked',
  // Called out on its own, per the spec's explicit instruction: `gone` is the one *arr state
  // that usually needs a human (a release that left the *arr's queue without ever importing).
  arr_gone: '*arr: gone (needs attention)',
}

const FACET_FILTER_VALUES: FacetFilter[] = [
  '',
  'has_remote',
  'has_local',
  'extracted',
  'not_extracted',
  'missing_locally',
  'arr_tracked',
  'arr_gone',
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
  // The removal grace period's own site-wide constant (2026-08-14) -- same "fetched once,
  // threaded down" shape as `settleSettings` immediately above.
  removalGraceSettings: RemovalGraceSettingsOut | null
  // Sonarr/Radarr integration (docs/arr-integration-spec.md "UI"): the name of the *arr
  // instance bound to *this row's own queue*, resolved by `FilesPage.tsx` from
  // `path_queue.arr_instance_id` -> `GET /api/settings/arr` and threaded down here the same
  // "fetched once, passed down" way `queueLocalPath` already is on `FileTree` itself --
  // `arr_status`/`arr_status_at` are the only *arr fields the item projection itself carries
  // (`entry` already has both, being a `FileNode`), so the instance's own name has to arrive
  // this way. `null` when the queue has no bound instance, or that fetch hasn't resolved yet --
  // `ArrRowChip` still renders (with a generic "the bound *arr instance" hover) rather than
  // waiting.
  arrInstanceName: string | null
  // The bound instance's `kind` (2026-08-16, prompts/2026-08-16-files-brand-logo-icons.md) --
  // same resolution shape as `arrInstanceName` immediately above (`FilesPage.tsx`'s own
  // `listArrInstances()` fetch, keyed by each queue's `path_queue.arr_instance_id`), selects
  // which brand logo `ArrRowChip` draws. `null` when the queue has no bound instance, that fetch
  // hasn't resolved yet, or a future/unrecognized kind -- `ArrRowChip` falls back to its own
  // `ArrTextChip` rather than rendering nothing for a tracked item.
  arrInstanceKind: string | null
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
  removalGraceSettings,
  arrInstanceName,
  arrInstanceKind,
  hoverCardRef,
}: RowProps) {
  const size = nodeDisplaySize(entry)
  // Speed cell text (2026-08-14, "ETA on Files rows") -- computed once here rather than inline
  // in the JSX below, both because the JSX needs the same two values twice (the visible text and
  // the cell's `title`) and to keep the render markup itself readable.
  const speedLabel = effectiveSpeedLabel(entry)
  const etaLabel = effectiveEtaLabel(entry)
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
  // The removal grace period's countdown (2026-08-14, prompts/2026-08-14-removal-grace-
  // countdown.md): the third substitution over the chip, same shape as `isRemoving`/
  // `isSettling` above -- `entry.state` stays at its last complete value (VERIFIED, say) for
  // the whole grace window (`core/mount_sentinel.py.resolve_absence`), so this is purely
  // display, not a claim `state` itself changed. Checked *before* `isRemoving`/`isSettling`
  // below can even apply to this row (a grace-pending row is never mid-delete or REMOTE_ONLY),
  // but ordered after them in the ternary chain so an actual delete-in-flight or settle wait
  // (different rows, but keep the precedence obvious) still wins if somehow both were true.
  // The eligible-state set comes from the server (`removalGraceSettings.eligible_states`,
  // `core/mount_sentinel.py.COMPLETE_STATES`) rather than the module constant, so a state added
  // on the Python side self-corrects here within one fetch. `undefined` before the fetch
  // resolves falls back to `lib/format.ts`'s bootstrap default -- see its comment.
  const isMissing = isRemovalGracePending(
    entry,
    removalGraceSettings ? new Set(removalGraceSettings.eligible_states) : undefined,
  )
  // A fourth, independent substitution (2026-08-14, prompts/2026-08-14-extracted-archives-rest-
  // as-extracted.md): a spent archive volume `core/local_delete.py.delete_extracted_archives`
  // removed after a successful extraction. `entry.state` reads `EXCLUDED` for this row server-
  // side (`core/engine.py._persist`'s vanished-row sweep, never through `isMissing`'s grace
  // clock -- `deleted_archive_at` and `first_missing_at` are never both set for the same row),
  // which is truthful but reads as "never wanted" rather than "fetched, unpacked, cleaned up."
  // Mutually exclusive with `isMissing` in practice for the same reason; checked last in the
  // ternary chain below, same left-to-right precedence the other three already established.
  const isDeletedArchive = isDeletedArchiveVolume(entry)
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
      {/* Speed (2026-08-14, prompts/2026-08-14-files-page-speed-column.md; extended
          2026-08-14, "per-file speed inside a mirror"): the live rate from the `progress` WS
          message, already resolved onto `entry.speed_bps` by `buildTree`. `transferSpeedLabel`
          is the one place that decides blank-vs-shown for that job-level reading -- gated on
          `entry.state === 'DOWNLOADING'`, never on the value itself, so a real `0 B/s` on a
          stalled-but-still-running transfer still shows as `0 B/s`, not blank (see that
          function's own docstring in `lib/format.ts`). `effectiveSpeedLabel` adds one fallback
          on top: a **child** file inside a mirroring directory sits at `PARTIAL`, never
          `DOWNLOADING`, so it falls through to its own freshness-gated `child_speed_bps`
          instead (`entry.speed_bps` is `null` for it in the first place -- a leaf file is never
          the parent of a running job).

          **ETA appended in the same cell (2026-08-14, "ETA on Files rows"):** "34 MB/s · 3m",
          not a seventh fixed-width column -- this project's Files columns are already tight
          (`a4a626d` trimmed labels once specifically because they were clipping), and rate/ETA
          read as one thought rather than two things to scan separately. Rejected alternatives
          (a dedicated ETA column, hover-only) are recorded in docs/decisions.md. The column
          still **sorts by rate only** (`sortValue`'s `'speed'` case, unchanged) -- ETA never
          gets its own sort key, so the header's sort semantics stay exactly what its label
          already implies. `etaLabel` is appended only when there is one to show
          (`effectiveEtaLabel` returns `'—'` for the same "nothing to report" cases
          `effectiveSpeedLabel` already does, so this never renders a dangling " · —"), and
          because both derivations share the same job-level/child-level gates, `etaLabel` is
          never non-dash while `speedLabel` is dash. Column width and busyness at a narrow
          viewport are unverified -- no browser exists in this environment; a human should
          check this at a narrow window. */}
      <span
        className="shrink-0 overflow-hidden text-right text-zinc-500 dark:text-zinc-400"
        style={fixedColumnStyle('speed')}
        title={etaLabel !== '—' ? `ETA ${etaLabel}` : undefined}
      >
        {speedLabel}
        {etaLabel !== '—' && ` · ${etaLabel}`}
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
            pairs share `SETTLING`'s amber chip styling; only the words differ.

            **`isMissing` is a third, independent substitution** (2026-08-14,
            prompts/2026-08-14-removal-grace-countdown.md): DESIGN.md §3.2 rule 3's grace
            period, running while a previously-complete item's local copy is absent and its
            remote copy is still present. Without this the chip kept showing the row's
            last-known-good state (VERIFIED, say) for the whole ~10-minute window with nothing
            indicating a transition was pending -- the live case this closed. Mutually
            exclusive with `isRemoving`/`isSettling` in practice (different `state`/`substate`
            combinations produce each), but `isRemoving` is still checked first in case a
            future state ever satisfies more than one, matching this ternary's existing
            left-to-right precedence.

            **`isDeletedArchive` is a fourth substitution** (2026-08-14,
            prompts/2026-08-14-extracted-archives-rest-as-extracted.md): a spent archive volume,
            real `state === 'EXCLUDED'` underneath. Grey (`StateChip.tsx`'s `FALLBACK_STYLE`,
            since `'ARCHIVE_EXTRACTED'` is deliberately not a key in its `STYLES` map), same
            word as the parent's own emerald `EXTRACTED` chip -- "consumed, and this is why,"
            not an alarm. Checked last: it never coincides with the other three in practice
            (`deleted_archive_at` and `first_missing_at` are never both set), but the same
            left-to-right precedence applies if that ever changes. */}
        <StateChip
          state={
            isRemoving
              ? 'REMOVING'
              : isSettling
                ? 'SETTLING'
                : isMissing
                  ? 'MISSING'
                  : isDeletedArchive
                    ? 'ARCHIVE_EXTRACTED'
                    : entry.state
          }
          percent={
            isRemoving || isSettling || isMissing || isDeletedArchive
              ? null
              : stateProgressPercent(entry.state, entry.local_size, entry.remote_size)
          }
          label={
            isStillArrivingRow
              ? settleArrivingShortLabel(entry)
              : isSettling
                ? settleWaitShortLabel(entry, settleSettings)
                : isMissing
                  ? removalGraceShortLabel(entry, removalGraceSettings)
                  : isDeletedArchive
                    ? 'Extracted'
                    : undefined
          }
          title={
            isStillArrivingRow
              ? settleArrivingLabel(entry)
              : isSettling
                ? settleWaitLabel(entry, settleSettings)
                : isMissing
                  ? removalGraceLabel(entry, removalGraceSettings)
                  : isDeletedArchive
                    ? deletedArchiveLabel(entry)
                    : undefined
          }
        />
      </span>
      {/* Sonarr/Radarr integration chip (docs/arr-integration-spec.md "UI"; unified onto the
          real-brand-logo chip 2026-08-16, prompts/2026-08-16-files-brand-logo-icons.md -- the
          same `ArrRowChip` Transfers/History render on their own row lines, "one visual
          language everywhere"): renders nothing for `arr_status: null` (no bound instance, or
          not matched yet) -- `ArrRowChip`'s own docstring covers the variant mapping and why
          `arrInstanceName`/`arrInstanceKind` are threaded in rather than resolved from `entry`
          alone. */}
      <span className="flex shrink-0 justify-end overflow-hidden" style={fixedColumnStyle('arr')}>
        <ArrRowChip
          arrStatus={entry.arr_status}
          arrStatusAt={entry.arr_status_at}
          instanceName={arrInstanceName}
          instanceKind={arrInstanceKind}
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
            title={
              hasLocalContent(entry)
                ? 'Delete the local copy -- this cannot be undone'
                : 'Delete the remote copy on the seedbox -- this cannot be undone'
            }
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

/** A row `runAction` sent no request for at all -- distinct from both success and failure
 * (2026-08-17, `prompts/2026-08-17-bulk-delete-per-entry-scopes.md`). Only produced for
 * `action === 'delete'`, when `effectiveDeleteScope` finds nothing applicable for this row
 * given the checked scopes (e.g. only Local checked, but this row has no local content).
 * Counted separately so the summary's arithmetic (`succeeded + failures.length +
 * skipped.length === total`) stays honest rather than folding a no-op into either bucket.
 */
interface BulkSkip {
  rel_path: string
  name: string
  reason: string
}

interface BulkOutcome {
  action: 'queue' | 'stop' | 'delete'
  total: number
  succeeded: number
  failures: BulkFailure[]
  skipped: BulkSkip[]
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
  etaByItemId,
  childSpeedByItemId,
  selected,
  onSelectionChange,
  queueLocalPath,
  queueSyncMode = 'copy',
  arrInstanceName,
  arrInstanceKind,
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
  /** The `progress` WS message's `eta_s`, keyed by `item_id` (2026-08-14, "ETA on Files rows")
   * -- `useLiveModel.ts`'s own map, the same shape and same source message as `speedByItemId`
   * above. Fed straight into `buildTree` below so every `TreeEntry` carries its own `eta_s`, the
   * one place the Speed cell's appended ETA text reads it from.
   */
  etaByItemId: Record<number, number | null>
  /** `useLiveModel.ts`'s `childSpeedByItemId` (2026-08-14, "per-file speed inside a mirror") --
   * a live rate per changed file inside a mirroring directory, each sample timestamped so
   * `buildTree` can gate display on freshness rather than `state` (see that map's own
   * docstring for why: a child never reaches `DOWNLOADING`). Threaded straight into `buildTree`
   * below, the same shape `speedByItemId` already established.
   */
  childSpeedByItemId: Record<number, ChildSpeedSample>
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
  /** This queue's `path_queue.local_path` (2026-08-14, "folder prefix during transfer") --
   * passed straight through to `ItemDrawer` so it can show an item's actual on-disk path.
   * Optional (`undefined` while `FilesPage.tsx`'s own queue-config fetch hasn't resolved yet,
   * or simply not supplied by a caller that doesn't have it) -- the drawer's physical-location
   * panel just doesn't render without it; nothing else here is affected.
   */
  queueLocalPath?: string
  /** This queue's `path_queue.sync_mode` (2026-08-16, the delete dialog's independent
   * Local/Source scopes, `prompts/2026-08-16-manual-delete-local-and-remote.md`) -- drives the
   * Source checkbox's default (`defaultSourceChecked`) and whether the dialog shows §7.1's
   * copy-queue warning (`showsCopyQueueSourceWarning`). Defaults to `'copy'`, the safer
   * (unchecked-by-default) reading, for the same "queue config not loaded yet" reason
   * `queueLocalPath` above is optional -- `FilesPage.tsx` always resolves the real value once
   * its own queue-config fetch lands.
   */
  queueSyncMode?: SyncMode
  /** Sonarr/Radarr integration (docs/arr-integration-spec.md "UI"): the name of the *arr
   * instance bound to this queue (via `path_queue.arr_instance_id`), resolved by the caller --
   * see `RowProps.arrInstanceName`'s own docstring for why this arrives as a prop rather than
   * being derived from `nodes` here. `undefined`/`null` both read as "not known" and degrade to
   * `ArrRowChip`'s generic hover text; optional so a caller that hasn't wired this up yet (or has
   * no queue config loaded) doesn't have to pass anything.
   */
  arrInstanceName?: string | null
  /** The bound instance's `kind` (2026-08-16, prompts/2026-08-16-files-brand-logo-icons.md) --
   * same resolution shape and same optionality as `arrInstanceName` immediately above; see
   * `RowProps.arrInstanceKind`'s own docstring for how the caller resolves it and why
   * `ArrRowChip` needs it (it selects which brand logo to draw).
   */
  arrInstanceKind?: string | null
}) {
  // The shared age ticker (module docstring above): bumping this forces a re-render of
  // whatever rows are currently mounted, which is all `stateAgeLabel` needs to catch up --
  // it's computed fresh from `Date.now()` on every render, not memoized against this value.
  // Also (2026-08-14, "per-file speed inside a mirror") the one thing that makes a child's
  // `CHILD_SPEED_FRESHNESS_MS` staleness check ever re-evaluate on its own: `tree` below is
  // `useMemo`'d against `childSpeedByItemId`, so a child that simply stops receiving new
  // `child_progress` samples (finished, or its job stopped) would otherwise keep showing its
  // last rate forever -- nothing else would trigger a recompute. Piggy-backing on this existing
  // ticker rather than adding a second interval means the same up-to-15s lag `stateAgeLabel`
  // already accepts for its own "how long ago" text, not a new tunable.
  const [ageTick, bumpAgeTick] = useReducer((c: number) => c + 1, 0)
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

  // The removal grace period's own constant (2026-08-14, prompts/2026-08-14-removal-grace-
  // countdown.md): `core/mount_sentinel.py.DEFAULT_GRACE_S`, fetched once for the whole tree
  // via `GET /api/settings/removal-grace`, the same "site-wide constant, not per-row" shape as
  // `settleSettings` immediately above. `null` until the fetch resolves, or forever on failure
  // -- `removalGraceShortLabel`/`removalGraceLabel` (`lib/format.ts`) degrade to the bare
  // `Missing` label either way, never blocking the tree from rendering.
  const [removalGraceSettings, setRemovalGraceSettings] = useState<RemovalGraceSettingsOut | null>(null)
  useEffect(() => {
    getRemovalGraceSettings()
      .then(setRemovalGraceSettings)
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
    () => sortTree(buildTree(nodes, speedByItemId, childSpeedByItemId, etaByItemId), sortPref.key, sortPref.dir),
    // `ageTick` isn't read inside `buildTree` (it always calls `Date.now()` fresh) -- it's
    // listed purely to force this memo to recompute periodically (the same ticker
    // `stateAgeLabel`'s "how long ago" text already rides), which is what makes a child's
    // `CHILD_SPEED_FRESHNESS_MS` staleness check ever take effect once `childSpeedByItemId`
    // itself stops changing (a finished/stopped child receives no further `child_progress`
    // samples, so nothing else here would trigger a recompute).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, speedByItemId, etaByItemId, childSpeedByItemId, sortPref, ageTick],
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
  // The dialog's independent Local/Source checkboxes (2026-08-16,
  // `prompts/2026-08-16-manual-delete-local-and-remote.md`) -- seeded to their per-queue
  // defaults (`defaultSourceChecked`) every time a new delete is requested (`requestDeleteRow`/
  // `requestDeleteSelected` below), then freely toggleable by the user before confirming.
  const [deleteLocalChecked, setDeleteLocalChecked] = useState(true)
  const [deleteSourceChecked, setDeleteSourceChecked] = useState(false)

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
   *
   * `deleteScope` (2026-08-16, the dialog's independent Local/Source checkboxes) is only read
   * for `action === 'delete'`. Per-entry, not a blanket flag across the whole bulk call
   * (2026-08-17, `prompts/2026-08-17-bulk-delete-per-entry-scopes.md` -- the live-reported bug:
   * a blanket `local: true` sent to every row 409'd any row with no local content before its
   * perfectly deletable source copy was ever attempted): `effectiveDeleteScope` (`lib/fileTree.ts`)
   * is the per-entry answer for *both* scopes, computed once per entry (`entryScopes` below) and
   * reused for the request body, the skip decision, and reading the response honestly afterward.
   *
   * A row whose effective scope comes back `null` (neither checked scope applies to it -- e.g.
   * only Local was checked but this row is `REMOTE_ONLY`) gets no HTTP request at all and lands
   * in `skipped`, not `failures` or `succeededPaths` -- it stays selected, like a failure does,
   * so a retry with a different scope is one click away, but it never shows a fabricated error
   * for a request that was never sent.
   *
   * **Honest partial-failure reporting** (the task's own instruction for a bulk delete): a
   * combined request that deletes the local copy but fails to delete the source resolves
   * (`api/jobs.py.delete_item`'s own docstring: the local side effect already happened, so the
   * endpoint reports 200 with `source_deleted: false` rather than throwing) -- `Promise.
   * allSettled` alone would read that as a plain success. Reading `source_deleted` out of a
   * fulfilled delete response is what keeps that partial outcome from disappearing into the
   * bulk summary's "succeeded" count.
   */
  const runAction = async (
    action: BulkOutcome['action'],
    targets: TreeEntry[],
    deleteScope?: { local: boolean; source: boolean },
  ) => {
    if (targets.length === 0) return
    setBulkBusy(true)
    setBulkOutcome(null)
    try {
      const failures: BulkFailure[] = []
      const skipped: BulkSkip[] = []
      const succeededPaths = new Set<string>()

      // Per-entry effective scope -- `undefined` (not in the map) for queue/stop, where no
      // scope question ever applies.
      const entryScopes = new Map<TreeEntry, { local: boolean; source: boolean } | null>()
      if (action === 'delete' && deleteScope) {
        for (const e of targets) entryScopes.set(e, effectiveDeleteScope(e, deleteScope))
      }

      // Rows with nothing applicable are filtered out before the request goes out at all --
      // never sent as a doomed request, never counted as a request that "succeeded".
      const sendable = targets.filter((e) => {
        if (action !== 'delete') return true
        if (entryScopes.get(e) !== null) return true
        const localOnly = (deleteScope?.local ?? false) && !(deleteScope?.source ?? false)
        const sourceOnly = !(deleteScope?.local ?? false) && (deleteScope?.source ?? false)
        skipped.push({
          rel_path: e.rel_path,
          name: e.name,
          reason: localOnly
            ? 'no local copy — only Local was selected'
            : sourceOnly
              ? 'no remote copy — only Source was selected'
              : 'nothing applicable to this row',
        })
        return false
      })

      const results = await Promise.allSettled(
        sendable.map((e) => {
          if (action === 'queue') return queueItem(e.id as number)
          if (action === 'stop') return stopItem(e.id as number)
          const scope = entryScopes.get(e) as { local: boolean; source: boolean }
          return deleteItem(e.id as number, scope.local, scope.source)
        }),
      )
      results.forEach((result, i) => {
        const entry = sendable[i]
        if (result.status !== 'fulfilled') {
          failures.push({ rel_path: entry.rel_path, name: entry.name, error: errorMessage(result.reason) })
          return
        }
        if (action === 'delete' && entryScopes.get(entry)?.source) {
          const response = result.value as DeleteItemResponse
          if (response.source_deleted === false) {
            failures.push({
              rel_path: entry.rel_path,
              name: entry.name,
              error: response.source_reason ?? 'source delete failed',
            })
            return
          }
        }
        succeededPaths.add(entry.rel_path)
      })
      const nextSelection = new Set(selected)
      for (const path of succeededPaths) nextSelection.delete(path)
      onSelectionChange(nextSelection)
      setBulkOutcome({
        action,
        total: targets.length,
        succeeded: succeededPaths.size,
        failures,
        skipped,
      })
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
  // Seeds the dialog's Local/Source checkboxes to their per-queue defaults every time a new
  // delete is requested (2026-08-16, `defaultSourceChecked`'s own docstring). Local starts
  // checked only when at least one target actually has local content for that scope to act on
  // (`shouldOfferLocalScope`, the pre-existing "always true" behavior for the common case where
  // it does) -- 2026-08-17, `canDeleteLocal`'s widened rule now lets a delete be requested for a
  // stranded no-local-content row too, and defaulting Local to checked there would offer a
  // checkbox (or send a scope) with nothing under it. Source, symmetrically, only when the queue
  // is `move` *and* at least one target actually has a remote copy for it to act on.
  const requestDeleteRow = (entry: TreeEntry) => {
    setPendingDelete([entry])
    setDeleteLocalChecked(shouldOfferLocalScope([entry]))
    setDeleteSourceChecked(defaultSourceChecked(queueSyncMode, hasRemoteCopy(entry)))
  }
  const requestDeleteSelected = () => {
    if (deletableSelected.length === 0) return
    setPendingDelete(deletableSelected)
    setDeleteLocalChecked(shouldOfferLocalScope(deletableSelected))
    setDeleteSourceChecked(defaultSourceChecked(queueSyncMode, deletableSelected.some(hasRemoteCopy)))
  }
  const confirmDelete = async () => {
    const targets = pendingDelete
    const scope = { local: deleteLocalChecked, source: deleteSourceChecked }
    setPendingDelete(null)
    if (targets) await runAction('delete', targets, scope)
  }
  const pendingDeleteBytes = useMemo(
    () => (pendingDelete ?? []).reduce((sum, e) => sum + localBytes(e), 0),
    [pendingDelete],
  )
  // Split by whether a remote copy survives the delete -- entirely different outcomes worth
  // telling the user apart (see `hasRemoteCopy`'s own docstring for why "will this come back"
  // is always answerable from `remote_size` alone, never a guess). Also what
  // `shouldOfferSourceScope` below is built from, at the granularity the dialog actually needs
  // (a count worth reporting, not just a boolean).
  const pendingDeleteRemoteCount = useMemo(
    () => (pendingDelete ?? []).filter(hasRemoteCopy).length,
    [pendingDelete],
  )
  const pendingDeleteOffersSource = useMemo(
    () => shouldOfferSourceScope(pendingDelete ?? []),
    [pendingDelete],
  )
  // Symmetric to `pendingDeleteOffersSource` above (2026-08-17) -- `false` only for a selection
  // made entirely of stranded no-local-content rows (`canDeleteLocal`'s widened rule is what
  // let such a selection reach this dialog at all), so the Local checkbox itself goes unrendered
  // rather than offered-then-refused.
  const pendingDeleteOffersLocal = useMemo(
    () => shouldOfferLocalScope(pendingDelete ?? []),
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

      {/* Delete is irreversible (Queue/Stop are not) -- a confirmation dialog sits between the
          request above and `runAction('delete', ...)`. Two independent scopes (2026-08-16, the
          delete dialog's own checkboxes, `prompts/2026-08-16-manual-delete-local-and-remote.md`,
          settled design): Local (the pre-existing behavior) and Source, the first manual
          remote-delete in the app -- the Source checkbox only renders when at least one pending
          entry actually has a remote copy (`pendingDeleteOffersSource`), defaults per queue mode
          (`defaultSourceChecked` -- checked for `move`, unchecked for `copy`/`sync`), and at
          least one scope must stay checked to confirm (`canConfirmDelete`). One dialog, not two
          (2026-08-13, prompts/2026-08-13-delete-during-transfer.md): the active-transfer fact is
          a line inside the same panel, right alongside (never replacing) the remote-copy line --
          a selection can be transferring *and* have a surviving remote copy at once, and both
          are true statements the user should see together. */}
      {pendingDelete && (
        <div className="flex flex-col gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm dark:border-red-900 dark:bg-red-950/40">
          <p className="text-red-900 dark:text-red-200">
            Delete <strong>{pendingDelete.length}</strong>{' '}
            {pendingDelete.length === 1 ? 'item' : 'items'}
            {deleteLocalChecked && ` (${formatBytes(pendingDeleteBytes)} local)`}? This cannot be
            undone.
          </p>
          {pendingDeleteOffersLocal && (
            <label className="flex items-center gap-2 text-red-900 dark:text-red-200">
              <input
                type="checkbox"
                checked={deleteLocalChecked}
                onChange={(e) => setDeleteLocalChecked(e.target.checked)}
              />
              Delete local copy
            </label>
          )}
          {pendingDeleteOffersSource && (
            <label className="flex items-center gap-2 text-red-900 dark:text-red-200">
              <input
                type="checkbox"
                checked={deleteSourceChecked}
                onChange={(e) => setDeleteSourceChecked(e.target.checked)}
              />
              Delete source (seedbox)
              {pendingDeleteRemoteCount < pendingDelete.length &&
                ` -- ${pendingDeleteRemoteCount} of ${pendingDelete.length} have a remote copy`}
            </label>
          )}
          {pendingDeleteActiveCount > 0 && (
            <p className="font-medium text-red-900 dark:text-red-200">
              {deleteLocalChecked
                ? activeTransferNote(pendingDelete.length, pendingDeleteActiveCount)
                : `${pendingDeleteActiveCount} of ${pendingDelete.length} ${
                    pendingDeleteActiveCount === 1 ? 'is' : 'are'
                  } transferring right now -- a source-only delete refuses until the transfer is stopped (check Delete local copy too, which stops it for you).`}
            </p>
          )}
          {!deleteSourceChecked ? (
            <p className="text-red-900 dark:text-red-200">
              {remoteCopyNote(pendingDelete.length, pendingDeleteRemoteCount)}
            </p>
          ) : (
            <p className="font-medium text-red-900 dark:text-red-200">
              {pendingDeleteRemoteCount === pendingDelete.length
                ? pendingDelete.length === 1
                  ? 'Its remote copy will also be deleted from the seedbox -- irreversible.'
                  : 'Their remote copies will also be deleted from the seedbox -- irreversible.'
                : `${pendingDeleteRemoteCount} of ${pendingDelete.length} remote ${
                    pendingDeleteRemoteCount === 1 ? 'copy' : 'copies'
                  } will also be deleted from the seedbox -- irreversible.`}
            </p>
          )}
          {showsCopyQueueSourceWarning(queueSyncMode, deleteSourceChecked) && (
            <p className="rounded-md border border-amber-400 bg-amber-50 px-2 py-1.5 text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
              ⚠ This queue is <strong>copy</strong> mode -- its remote path is not required to be
              a hardlink pickup directory the way a <strong>move</strong> queue's is. If it
              points at live torrent data instead, deleting the source here can destroy a seed
              (DESIGN.md §7.1).
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={confirmDelete}
              disabled={!canConfirmDelete(deleteLocalChecked, deleteSourceChecked)}
              title={
                canConfirmDelete(deleteLocalChecked, deleteSourceChecked)
                  ? undefined
                  : 'Check at least one of Delete local copy / Delete source'
              }
              className="rounded-md bg-red-700 px-2 py-1 text-xs font-medium text-white hover:bg-red-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-red-800 dark:hover:bg-red-700"
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
            bulkOutcome.failures.length === 0 && bulkOutcome.skipped.length === 0
              ? 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200'
              : 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200'
          }`}
        >
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">
              {BULK_OUTCOME_LABEL[bulkOutcome.action]}: {bulkOutcome.succeeded} of {bulkOutcome.total}{' '}
              succeeded
              {bulkOutcome.failures.length > 0 && `, ${bulkOutcome.failures.length} failed`}
              {bulkOutcome.skipped.length > 0 && `, ${bulkOutcome.skipped.length} skipped`}
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
          {/* Skipped rows (2026-08-17): a distinct bucket from failures -- no request was ever
              sent for these, so they get their own muted line rather than reading as an error. */}
          {bulkOutcome.skipped.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-xs text-zinc-600 dark:text-zinc-400">
              {bulkOutcome.skipped.map((s) => (
                <li key={s.rel_path}>
                  <span className="font-mono">{s.name}</span> — skipped: {s.reason}
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
                    removalGraceSettings={removalGraceSettings}
                    arrInstanceName={arrInstanceName ?? null}
                    arrInstanceKind={arrInstanceKind ?? null}
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
          localPath={queueLocalPath}
        />
      )}

      {/* The hover card (2026-08-13) -- mounted once, outside the virtualized row list, so its
          own open/close state never touches `FileTree`'s render or any row's. See
          `HoverCardHost`'s own docstring. */}
      <HoverCardHost controllerRef={hoverCardRef} />
    </div>
  )
}
