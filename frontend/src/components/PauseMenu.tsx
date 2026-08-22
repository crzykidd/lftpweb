import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import { type PauseDurationMinutes, type PauseMenuOption, pauseMenuOptions } from '../lib/pause'

// The Transfers -> Queue tab's Pause control, redesigned 2026-08-21
// (`prompts/2026-08-21-pause-control-redesign.md`, findings 2 and 3 of
// `prompts/test-findings-2026-08-21.md`) -- **one dropdown, selection is the action.**
//
// Before this task there were two controls and two steps here: a separate duration `<select>`
// that did nothing on its own, then this menu asking "after current" vs "now" and requiring its
// own click. Both are gone. This is now a single list -- "Till I unpause", then 1/10/30/60
// minutes (`lib/pause.ts.pauseMenuOptions`) -- and clicking (or Enter/Space-selecting) an entry
// pauses immediately, with no confirm step. The "after current" vs "now" fork that used to be
// this menu's second level is now `TransfersPage.tsx`'s persistent "Pause after active" checkbox,
// which sits beside this button rather than inside it -- so `onSelect` here only ever carries
// *how long*, never *which mode*; the caller reads the checkbox itself at selection time.
//
// Reuses `StartNowMenu.tsx`'s own menu mechanics verbatim (portal into `document.body` so no
// ancestor's `overflow` can clip it, `lib/popoverPosition.ts` for placement, plain keyboard-
// accessible `role="menu"`) rather than a new dependency or a hand-rolled variant. Every entry is
// always selectable -- unlike `StartNowMenu`, there is no per-option disabled state to compute,
// so there is no pure `options` builder call needed beyond the static list itself.

const buttonClasses =
  'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

function PauseMenuList({
  id,
  anchorEl,
  options,
  activeIndex,
  itemRefs,
  onSelect,
  onItemKeyDown,
}: {
  id: string
  anchorEl: HTMLElement
  options: PauseMenuOption[]
  activeIndex: number
  itemRefs: React.MutableRefObject<Array<HTMLButtonElement | null>>
  onSelect: (option: PauseMenuOption) => void
  onItemKeyDown: (e: ReactKeyboardEvent<HTMLButtonElement>, index: number) => void
}) {
  const listRef = useRef<HTMLDivElement>(null)
  const [style, setStyle] = useState<CSSProperties>({ position: 'fixed', top: 0, left: 0, opacity: 0 })

  useLayoutEffect(() => {
    const list = listRef.current
    if (list == null) return
    const { top, left } = placePopover(
      anchorEl.getBoundingClientRect(),
      list.getBoundingClientRect(),
      { width: window.innerWidth, height: window.innerHeight },
      POPOVER_EDGE_MARGIN_PX,
    )
    setStyle({ position: 'fixed', top, left, opacity: 1 })
  }, [anchorEl])

  return (
    <div
      ref={listRef}
      id={id}
      role="menu"
      aria-label="Pause the transfer queue"
      className="fixed z-50 flex min-w-[10rem] flex-col gap-0.5 rounded-md border border-zinc-200 bg-white p-1 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
      style={style}
    >
      {options.map((option, index) => (
        <button
          key={option.label}
          ref={(el) => {
            itemRefs.current[index] = el
          }}
          type="button"
          role="menuitem"
          tabIndex={index === activeIndex ? 0 : -1}
          onClick={() => onSelect(option)}
          onKeyDown={(e) => onItemKeyDown(e, index)}
          className="rounded px-2 py-1.5 text-left font-medium text-zinc-700 hover:bg-zinc-100 focus:bg-zinc-100 focus:outline-none dark:text-zinc-200 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800"
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function PauseMenu({
  disabled,
  onSelect,
}: {
  /** A pause/unpause request already in flight. */
  disabled: boolean
  /** Fires the instant an entry is chosen -- the selection *is* the pause action, no second
   * click. Carries only the duration; the caller (`TransfersPage.tsx`) reads its own "Pause
   * after active" checkbox at the moment of the call to decide `stopRunning`
   * (`lib/pause.ts.pauseStopRunning`).
   */
  onSelect: (durationMinutes: PauseDurationMinutes) => void
}) {
  const id = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const options = pauseMenuOptions()

  const close = (focusButton: boolean) => {
    setOpen(false)
    if (focusButton) buttonRef.current?.focus()
  }

  const openAt = (index: number) => {
    setActiveIndex(index < 0 ? 0 : index)
    setOpen(true)
  }

  // Same two dismissal paths StartNowMenu.tsx uses: Escape from anywhere in the menu, a
  // click/tap outside closes it without moving focus.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close(true)
    }
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target
      if (target instanceof Node && buttonRef.current?.contains(target)) return
      if (target instanceof Element && target.closest(`[id="${CSS.escape(id)}"]`) != null) return
      close(false)
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `close` is stable enough here.
  }, [open, id])

  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.focus()
  }, [open, activeIndex])

  const moveActive = (delta: number) => {
    setActiveIndex((prev) => (prev + delta + options.length) % options.length)
  }

  const select = (option: PauseMenuOption) => {
    close(true)
    onSelect(option.durationMinutes)
  }

  const handleButtonKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openAt(0)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      openAt(options.length - 1)
    }
  }

  const handleItemKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        moveActive(1)
        break
      case 'ArrowUp':
        e.preventDefault()
        moveActive(-1)
        break
      case 'Home':
        e.preventDefault()
        setActiveIndex(0)
        break
      case 'End':
        e.preventDefault()
        setActiveIndex(options.length - 1)
        break
      case 'Escape':
        e.preventDefault()
        close(true)
        break
      case 'Tab':
        close(false)
        break
      case 'Enter':
      case ' ':
        e.preventDefault()
        select(options[index])
        break
      default:
        break
    }
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => (open ? close(true) : openAt(0))}
        onKeyDown={handleButtonKeyDown}
        className={buttonClasses}
      >
        Pause ▾
      </button>
      {open &&
        buttonRef.current &&
        createPortal(
          <PauseMenuList
            id={id}
            anchorEl={buttonRef.current}
            options={options}
            activeIndex={activeIndex}
            itemRefs={itemRefs}
            onSelect={select}
            onItemKeyDown={handleItemKeyDown}
          />,
          document.body,
        )}
    </>
  )
}
