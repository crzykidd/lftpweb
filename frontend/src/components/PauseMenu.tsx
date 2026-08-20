import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'

// The Transfers -> Queue tab's Pause control (2026-08-20, `prompts/2026-08-20-queue-pause.md`)
// -- two entry modes into one paused state:
//
// - **Pause after current**: running transfers finish; nothing new is admitted.
// - **Pause now**: also stops what's running, returning each job to `queued` at its same
//   position so it resumes (not restarts) once unpaused.
//
// Reuses `StartNowMenu.tsx`'s own menu mechanics verbatim (portal into `document.body` so no
// ancestor's `overflow` can clip it, `lib/popoverPosition.ts` for placement, plain keyboard-
// accessible `role="menu"`) rather than a new dependency or a hand-rolled variant -- the task's
// own instruction. Unlike that menu, both options here are always enabled: there is no
// per-option "disabled" state to compute (that menu's whole `lib/startNow.ts` doesn't have an
// analogue here), so this component owns its two options directly rather than reading them from
// a pure module.

const buttonClasses =
  'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

interface PauseOption {
  key: 'after_current' | 'now'
  label: string
  hint: string
}

const PAUSE_OPTIONS: PauseOption[] = [
  {
    key: 'after_current',
    label: 'Pause after current',
    hint: 'Nothing new starts; running transfers finish normally.',
  },
  {
    key: 'now',
    label: 'Pause now',
    hint: 'Also stops what is running -- resumes from the same bytes once unpaused.',
  },
]

function PauseMenuList({
  id,
  anchorEl,
  activeIndex,
  itemRefs,
  onSelect,
  onItemKeyDown,
}: {
  id: string
  anchorEl: HTMLElement
  activeIndex: number
  itemRefs: React.MutableRefObject<Array<HTMLButtonElement | null>>
  onSelect: (option: PauseOption) => void
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
      className="fixed z-50 flex min-w-[16rem] flex-col gap-0.5 rounded-md border border-zinc-200 bg-white p-1 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
      style={style}
    >
      {PAUSE_OPTIONS.map((option, index) => (
        <button
          key={option.key}
          ref={(el) => {
            itemRefs.current[index] = el
          }}
          type="button"
          role="menuitem"
          tabIndex={index === activeIndex ? 0 : -1}
          onClick={() => onSelect(option)}
          onKeyDown={(e) => onItemKeyDown(e, index)}
          className="flex flex-col items-start gap-0.5 rounded px-2 py-1.5 text-left text-zinc-700 hover:bg-zinc-100 focus:bg-zinc-100 focus:outline-none dark:text-zinc-200 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800"
        >
          <span className="font-medium">{option.label}</span>
          <span className="text-xs text-zinc-500 dark:text-zinc-400">{option.hint}</span>
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
  onSelect: (mode: 'after_current' | 'now') => void
}) {
  const id = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

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
    setActiveIndex((prev) => (prev + delta + PAUSE_OPTIONS.length) % PAUSE_OPTIONS.length)
  }

  const select = (option: PauseOption) => {
    close(true)
    onSelect(option.key)
  }

  const handleButtonKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openAt(0)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      openAt(PAUSE_OPTIONS.length - 1)
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
        setActiveIndex(PAUSE_OPTIONS.length - 1)
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
        select(PAUSE_OPTIONS[index])
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
