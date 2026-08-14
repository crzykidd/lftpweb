import type { CSSProperties, ReactNode } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import { InfoIcon } from './LifecycleIcons'

// Per-field help for the Settings pages (2026-08-13, prompts/2026-08-13-docs-section.md). A
// small info icon next to a field label that reveals a short explanation of what the field
// actually does -- the in-place counterpart to the Docs section, for the person who is already
// on the page they need and just wants to know what one control means.
//
// **This is not a third popup mechanism.** It reuses `f4a4205`'s hover-card mechanics
// wholesale: rendered into `document.body` through a portal (so no ancestor's `overflow`,
// `transform`, or stacking context can clip it), positioned from the trigger's own
// `getBoundingClientRect()`, and placed by the same `lib/popoverPosition.ts.placePopover` the
// Files-row hover card now calls. Painted at `opacity: 0` for one frame so the real rendered
// size can be measured before placing it, exactly as `HoverCardContent` does, rather than
// jumping from a guessed position to the real one.
//
// **Two deliberate divergences from the Files-row card**, both because this is a settings form,
// not a dense scrolling list:
//
// 1. **Click/tap toggles it, hover only assists.** The Files card is hover-first because a
//    1,000-row virtualized tree cannot afford a click target per row. A hover-only affordance
//    is *unusable on a phone*, and every path into this one has to work without a pointer, so
//    the trigger is a real `<button>`: tap opens it on touch, and Enter/Space opens it from the
//    keyboard through the identical `onClick` (no `onFocus`-opens race with the click that
//    caused the focus, which is the usual way this pattern breaks). Hover is layered on top for
//    mouse users and is never the only way in.
// 2. **The card accepts pointer events.** The Files card is `pointer-events-none` because it
//    floats over clickable rows and must never swallow their clicks; here it floats over form
//    whitespace, and being able to select the text (or click a link inside it -- the help text
//    routinely points at the Docs section) is worth more than that guarantee.

const HOVER_SHOW_DELAY_MS = 250

/** Where the card is currently anchored, or `null` when closed. Held as one object so the
 * portal only ever renders with a measured anchor, never with a stale one from a previous open.
 */
interface OpenState {
  anchorEl: HTMLElement
}

function FieldHelpCard({
  id,
  anchorEl,
  children,
}: {
  id: string
  anchorEl: HTMLElement
  children: ReactNode
}) {
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
  }, [anchorEl, children])

  return (
    <div
      ref={cardRef}
      id={id}
      role="tooltip"
      className="fixed z-50 flex max-w-[min(26rem,calc(100vw-16px))] flex-col gap-2 rounded-md border border-zinc-200 bg-white px-3 py-2 text-xs leading-relaxed text-zinc-700 shadow-lg dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200"
      style={style}
    >
      {children}
    </div>
  )
}

/** The info-icon trigger plus its portal-rendered card.
 *
 * `label` names the field this explains -- it is not rendered, it is the accessible name of the
 * button ("Help: Sync mode"), because an icon-only control with no name is unusable with a
 * screen reader and "info" alone would be indistinguishable from every other one on the page.
 *
 * Usage: put it inside the field's own `<label>`'s text span, after the label words. It is an
 * inline element and carries no layout of its own.
 */
export function FieldHelp({ label, children }: { label: string; children: ReactNode }) {
  const id = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  // Two independent reasons the card can be up, tracked separately so a pointer leaving the
  // icon can't close a card the user deliberately clicked open.
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const hoverTimer = useRef<number | null>(null)

  const shown = pinned || hovered
  const [open, setOpen] = useState<OpenState | null>(null)

  useEffect(() => {
    setOpen(shown && buttonRef.current != null ? { anchorEl: buttonRef.current } : null)
  }, [shown])

  const clearHoverTimer = () => {
    if (hoverTimer.current != null) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = null
  }
  useEffect(() => clearHoverTimer, [])

  // Escape closes from anywhere, a click elsewhere closes a pinned card, and any scroll or
  // resize closes it outright rather than leaving it floating beside where the field used to
  // be -- the card is positioned in viewport coordinates from a one-shot measurement, so it
  // does not follow its anchor.
  useEffect(() => {
    if (!shown) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setPinned(false)
      setHovered(false)
      buttonRef.current?.focus()
    }
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target
      if (target instanceof Node && buttonRef.current?.contains(target)) return
      if (target instanceof Element && target.closest(`[id="${CSS.escape(id)}"]`) != null) return
      setPinned(false)
      setHovered(false)
    }
    const dismiss = () => {
      setPinned(false)
      setHovered(false)
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    // `capture` so a scroll inside any scrollable ancestor counts, not only one on `window`.
    window.addEventListener('scroll', dismiss, true)
    window.addEventListener('resize', dismiss)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('resize', dismiss)
    }
  }, [shown, id])

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label={`Help: ${label}`}
        aria-expanded={shown}
        aria-describedby={shown ? id : undefined}
        onClick={() => {
          clearHoverTimer()
          setHovered(false)
          setPinned((prev) => !prev)
        }}
        onPointerEnter={(e) => {
          // Touch taps also raise `pointerenter`; letting them start the hover timer would
          // open the card and then have the click that follows immediately toggle it shut.
          if (e.pointerType !== 'mouse') return
          clearHoverTimer()
          hoverTimer.current = window.setTimeout(() => {
            hoverTimer.current = null
            setHovered(true)
          }, HOVER_SHOW_DELAY_MS)
        }}
        onPointerLeave={() => {
          clearHoverTimer()
          setHovered(false)
        }}
        className="ml-1 inline-flex translate-y-0.5 items-center rounded text-zinc-400 hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 dark:text-zinc-500 dark:hover:text-zinc-200"
      >
        <InfoIcon title={`Help: ${label}`} />
      </button>
      {open != null &&
        createPortal(
          <FieldHelpCard id={id} anchorEl={open.anchorEl}>
            {children}
          </FieldHelpCard>,
          document.body,
        )}
    </>
  )
}
