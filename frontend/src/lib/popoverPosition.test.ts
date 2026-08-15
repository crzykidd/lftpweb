import { describe, expect, it } from 'vitest'
import { placePopover, POPOVER_EDGE_MARGIN_PX } from './popoverPosition'

const VIEWPORT = { width: 1000, height: 800 }
const M = POPOVER_EDGE_MARGIN_PX

function anchor(top: number, height: number, left: number) {
  return { top, bottom: top + height, left }
}

describe('placePopover', () => {
  it('places the card below the anchor when there is room', () => {
    const { top, left } = placePopover(anchor(100, 20, 200), { width: 300, height: 120 }, VIEWPORT)
    expect(top).toBe(120 + M)
    expect(left).toBe(200)
  })

  it('flips above the anchor when the card would overflow the bottom edge', () => {
    // Anchor near the bottom: below would be 780+8=788, and 788+120 > 800-8.
    const { top } = placePopover(anchor(760, 20, 200), { width: 300, height: 120 }, VIEWPORT)
    expect(top).toBe(760 - 120 - M)
  })

  it('never places the card above the top edge, even when neither side fits', () => {
    // Taller than the viewport minus margins: the flip would go negative, so the clamp wins.
    const { top } = placePopover(anchor(10, 20, 200), { width: 300, height: 900 }, VIEWPORT)
    expect(top).toBe(M)
  })

  it('pulls the card left rather than overflowing the right edge', () => {
    const { left } = placePopover(anchor(100, 20, 900), { width: 300, height: 120 }, VIEWPORT)
    expect(left).toBe(1000 - 300 - M)
  })

  it('clamps to the left margin when the card is wider than the viewport', () => {
    // The left clamp is applied after the right-edge pull, so a too-wide card ends up flush
    // with the left edge (where its text starts) rather than flush with the right.
    const { left } = placePopover(anchor(100, 20, 50), { width: 1200, height: 120 }, VIEWPORT)
    expect(left).toBe(M)
  })

  it('honours a caller-supplied margin', () => {
    const { top, left } = placePopover(anchor(100, 20, 200), { width: 300, height: 120 }, VIEWPORT, 30)
    expect(top).toBe(150)
    expect(left).toBe(200)
  })

  it('accepts a real DOMRect-shaped object structurally', () => {
    // `HoverCardContent`/`FieldHelpCard` both hand this function `getBoundingClientRect()`
    // results directly -- a DOMRect carries more fields than `AnchorRect`, and must still be
    // accepted without a cast.
    const rect = { x: 200, y: 100, top: 100, bottom: 120, left: 200, right: 500, width: 300, height: 20 }
    expect(placePopover(rect, { width: 300, height: 120 }, VIEWPORT).top).toBe(120 + M)
  })
})
