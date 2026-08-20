// The per-item Events deep link (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage
// 7) -- "a row on the Queue or Files tab gets an affordance that opens Events pre-filtered to
// that item," so the item drawer can stop duplicating the audit trail (one canonical place,
// reachable in one click from anywhere). The filter lives entirely in the URL -- search params,
// not a path segment or component state -- so the resulting view is linkable, reloadable, and
// back-button friendly, the same principle stage 6's tabs already established for `/transfers`.
//
// Pure functions, tested without mounting anything (this project's whole component-testing
// story, README.md's Known gaps) -- the same discipline `nav.ts`'s `tabsForPath` and
// `docLinks.ts`'s `classifyLink` already establish for route logic.

/** The row-level affordance's own href. `item_id` is the only filter `GET /api/history/events`
 * needs (`api/history.py` already accepts it, unchanged by this task); `label` rides along only
 * so the Events page's own "filtered to <item>" banner has something to show immediately,
 * without a second fetch just to resolve a name from the id.
 */
export function itemEventsHref(itemId: number, label: string): string {
  const params = new URLSearchParams({ item_id: String(itemId), item: label })
  return `/events?${params.toString()}`
}

export interface ItemEventsFilterParams {
  itemId: number | null
  itemLabel: string | null
}

/** The Events page's own read side of `itemEventsHref` above. `item_id` must parse as a
 * non-negative integer or this reports no filter at all -- a malformed or hand-edited URL
 * degrades to the unfiltered log, never a crash or a silently-wrong filter. `itemLabel` is only
 * ever meaningful alongside a valid `itemId`; it comes back `null` on its own otherwise, so the
 * caller never renders a "filtered to X" banner with no actual filter behind it.
 */
export function parseItemEventsFilter(search: string | URLSearchParams): ItemEventsFilterParams {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search
  const raw = params.get('item_id')
  const itemId = raw != null && /^\d+$/.test(raw) ? Number(raw) : null
  return { itemId, itemLabel: itemId != null ? params.get('item') : null }
}
