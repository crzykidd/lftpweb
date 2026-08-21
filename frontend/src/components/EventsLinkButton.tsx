import { Link } from 'react-router-dom'
import { itemEventsHref } from '../lib/eventsLink'

/** The per-row "Events" deep link (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage
 * 7) -- opens the Events page pre-filtered to this item. Plain text, not a new icon glyph:
 * every icon in this project is Lucide path data copied verbatim, unmodified
 * (`LifecycleIcons.tsx`'s own module comment) -- inventing a new glyph's path data from memory,
 * with no browser in this environment to check the result against, is exactly the risk that
 * discipline exists to avoid. This instead reuses the row's existing "small, quiet text button"
 * idiom (the Events page's own per-row "Clear" button).
 */
export function EventsLinkButton({ itemId, label }: { itemId: number; label: string }) {
  return (
    <Link
      to={itemEventsHref(itemId, label)}
      onClick={(e) => e.stopPropagation()}
      title={`View Events for ${label}`}
      className="shrink-0 text-xs font-medium text-zinc-400 hover:text-zinc-700 hover:underline dark:text-zinc-600 dark:hover:text-zinc-300"
    >
      Events
    </Link>
  )
}
