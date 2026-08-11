import { useCallback } from 'react'
import { getStats } from '../api/client'
import { usePoll } from '../hooks/usePoll'
import { formatBytes, formatRate } from '../lib/format'

const POLL_INTERVAL_MS = 5000

/** DESIGN.md §9.1 header bar. Allocated-vs-ceiling is not decoration: under admission
 * control (§4.5) it's the honest answer to "why hasn't the next item started", not
 * current speed. Zeros in phase 1 — the scheduler that fills these in lands in phase 3.
 */
export function StatsHeader() {
  const fetcher = useCallback(getStats, [])
  const stats = usePoll(fetcher, POLL_INTERVAL_MS)

  const item = (label: string, value: string) => (
    <div className="flex items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-zinc-500 dark:text-zinc-400">{label}</span>
      <span className="font-medium text-zinc-900 dark:text-zinc-100">{value}</span>
    </div>
  )

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 border-b border-zinc-200 bg-zinc-50 px-4 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-900">
      {item('Speed', formatRate(stats?.current_speed_bps ?? 0))}
      {item(
        'Allocated',
        `${formatRate(stats?.allocated_bps ?? 0)} / ${formatRate(stats?.ceiling_bps ?? 0)}`,
      )}
      {item(
        'Queued',
        `${stats?.queued_count ?? 0} (${formatBytes(stats?.queued_bytes ?? 0)})`,
      )}
      {item('24h', formatBytes(stats?.transferred_24h_bytes ?? 0))}
    </div>
  )
}
