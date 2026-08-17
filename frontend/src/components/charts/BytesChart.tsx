import { useMemo } from 'react'
import type { MetricsBucketOut, PathQueueOut } from '../../api/types'
import { bucketLabel, bytesChartTitle, sumBytesByQueue, sumTotalBytes } from '../../lib/bytesChart'
import { formatBytes } from '../../lib/format'
import { CHART_BLOCK_CLASSES, CHART_SVG_MAX_HEIGHT_CLASS } from './chartLayout'
import { assignQueueColorSlots, colorVarForSlot } from './queueColors'
import './chartTheme.css'

const WIDTH = 760
const HEIGHT = 260
const PAD_LEFT = 52
const PAD_RIGHT = 8
const PAD_TOP = 16
const PAD_BOTTOM = 28
const PLOT_W = WIDTH - PAD_LEFT - PAD_RIGHT
const PLOT_H = HEIGHT - PAD_TOP - PAD_BOTTOM
const Y_GRIDLINES = 4

interface BytesChartProps {
  buckets: MetricsBucketOut[]
  // Drives labeling/title (`lib/bytesChart.ts`) -- 3600s (24h range), 21600s (7d), or 86400s
  // (30d), per `api/metrics.py`'s `_RANGES`. Passed rather than re-derived from `buckets` so an
  // empty response (no buckets at all) still knows what it would have shown.
  bucketSeconds: number
  queues: PathQueueOut[]
  // Task prompt item 5 -- a one-line muted note when the selected range outruns configured
  // retention, or `null` to show nothing (`lib/bytesChart.ts.retentionNoteForRange`).
  retentionNote: string | null
}

/** Chart 1 (DESIGN.md new section proposed; renamed from `BytesPerHourChart` 2026-08-17,
 * prompts/done/2026-08-17-bytes-chart-7d-30d-ranges-and-total.md, once it stopped being
 * per-hour-specific -- the 7d/30d ranges that task added bucket at 6h/1d instead) -- bytes
 * transferred over the selected range, stacked per queue with a range total. Hand-rolled
 * inline SVG, no charting dependency (docs/decisions.md) -- a `down` bucket (no heartbeat at
 * all, `up: false`) renders as a short muted dash at the baseline, never a zero-height bar, so
 * "nothing was transferring" and "lftpweb wasn't running" are never visually the same thing.
 */
