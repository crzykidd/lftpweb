import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { listQueues } from '../api/client'
import type { PathQueueOut } from '../api/types'
import { EventsSection } from '../components/EventsSection'
import { parseItemEventsFilter } from '../lib/eventsLink'

/** Events -- the audit-event log (2026-08-20, docs/transfers-redesign-spec.md §2, phase 1 stage
 * 7). Formerly "History," a two-section page pairing this event log with a `job` list
 * (`HistoryJobsSection.tsx`, now removed). The jobs half was dropped because the Queue tab's
 * Complete box (stage 4b) already covers "what finished, in what order" -- keeping both was the
 * exact overlapping-answer duplication this redesign exists to remove. This page keeps what
 * nothing else has: every verify/extract/move outcome, every remote delete, and every delete
 * *withheld*, with the reason.
 *
 * `/history` still resolves here (`App.tsx`'s redirect) so nothing that links or bookmarks the
 * old path breaks -- the same pattern stage 6 established for `/files` -> `/transfers/files`.
 *
 * The per-item deep link (this task) lives entirely in the URL's `item_id`/`item` search params
 * (`lib/eventsLink.ts`) -- read once here via `useSearchParams`, not component state, so a
 * reload or a bookmark reproduces the exact same filtered view. `EventsSection` renders the
 * "filtered to X" banner and the way back to the unfiltered log.
 */
export function EventsPage() {
  const [queues, setQueues] = useState<PathQueueOut[]>([])
  const [searchParams, setSearchParams] = useSearchParams()

  useEffect(() => {
    listQueues()
      .then(setQueues)
      .catch(() => {
        // The queue filter dropdown just shows no queues if this fails; the section below still
        // works with an unfiltered ("All queues") view.
      })
  }, [])

  const { itemId, itemLabel } = useMemo(() => parseItemEventsFilter(searchParams), [searchParams])
  const itemFilter = itemId != null ? { itemId, itemLabel } : null
  const clearItemFilter = () => setSearchParams({})

  return (
    <div className="flex flex-col gap-6">
      {/* Clearing scope note (2026-08-13, prompts/2026-08-13-clear-history.md) -- placed once,
       * above the section, since it's true of every clear control below. This is scope-setting,
       * not a warning: a control that implies more than it does is worse than no control, so it
       * says plainly what "Clear" does and does not reach, right next to the buttons that do it. */}
      <p className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
        <strong className="font-medium text-zinc-700 dark:text-zinc-300">Clearing</strong> deletes
        these audit-event records only. It never touches downloaded files, queue settings, or
        auto-queue suppression -- nothing about what happens on the next scan changes -- and it
        has no effect on the Dashboard, which tracks throughput in its own table, independent of
        this page. A transfer's own job history stays in the database and is still reachable from
        the item drawer; Logs and backups (Settings) are separate and are not covered by any
        control here.
      </p>
      <EventsSection queues={queues} itemFilter={itemFilter} onClearItemFilter={clearItemFilter} />
    </div>
  )
}
