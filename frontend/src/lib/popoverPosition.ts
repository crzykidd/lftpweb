// Where a portal-rendered popover goes, given its anchor. Extracted verbatim from
// `FileTree.tsx`'s `HoverCardContent` (`f4a4205`, the both-sides hover card) when the Docs
// section's `FieldHelp` needed the identical placement behaviour: prefer below the anchor, flip
// above when there isn't room, and clamp both axes inside the viewport rather than ever
// overflowing off-screen.
//
// Pulled into `lib/` rather than copied into a second component **because this project already
// has two popup mechanisms** (the hover card and the inline confirm panels) and a third one
// that quietly disagreed about edge behaviour would be the worst of both. Both callers now
// share one function, so a fix to one is a fix to both -- and, being pure arithmetic over plain
// rectangles, it is unit-testable without mounting anything, unlike the `useLayoutEffect` it
// used to live inside.

/** The subset of `DOMRect` this needs -- a plain shape so a test can pass literals rather than
 * constructing real DOM rectangles. `DOMRect` structurally satisfies it.
 */
export interface AnchorRect {
  top: number
  bottom: number
  left: number
}

export interface PopoverSize {
  width: number
  height: number
}

export interface Viewport {
  width: number
  height: number
}

export interface PopoverPlacement {
  top: number
  left: number
}

/** Vertical/horizontal breathing room kept between the card and every viewport edge. */
export const POPOVER_EDGE_MARGIN_PX = 8

/** Places `size` relative to `anchor` inside `viewport`.
 *
 * Vertical: below the anchor by `margin`; if that would overflow the bottom edge, above it
 * instead; then clamped into `[margin, viewport.height - height - margin]` so a card taller
 * than the space on either side still lands on-screen rather than half off it.
 *
 * Horizontal: left-aligned with the anchor, pulled left if it would overrun the right edge,
 * then clamped at `margin` on the left. The left clamp is applied last deliberately -- on a
 * viewport narrower than the card, being flush with the left edge beats being flush with the
 * right, because that is the side the text starts on.
 */
export function placePopover(
  anchor: AnchorRect,
  size: PopoverSize,
  viewport: Viewport,
  margin: number = POPOVER_EDGE_MARGIN_PX,
): PopoverPlacement {
  let top = anchor.bottom + margin
  if (top + size.height > viewport.height - margin) top = anchor.top - size.height - margin
  top = Math.max(margin, Math.min(top, viewport.height - size.height - margin))

  let left = anchor.left
  if (left + size.width > viewport.width - margin) left = viewport.width - size.width - margin
  left = Math.max(margin, left)

  return { top, left }
}
