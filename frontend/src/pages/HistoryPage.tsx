import { useEffect, useState } from 'react'
import { listQueues } from '../api/client'
import type { PathQueueOut } from '../api/types'
import { HistoryEventsSection } from '../components/HistoryEventsSection'
import { HistoryJobsSection } from '../components/HistoryJobsSection'

/** DESIGN.md §9.2 History page -- "the job and event tables, grouped by queue, filterable by
 * state, error class, date range, and event kind. This is where remote deletes are reviewed."
 * Two independently filtered/paginated sections rather than one combined feed: the `job` and
 * `event` tables have different shapes and different useful filters (state/error class vs.
 * kind/level), and DESIGN.md's own wording treats them as the two things this page surfaces,
 * not one merged timeline -- see docs/decisions.md for the alternative considered.
 */
export function HistoryPage() {
  const [queues, setQueues] = useState<PathQueueOut[]>([])

  useEffect(() => {
    listQueues()
      .then(setQueues)
      .catch(() => {
        // Filter dropdowns just show no queues if this fails; the sections below still
        // work with an unfiltered ("All queues") view.
      })
  }, [])

  return (
    <div className="flex flex-col gap-6">
      <HistoryJobsSection queues={queues} />
      <HistoryEventsSection queues={queues} />
    </div>
  )
}
