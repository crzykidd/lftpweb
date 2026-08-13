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
      {/* Clearing scope note (2026-08-13, prompts/2026-08-13-clear-history.md) -- placed once,
       * above both sections, since it's true of every clear control below. This is
       * scope-setting, not a warning: a control that implies more than it does is worse than
       * no control, so it says plainly what "Clear" does and does not reach, right next to the
       * buttons that do it. */}
      <p className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
        <strong className="font-medium text-zinc-700 dark:text-zinc-300">Clearing history</strong>{' '}
        deletes these job and event records only. It never touches downloaded files, queue
        settings, or auto-queue suppression -- nothing about what happens on the next scan
        changes -- and it has no effect on the Dashboard, which tracks throughput in its own
        table, independent of this page. Logs and backups (Settings) are separate and are not
        covered by any control here.
      </p>
      <HistoryJobsSection queues={queues} />
      <HistoryEventsSection queues={queues} />
    </div>
  )
}
