// Categorical color assignment for the Dashboard's two charts -- dataviz skill: "assign
// categorical hues in fixed order, never cycled." Keyed by queue id, sorted ascending, so a
// queue's color never changes when the set of queues shown changes (a filter, a queue
// selector, a queue being added later). See `chartTheme.css` for the actual hex values per
// mode.

export const SERIES_SLOT_COUNT = 8

export const SERIES_COLOR_VARS = Array.from(
  { length: SERIES_SLOT_COUNT },
  (_, i) => `var(--series-${i + 1})`,
)

/** Queues beyond the 8th slot fold into the last ("Other") slot rather than generating a 9th
 * hue -- the palette's own documented limit (references/palette.md).
 */
export function assignQueueColorSlots(queues: { id: number }[]): Map<number, number> {
  const sorted = [...queues].sort((a, b) => a.id - b.id)
  const map = new Map<number, number>()
  sorted.forEach((q, i) => map.set(q.id, Math.min(i, SERIES_SLOT_COUNT - 1)))
  return map
}

export function colorVarForSlot(slot: number): string {
  return SERIES_COLOR_VARS[slot] ?? SERIES_COLOR_VARS[SERIES_SLOT_COUNT - 1]
}
