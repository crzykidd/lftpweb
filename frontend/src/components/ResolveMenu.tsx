import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from '../lib/popoverPosition'
import type { ResolveOption } from '../lib/transferPanel'
import { resolveMenuOptions } from '../lib/transferPanel'

// The per-row manual escape hatch on an in-flight Transfers row (2026-08-20,
// docs/transfers-redesign-spec.md §3.2's pipeline-completion rule,
// prompts/done/2026-08-20-active-box-holds-inflight-pipeline.md) -- "Mark complete" / "Mark
// failed", which files a genuinely wedged row out of Active/pending with that outcome.
//
// **It is a classification only.** The menu writes nothing but `item.manual_outcome`
// (`api/client.ts.resolveItem` -> `api/jobs.py.resolve_item`): no source delete, no *arr import,
// no post-processing, no cleanup. Migration 025's own comment is the canonical list.
//
// Copies `DismissMenu.tsx`'s portal/keyboard-navigable pattern (which itself copies
// `StartNowMenu.tsx`'s) rather than generalizing the three into one component -- the same
// judgement `DismissMenu.tsx`'s own header records: the item shapes differ just enough that
// extracting a shared abstraction now would be a bigger, riskier change than this follow-up
// calls for. `lib/transferPanel.ts.resolveMenuOptions` owns the option list, so the vocabulary
// still lives in one unit-tested place.

function ResolveMenuList({
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
  options: ResolveOption[]
  activeIndex: number
  itemRefs: React.MutableRefObject<Array<HTMLButtonElement | null>>
  onSelect: (option: ResolveOption) => void
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
      aria-label="Resolve"
      className="fixed z-50 flex min-w-[10rem] flex-col gap-0.5 rounded-md border border-zinc-200 bg-white p-1 text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
      style={style}
    >
      {options.map((option, index) => (
        <button
          key={option.outcome ?? 'clear'}
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

export function ResolveMenu({
  disabled,
  options = resolveMenuOptions(),
  label = 'Resolve',
  title,
  onSelect,
}: {
  disabled: boolean
  options?: ResolveOption[]
  label?: string
  title?: string
  onSelect: (outcome: ResolveOption['outcome']) => void
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- same stability reasoning as
    // `DismissMenu.tsx`'s identical effect.
  }, [open, id])

  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.focus()
  }, [open, activeIndex])

  const moveActive = (delta: number) => {
    setActiveIndex((prev) => (prev + delta + options.length) % options.length)
  }

  const select = (option: ResolveOption) => {
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
        title={title}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => (open ? close(true) : openAt(0))}
        onKeyDown={handleButtonKeyDown}
        className="rounded-md border border-zinc-300 px-2 py-1 text-xs font-medium text-zinc-500 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900"
      >
        {label} ▾
      </button>
      {open &&
        buttonRef.current &&
        createPortal(
          <ResolveMenuList
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