export function BytesChart({ buckets, bucketSeconds, queues, retentionNote }: BytesChartProps) {
  const colorSlots = useMemo(() => assignQueueColorSlots(queues), [queues])
  const queuesById = useMemo(() => new Map(queues.map((q) => [q.id, q])), [queues])

  const upBuckets = buckets.filter((b) => b.up)
  const maxTotal = Math.max(1, ...upBuckets.map((b) => b.total_bytes ?? 0))
  // 10% headroom so the tallest bar doesn't touch the plot's top edge.
  const yMax = maxTotal * 1.1

  const barSlot = buckets.length > 0 ? PLOT_W / buckets.length : PLOT_W
  const barWidth = Math.max(barSlot * 0.7, 1)
  const baselineY = PAD_TOP + PLOT_H

  const yToPixel = (bytes: number) => baselineY - (bytes / yMax) * PLOT_H

  const gridlines = Array.from({ length: Y_GRIDLINES + 1 }, (_, i) => {
    const value = (yMax / Y_GRIDLINES) * i
    return { value, y: yToPixel(value) }
  })

  // Every queue that appears anywhere in this window, in fixed color order -- the legend and
  // the stacking order both use this so a queue's segment is always in the same visual
  // position relative to the others.
  const activeQueueIds = useMemo(() => {
    const ids = new Set<number>()
    for (const b of buckets) {
      for (const qid of Object.keys(b.by_queue)) ids.add(Number(qid))
    }
    return [...ids].sort((a, b) => (colorSlots.get(a) ?? 0) - (colorSlots.get(b) ?? 0))
  }, [buckets, colorSlots])

  const totalWindowBytes = sumTotalBytes(buckets)
  // Same numbers as the header total, split by queue -- appended to each legend entry (task
  // prompt item 4: "same numbers, one place") rather than computed a second way.
  const totalsByQueue = useMemo(() => sumBytesByQueue(buckets), [buckets])

  const title = bytesChartTitle(bucketSeconds)

  // Thin the x-axis labels -- as many labels as buckets on a 760px-wide chart is unreadable
  // clutter (dataviz skill: "recessive grid/axes"). Show roughly every 4th bucket, plus the
  // last, regardless of how many buckets the selected range produced.
  const labelEvery = Math.max(1, Math.ceil(buckets.length / 6))

  return (
    <div className={`viz-root flex flex-col gap-2 ${CHART_BLOCK_CLASSES}`}>
      <div className="flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">{title}</h3>
        <span className="text-xs" style={{ color: 'var(--chart-ink)' }}>
          Total: {formatBytes(totalWindowBytes)}
        </span>
      </div>

      {retentionNote && (
        <p className="text-xs" style={{ color: 'var(--chart-muted)' }}>
          {retentionNote}
        </p>
      )}

      {buckets.length === 0 ? (
        <div className="flex h-40 items-center justify-center rounded-lg border border-dashed border-zinc-300 text-sm text-zinc-400 dark:border-zinc-700 dark:text-zinc-600">
          No throughput data yet.
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`${title}, total ${formatBytes(totalWindowBytes)}`}
          className={`w-full ${CHART_SVG_MAX_HEIGHT_CLASS}`}
        >
          {gridlines.map(({ value, y }) => (
            <g key={value}>
              <line
                x1={PAD_LEFT}
                x2={WIDTH - PAD_RIGHT}
                y1={y}
                y2={y}
                stroke="var(--chart-grid)"
                strokeWidth={1}
              />
              <text
                x={PAD_LEFT - 6}
                y={y}
                textAnchor="end"
                dominantBaseline="middle"
                fontSize={10}
                fill="var(--chart-muted)"
              >
                {formatBytes(value)}
              </text>
            </g>
          ))}
          <line
            x1={PAD_LEFT}
            x2={WIDTH - PAD_RIGHT}
            y1={baselineY}
            y2={baselineY}
            stroke="var(--chart-axis)"
            strokeWidth={1}
          />

          {buckets.map((bucket, i) => {
            const x = PAD_LEFT + i * barSlot + (barSlot - barWidth) / 2
            const showLabel = i % labelEvery === 0 || i === buckets.length - 1
            const label = bucketLabel(bucket.ts, bucketSeconds)

            if (!bucket.up) {
              return (
                <g key={bucket.ts}>
                  <rect
                    x={x}
                    y={baselineY - 3}
                    width={barWidth}
                    height={3}
                    rx={1}
                    fill="var(--chart-gap-fill)"
                  >
                    <title>{`${label} — no data (lftpweb was not running)`}</title>
                  </rect>
                  {showLabel && (
                    <text
                      x={x + barWidth / 2}
                      y={baselineY + 14}
                      textAnchor="middle"
                      fontSize={9}
                      fill="var(--chart-muted)"
                    >
                      {label}
                    </text>
                  )}
                </g>
              )
            }

            let cumulative = 0
            return (
              <g key={bucket.ts}>
                {activeQueueIds.map((qid) => {
                  const value = bucket.by_queue[String(qid)] ?? 0
                  if (value <= 0) return null
                  const h = (value / yMax) * PLOT_H
                  const y = baselineY - cumulative - h
                  cumulative += h
                  const slot = colorSlots.get(qid) ?? 0
                  const queueName = queuesById.get(qid)?.name ?? `queue ${qid}`
                  return (
                    <rect
                      key={qid}
                      x={x}
                      y={y}
                      width={barWidth}
                      height={h}
                      fill={colorVarForSlot(slot)}
                    >
                      <title>{`${label} — ${queueName}: ${formatBytes(value)}`}</title>
                    </rect>
                  )
                })}
                {showLabel && (
                  <text
                    x={x + barWidth / 2}
                    y={baselineY + 14}
                    textAnchor="middle"
                    fontSize={9}
                    fill="var(--chart-muted)"
                  >
                    {label}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      )}

      {activeQueueIds.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs" style={{ color: 'var(--chart-ink)' }}>
          {activeQueueIds.map((qid) => (
            <span key={qid} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: colorVarForSlot(colorSlots.get(qid) ?? 0) }}
              />
              {queuesById.get(qid)?.name ?? `queue ${qid}`}: {formatBytes(totalsByQueue[qid] ?? 0)}
            </span>
          ))}
        </div>
      )}

      {/* Accessible text alternative for the numbers above -- the existing pages' convention
       * (aria-label on interactive controls) doesn't cover a chart's data directly, so this
       * mirrors the same intent as a plain table a screen reader can read row by row. */}
      <table className="sr-only">
        <caption>{title}, by queue</caption>
        <thead>
          <tr>
            <th>Bucket</th>
            <th>Status</th>
            {activeQueueIds.map((qid) => (
              <th key={qid}>{queuesById.get(qid)?.name ?? `queue ${qid}`}</th>
            ))}
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket.ts}>
              <td>{bucketLabel(bucket.ts, bucketSeconds)}</td>
              <td>{bucket.up ? 'up' : 'lftpweb offline'}</td>
              {activeQueueIds.map((qid) => (
                <td key={qid}>{formatBytes(bucket.by_queue[String(qid)] ?? 0)}</td>
              ))}
              <td>{bucket.up ? formatBytes(bucket.total_bytes ?? 0) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
