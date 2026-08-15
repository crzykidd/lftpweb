import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { getHealth, getStats } from '../api/client'
import { usePoll } from '../hooks/usePoll'
import { formatBytes, formatRate } from '../lib/format'

const POLL_INTERVAL_MS = 5000

// `/api/health` is on `logsetup.py`'s `_POLLED_PATHS` exemption list specifically so a
// continuous poll like this one doesn't spam the access log (phase 7's own reasoning,
// docs/decisions.md) -- polling it here at the same cadence as `/api/stats` is exactly the
// case that exemption exists for.
function dotClass(ok: boolean | null): string {
  if (ok === null) return 'text-zinc-400 dark:text-zinc-600'
  return ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'
}

/** DESIGN.md §9.1 header bar. Allocated-vs-ceiling is not decoration: under admission
 * control (§4.5) it's the honest answer to "why hasn't the next item started", not
 * current speed. Zeros in phase 1 — the scheduler that fills these in lands in phase 3.
 * Phase 9 adds `host_reachable`/`scheduler_alive` (DESIGN.md §10.3) -- phase 7 extended
 * `/api/health` with both fields but deliberately deferred the UI to this phase (see
 * docs/decisions.md's phase 7 entry, point 16) since its own brief was the Logs/Backup
 * pages, not a health readout. "24h" (2026-08-13, prompts/2026-08-13-header-24h-from-metrics.md)
 * links to `/dashboard` -- just that item, not the whole row -- since it's the one figure here
 * that's history rather than live scheduler state, and the Dashboard is where its detail lives.
 */
export function StatsHeader() {
  const statsFetcher = useCallback(getStats, [])
  const stats = usePoll(statsFetcher, POLL_INTERVAL_MS)
  const healthFetcher = useCallback(getHealth, [])
  const health = usePoll(healthFetcher, POLL_INTERVAL_MS)

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
      <Link
        to="/dashboard"
        className="flex items-baseline gap-1.5 whitespace-nowrap rounded outline-none hover:underline focus-visible:ring-2 focus-visible:ring-zinc-400 dark:focus-visible:ring-zinc-500"
        title="Bytes actually transferred in the last 24h (metric_sample) -- open the Dashboard for the full breakdown."
      >
        <span className="text-zinc-500 dark:text-zinc-400">24h</span>
        <span className="font-medium text-zinc-900 dark:text-zinc-100">
          {formatBytes(stats?.transferred_24h_bytes ?? 0)}
        </span>
      </Link>
      {health && (
        <div className="ml-auto flex items-center gap-4 whitespace-nowrap text-xs">
          <span
            className={dotClass(health.host_reachable)}
            title="Reachability of the pooled connection to the configured seedbox (DESIGN.md §10.3). Read from the connection Engine's scans already maintain, not a fresh probe on every poll."
          >
            ● host{' '}
            {health.host_reachable === null
              ? 'not configured'
              : health.host_reachable
                ? 'reachable'
                : 'unreachable'}
          </span>
          <span
            className={dotClass(health.scheduler_alive)}
            title="Whether the transfer scheduler's admission loop is running."
          >
            ● scheduler {health.scheduler_alive ? 'alive' : 'dead'}
          </span>
        </div>
      )}
    </div>
  )
}
