import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import { type StartNowOption, type StartNowRatePercent, startNowOptions, startNowRequestArg } from '../lib/startNow'

// The Transfers row's "Start now" control (2026-08-19,
// prompts/done/2026-08-19-start-now-bandwidth-fractions.md): DESIGN.md §4.5's single "Start now
// at max bandwidth" button widened into a 10%/25%/50%/75%/Max menu. `lib/startNow.ts` owns every
// decision (labels, which options are disabled, the request payload); this component only
// renders what that pure module returns and wires up the interaction.
//
// **No existing menu idiom to reuse.** `FieldHelp.tsx`/`FileTree.tsx`'s hover card are
// passive tooltips (`role="tooltip"`, no keyboard navigation between items) -- this is a real
// action menu, so it borrows their portal/positioning mechanics (`lib/popoverPosition.ts`,
// `createPortal` into `document.body` so no ancestor's `overflow` can clip it, painted at
// `opacity: 0` for one frame to measure real size before placing) but is otherwise a plain,
// native, keyboard-accessible `role="menu"` -- no new dependency, per the task's own fallback
// instruction.

const buttonClasses =
  'rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

function StartNowMenuList({
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
  options: StartNowOption[]
  activeIndex: number
  itemRefs: React.MutableRefObject<Array<HTMLButtonElement | null>>
  onSelect: (option: StartNowOption) => void
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
      aria-label="Start now at"
      className="fixed z-50 flex min-w-[8rem] flex-col gap-0.5 rounded-md border border-zinc-200 bg-white p-1 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
      style={style}
    >
      {options.map((option, index) => (
        <button
          key={option.ratePercent}
          ref={(el) => {
            itemRefs.current[index] = el
          }}
          type="button"
          role="menuitem"
          disabled={option.disabled}
          title={option.disabledHint ?? undefined}
          tabIndex={index === activeIndex ? 0 : -1}
          onClick={() => onSelect(option)}
          onKeyDown={(e) => onItemKeyDown(e, index)}
          className="rounded px-2 py-1 text-left text-zinc-700 hover:bg-zinc-100 focus:bg-zinc-100 focus:outline-none disabled:cursor-not-allowed disabled:text-zinc-400 disabled:hover:bg-transparent dark:text-zinc-200 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800 dark:disabled:text-zinc-600"
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function StartNowMenu({
  disabled,
  maxBandwidthBps,
  onSelect,
}: {
  /** The row's own busy state (a request already in flight) -- distinct from an *option's* own
   * `disabled` (no site limit for a fraction), which `lib/startNow.ts` decides per-option.
   */
  disabled: boolean
  maxBandwidthBps: number | null | undefined
  onSelect: (ratePercent: StartNowRatePercent | undefined) => void
}) {
  const id = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)

  const options = startNowOptions(maxBandwidthBps)
  const firstEnabledIndex = options.findIndex((o) => !o.disabled)
  const lastEnabledIndex = options.length - 1 - [...options].reverse().findIndex((o) => !o.disabled)

  const close = (focusButton: boolean) => {
    setOpen(false)
    if (focusButton) buttonRef.current?.focus()
  }

  const openAt = (index: number) => {
    setActiveIndex(index < 0 ? 0 : index)
    setOpen(true)
  }

  // Escape closes from anywhere in the menu, a click/tap outside closes it without moving
  // focus -- the same two dismissal paths `FieldHelp.tsx`'s own portal-rendered card uses.
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `close` is stable enough here (no
    // deps of its own beyond refs/setState); re-subscribing every render would be needless churn.
  }, [open, id])

  // Move focus onto the active item once the menu is open, and whenever the active item changes
  // via arrow-key navigation.
  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.focus()
  }, [open, activeIndex])

  const moveActive = (delta: number) => {
    setActiveIndex((prev) => {
      let next = prev
      for (let i = 0; i < options.length; i++) {
        next = (next + delta + options.length) % options.length
        if (!options[next].disabled) return next
      }
      return prev
    })
  }

  const select = (option: StartNowOption) => {
    if (option.disabled) return
    close(true)
    onSelect(startNowRequestArg(option))
  }

  const handleButtonKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openAt(firstEnabledIndex)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      openAt(lastEnabledIndex)
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
        setActiveIndex(firstEnabledIndex)
        break
      case 'End':
        e.preventDefault()
        setActiveIndex(lastEnabledIndex)
        break
      case 'Escape':
        e.preventDefault()
        close(true)
        break
      case 'Tab':
        // Let focus leave the menu normally rather than trapping it -- just close first.
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
        onClick={() => (open ? close(true) : openAt(firstEnabledIndex))}
        onKeyDown={handleButtonKeyDown}
        className={buttonClasses}
      >
        Start now ▾
      </button>
      {open &&
        buttonRef.current &&
        createPortal(
          <StartNowMenuList
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
