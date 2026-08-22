import { useCallback, useEffect, useMemo, useState } from 'react'
import { getMetricsSettings, getMetricsTotal, getThroughput, listQueues } from '../api/client'
import type {
  BytesRange,
  MetricsThroughputResponse,
  MetricsTotalOut,
  PathQueueOut,
  SpeedRange,
} from '../api/types'
import { BytesChart } from '../components/charts/BytesChart'
import { SpeedLineChart } from '../components/charts/SpeedLineChart'
import { assignQueueColorSlots, colorVarForSlot } from '../components/charts/queueColors'
import { usePoll } from '../hooks/usePoll'
import { retentionNoteForRange, totalSinceLabel } from '../lib/bytesChart'
import { formatBytes } from '../lib/format'
import { readLocalStorage, writeLocalStorage } from '../lib/storage'

const SPEED_RANGES: { value: SpeedRange; label: string }[] = [
  { value: '1h', label: '1h' },
  { value: '12h', label: '12h' },
  { value: '24h', label: '24h' },
]

// Chart 1's own range selector (task prompt, 2026-08-17,
// prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md) -- independent of Chart 2's
// `SPEED_RANGES` above, both in storage key and in option list (`api/types.ts`'s
// `BytesRange`/`SpeedRange` split).
// 90d/1y (2026-08-21, daily rollups, prompts/done/2026-08-21-daily-metric-rollups.md) read
// `metric_daily` server-side (api/metrics.py's `_DAILY_RANGES`) instead of the raw tables --
// nothing here needs to know that; they're just two more buttons feeding the same
// `getThroughput`/`BytesChart`.
const BYTES_RANGES: { value: BytesRange; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
  { value: '1y', label: '1y' },
]

// Refreshed on the same order of cadence as the rest of the app's polled pages (StatsHeader:
// 5s for numbers that change every tick; History's manual refresh for a paginated list). A
// dashboard built from 30s-interval samples doesn't need sub-minute polling to feel live --
// 60s keeps it current without adding load for a page nobody is watching in real time the way
// they watch Transfers.
const POLL_INTERVAL_MS = 60_000

const selectClasses =
  'rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'

// The remembered timeframe (2026-08-13, prompts/2026-08-13-files-ux-pass.md item 5) -- same
// `lib/storage.ts` helper the Files page's sort/collapse preferences already use, not a second
// one, with the same "a preference read/write failing must never break the page" guarantee.
const SPEED_RANGE_VALUES: SpeedRange[] = ['1h', '12h', '24h']
function isSpeedRange(value: unknown): value is SpeedRange {
  return typeof value === 'string' && (SPEED_RANGE_VALUES as string[]).includes(value)
}

// Chart 1's own remembered timeframe (`dashboard.bytesRange`, distinct key from Chart 2's
// `dashboard.range` above) -- same synchronous-read-before-first-paint pattern, so this chart
// never flashes 24h before jumping to a saved 7d/30d.
const BYTES_RANGE_VALUES: BytesRange[] = ['24h', '7d', '30d', '90d', '1y']
function isBytesRange(value: unknown): value is BytesRange {
  return typeof value === 'string' && (BYTES_RANGE_VALUES as string[]).includes(value)
}

/** DESIGN.md — new "Dashboard" page proposed alongside this task (docs/decisions.md); not an
 * expansion of the header stats (decision 4) -- a new page, so the header doesn't try to
 * cram a chart into a single row of chrome. Two hand-rolled SVG charts, both fed by
 * `core/metrics.py`'s throughput sample store, each with its own independent range selector
 * (2026-08-17, prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md): bytes
 * transferred (with a range total) over 24h/7d/30d, and speed over a selectable 1h/12h/24h
 * window, per queue or site-wide.
 *
 * **"Total downloaded" readout, and 90d/1y bytes-chart ranges** (2026-08-21, daily rollups,
 * prompts/done/2026-08-21-daily-metric-rollups.md) -- the user's own ask by name: a long-horizon
 * running total that survives past raw retention, backed by the new `metric_daily` table
 * (`GET /api/metrics/total`, `GET /api/metrics/throughput?range=90d|1y`). Reuses `BytesChart`
 * unchanged for the two new ranges -- they're just two more buttons and the same response
 * shape, one-day buckets throughout.
 */
