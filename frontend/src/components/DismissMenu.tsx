import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { DismissMenuOption } from '../lib/transferPanel'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'

// The Complete box's "Dismiss" control (2026-08-20, follow-up to phase 1 stage 4b from the
// user's browser review, prompts/2026-08-20-transfers-dismiss-menu-and-counts.md) -- "maybe it
// is dismiss with a drop down list all, downloaded, failed (or whatever the completed status
// are)" (the user's own words). `lib/transferPanel.ts.dismissMenuOptions` owns the option
// list/labels; this component only renders what that pure function returns and wires up the
// interaction, the same split `lib/startNow.ts`/`components/StartNowMenu.tsx` already establish.
//
// **Copies `StartNowMenu.tsx`'s own portal/keyboard-navigable menu pattern** (the task's own
// instruction: reuse it, don't invent a second menu idiom or add a dependency) rather than
// factoring a shared generic component out of the two -- the two menus' item shapes differ just
// enough (no per-item `disabled`/`disabledHint` here, since per-outcome counts aren't fetched --
// see `dismissMenuOptions`'s own docstring) that extracting one now would be a bigger, riskier
// change than this follow-up calls for.

const buttonClasses =
  'rounded-md border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900'

function DismissMenuList({
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
  options: DismissMenuOption[]
  activeIndex: number
  itemRefs: React.MutableRefObject<Array<HTMLButtonElement | null>>
  onSelect: (option: DismissMenuOption) => void
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
      aria-label="Dismiss"
      className="fixed z-50 flex min-w-[8rem] flex-col gap-0.5 rounded-md border border-zinc-200 bg-white p-1 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
      style={style}
    >
      {options.map((option, index) => (
        <button
          key={option.outcome ?? 'all'}
          ref={(el) => {
            itemRefs.current[index] = el
          }}
          type="button"
          role="menuitem"
          tabIndex={index === activeIndex ? 0 : -1}
          onClick={() => onSelect(option)}
          onKeyDown={(e) => onItemKeyDown(e, index)}
          className="rounded px-2 py-1 text-left text-zinc-700 hover:bg-zinc-100 focus:bg-zinc-100 focus:outline-none dark:text-zinc-200 dark:hover:bg-zinc-800 dark:focus:bg-zinc-800"
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export function DismissMenu({
  disabled,
  busy,
  label,
  title,
  options,
  onSelect,
}: {
  /** Nothing dismissable at all under the box's current filter (`completeTotal === 0` at the
   * call site) -- distinct from `busy` (a dismiss request already in flight for a prior choice).
   */
  disabled: boolean
  busy: boolean
  /** The trigger button's own text, before the "Dismissing…" busy override -- the caller
   * decides whether to append a count (`TransfersPage.tsx` appends `completeTotal`, the same
   * number "All" would dismiss, since that's the one count already on hand -- see
   * `dismissMenuOptions`'s own docstring for why the per-outcome options stay uncounted).
   */
  label: string
  title?: string
  options: DismissMenuOption[]
  onSelect: (outcome: DismissMenuOption['outcome']) => void
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

  // Escape closes from anywhere in the menu, a click/tap outside closes it without moving focus
  // -- the same two dismissal paths `StartNowMenu.tsx` (and, before it, `FieldHelp.tsx`'s own
  // portal-rendered card) already use.
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
    setActiveIndex((prev) => (prev + delta + options.length) % options.length)
  }

  const select = (option: DismissMenuOption) => {
    close(true)
    onSelect(option.outcome)
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
        disabled={disabled || busy}
        title={title}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => (open ? close(true) : openAt(0))}
        onKeyDown={handleButtonKeyDown}
        className={buttonClasses}
      >
        {busy ? 'Dismissing…' : `${label} ▾`}
      </button>
      {open &&
        buttonRef.current &&
        createPortal(
          <DismissMenuList
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