export function DashboardPage() {
  const [queues, setQueues] = useState<PathQueueOut[]>([])
  // Read synchronously in the initial `useState`, not a `useEffect` (2026-08-13, item 5) -- a
  // `useEffect` would paint the default range first and then jump to the saved one once it
  // runs, which is exactly the flash the task's prompt calls out ("read it synchronously...
  // or the chart renders one timeframe and then jumps"). Chart 1's `bytesRange` follows the
  // identical pattern (2026-08-17) with its own key -- the two charts' selectors are
  // independent, so one never clobbers the other's saved preference.
  const [bytesRange, setBytesRangeState] = useState<BytesRange>(
    () => readLocalStorage('dashboard.bytesRange', isBytesRange) ?? '24h',
  )
  const setBytesRange = (next: BytesRange) => {
    setBytesRangeState(next)
    writeLocalStorage('dashboard.bytesRange', next)
  }
  const [speedRange, setSpeedRangeState] = useState<SpeedRange>(
    () => readLocalStorage('dashboard.range', isSpeedRange) ?? '24h',
  )
  const setSpeedRange = (next: SpeedRange) => {
    setSpeedRangeState(next)
    writeLocalStorage('dashboard.range', next)
  }
  const [queueId, setQueueId] = useState<number | undefined>(undefined)

  // Retention (`core/metrics.py.MetricsSettings.retention_days`, read via the existing
  // `GET /api/settings/metrics` the Settings page already round-trips -- no new endpoint,
  // task prompt item 5) fetched once, not polled -- it changes only when a human edits it in
  // Settings, so a stale read for the length of this page visit is harmless. `null` while
  // unresolved or on a failed fetch means "say nothing," not "assume 7."
  const [retentionDays, setRetentionDays] = useState<number | null>(null)
  useEffect(() => {
    getMetricsSettings()
      .then((s) => setRetentionDays(s.retention_days))
      .catch(() => {
        // The retention note just doesn't render -- everything else on the page still works.
      })
  }, [])

  useEffect(() => {
    listQueues()
      .then(setQueues)
      .catch(() => {
        // Chart 1's legend/breakdown and Chart 2's queue selector just show nothing extra;
        // the charts themselves still work off queue ids present in the throughput data.
      })
  }, [])

  const dailyFetcher = useCallback(() => getThroughput(bytesRange), [bytesRange])
  const daily = usePoll<MetricsThroughputResponse>(dailyFetcher, POLL_INTERVAL_MS)

  const speedFetcher = useCallback(() => getThroughput(speedRange, queueId), [speedRange, queueId])
  const speed = usePoll<MetricsThroughputResponse>(speedFetcher, POLL_INTERVAL_MS)

  // The task's own ask, by name: "a user can have the option to just see their total
  // downloaded amount." Site-wide (no `queueId` filter) regardless of Chart 2's own queue
  // selector -- this is the headline number, not a per-queue one. Same poll cadence as the
  // charts (`core/metrics.py.total_bytes` folds in today's not-yet-rolled-up raw samples, so
  // it's already live; polling just keeps it current across a long-open tab).
  const totalFetcher = useCallback(() => getMetricsTotal(), [])
  const total = usePoll<MetricsTotalOut>(totalFetcher, POLL_INTERVAL_MS)

  const colorSlots = useMemo(() => assignQueueColorSlots(queues), [queues])
  const selectedQueueName = queueId != null ? queues.find((q) => q.id === queueId)?.name : undefined
  const seriesLabel = selectedQueueName ?? 'All queues'
  const seriesColor =
    queueId != null ? colorVarForSlot(colorSlots.get(queueId) ?? 0) : 'var(--series-1)'

  const retentionNote = retentionNoteForRange(bytesRange, retentionDays)

  return (
    <div className="flex flex-col gap-6">
      <section className="flex items-baseline justify-between gap-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
        <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Total downloaded
        </span>
        {total ? (
          <span className="text-sm text-zinc-500 dark:text-zinc-400">
            <span className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
              {formatBytes(total.total_bytes)}
            </span>{' '}
            {totalSinceLabel(total.since_day)}
          </span>
        ) : (
          <span className="text-sm text-zinc-400 dark:text-zinc-600">Loading…</span>
        )}
      </section>

      <section className="flex flex-col gap-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
        <div className="flex justify-end">
          <div className="flex overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-700">
            {BYTES_RANGES.map((r) => (
              <button
                key={r.value}
                type="button"
                onClick={() => setBytesRange(r.value)}
                aria-pressed={bytesRange === r.value}
                className={`px-2.5 py-1.5 text-sm font-medium ${
                  bytesRange === r.value
                    ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
                    : 'bg-white text-zinc-700 hover:bg-zinc-50 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>

        {daily ? (
          <BytesChart
            buckets={daily.buckets}
            bucketSeconds={daily.bucket_seconds}
            queues={queues}
            retentionNote={retentionNote}
          />
        ) : (
          <div className="flex h-40 items-center justify-center text-sm text-zinc-400 dark:text-zinc-600">
            Loading…
          </div>
        )}
      </section>

      <section className="flex flex-col gap-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            Transfer speed — {seriesLabel}
          </h3>
          <div className="flex items-center gap-2">
            <select
              className={selectClasses}
              value={queueId ?? ''}
              onChange={(e) => setQueueId(e.target.value ? Number(e.target.value) : undefined)}
              aria-label="Queue"
            >
              <option value="">All queues</option>
              {queues.map((q) => (
                <option key={q.id} value={q.id}>
                  {q.name}
                </option>
              ))}
            </select>
            <div className="flex overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-700">
              {SPEED_RANGES.map((r) => (
                <button
                  key={r.value}
                  type="button"
                  onClick={() => setSpeedRange(r.value)}
                  aria-pressed={speedRange === r.value}
                  className={`px-2.5 py-1.5 text-sm font-medium ${
                    speedRange === r.value
                      ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
                      : 'bg-white text-zinc-700 hover:bg-zinc-50 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800'
                  }`}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {speed ? (
          <SpeedLineChart
            buckets={speed.buckets}
            bucketSeconds={speed.bucket_seconds}
            seriesLabel={seriesLabel}
            colorVar={seriesColor}
          />
        ) : (
          <div className="flex h-32 items-center justify-center text-sm text-zinc-400 dark:text-zinc-600">
            Loading…
          </div>
        )}
      </section>
    </div>
  )
}
